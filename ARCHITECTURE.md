# Architecture

> dip-alert 의 핵심 설계 결정 기록.
> "왜 이런 구조인가" 질문은 여기서 답변. 모듈 추가/리팩토링 시 일관성 잣대.

---

## 모듈 책임 매트릭스

```
backend/core/
├── datasource/                — 외부 데이터 fetch 추상화
│   ├── base.py                  DataSource ABC (interface)
│   ├── yfinance_source.py       주식/ETF/야후크립토
│   ├── binance_source.py        ccxt 기반 Binance
│   ├── provider.py              티커 → 소스 자동 라우팅
│   └── calendar_fetcher.py      이벤트 캘린더 (Finnhub/FRED/FOMC/DART) [M1]
│
├── strategy/dip_buy.py        — RSI+BB 매수 신호 + 캘린더 컨텍스트 + 동적 threshold
│
├── alerter.py                 — Telegram 발송 + 쿨다운 + 통화 인식 + 메시지 포맷
├── scanner.py                 — 스캐너 루프 (DB 워치리스트 → 신호 평가 → 알림)
├── threshold_alerts.py        — 가격/VIX/F&G 임계치 알림 평가 [S1]
├── positions.py               — 보유 포지션 + 익절 룰 마일스톤 [S3]
│
├── market.py                  — 시장 스냅샷 (지수/VIX/F&G/원자재/크립토/상관)
├── correlations.py            — Dual-window 상관계수 [M2-C]
├── exposure.py                — 포트폴리오 베타 + R² [M2-B]
├── calibration.py             — 이벤트별 RSI grid search [M2-A]
│
├── macro_briefing.py          — 일일 06:00 KST 매크로 해설 (LLM + web_search) [S2]
├── on_demand.py               — /why TICKER on-demand 해설 [S4]
├── money.py                   — 티커 → 통화 추정 + 포맷 [S5]
└── enrichment/                — LLMClient + analysts + synthesizer
```

---

## 핵심 설계 결정

### 1. 데이터 소스 추상화 (Freqtrade 패턴 차용)

**왜**: 새 거래소 / 데이터 공급자 추가 시 엔진 수정 없이 상속만으로 확장 가능해야.

```
DataSource (ABC)
  ├─ get_ohlcv(ticker, interval, period)
  └─ source_of(ticker)

YFinanceSource    (미국 주식 + ETF + 한국 .KS/.KQ + 일본 .T 등)
BinanceSource     (ETH/USDT 같은 슬래시 페어)

DataProvider (라우팅)
  ├─ 슬래시 + USDT/BTC/ETH/BNB/USDC  → BinanceSource
  ├─ -USD 후미 (BTC-USD, ETH-USD)    → YFinanceSource (야후 크립토)
  └─ 그 외                             → YFinanceSource
```

엔진(Scanner)은 소스를 모름. `provider.get_ohlcv(ticker, ...)` 만 호출.

### 2. 알림 계층 3종 (서로 격리, 같은 출력 채널)

세 개의 독립 신호 종류, 모두 `AlertEngine`을 통과:

| 종류 | 발동 조건 | 평가자 | 메시지 포맷 |
|---|---|---|---|
| **DipBuy 시그널** | RSI + BB | `DipBuyStrategy` | `_format_message` |
| **ThresholdAlert** | 가격/VIX/F&G 임계치 돌파 | `ThresholdAlertEvaluator` | `_format_threshold_message` |
| **PositionMilestone** | 평단 대비 +50/+100/+200/+400/+600% | `PositionEvaluator` | `_format_milestone_message` |

`AlertEngine.process()` / `process_threshold()` / `process_milestone()` 메소드 분리. 쿨다운 / DB 로깅 / Telegram 발송 공통 인프라 공유.

**격리 원칙**: 한 신호 종류 변경이 다른 종류 흐름 깨면 안 됨. condition_reasons 와 calendar_contexts 분리도 같은 원칙.

### 3. 매크로 해설 — 사용자 needs 1순위

> 메모리: "매수/매도 알림보다 '왜 움직이는가' 매크로 해설이 더 큰 needs"

두 채널:

- **일일 자동** (`macro_briefing.py`): 06:00 KST → "어제 시장 왜 움직였나"
- **On-demand** (`on_demand.py`): `/why TICKER` 또는 `/why` (현재 매크로)

공통: `LLMClient.call_with_web_search()` — Anthropic web_search 서버 툴. 출처 URL 자동 추출.

**비용 가드**: `MAX_DAILY_LLM_USD` 캡 (~$0.05~0.10 / 호출). 일일 1회 자동 + 20~40회 on-demand 가능.

### 4. 통화 자동 인식 (`money.py`)

티커 suffix → 통화 매핑. **데이터는 yfinance가 이미 통화별 가격 반환** — 우리는 표시만 손봄.

```
.KS / .KQ → KRW (₩)
.T        → JPY (¥)
.HK       → HKD (HK$)
그 외     → USD ($)
```

`format_money(value, ticker)` 헬퍼로 모든 가격 출력 통일. 회사명은 `Watchlist.name` (yfinance.info.longName 캐싱), `_short_company_name()` 으로 cutoff.

### 5. 이벤트 캘린더 컨텍스트 [M1]

**왜**: 알림이 떴을 때 "왜 지금 RSI가 낮은가?"를 외부 앱 없이 답하기 위해.

`CalendarFetcher` 가 4개 소스 통합:
- **Finnhub**: 미국 어닝스 (FINNHUB_API_KEY)
- **FRED**: CPI / PPI / NFP 발표일 (FRED_API_KEY)
- **FOMC**: 2026 일정 하드코딩 (Fed 공식)
- **DART**: 한국 공시 (DART_API_KEY, backward 7일)

원칙:
- API 키 없으면 그 소스만 silent skip
- 모든 소스 실패해도 빈 리스트 (graceful, 시그널 정상 발동)
- 일별 캐싱 (자정 reset, 15분 스캔마다 API 호출 X)
- `lookahead_days` override 가능 (알림 7일 / `/market` 21일)

`Signal.reasons` 와 `AlertEvaluation.calendar_contexts` 양쪽에 자동 주입.

### 6. 동적 RSI 임계치 [M2-A]

기본 RSI 35는 직관 박힌 값. 백테스트로 보정:

1. `calibration.py` 가 FRED historical + 하드코딩 FOMC 발표일 가져옴
2. 각 (event_type, ticker) 조합에서 D-3 ~ D+0 구간 필터링
3. RSI 30~40 grid 0.5 step → 각 RSI에서 "D+5 종가가 +2% 도달" 적중률 측정
4. 적중률 최고 RSI를 `event_calibration` 테이블에 저장
5. `DipBuyStrategy._effective_rsi_threshold()` 가 캘린더 임박 검출 시 calibrated RSI 적용 (없으면 default fallback)

**중요**: calibration_label은 reasons에 부착하지만 **STRONG/WEAK 강도 판정에는 영향 없음** — `condition_reasons` 별도 분리.

### 7. Dual-Window 상관관계 [M2-C]

3개 페어를 20일(현재 레짐) vs 200일(기준선) 두 윈도로:

- `SOXL ↔ ^TNX` — 반도체 ETF 의 금리 민감도
- `^GSPC ↔ ^TNX` — 시장 전반 금리 민감도
- `BTC-USD ↔ DX-Y.NYB` — 디지털 골드 가설

`delta = short - long` 가 ±0.20 넘으면 "심화"/"완화" 자동 라벨. `MarketSnapshot.correlations` 에 부착, `/market` 출력에 한 섹션 자동 추가.

### 8. 포트폴리오 노출도 [M2-B]

`compute_exposure()` 가 보유 포지션 vs SPY/SOXX 베타 + R²:
- 종목별 회귀
- 가중 시계열로 포트폴리오 종합 회귀 (R² 가중평균 불가, 직접 회귀 필요)
- 자동 경고: R²>0.90 (단일섹터), β>1.5 (공격적), 통화혼용

---

## 모듈 의존 그래프

```
provider ── yfinance/binance source

scanner ──┬── strategy ── populate_indicators
          │              └── (M1) calendar_fetcher → context
          │              └── (M2-A) crud.get_event_calibration → dynamic RSI
          │
          ├── threshold_alerts ── (M1) calendar_fetcher
          ├── positions
          └── alerter ──┬── enrichment (LLM)
                        └── _format_*_message

market ──┬── yfinance, CNN, alternative.me, coingecko
         └── (M2-C) correlations

exposure ── provider (병렬 fetch) ── stats
calibration ── FRED + yfinance ── grid search

macro_briefing ── llm_client (web_search) ── market snapshot
on_demand ── llm_client (web_search) ── ticker df + market snapshot
```

---

## DB 스키마 (SQLAlchemy)

| 테이블 | 책임 | 마일스톤 |
|---|---|---|
| `watchlist` | 감시 종목 + 회사명 캐시 | P1, S5 |
| `alert_log` | DipBuy 알림 히스토리 | P1 |
| `backtest_result` | 백테스트 결과 | P2 |
| `threshold_alert` | 가격/VIX/F&G 알림 룰 | S1 |
| `position` | 보유 평단 + 마일스톤 | S3 |
| `event_calibration` | 이벤트별 RSI grid search 결과 | M2-A |
| `llm_call_log` | LLM 비용 추적 | (분산) |

**마이그레이션**: Alembic 미도입. `init_db()` 에서 `Base.metadata.create_all()` + `_run_lightweight_migrations()` (SQLite ALTER TABLE 지원). 신규 테이블은 자동, 컬럼 추가는 케이스별 핸들링.
