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
