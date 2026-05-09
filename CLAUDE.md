# dip-alert

> **마지막 업데이트**: 2026-05-08

미국/한국 주식·ETF + 크립토를 동시에 감시. 가격 트리거 알림은 표면, 핵심은
**"왜 움직이는가" 매크로 해설** (LLM + web_search). 본인 운영 → 지인 공유 → 멀티유저로 확장 예정.

---

## 문서 인덱스

| 파일 | 언제 보나 |
|---|---|
| **CLAUDE.md** (이 파일) | 시작 / 환경 / 운영 명령 — 빠른 참조 |
| [ARCHITECTURE.md](ARCHITECTURE.md) | "왜 이런 구조인가" — 모듈 책임, 핵심 설계 결정 |
| [SKILLS.md](SKILLS.md) | "X 를 추가하려면" — 반복 작업 패턴 |
| [ROADMAP.md](ROADMAP.md) | 페이즈 / 마일스톤 진행 + 설계 결정 + 남은 작업 |

> 사용자 횡단 메모리: `~/.claude/projects/-Users-tg-bullsfact/memory/MEMORY.md` 참조

---

## 파일 트리

```
backend/
├── core/
│   ├── datasource/             yfinance / Binance(ccxt) / 라우팅 / 캘린더(M1)
│   ├── strategy/dip_buy.py     RSI+BB 전략 (+ 캘린더 컨텍스트 + 동적 threshold)
│   ├── backtest/               engine, metrics, rules
│   ├── alerter.py              Telegram 발송 + 쿨다운 + 통화 인식
│   ├── scanner.py              스캐너 루프 (DB 워치리스트)
│   ├── threshold_alerts.py     가격/VIX/F&G 임계치 알림 [S1]
│   ├── positions.py            보유 포지션 + 익절 룰 [S3]
│   ├── market.py               시장 스냅샷 (지수/VIX/F&G/원자재/크립토)
│   ├── correlations.py         Dual-window 상관계수 [M2-C]
│   ├── exposure.py             포트폴리오 베타 + R² [M2-B]
│   ├── calibration.py          이벤트별 RSI 캘리브레이션 [M2-A]
│   ├── macro_briefing.py       일일 매크로 해설 [S2]
│   ├── on_demand.py            /why on-demand 해설 [S4]
│   ├── money.py                통화 추정 + 포맷 [S5]
│   └── enrichment/             LLMClient + analysts + synthesizer
├── db/                         SQLAlchemy + SQLite (lightweight migration)
├── api/                        FastAPI (watchlist, alerts, backtest)
├── scripts/                    bot.py, seed/backfill/calibrate 스크립트
├── main.py                     스캐너 진입점
└── dipalert.db                 운영중

frontend/                       React + Vite (Watchlist/Alerts/Backtest)
run.sh                          start/stop/restart/status/logs/attach
```

---

## 환경 변수 (`backend/.env`)

```bash
TELEGRAM_TOKEN=...
TELEGRAM_CHAT_ID=...
BINANCE_API_KEY=               # 퍼블릭은 빈칸 OK
BINANCE_API_SECRET=
ANTHROPIC_API_KEY=...          # /why, 매크로 해설
MAX_DAILY_LLM_USD=2
RSI_THRESHOLD=35
BB_STD=2.0
COOLDOWN_MIN=60
CHECK_INTERVAL_MIN=15
DATA_INTERVAL=1d
DATA_PERIOD=2y
FINNHUB_API_KEY=               # 미국 어닝스 [M1]
FRED_API_KEY=                  # CPI/PPI/NFP [M1]
DART_API_KEY=                  # 한국 공시 [M1]
```

---

## 운영 명령

```bash
./run.sh start | stop | restart | status
./run.sh logs bot|scanner       # tail -f
./run.sh attach bot|scanner     # tmux 직접 (Ctrl+B D 빠짐)

# 시드/백필/캘리브레이션
python -m backend.scripts.seed_threshold_alerts        # 매매전략 §3 알림 25건
python -m backend.scripts.backfill_names               # Watchlist.name 일괄 갱신
python -m backend.scripts.calibrate_events             # M2-A 백테스트 캘리브레이션
```

---

## 개발자 컨텍스트

- 오너: Teo (Seoul, FAB AMR 시스템 개발 배경)
- 스택: FastAPI + React/Vite (amr-sim 경험)
- 보유 종목: NVDA, SOXL, AMD, TQQQ, GOOGL, AVGO, QQQ, TSLA, 005930.KS, 000660.KS 등
- 매매 전략: 레버리지 ETF 분할 매도 + T1/T2/T3 매수 사다리 + 익절 룰 (평단 단계별)

---

## 시작 (신규 환경)

```bash
cd backend
pip install -r requirements.txt
cp .env.example .env
# .env 채우고:
python main.py
```
