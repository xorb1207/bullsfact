# Roadmap

> 페이즈 / 마일스톤 진행 상태 + 설계 결정 + 남은 작업.
> 새 작업 시작 시 먼저 여기 추가, 끝나면 ✅.

---

## 페이즈

| 페이즈 | 내용 | 상태 |
|---|---|---|
| P1 | Core Engine (DataSource + Strategy + AlertEngine + Scanner) | ✅ |
| P2 | 백테스트 엔진 (Signal Replay, 승률/MDD/P&L) | ✅ |
| P3 | React 대시보드 (워치리스트 / 알람 / 백테스트) | ✅ |
| P3+ | Telegram 봇 + 일일 브리핑 + LLM enrichment | ✅ (로드맵 외 추가) |
| P4 | 멀티유저 / JWT 인증 / PostgreSQL 마이그레이션 | ⬜ |
| P5 | Docker Compose + Railway/Render 배포 | ⬜ |

---

## 스프린트 (S 시리즈)

| 스프린트 | 내용 | 상태 |
|---|---|---|
| S1 | 가격/VIX/F&G 임계치 알림 (`/alert`) | ✅ |
| S2 | 일일 매크로 해설 (LLM + web_search) | ✅ |
| S3 | 보유 포지션 + 익절 룰 (`/position`) | ✅ |
| S4 | `/why` on-demand 매크로/티커 해설 | ✅ |
| S5 | 통화 자동 인식 + 회사명 캐싱 + UI 정리 | ✅ |

---

## 마일스톤 (M 시리즈)

| 마일스톤 | 내용 | 상태 |
|---|---|---|
| **M1** | 이벤트 캘린더 컨텍스트 주입 (CalendarFetcher) | ✅ |
| **M2-A** | 백테스트 기반 RSI 임계치 캘리브레이션 | ✅ |
| **M2-B** | `/exposure` 포트폴리오 베타 + R² | ✅ |
| **M2-C** | Dual-Window 상관관계 (`/market` 통합) | ✅ |
| **M3** | Information Gap Analysis (원인 불명 수급) | ⬜ |
| M3-부가 | 알림 후속 추적 (price_7d/30d, 자기 검증 통계) | ⬜ |
| M3-부가 | 8-K / DART Key Sentence 추출 | ⬜ |

---

## M1 설계 결정사항 (완료)

**목적**: DipBuy/Threshold 시그널 발동 시 `Signal.reasons` 에 이벤트 컨텍스트 한 줄 자동 주입. "왜 지금 RSI가 낮은가?" 외부 앱 없이 알림 안에서 즉답.

**데이터 소스**:
- 미국 어닝스: Finnhub (무료 60req/min) — yfinance.calendar 신뢰도 낮아 제외
- 매크로 (CPI/PPI/NFP): FRED (무료, historical 포함)
- FOMC: 연 8회 하드코딩 (Fed 공식)
- 한국 공시: DART OpenAPI

**원칙**:
- CalendarFetcher 실패 시 시그널은 정상 발동 (graceful degradation)
- 이벤트 없으면 reasons에 아무것도 추가하지 않음 (silent)
- M2 위해 historical 발표일도 함께 저장 (FRED 는 historical 제공)

**구현 결과**: `core/datasource/calendar_fetcher.py`. DipBuy + ThresholdAlert 양쪽 통합. `/test`, `/market`, `/list` 에도 가시화.

---

## M2 설계 결정사항 (완료)

### M2-A: 임계치 캘리브레이션

**event_calibration 테이블**: `(event_type, ticker, rsi_threshold, hit_rate, sample_count, ...)`. 스캐너가 시그널 생성 시 참조해 임계치 동적 교체.

**중요 원칙**: 초기값 RSI 35 같은 직관 박지 말 것. `backtest/engine.py` 패턴으로 과거 이벤트 발생일 구간만 필터링 → RSI 30~40 grid search → 적중률 최고 수치 저장.

**이벤트별 거동 차이 주의**:
- 어닝스: 실적에 따라 갭업/갭다운 양방향 → 단순 보수화 답 아님
- FOMC: 변동성 크나 방향성 불명
- CPI: 시장 방향성과 가장 직결

→ 이벤트 타입별로 calibration 결과 다를 수 있어 개별 row 관리. **MVP 범위**: CPI, NFP, FOMC × SPY, QQQ, SOXL, TQQQ, NVDA. 적중 정의: D+5 종가 +2%.

**향후 확장**: 어닝 갭업/갭다운 방향성 분리, FOMC 매파/비둘기 톤 분류, CPI 서프라이즈 부호 가중.

### M2-B: 포트폴리오 노출도 (`/exposure`)

베타(민감도) + R²(동조성) 두 지표 나란히. 벤치마크 SPY + SOXX(반도체 특화) 병행. 사용자 포트가 NVDA/SOXL/AMD/TQQQ 반도체 집중이라 "사실상 반도체 ETF 와 R²=0.94" 한 줄이 과잉확신 방지에 핵심.

**구현**: `core/exposure.py`. 가중 시계열 회귀로 포트폴리오 종합 R² 정확 계산 (R² 가중평균 불가). 자동 경고 — R²>0.90 / β>1.5 / 통화혼용.

### M2-C: Dual-Window 상관관계

**MarketSnapshot 에 SOXL-US10Y / S&P-US10Y / BTC-DXY 상관 추가**. 20일(현재 레짐) vs 200일(기준선) 동시 계산. 5일 윈도는 spurious correlation 위험으로 사용 X.

**출력**: "SOXL–10Y: 현재(20d) -0.45 / 기준(200d) -0.03 ↓ 역상관 심화" 같은 자동 라벨. 이 한 줄이 LLM 매크로 해설에서 "금리 우려성 하락" 판단의 정량 근거.

---

## M3 설계 결정사항 (예정)

### Information Gap Analysis

LLM purpose `null_result_classifier` 추가. 분석가 리포트 / 8-K / DART / 뉴스 헤드라인 모두 empty 일 때 별도 카테고리 분류.

**False Negative 방지 원칙**: "이유 없음" 단정 X. 점검 범위 명시.

알림 포맷 예시:
```
📋 점검 완료: SEC 8-K, DART, Finnhub 뉴스 5건 → 특이사항 없음
→ 원인 불명 수급 쏠림 또는 미공개 리스크 가능성. 직접 확인 권장.
```

### 알림 후속 추적 (Post-mortem)

`AlertLog` 에 `price_7d`, `price_30d`, `return_7d`, `return_30d` 컬럼 추가. 스캐너가 주 1회 배치로 발동 후 7일/30일 경과 알림 업데이트.

누적 후 "VIX 30 이상 시 발동 시그널 승률 67%" 같은 자기 검증 통계 생성. `/cost` 명령 출력에 승률 통계도 함께 표시.

### 8-K / DART Key Sentence 추출

LLM purpose `filing_summary` 로 공시 원문에서 "유상증자 / 공급계약 체결 / 최대주주 변경" 같은 주가 직결 키워드 + 금액만 추출. 알림 첫 줄 배치. 원문 전체 요약이 아닌 임팩트 한 줄이 목표.

- SEC EDGAR RSS: https://www.sec.gov/cgi-bin/browse-edgar (무료)
- DART OpenAPI: https://opendart.fss.or.kr (무료, DART_API_KEY)

**LLM 호출 원칙**: filing_summary, null_result_classifier 등 LLM 심층 분석은 STRONG 시그널에서만 트리거. WEAK·Threshold 알림은 CalendarFetcher 캐시 데이터만 사용.

---

## 남은 작업 (우선순위)

### 🟢 작은 + 가성비
1. **매도 캘린더 리마인더** — 양도세 250만원 분할 매도 (5~6월/8~9월/12월) cron + reminders 테이블. 반나절.
2. **한국어 회사명** — yfinance 영문만이라 005930.KS = "Samsung Electronics". NAVER 검색 또는 KRX 마스터. 2~3시간.
3. **알림 후속 추적** (M3 부가) — `AlertLog` price_7d/30d 컬럼 + 주 1회 배치. 1일.

### 🟡 중간
4. **M3 본체** — Information Gap Analysis. 1~2일.
5. **8-K/DART Key Sentence 추출**. 1~2일.

### 🔴 큰 인프라
6. **P4 멀티유저** — JWT + PostgreSQL (DATABASE_URL 만 교체로 가능하게 설계됨, 검증 필요).
7. **P5 배포** — Docker Compose + Railway/Render.

### 🟣 운영 개선 (자투리)
8. **이벤트별 거동 차이 보정** — 어닝 양방향 처리, FOMC 방향성 분리.
9. **NaN 가격 표시** — 한국 종목 KRX 휴장 시간대 처리.

---

## 합의된 진행 순서 (현재)

1. ✅ 커밋 (M2 작업 정리)
2. 명령어 컨벤션 + Markdown 분리 ← **현재 진행**
3. 1, 2, 3번 (매도 캘린더 / 한국어 사명 / 알림 후속 추적)
4. M3 본체 + 8-K Key Sentence
5. P4 멀티유저
6. P5 배포

---

## 합의된 운영 원칙

- **외부 풀 프로덕트 클론 지양**: 핵심 20%만 차용 (Dexter 논의 정착, 메모리 참조)
- **사용자 needs 1순위**: "왜 움직이는가" 매크로 해설 > 매수/매도 알림
- **본인 안정 운영 우선**: P4-P5 는 본인 며칠 운영 후 데이터 + 감각 쌓인 뒤
- **새 LLM 호출**: STRONG 시그널 또는 명시적 사용자 트리거(`/why`)에서만. WEAK·Threshold 는 캐시 데이터만
