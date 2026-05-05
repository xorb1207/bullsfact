"""
Telegram 봇 — 워치리스트 관리 + 헬스체크.

스캐너와 별도 프로세스. DB는 공유 (SQLite).
한 사람만 사용 가정 (TELEGRAM_CHAT_ID 일치하는 메시지만 처리).

실행:
    python -m backend.scripts.bot

명령어: /help 참고.
"""
from __future__ import annotations

import html
import logging
import math
import os
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

import requests
from dotenv import load_dotenv

load_dotenv("backend/.env", override=True)

from backend.db import SessionLocal, init_db, crud
from backend.core.datasource import DataProvider
from backend.core.strategy import DipBuyStrategy
from backend.core.strategy.dip_buy import Signal, SignalStrength
from backend.core.alerter import AlertEngine

log = logging.getLogger("bot")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


@dataclass
class BotContext:
    token: str
    allowed_chat_id: str
    provider: DataProvider
    strategy: DipBuyStrategy
    alerter: AlertEngine


# ──────────────────────────────────────────────
# Telegram I/O
# ──────────────────────────────────────────────

API_BASE = "https://api.telegram.org/bot{token}"


def _api(ctx: BotContext, method: str, **payload) -> dict:
    url = f"{API_BASE.format(token=ctx.token)}/{method}"
    resp = requests.post(url, json=payload, timeout=35)
    if not resp.ok:
        log.error(f"telegram {method} 실패 status={resp.status_code} body={resp.text[:200]}")
        return {}
    return resp.json()


def send(ctx: BotContext, text: str, chat_id: str | None = None) -> None:
    target = chat_id or ctx.allowed_chat_id
    _api(ctx, "sendMessage", chat_id=target, text=text, parse_mode="HTML",
         disable_web_page_preview=True)


# ──────────────────────────────────────────────
# 명령어 핸들러
# ──────────────────────────────────────────────

def _esc(s: str) -> str:
    return html.escape(s, quote=False)


def cmd_help(args: list[str], ctx: BotContext) -> str:
    return (
        "<b>bullsfact 봇 명령어</b>\n"
        "━━━━━━━━━━━━━━━━━\n"
        "<b>/list</b> (또는 /ls)\n"
        "  워치리스트 + 현재 RSI/신호\n\n"
        "<b>/add TICKER</b>\n"
        "  종목 추가 (예: /add NVDA, /add ETH/USDT)\n\n"
        "<b>/remove TICKER</b> (또는 /rm)\n"
        "  종목 삭제\n\n"
        "<b>/cost</b>\n"
        "  오늘/어제 LLM 비용 + 알람 카운트\n\n"
        "<b>/test [TICKER]</b>\n"
        "  가짜 STRONG 알람 (raw, LLM 비용 0)\n\n"
        "<b>/help</b>\n"
        "  이 메시지\n"
    )


def cmd_list(args: list[str], ctx: BotContext) -> str:
    db = SessionLocal()
    try:
        items = crud.list_watchlist(db, active_only=False)
    finally:
        db.close()

    if not items:
        return "워치리스트 비어있음. <code>/add TICKER</code> 로 추가."

    rows = ["<b>워치리스트</b>", "━━━━━━━━━━━━━━━━━"]
    for w in items:
        if not w.active:
            rows.append(f"⚪ {_esc(w.ticker)} ({w.source}) — 비활성")
            continue
        try:
            df = ctx.provider.get_ohlcv(w.ticker, interval="1h", period="60d")
            sig = ctx.strategy.generate_signal(df, w.ticker)
            rsi = sig.indicators.get("rsi")
            rsi_str = f"{rsi:.1f}" if isinstance(rsi, float) and not math.isnan(rsi) else "N/A"
            emoji = {"strong": "🔴", "weak": "🟡", "none": "⚪"}.get(sig.strength.value, "⚪")
            rows.append(
                f"{emoji} <b>{_esc(w.ticker)}</b> ({w.source}) "
                f"${sig.price:.4g} | RSI {rsi_str} | {sig.strength.value}"
            )
        except Exception as e:
            rows.append(f"⚠️ {_esc(w.ticker)}: {_esc(type(e).__name__)}")
    return "\n".join(rows)


def cmd_add(args: list[str], ctx: BotContext) -> str:
    if not args:
        return "사용법: <code>/add TICKER</code> (예: /add NVDA, /add ETH/USDT)"
    ticker = args[0].strip().upper()
    # 크립토 슬래시는 대소문자 보존 X — 포맷만 체크
    if "/" not in ticker and "-" not in ticker:
        ticker = ticker.upper()

    db = SessionLocal()
    try:
        existing = crud.get_watchlist_item(db, ticker=ticker)
        if existing:
            if existing.active:
                return f"이미 등록됨: <b>{_esc(ticker)}</b>"
            # 비활성이면 활성화. 없으면 add_watchlist에 active 인자 없음 → 단순 처리
            return f"이미 있음 (비활성): <b>{_esc(ticker)}</b> — 직접 활성화 필요"
        try:
            source = ctx.provider.source_of(ticker)
        except Exception:
            return f"⚠️ <b>{_esc(ticker)}</b> 라우팅 불가 — yfinance/binance 어느 쪽도 매칭 안 됨"
        crud.add_watchlist(db, ticker=ticker, source=source)
        return f"✅ 추가됨: <b>{_esc(ticker)}</b> ({source})"
    finally:
        db.close()


def cmd_remove(args: list[str], ctx: BotContext) -> str:
    if not args:
        return "사용법: <code>/remove TICKER</code>"
    ticker = args[0].strip().upper()
    db = SessionLocal()
    try:
        ok = crud.remove_watchlist(db, ticker=ticker)
        return f"🗑️ 삭제됨: <b>{_esc(ticker)}</b>" if ok else f"❌ 없음: <b>{_esc(ticker)}</b>"
    finally:
        db.close()


def _alert_summary_today(db) -> dict:
    from sqlalchemy import select, func
    from backend.db import AlertLog
    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    rows = db.execute(
        select(AlertLog.strength, func.count(AlertLog.id))
        .where(AlertLog.sent_at >= today)
        .group_by(AlertLog.strength)
    ).all()
    counts = {"strong": 0, "weak": 0}
    for s, n in rows:
        counts[s] = n
    return counts


def cmd_cost(args: list[str], ctx: BotContext) -> str:
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        yest_start = today_start - timedelta(days=1)
        wk_start = today_start - timedelta(days=7)

        today = crud.llm_cost_summary(db, since=today_start)
        yest = crud.llm_cost_summary(db, since=yest_start, until=today_start)
        wk = crud.llm_cost_summary(db, since=wk_start)

        alerts = _alert_summary_today(db)
        cap = float(os.getenv("MAX_DAILY_LLM_USD", "2.0"))
        today_pct = today["cost_usd"] / cap * 100 if cap else 0
        return (
            "<b>💰 bullsfact 비용 리포트</b>\n"
            "━━━━━━━━━━━━━━━━━\n"
            f"<b>오늘</b>: ${today['cost_usd']:.4f} / 캡 ${cap:.2f} ({today_pct:.1f}%)\n"
            f"  LLM {today['calls']}회, "
            f"알람 STRONG {alerts['strong']} / WEAK {alerts['weak']}\n\n"
            f"<b>어제</b>: ${yest['cost_usd']:.4f} ({yest['calls']}회)\n"
            f"<b>최근 7일</b>: ${wk['cost_usd']:.4f} ({wk['calls']}회)\n"
        )
    finally:
        db.close()


def cmd_test(args: list[str], ctx: BotContext) -> str:
    """가짜 STRONG 시그널을 raw 모드로 발사 (LLM 비용 0)."""
    ticker = args[0].upper() if args else "ETH/USDT"
    if "/" in ticker:
        source = "binance"
        price, bb_lower = 3200.0, 3250.0
    else:
        source = "yfinance"
        price, bb_lower = 18.42, 18.65

    sig = Signal(
        ticker=ticker, strength=SignalStrength.STRONG, price=price,
        reasons=["RSI=31.2 < 35", f"가격 ${price:.2f} < BB하단 ${bb_lower:.2f}"],
        indicators={"rsi": 31.2, "bb_lower": bb_lower, "bb_mid": price + 1, "bb_upper": price + 2},
    )

    # raw 발사 — enricher 끄고
    saved_enricher = ctx.alerter._enricher
    ctx.alerter._enricher = None
    try:
        ok = ctx.alerter.process(sig, source)
    finally:
        ctx.alerter._enricher = saved_enricher

    return f"{'✅' if ok else '❌'} 테스트 알람 ({_esc(ticker)}) 발사 완료" if ok else "❌ 발사 실패"


COMMANDS: dict[str, Callable[[list[str], BotContext], str]] = {
    "help":   cmd_help,
    "start":  cmd_help,
    "list":   cmd_list,
    "ls":     cmd_list,
    "add":    cmd_add,
    "remove": cmd_remove,
    "rm":     cmd_remove,
    "cost":   cmd_cost,
    "test":   cmd_test,
}


# ──────────────────────────────────────────────
# 폴링 루프
# ──────────────────────────────────────────────

def _process_update(update: dict, ctx: BotContext) -> None:
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return
    text = (msg.get("text") or "").strip()
    chat_id = str((msg.get("chat") or {}).get("id", ""))
    if not text:
        return

    # 권한 체크
    if chat_id != ctx.allowed_chat_id:
        log.warning(f"unauthorized chat_id={chat_id} text={text[:30]!r}")
        # 공격자에게 응답 안 함 (정보 누출 방지)
        return

    if not text.startswith("/"):
        return  # 명령어 아니면 무시

    parts = text[1:].split()
    cmd = parts[0].split("@")[0].lower()  # /add@bot_name 같은 형태 정리
    args = parts[1:]

    handler = COMMANDS.get(cmd)
    if not handler:
        send(ctx, f"❓ 모르는 명령: <code>/{_esc(cmd)}</code>\n<code>/help</code> 참고")
        return

    try:
        reply = handler(args, ctx)
    except Exception as e:
        log.error(f"handler {cmd} 실패: {e}\n{traceback.format_exc()}")
        reply = f"⚠️ <code>/{_esc(cmd)}</code> 처리 중 오류: <code>{_esc(type(e).__name__)}</code>"
    if reply:
        send(ctx, reply)


def main() -> None:
    init_db()

    token = os.getenv("TELEGRAM_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or "YOUR_" in token:
        raise SystemExit("TELEGRAM_TOKEN 미설정")
    if not chat_id or "YOUR_" in chat_id:
        raise SystemExit("TELEGRAM_CHAT_ID 미설정")

    provider = DataProvider(
        binance_api_key=os.getenv("BINANCE_API_KEY", ""),
        binance_api_secret=os.getenv("BINANCE_API_SECRET", ""),
    )
    strategy = DipBuyStrategy(
        rsi_threshold=float(os.getenv("RSI_THRESHOLD", "35")),
        bb_std=float(os.getenv("BB_STD", "2.0")),
    )
    alerter = AlertEngine(
        telegram_token=token,
        telegram_chat_id=chat_id,
        cooldown_min=0,           # /test 가 쿨다운에 막히지 않게
        log_to_db=False,          # /test 는 DB에 안 쌓이게
        enricher=None,            # /test 는 raw
    )
    ctx = BotContext(
        token=token, allowed_chat_id=chat_id,
        provider=provider, strategy=strategy, alerter=alerter,
    )

    log.info("🤖 봇 시작 — 폴링 모드")
    offset: int | None = None
    backoff = 1
    while True:
        try:
            params = {"timeout": 30, "allowed_updates": ["message"]}
            if offset is not None:
                params["offset"] = offset
            resp = requests.get(
                f"{API_BASE.format(token=token)}/getUpdates",
                params=params, timeout=40,
            )
            if not resp.ok:
                log.error(f"getUpdates status={resp.status_code} body={resp.text[:200]}")
                time.sleep(min(backoff, 60))
                backoff = min(backoff * 2, 60)
                continue
            backoff = 1
            data = resp.json()
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                _process_update(update, ctx)
        except requests.exceptions.RequestException as e:
            log.error(f"네트워크 오류 ({type(e).__name__}) — 재시도")
            time.sleep(min(backoff, 60))
            backoff = min(backoff * 2, 60)
        except KeyboardInterrupt:
            log.info("중단됨")
            return


if __name__ == "__main__":
    main()
