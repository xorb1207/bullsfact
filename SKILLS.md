# Skills — 반복 작업 패턴

> "X 를 추가하려면" 형태로 정리된 절차. 코드 흩어진 곳을 한 번에 파악.

---

## Telegram 명령어 추가

**파일**: `backend/scripts/bot.py`

**4 곳 동기 갱신 필수**:

```python
# 1. 핸들러 함수 정의
def cmd_foo(args: list[str], ctx: BotContext) -> str:
    ...
    return "결과 메시지 (HTML)"

# 2. COMMANDS dict 등록 (alias 도 여기에)
COMMANDS = {
    ...
    "foo": cmd_foo,
    "f":   cmd_foo,    # alias
}

# 3. COMMAND_DESCRIPTIONS — Telegram 자동완성 메뉴 (alias 제외)
COMMAND_DESCRIPTIONS = [
    ...
    ("foo", "한 줄 설명 (자동완성에 노출)"),
]
# 자동완성에서 숨길 명령은 빼기 (test, cost 같은 것)

# 4. cmd_help 본문에 카테고리에 맞춰 추가
```

**서브명령 패턴** (`/alert add`, `/portfolio list` 등):

```python
def cmd_foo(args, ctx):
    if not args:
        return _FOO_HELP
    sub = args[0].lower()
    rest = args[1:]
    if sub == "list": return _cmd_foo_list(rest, ctx)
    if sub == "add":  return _cmd_foo_add(rest, ctx)
    if sub in ("remove", "rm"): return _cmd_foo_remove(rest, ctx)
    return f"알 수 없는 하위 명령: <code>{_esc(sub)}</code>\n\n" + _FOO_HELP
```

**HTML 이스케이프**: 사용자 입력 표시 시 항상 `_esc()` (이스케이프 함수 모듈 상단에 정의됨).

---

## 새 데이터 소스 (거래소/공급자) 추가

**예**: 일본 J-Quants 추가 시.

1. `backend/core/datasource/base.py` 의 `DataSource` ABC 상속:
   ```python
   class JQuantsSource(DataSource):
       def get_ohlcv(self, ticker, interval, period): ...
       def is_market_open(self, ticker): ...
   ```

2. `provider.py` 에 라우팅 분기 추가:
   ```python
   def source_of(self, ticker):
       if ticker.endswith(".T"): return "jquants"
       ...
   ```

3. `DataProvider.__init__` 에 인스턴스 보유.

4. **(M1 영향)** `calendar_fetcher.py` 의 `_is_us_ticker()` 분기 영향 받는지 점검. 일본 어닝 fetch 미지원이면 그대로.

5. **(통화 영향)** `money.py` 의 `currency_for()` 가 `.T → JPY` 이미 지원. 다른 suffix면 추가.

---

## DB 모델 추가

**파일**: `backend/db/models.py`

```python
class FooBar(Base):
    __tablename__ = "foo_bar"
    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(32), nullable=False, index=True)
    ...
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


Index("ix_foo_bar_ticker", FooBar.ticker)   # 필요시 별도 인덱스
```

**자동 마이그레이션**: `init_db()` 가 `create_all()` 호출 → 신규 테이블 자동 생성.

**기존 테이블에 컬럼 추가** 시: `database.py` 의 `_run_lightweight_migrations()` 에 `ALTER TABLE ... ADD COLUMN` 추가:

```python
if "watchlist" in existing_tables:
    cols = {c["name"] for c in inspector.get_columns("watchlist")}
    if "new_col" not in cols:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE watchlist ADD COLUMN new_col VARCHAR(64)"))
```

**CRUD 추가**: `backend/db/crud.py` 에 `list_foo_bars`, `insert_foo_bar`, `get_foo_bar`, `delete_foo_bar` 등 패턴 따라.

---

## 새 LLM 호출 종류 (purpose) 추가

**예**: M3 의 `null_result_classifier`.

1. `LLMClient.call()` 또는 `call_with_web_search()` 호출 시 `purpose="null_result_classifier"` 명시.
2. 비용은 `_persist_llm_call` 콜백이 자동으로 `llm_call_log` 에 기록 (`main.py` / `bot.py` 에서 wiring).
3. **비용 가드 확인**: `MAX_DAILY_LLM_USD` 캡 안에서 도는지 산정. 새 purpose가 일일 N회 추가되면 평균 cost × N 더해서 검증.
4. `/cost` 명령 출력에 자동 포함됨 (purpose 별 합계).

---

## Smoke Test (one-off 검증)

**패턴**: `python -c` 로 빠른 검증.

```bash
cd /Users/tg/bullsfact && python -c "
import os, logging
from dotenv import load_dotenv
load_dotenv('backend/.env', override=True)
logging.basicConfig(level=logging.WARNING)

# 검증할 모듈 import
from backend.core.X import Y
...
print('✅ 검증 OK')
"
```

**임시 DB 데이터 사용 시 정리 필수**:
```python
# 시드
db = SessionLocal()
crud.upsert_position(db, ticker='TEST', qty=1, avg_cost=1)
# ... 검증 ...
# 정리
crud.delete_position(db, 'TEST')
db.close()
```

**기존 smoke script**:
- `backend/scripts/test_alert.py` — 가짜 STRONG 시그널 발사
- `backend/scripts/test_calendar.py` — CalendarFetcher 동작 + key 유무 검증
- `backend/scripts/calibrate_events.py` — 이벤트 캘리브레이션 실행
- `backend/scripts/backfill_names.py` — Watchlist.name 일괄 갱신
- `backend/scripts/seed_threshold_alerts.py` — 매매전략 §3 알림 시드

---

## 운영 / 재시작

`run.sh` 한 곳에서 통합:

```bash
./run.sh start             # 봇 + 스캐너 둘 다
./run.sh stop
./run.sh restart           # 코드 변경 후 적용
./run.sh status            # tmux + 프로세스 확인
./run.sh logs bot          # bot.log tail -f
./run.sh logs scanner      # scanner.log tail -f
./run.sh attach bot        # tmux 세션 직접 (Ctrl+B D 로 빠짐)
```

**코드 변경 후 흐름**:
```
편집 → ./run.sh restart → ./run.sh logs scanner   (또는 bot)
```

scanner.log 마지막 사이클이 정상이면 OK.

---

## 신규 마일스톤 시작 시 절차

1. `ROADMAP.md` 의 마일스톤 표 확인 + 설계 결정사항 박스 추가
2. **별도 사양 합의** — Claude 와 대화로 구체화:
   - 데이터 소스 결정
   - DB 스키마 결정
   - 통합 포인트 (Strategy / Threshold / Alerter / Bot)
3. 구현
4. Smoke test
5. `./run.sh restart`
6. `git add -p` 로 작업 커밋 (시크릿/DB/log 제외 자동 — `.gitignore` 적용됨)
7. `git push`
8. `ROADMAP.md` 의 상태 ✅ 변경

---

## 회귀 검증 체크리스트

기존 기능 깨지지 않았나 확인:

```python
# 1. 모든 import 통과
python -c "
from backend.core.alerter import AlertEngine
from backend.core.scanner import Scanner
from backend.core.strategy import DipBuyStrategy
from backend.core.threshold_alerts import ThresholdAlertEvaluator
from backend.core.positions import PositionEvaluator
from backend.core.market import MarketFetcher
from backend.scripts.bot import COMMANDS
print('✅ imports OK')
print(f'명령어 {len(COMMANDS)}개')
"

# 2. DB 테이블 생성 + 마이그레이션
python -c "
from backend.db import init_db
from sqlalchemy import inspect
from backend.db.database import engine
init_db()
print(sorted(inspect(engine).get_table_names()))
"

# 3. 봇/스캐너 재시작 후 사이클 1회
./run.sh restart && sleep 60 && tail -20 scanner.log
```

---

## 자주 쓰는 디버깅

**Telegram 메시지 미발송**:
1. `bot.log` tail → 토큰/chat_id 에러? 권한?
2. `_send_telegram` 가 본문 4096자 초과? Telegram 제한 확인
3. HTML 깨짐? `_esc()` 누락? 미닫힌 태그?

**Scanner 사이클 실패**:
1. `scanner.log` 마지막 줄 확인
2. 특정 ticker만 실패면 → `provider.get_ohlcv(ticker, ...)` 직접 시도
3. 모든 ticker 실패면 → 네트워크 / yfinance API 체크

**LLM 캡 초과**:
- `/cost` 로 누적 확인
- `BudgetExceeded` 예외 → 호출자가 raw 폴백
- 며칠 누적 cap 자주 닿으면 `MAX_DAILY_LLM_USD` 상향 검토

**한국 종목 가격 NaN**:
- KRX 휴장 시간대 → yfinance 가 NaN 반환하기도 함
- scanner는 이 경우 신호 'none' 으로 자연스럽게 처리됨 (별도 핸들링 불필요)
