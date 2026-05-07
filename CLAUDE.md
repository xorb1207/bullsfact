# dip-alert — Claude Code 인수인계 문서

> **마지막 업데이트**: 2026-05-07

## 프로젝트 개요

미국/한국 주식·ETF + 크립토를 동시에 감시. 가격 트리거 알림은 표면, 핵심은
**"왜 움직이는가"** 매크로 해설(LLM + web_search). 본인 운영 → 지인 공유 → 멀티유저로 확장 예정.

---

## 현재 구현 현황

```
backend/
├── core/
│   ├── datasource/             ✅ yfinance / Binance(ccxt) / 라우팅
│   ├── strategy/dip_buy.py     ✅ RSI+BB 전략, Signal 타입
│   ├── backtest/               ✅ engine, metrics, rules
│   ├── alerter.py              ✅ Telegram 발송 + 쿨다운 + 통화 인식
│   ├── scanner.py              ✅ 스케줄러 루프 (DB 워치리스트)
│   ├── market.py               ✅ 지수/VIX/F&G/원자재/크립토 스냅샷
│   ├── threshold_alerts.py     ✅ 가격/VIX/F&G 임계치 (절대+상대)
│   ├── positions.py            ✅ 보유 포지션 + 익절 룰 마일스톤
│   ├── macro_briefing.py       ✅ 일일 매크로 해설 (LLM + web_search)
│   ├── on_demand.py            ✅ /why TICKER, /why (매크로) on-demand
│   ├── money.py                ✅ 티커→통화 추정 + 포맷 (USD/KRW/JPY/HKD)
│   └── enrichment/             ✅ LLMClient + analysts + synthesizer
├── db/                         ✅ SQLAlchemy + SQLite (lightweight migration)
├── api/                        ✅ FastAPI (watchlist, alerts, backtest)
├── scripts/                    ✅ bot.py, seed/backfill 스크립트
├── main.py                     ✅ 진입점 (스캐너)
└── dipalert.db                 ✅ 운영중

frontend/                       ✅ React + Vite (Watchlist/Alerts/Backtest)
run.sh                          ✅ start/stop/restart/status/logs/attach
```

---

## 아키텍처 핵심 결정사항

### 티커 라우팅 (`core/datasource/provider.py`)
```
ETH/USDT, BTC/USDT  →  Binance (ccxt)   # 슬래시 + USDT/BTC/ETH/BNB/USDC
ETH-USD, BTC-USD    →  yfinance          # 야후 크립토
SOXL, NVDA          →  yfinance          # 미국 주식/ETF
005930.KS, .KQ      →  yfinance (KRW 자동)
7203.T              →  yfinance (JPY 자동)
```

### 알림 계층 (3종)
- **DipBuy 시그널** (RSI+BB) — STRONG/WEAK/NONE, 단기 dip 레이더
- **ThresholdAlert** — 가격/VIX/F&G 임계치 돌파. 절대값 OR 상대값 (`high_252d`/`low_252d`/`ema_50d` 대비 %)
- **PositionMilestone** — 평단 대비 +50/+100/+200/+400/+600% 익절 룰 (각 단계 매도 비율 자동 계산)

세 가지 모두 `AlertEngine.process*()` 통일 인터페이스. 발동 시 자동 비활성화 (재발동 방지).

### 매크로 해설 (사용자 needs 1순위)
- **일일 06:00 KST**: `macro_briefing.py` — 어제 시장 자동 해설
- **On-demand**: `/why TICKER` / `/why` — Anthropic web_search 서버툴 사용
- 비용 가드: `MAX_DAILY_LLM_USD` 캡 (~$0.05/회, 일 20~40회 가능)

### 통화 자동 인식 (`core/money.py`)
티커 suffix로 통화 추정 → 표시 시 자동 (`$165.76`, `₩271,500`, `¥3,500`).
`/position list` 합계는 통화별로 분리.

---

## 전체 로드맵

| 페이즈 | 내용 | 상태 |
|---|---|---|
| P1 | Core Engine | ✅ |
| P2 | 백테스트 엔진 | ✅ |
| P3 | React 대시보드 | ✅ |
| P3+ | Telegram 봇 + 일일 브리핑 + LLM enrichment | ✅ |
| **S1** | **가격/VIX/F&G 임계치 알림 (`/alert`)** | ✅ |
| **S2** | **일일 매크로 해설 (LLM + web_search)** | ✅ |
| **S3** | **포지션 + 익절 룰 (`/position`)** | ✅ |
| **S4** | **`/why` on-demand 매크로/티커 해설** | ✅ |
| **S5** | **통화 자동 인식 + 회사명 캐싱** | ✅ |
| P4 | 멀티유저 / JWT / PostgreSQL | ⬜ |
| P5 | Docker Compose / Railway 배포 | ⬜ |

---

## Telegram 명령어

```
/list                        워치리스트 + 현재가 + RSI
/add TICKER                  추가 (회사명 자동 캐싱)
/remove TICKER
/market                      시장 스냅샷 (지수/VIX/F&G/크립토)
/why [TICKER]                LLM 해설 (인자 없으면 매크로)
/alert add price SOXL below 110 T1 HIGH 메모
/alert add price SOXL below high_252d -32% T1 HIGH
/alert add vix above 30 MED
/alert list / remove / pause / resume
/position add SOXL 23 21.47
/position list / update / remove
/cost                        LLM 비용 + 알람 카운트
/test [TICKER]               헬스체크
```

매일 06:00 KST 자동 발송: 시장 스냅샷 + LLM 매크로 해설 + 출처.

---

## 환경 변수 (`backend/.env`)

```bash
TELEGRAM_TOKEN=...
TELEGRAM_CHAT_ID=...
BINANCE_API_KEY=              # 퍼블릭은 빈칸 OK
BINANCE_API_SECRET=
ANTHROPIC_API_KEY=...         # /why, 매크로 해설
MAX_DAILY_LLM_USD=2
RSI_THRESHOLD=35
BB_STD=2.0
COOLDOWN_MIN=60
CHECK_INTERVAL_MIN=15
DATA_INTERVAL=1d              # 1h → 1d (2026-05-06)
DATA_PERIOD=2y
FINNHUB_API_KEY=          # 미국 어닝스 캘린더
FRED_API_KEY=             # CPI/PPI/NFP 매크로 캘린더
DART_API_KEY=             # 한국 공시
```

---

## 개발자 컨텍스트

- 오너: Teo (Seoul, FAB AMR 시스템 개발 배경)
- 스택: FastAPI + React/Vite (amr-sim 경험)
- 사용자 needs 1순위: **"왜 움직이는가" 매크로 해설** > 매수/매도 알림
- 매매 전략: 레버리지 ETF 분할 매도 + T1/T2/T3 매수 사다리 + 익절 룰 (평단 대비 단계별)
- 보유: NVDA, SOXL, AMD, TQQQ, GOOGL, AVGO, QQQ, TSLA, 005930.KS, 000660.KS 등
- 우선순위: 본인 안정 운영 → 배포(P5) → 멀티유저(P4)
- 클론 지양: 외부 풀 프로덕트 복제보다 핵심 20% 차용 선호 (메모리 참조)

---

## 운영 명령

```bash
./run.sh start | stop | restart | status
./run.sh logs bot|scanner       # tail -f
./run.sh attach bot|scanner     # tmux 직접 보기 (Ctrl+B D 빠짐)

# 시드/백필
python -m backend.scripts.seed_threshold_alerts        # 매매전략 §3 알림 25건
python -m backend.scripts.backfill_names               # Watchlist.name 일괄 갱신
```


---

## 신규 마일스톤 (2026-05-07 논의)

| 마일스톤 | 내용 | 상태 |
|---|---|---|
| **M1** | **이벤트 캘린더 컨텍스트 주입 (CalendarFetcher + Signal.reasons 통합)** | ⬜ |
| **M2** | **백테스트 기반 임계치 캘리브레이션 (event_calibration 테이블)** | ⬜ |
| **M3** | **Information Gap Analysis (원인 불명 수급 분류기)** | ⬜ |

### M1 설계 결정사항

**목적**: DipBuy/Threshold 시그널 발동 시 `Signal.reasons`에 이벤트 컨텍스트 한 줄 자동 주입.
"왜 지금 RSI가 낮은가?"를 외부 앱 없이 알림 안에서 즉시 파악.

**데이터 소스 결정**:
- 미국 어닝스 캘린더: Finnhub API (무료 티어, 60req/min) — yfinance.calendar는 신뢰도 낮아 제외
- 매크로 캘린더 (CPI/PPI/NFP): FRED API (무료, historical 발표일 포함)
- FOMC 일정: 연초 1회 하드코딩 (Fed 공식 발표, 연 8회 고정)
- 한국 공시: DART OpenAPI (무료)

**CalendarFetcher 인터페이스**:
```python
# backend/core/datasource/calendar_fetcher.py
@dataclass
class CalendarEvent:
    ticker: str | None   # None이면 매크로 이벤트
    event_type: str      # "earnings" | "cpi" | "fomc" | "ppi" | "nfp" | "dart"
    event_date: date
    days_until: int
    description: str     # "NVDA Q2 Earnings", "CPI (May 2026)"
    source: str          # "finnhub" | "fred" | "fed_static" | "dart"

class CalendarFetcher:
    def get_context_strings(self, ticker: str) -> list[str]:
        # Signal.reasons에 바로 append할 문자열 반환
        # 예: ["⚠️ NVDA 어닝 D-1 (2026-05-08)", "📅 CPI 발표 당일"]
```

**통합 포인트**: `core/strategy/dip_buy.py`의 `generate_signal()`에서
`signal.reasons += calendar_fetcher.get_context_strings(ticker)` 한 줄 추가.

**설계 원칙**:
- CalendarFetcher 실패 시 시그널은 정상 발동 (graceful degradation)
- 이벤트 없으면 reasons에 아무것도 추가하지 않음 (silent)
- M2를 위해 historical 발표일도 함께 저장 (FRED는 historical 제공)

### M2 추가 설계 — 임계치 캘리브레이션 상세

**event_calibration 테이블 구조**: event_type, ticker(nullable), rsi_threshold,
bb_std, lookback_days, last_calibrated_at 컬럼으로 구성.
스캐너가 시그널 생성 시 이 테이블을 참조해 임계치를 동적으로 교체.

**중요 원칙**: 초기값(RSI 35 등)은 직관으로 박지 말 것.
기존 backtest/engine.py로 과거 이벤트 발생일 구간만 필터링해
RSI 30~35 범위를 grid search → 적중률 최고 수치를 저장.
FRED API가 historical 발표일을 제공하므로 과거 CPI/PPI/NFP 날짜 확보 가능.

**이벤트별 거동 차이 주의**:
어닝스는 실적에 따라 갭업/갭다운 양방향이라 단순 보수화가 답이 아님.
FOMC는 변동성은 크나 방향성 불명. CPI는 시장 방향성과 가장 직결.
이벤트 타입별로 calibration 결과가 다를 수 있으므로 개별 row로 관리.

### M2 추가 설계 — 포트폴리오 노출도 (/exposure)

베타(민감도)와 상관계수(동조성) 두 지표를 나란히 계산해 표시.
베타: "SPY 1% 변동 시 내 포트는 몇 % 변동하나" (레버리지 감지).
상관계수(R²): "SPY와 얼마나 같은 방향으로 움직이나" (분산 감지).
벤치마크는 SPY 기본, SOXX(반도체 특화) 병행 계산 권장.
사용자 포트(NVDA/SOXL/AMD/TQQQ 등)가 반도체에 집중돼 있어
"사실상 반도체 ETF와 R²=0.94" 같은 한 줄이 과잉확신 방지에 핵심.

### M2 추가 설계 — Dual-Window 상관관계 (Regime 감지)

MarketFetcher 스냅샷에 SOXL-US10Y 상관관계를 두 윈도로 추가.
20일(현재 레짐) vs 200일(기준선)을 동시에 계산.
5일 윈도는 spurious correlation 위험이 커서 사용하지 않음.
출력 형태: "SOXL-US10Y 상관: 현재(20d) -0.75 / 기준(200d) -0.31 → 역상관 심화"
이 한 줄이 LLM 매크로 해설에서 "금리 우려성 하락" 판단의 정량 근거가 됨.

### M3 추가 설계 — Information Gap Analysis 상세

LLMClient에 purpose="null_result_classifier" 추가.
분석가 리포트, 8-K/DART, 뉴스 헤드라인 모두 empty일 때 별도 카테고리로 분류.

**False Negative 방지 원칙**: "이유 없음"을 단정하지 말고 점검 범위를 명시.
알림 포맷 예시:
"📋 점검 완료: SEC 8-K, DART, Finnhub 뉴스 5건 → 특이사항 없음
→ 원인 불명 수급 쏠림 또는 미공개 리스크 가능성. 직접 확인 권장."

### 알림 후속 추적 (Post-mortem)

AlertLog 테이블에 price_7d, price_30d, return_7d, return_30d 컬럼 추가.
스캐너가 주 1회 배치로 발동 후 7일/30일 경과 알림을 업데이트.
누적 후 "VIX 30 이상 시 발동 시그널 승률 67%" 같은 자기 검증 통계 생성.
/cost 명령에 승률 통계도 함께 표시.

### 8-K / DART 공시 Key Sentence 추출

LLMClient purpose="filing_summary"로 공시 원문에서
"유상증자", "공급계약 체결", "최대주주 변경" 등 주가 직결 키워드와 금액만 추출.
알림 첫 줄에 배치. 원문 전체 요약이 아닌 임팩트 한 줄이 목표.
SEC EDGAR RSS: https://www.sec.gov/cgi-bin/browse-edgar (무료)
DART OpenAPI: https://opendart.fss.or.kr (무료, API 키 필요 → .env DART_API_KEY)

**LLM 호출 원칙**: filing_summary, null_result_classifier 등 LLM 심층 분석은
STRONG 시그널에서만 트리거. WEAK·Threshold 알림은 CalendarFetcher 캐시 데이터만 사용.