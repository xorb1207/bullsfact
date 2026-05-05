# dip-alert — Claude Code 인수인계 문서

## 프로젝트 개요

미국 주식/ETF(SOXL, TQQQ 등)와 크립토(ETH/USDT 등)를 동시에 감시하여
RSI 과매도 + 볼린저 밴드 하단 이탈 시 Telegram으로 알람을 보내는 시스템.
장기적으로 멀티유저 서비스로 확장 예정.

---

## 현재 완료 상태 (P1 Core Engine — 골격)

```
backend/
├── core/
│   ├── datasource/
│   │   ├── base.py            ✅ DataSource 추상 클래스
│   │   ├── yfinance_source.py ✅ 주식/ETF/야후크립토
│   │   ├── binance_source.py  ✅ ccxt 기반 Binance
│   │   ├── provider.py        ✅ 티커 자동 라우팅
│   │   └── __init__.py        ✅
│   ├── strategy/
│   │   └── dip_buy.py         ✅ RSI+BB 전략, Signal 타입
│   ├── alerter.py             ✅ Telegram 발송 + 쿨다운
│   └── scanner.py             ✅ 스케줄러 루프
├── main.py                    ✅ 진입점
├── requirements.txt           ✅
└── .env.example               ✅
```

**미완료 (다음 작업)**:
- SQLite DB 레이어 (워치리스트 CRUD, 알람 로그)
- FastAPI 엔드포인트
- 백테스트 엔진
- React 프론트엔드
- Docker Compose

---

## 아키텍처 핵심 결정사항

### 티커 라우팅 규칙 (`core/datasource/provider.py`)
```
ETH/USDT, BTC/USDT  →  Binance (ccxt)   # 슬래시 + USDT/BTC/ETH/BNB/USDC
ETH-USD, BTC-USD    →  yfinance          # 야후 크립토 포맷
SOXL, TQQQ, NVDA   →  yfinance          # 미국 주식/ETF
```

### Freqtrade 참고 패턴
- `DataSource` ABC → exchange 추상화 (새 거래소 추가 시 상속만 하면 됨)
- `populate_indicators()` + `generate_signal()` → 전략 분리
- `DataProvider`가 라우팅 담당 → 엔진은 소스를 몰라도 됨

### 알람 신호 강도
```python
SignalStrength.STRONG  # RSI < 35 AND 가격 < BB하단 (둘 다)
SignalStrength.WEAK    # RSI < 35 OR  가격 < BB하단 (하나만)
SignalStrength.NONE    # 조건 미충족
```

---

## 전체 로드맵

| 페이즈 | 내용 | 상태 |
|--------|------|------|
| P1 | Core Engine (DataSource + Strategy + AlertEngine + Scanner) | 🔨 진행중 |
| P2 | 백테스트 엔진 (Signal Replay, 승률/MDD/P&L) | ⬜ 미시작 |
| P3 | React 대시보드 (워치리스트 UI, 알람 히스토리, 백테스트 차트) | ⬜ 미시작 |
| P4 | 멀티유저 / JWT 인증 / PostgreSQL 마이그레이션 | ⬜ 미시작 |
| P5 | Docker Compose + Railway/Render 배포 | ⬜ 미시작 |

---

## P1 다음 작업 목록

### 1. DB 레이어 (`backend/db/`)
```python
# 필요한 테이블
watchlist:      id, ticker, source (yfinance/binance), added_at, active
alert_log:      id, ticker, strength, price, rsi, bb_lower, sent_at
backtest_result: id, ticker, strategy_params, win_rate, mdd, total_return, created_at
```
SQLAlchemy + SQLite로 구현. 나중에 PostgreSQL로 교체 쉽도록 engine URL만 바꾸면 되게.

### 2. FastAPI 엔드포인트 (`backend/api/`)
```
POST   /watchlist          { ticker: "SOXL" }
DELETE /watchlist/{ticker}
GET    /watchlist          → 전체 목록 + 현재 지표
GET    /alerts             → 알람 히스토리 (페이지네이션)
POST   /backtest           { ticker, start_date, end_date, rsi_threshold, bb_std }
GET    /backtest/{id}      → 결과 조회
```

### 3. Scanner를 DB 워치리스트와 연동
현재 `main.py`의 `TICKERS` 하드코딩 → DB에서 동적으로 읽어오게 변경.

---

## 기술 스택

| 레이어 | 기술 |
|--------|------|
| 백엔드 | FastAPI + uvicorn |
| 데이터 | yfinance, ccxt (Binance) |
| 지표 | pandas-ta (RSI, BB, MACD) |
| DB | SQLite → PostgreSQL (SQLAlchemy) |
| 알람 | Telegram Bot API |
| 프론트 | React + Vite (amr-sim 구조 재활용) |
| 배포 | Docker Compose + Railway |

---

## 환경 변수 (`.env.example` 참고)

```bash
TELEGRAM_TOKEN=...
TELEGRAM_CHAT_ID=...
BINANCE_API_KEY=          # 퍼블릭 데이터는 빈칸도 됨
BINANCE_API_SECRET=
RSI_THRESHOLD=35
BB_STD=2.0
COOLDOWN_MIN=60
CHECK_INTERVAL_MIN=15
DATA_INTERVAL=1h
DATA_PERIOD=60d
```

---

## 개발자 컨텍스트

- 오너: Teo (Seoul 기반, FAB AMR 시스템 개발 배경)
- 코딩 스타일: FastAPI + React/Vite에 익숙 (amr-sim 프로젝트 경험)
- 목표: 개인 사용 → 지인 공유 → 멀티유저 서비스 순서로 확장
- 감시 대상: SOXL, TQQQ (레버리지 ETF) + ETH/USDT 등 크립토 혼합
- 미래 확장: 전체 섹터 스캔, DXF 맵 임포트 수준의 확장성 염두

---

## Claude Code 시작 명령어

```bash
cd backend
pip install -r requirements.txt
cp .env.example .env
# .env에 TELEGRAM_TOKEN, TELEGRAM_CHAT_ID 입력 후:
python main.py
```
