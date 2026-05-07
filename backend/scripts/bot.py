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
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Optional

import requests
import schedule
from dotenv import load_dotenv

load_dotenv("backend/.env", override=True)

from backend.db import SessionLocal, init_db, crud
from backend.core.datasource import DataProvider
from backend.core.strategy import DipBuyStrategy
from backend.core.strategy.dip_buy import Signal, SignalStrength
from backend.core.alerter import AlertEngine
from backend.core.market import MarketFetcher, format_telegram as format_market
from backend.core.macro_briefing import generate_daily_recap, format_for_telegram as format_macro
from backend.core.on_demand import (
    research_ticker, research_macro_now,
    format_for_telegram as format_research,
)
from backend.core.enrichment.llm_client import LLMClient
from backend.core.positions import MILESTONES, highest_passed_milestone
from backend.core.money import format_money, format_money_signed, currency_for

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
    market: MarketFetcher
    llm: Optional[LLMClient] = None         # 매크로 해설 / on-demand 분석에 사용


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


def _persist_llm_call(usage) -> None:
    """LLM 호출 비용을 DB에 기록 — main.py와 동일 로직."""
    db = SessionLocal()
    try:
        crud.insert_llm_call(
            db,
            model=usage.model,
            purpose=usage.purpose or "unknown",
            ticker=usage.ticker,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_creation_tokens=usage.cache_creation_tokens,
            cost_cents=round(usage.cost_usd() * 100, 4),
            latency_ms=usage.latency_ms,
        )
    except Exception as e:
        log.warning(f"[LLM persist] 실패 (무시): {e}")
    finally:
        db.close()


def cmd_help(args: list[str], ctx: BotContext) -> str:
    return (
        "<b>bullsfact 봇 명령어</b>\n"
        "━━━━━━━━━━━━━━━━━\n"
        "<b>/list</b> (또는 /ls)\n"
        "  워치리스트 + 현재 RSI/신호\n\n"
        "<b>/market</b>\n"
        "  시장 현황 (지수/채권/심리/크립토/원자재)\n"
        "  매일 06:00 KST 자동 발송\n\n"
        "<b>/add TICKER</b>\n"
        "  종목 추가 (예: /add NVDA, /add ETH/USDT)\n\n"
        "<b>/remove TICKER</b> (또는 /rm)\n"
        "  종목 삭제\n\n"
        "<b>/cost</b>\n"
        "  오늘/어제 LLM 비용 + 알람 카운트\n\n"
        "<b>/alert</b>\n"
        "  가격/VIX/F&G 임계치 알림 관리 (서브명령 안내)\n\n"
        "<b>/position</b> (또는 /pos)\n"
        "  보유 포지션 + 익절 룰 추적 (서브명령 안내)\n\n"
        "<b>/why [TICKER]</b>\n"
        "  왜 움직이는가 — LLM + 웹검색 해설\n"
        "  인자 없으면 현재 매크로 상황 (~$0.05/회)\n\n"
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
        name_str = f"  <i>{_esc(w.name)}</i>" if w.name else ""
        if not w.active:
            rows.append(f"⚪ {_esc(w.ticker)} ({w.source}){name_str} — 비활성")
            continue
        try:
            df = ctx.provider.get_ohlcv(w.ticker, interval="1h", period="60d")
            sig = ctx.strategy.generate_signal(df, w.ticker)
            rsi = sig.indicators.get("rsi")
            rsi_str = f"{rsi:.1f}" if isinstance(rsi, float) and not math.isnan(rsi) else "N/A"
            emoji = {"strong": "🔴", "weak": "🟡", "none": "⚪"}.get(sig.strength.value, "⚪")
            price_str = format_money(sig.price, w.ticker)
            rows.append(
                f"{emoji} <b>{_esc(w.ticker)}</b> ({w.source}){name_str} "
                f"{price_str} | RSI {rsi_str} | {sig.strength.value}"
            )
        except Exception as e:
            rows.append(f"⚠️ {_esc(w.ticker)}{name_str}: {_esc(type(e).__name__)}")
    return "\n".join(rows)


def _fetch_ticker_name(ticker: str, source: str) -> Optional[str]:
    """yfinance.info에서 회사명 가져오기. 실패는 조용히 None."""
    if source != "yfinance":
        return None
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info
        return info.get("longName") or info.get("shortName")
    except Exception:
        return None


def cmd_add(args: list[str], ctx: BotContext) -> str:
    if not args:
        return "사용법: <code>/add TICKER</code> (예: /add NVDA, /add ETH/USDT, /add 005930.KS)"
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
            return f"이미 있음 (비활성): <b>{_esc(ticker)}</b> — 직접 활성화 필요"
        try:
            source = ctx.provider.source_of(ticker)
        except Exception:
            return f"⚠️ <b>{_esc(ticker)}</b> 라우팅 불가 — yfinance/binance 어느 쪽도 매칭 안 됨"

        name = _fetch_ticker_name(ticker, source)
        crud.add_watchlist(db, ticker=ticker, source=source, name=name)
        name_str = f"  <i>{_esc(name)}</i>" if name else ""
        return f"✅ 추가됨: <b>{_esc(ticker)}</b> ({source}){name_str}"
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


def cmd_market(args: list[str], ctx: BotContext) -> str:
    """시장 현황 스냅샷 — 지수/채권/심리/크립토/원자재."""
    snap = ctx.market.fetch()
    return format_market(snap)


def send_market_report(ctx: BotContext) -> None:
    """매일 자동 발송 (스케줄러 호출). 시장 스냅샷 + LLM 매크로 해설."""
    try:
        log.info("[scheduler] 매일 시장 리포트 발송")
        snap = ctx.market.fetch()
        text = "🌅 <b>모닝 브리핑</b>\n\n" + format_market(snap)

        # 매크로 해설 (LLM + web_search) — 실패해도 본 브리핑은 발송
        if ctx.llm is not None:
            try:
                briefing = generate_daily_recap(ctx.llm, snap)
                if briefing:
                    text += "\n\n" + format_macro(briefing)
                    log.info(f"[scheduler] 매크로 해설 추가 (cost ${briefing.cost_usd:.4f})")
                else:
                    log.warning("[scheduler] 매크로 해설 생성 실패 — 스킵")
            except Exception as e:
                log.error(f"[scheduler] 매크로 해설 예외 (무시): {type(e).__name__}: {e}")

        send(ctx, text)
    except Exception as e:
        log.error(f"[scheduler] 매일 시장 리포트 실패: {e}\n{traceback.format_exc()}")


# ──────────────────────────────────────────────
# /alert — ThresholdAlert 관리
# ──────────────────────────────────────────────

_ALERT_HELP = (
    "<b>가격/지표 임계치 알림</b>\n"
    "━━━━━━━━━━━━━━━━━\n"
    "<b>/alert list</b>  활성 알림 목록\n"
    "<b>/alert list all</b>  발동완료 포함 전체\n"
    "<b>/alert remove ID</b>  삭제\n"
    "<b>/alert pause ID</b>  / <b>resume ID</b>\n\n"
    "<b>추가 — 가격 절대값:</b>\n"
    "<code>/alert add price SOXL below 110 [T1] [HIGH] [메모]</code>\n\n"
    "<b>추가 — 가격 상대값 (52주 고점 등):</b>\n"
    "<code>/alert add price SOXL below high_252d -32% [T1] [HIGH] [메모]</code>\n"
    "  ref: high_252d | low_252d | ema_50d\n\n"
    "<b>추가 — VIX / F&G:</b>\n"
    "<code>/alert add vix above 30 [MED]</code>\n"
    "<code>/alert add fg_cnn below 25 [HIGH]</code>\n"
    "<code>/alert add fg_crypto above 75</code>\n"
)


def _parse_priority(token: str) -> Optional[str]:
    t = token.upper()
    return t if t in ("HIGH", "MED", "LOW") else None


def _parse_tier(token: str) -> Optional[str]:
    t = token.upper()
    return t if t in ("T1", "T2", "T3") else None


def _parse_pct(token: str) -> Optional[float]:
    """'-32%' 또는 '-0.32' 둘 다 받기."""
    s = token.strip()
    if s.endswith("%"):
        try:
            return float(s[:-1]) / 100.0
        except ValueError:
            return None
    try:
        v = float(s)
    except ValueError:
        return None
    # 절대값이 1보다 크면 % 단위로 입력한 것으로 간주
    return v / 100.0 if abs(v) > 1 else v


_VALID_REF = {"high_252d", "low_252d", "ema_50d"}


def _cmd_alert_add(args: list[str]) -> str:
    """args[0]은 'add' 가 이미 빠진 상태. 즉 args = [metric_type, ...]."""
    if not args:
        return _ALERT_HELP
    metric = args[0].lower()

    db = SessionLocal()
    try:
        if metric == "price":
            # /alert add price TICKER DIRECTION VALUE_OR_REF [PCT] [tier] [priority] [note...]
            if len(args) < 4:
                return "사용법: <code>/alert add price TICKER below 110</code> 또는\n<code>/alert add price TICKER below high_252d -32%</code>"
            ticker = args[1].upper()
            direction = args[2].lower()
            third = args[3]

            # 절대값 vs 상대값 구분
            if third in _VALID_REF:
                if len(args) < 5:
                    return "상대값은 PCT 필요: <code>/alert add price SOXL below high_252d -32%</code>"
                pct = _parse_pct(args[4])
                if pct is None:
                    return f"PCT 파싱 실패: <code>{_esc(args[4])}</code>"
                kwargs = {"ref_window": third, "ref_pct": pct}
                rest = args[5:]
            else:
                try:
                    abs_v = float(third)
                except ValueError:
                    return f"가격 파싱 실패: <code>{_esc(third)}</code>"
                kwargs = {"abs_value": abs_v}
                rest = args[4:]

            tier = None
            priority = "MED"
            note_parts: list[str] = []
            for tok in rest:
                if (t := _parse_tier(tok)):
                    tier = t
                elif (p := _parse_priority(tok)):
                    priority = p
                else:
                    note_parts.append(tok)
            note = " ".join(note_parts) if note_parts else None

            row = crud.insert_threshold_alert(
                db, metric_type="price", ticker=ticker, direction=direction,
                tier=tier, priority=priority, note=note, **kwargs,
            )
            return f"✅ 알림 #{row.id} 등록: <b>{_esc(ticker)}</b> {direction} {_esc(args[3])}{('  ' + args[4]) if 'ref_window' in kwargs else ''}  [{priority}]{(' ' + tier) if tier else ''}"

        if metric in ("vix", "fg_cnn", "fg_crypto"):
            # /alert add METRIC DIRECTION VALUE [priority] [note...]
            if len(args) < 3:
                return f"사용법: <code>/alert add {metric} above 30</code>"
            direction = args[1].lower()
            try:
                abs_v = float(args[2])
            except ValueError:
                return f"값 파싱 실패: <code>{_esc(args[2])}</code>"
            priority = "MED"
            note_parts: list[str] = []
            for tok in args[3:]:
                if (p := _parse_priority(tok)):
                    priority = p
                else:
                    note_parts.append(tok)
            note = " ".join(note_parts) if note_parts else None
            row = crud.insert_threshold_alert(
                db, metric_type=metric, direction=direction, abs_value=abs_v,
                priority=priority, note=note,
            )
            return f"✅ 알림 #{row.id} 등록: <b>{metric.upper()}</b> {direction} {abs_v}  [{priority}]"

        return f"알 수 없는 metric: <code>{_esc(metric)}</code>\n\n" + _ALERT_HELP
    except ValueError as e:
        return f"⚠️ {_esc(str(e))}"
    finally:
        db.close()


def _format_alert_row(a) -> str:
    pri_emoji = {"HIGH": "🔴", "MED": "🟡", "LOW": "🟢"}.get(a.priority, "⚪")
    status = "" if a.active else " <i>(발동완료)</i>"
    if a.metric_type == "price":
        if a.abs_value is not None:
            cond = f"{a.direction} ${a.abs_value:g}"
        else:
            cond = f"{a.direction} {a.ref_window} {a.ref_pct*100:+.1f}%"
        head = f"{a.ticker}"
        if a.tier:
            head += f" {a.tier}"
    else:
        cond = f"{a.direction} {a.abs_value:g}"
        head = a.metric_type.upper()
    last = f"  현재≈{a.last_value:g}" if a.last_value is not None else ""
    note = f"\n   📌 {_esc(a.note)}" if a.note else ""
    return f"{pri_emoji} <b>#{a.id}</b> {_esc(head)} {_esc(cond)}{last}{status}{note}"


def _cmd_alert_list(args: list[str]) -> str:
    show_all = bool(args and args[0].lower() == "all")
    db = SessionLocal()
    try:
        rows = crud.list_threshold_alerts(db, active_only=not show_all)
    finally:
        db.close()
    if not rows:
        return "등록된 알림 없음. <code>/alert</code> 로 사용법 확인."
    header = "<b>알림 목록</b>" + (" (전체)" if show_all else " (활성)")
    return header + "\n━━━━━━━━━━━━━━━━━\n" + "\n".join(_format_alert_row(r) for r in rows)


def _cmd_alert_remove(args: list[str]) -> str:
    if not args:
        return "사용법: <code>/alert remove 42</code>"
    try:
        aid = int(args[0])
    except ValueError:
        return f"ID 파싱 실패: <code>{_esc(args[0])}</code>"
    db = SessionLocal()
    try:
        ok = crud.delete_threshold_alert(db, aid)
    finally:
        db.close()
    return f"🗑️ 알림 #{aid} 삭제됨" if ok else f"❌ 없음: #{aid}"


def _cmd_alert_set_active(args: list[str], active: bool) -> str:
    if not args:
        return f"사용법: <code>/alert {'resume' if active else 'pause'} 42</code>"
    try:
        aid = int(args[0])
    except ValueError:
        return f"ID 파싱 실패: <code>{_esc(args[0])}</code>"
    db = SessionLocal()
    try:
        ok = crud.set_threshold_active(db, aid, active)
    finally:
        db.close()
    label = "재활성화" if active else "일시중지"
    return f"✅ 알림 #{aid} {label}됨" if ok else f"❌ 없음: #{aid}"


def cmd_alert(args: list[str], ctx: BotContext) -> str:
    if not args:
        return _ALERT_HELP
    sub = args[0].lower()
    rest = args[1:]
    if sub == "list":
        return _cmd_alert_list(rest)
    if sub == "add":
        return _cmd_alert_add(rest)
    if sub in ("remove", "rm", "delete"):
        return _cmd_alert_remove(rest)
    if sub == "pause":
        return _cmd_alert_set_active(rest, active=False)
    if sub == "resume":
        return _cmd_alert_set_active(rest, active=True)
    if sub in ("help", "?"):
        return _ALERT_HELP
    return f"알 수 없는 하위 명령: <code>{_esc(sub)}</code>\n\n" + _ALERT_HELP


# ──────────────────────────────────────────────
# /position — 보유 포지션 + 익절 룰 추적
# ──────────────────────────────────────────────

_POSITION_HELP = (
    "<b>보유 포지션 + 익절 룰</b>\n"
    "━━━━━━━━━━━━━━━━━\n"
    "<b>/position list</b>  현재 P&amp;L + 다음 마일스톤\n"
    "<b>/position add TICKER QTY AVG_COST [메모]</b>\n"
    "  예: <code>/position add SOXL 23 21.47</code>\n"
    "  → 신규 추가. 이미 지나간 마일스톤은 자동 스킵 (알림 폭탄 방지)\n\n"
    "<b>/position update TICKER QTY AVG_COST</b>\n"
    "  수량/평단 갱신. <i>마일스톤은 0으로 초기화</i>\n\n"
    "<b>/position remove TICKER</b>\n\n"
    "<b>익절 룰 (참고):</b>\n"
    "  +50% → 20% 매도 (누적 20%)\n"
    "  +100% → 30% 매도 (누적 50%, 원금 회수)\n"
    "  +200% → 25% 매도 (누적 75%)\n"
    "  +400% → 15% 매도 (누적 90%)\n"
    "  +600% → 재량 매도 (공짜 칩)\n"
)


def _next_milestone_label(highest: float, current_pct: float) -> str:
    """포지션 리스트 출력용 — 다음 목표 표시."""
    for ms, _, _, label in MILESTONES:
        if ms > highest + 1e-9:
            gap = (ms - current_pct) * 100
            if gap > 0:
                return f"다음: +{ms*100:.0f}% (까지 {gap:+.1f}%p)"
            return f"다음: +{ms*100:.0f}% (도달, 곧 알림)"
    return "최종 마일스톤 도달"


def _cmd_position_list(ctx: BotContext) -> str:
    db = SessionLocal()
    try:
        rows = crud.list_positions(db)
        if not rows:
            return "등록된 포지션 없음. <code>/position add TICKER QTY AVG_COST</code> 로 추가."
        return _format_position_list(rows, db, ctx)
    finally:
        db.close()


def _format_position_list(rows, db, ctx: BotContext) -> str:
    lines = ["<b>보유 포지션</b>", "━━━━━━━━━━━━━━━━━"]
    totals: dict[str, dict] = {}  # cur → {"value": float, "pnl": float}

    for p in rows:
        try:
            df = ctx.provider.get_ohlcv(p.ticker, interval="1d", period="5d")
            cur = float(df["close"].iloc[-1])
        except Exception as e:
            lines.append(f"⚠️ <b>{_esc(p.ticker)}</b>: {_esc(type(e).__name__)} (현재가 fetch 실패)")
            continue

        ret = (cur / p.avg_cost) - 1.0 if p.avg_cost > 0 else 0.0
        pnl = (cur - p.avg_cost) * p.qty
        value = cur * p.qty

        cur_code = currency_for(p.ticker)
        slot = totals.setdefault(cur_code, {"value": 0.0, "pnl": 0.0})
        slot["value"] += value
        slot["pnl"] += pnl

        # 회사명 (Watchlist 캐시 활용)
        wl = crud.get_watchlist_item(db, p.ticker)
        name_str = f"  <i>{_esc(wl.name)}</i>" if wl and wl.name else ""

        emoji = "🟢" if ret >= 0 else "🔴"
        ms_str = _next_milestone_label(p.highest_milestone, ret)
        notes_str = f"\n   📝 {_esc(p.notes)}" if p.notes else ""
        qty_str = f"{p.qty:.4f}".rstrip("0").rstrip(".")

        price_str = format_money(cur, p.ticker)
        avg_str = format_money(p.avg_cost, p.ticker)
        value_str = format_money(value, p.ticker)
        pnl_str = format_money_signed(pnl, p.ticker)

        lines.append(
            f"{emoji} <b>{_esc(p.ticker)}</b>{name_str}  {ret*100:+.1f}%  "
            f"({qty_str}주 × {price_str} = {value_str})\n"
            f"   평단 {avg_str} | 손익 {pnl_str} | {_esc(ms_str)}"
            f"{notes_str}"
        )

    lines.append("━━━━━━━━━━━━━━━━━")
    if totals:
        # 통화 1개면 단순 합계, 여러개면 통화별 분리
        total_lines = []
        for cur_code, slot in totals.items():
            total_lines.append(
                f"  {cur_code}: 평가액 {format_money(slot['value'], '_'+cur_code if cur_code!='USD' else '')}"
                f" | 평가손익 {format_money_signed(slot['pnl'], '_'+cur_code if cur_code!='USD' else '')}"
            )
        # format_money는 ticker 기반이라 KRW 강제하려면 트릭 필요 — 간단히 직접
        total_lines = []
        for cur_code, slot in totals.items():
            sym = {"USD": "$", "KRW": "₩", "JPY": "¥", "HKD": "HK$"}.get(cur_code, cur_code + " ")
            v_fmt = f"{sym}{slot['value']:,.0f}" if cur_code in ("KRW", "JPY") else f"{sym}{slot['value']:,.2f}"
            p_sign = "+" if slot["pnl"] >= 0 else "-"
            p_abs = abs(slot["pnl"])
            p_fmt = f"{p_sign}{sym}{p_abs:,.0f}" if cur_code in ("KRW", "JPY") else f"{p_sign}{sym}{p_abs:,.2f}"
            total_lines.append(f"  {cur_code}: 평가액 {v_fmt} | 평가손익 {p_fmt}")
        lines.append("<b>합계</b>:")
        lines.extend(total_lines)
    return "\n".join(lines)


def _cmd_position_add(args: list[str], ctx: BotContext, *, force_milestone_zero: bool = False) -> str:
    """/position add TICKER QTY AVG_COST [메모...]"""
    if len(args) < 3:
        return "사용법: <code>/position add SOXL 23 21.47 [메모]</code>"
    ticker = args[0].upper()
    try:
        qty = float(args[1])
        avg_cost = float(args[2])
    except ValueError:
        return f"수량/평단 파싱 실패: <code>{_esc(args[1])} {_esc(args[2])}</code>"
    if qty <= 0 or avg_cost <= 0:
        return "수량과 평단은 양수여야 함"

    notes = " ".join(args[3:]) if len(args) > 3 else None

    # 이미 지나간 마일스톤 자동 스킵 (알림 폭탄 방지)
    skip_to = 0.0
    if not force_milestone_zero:
        try:
            df = ctx.provider.get_ohlcv(ticker, interval="1d", period="5d")
            cur = float(df["close"].iloc[-1])
            cur_ret = (cur / avg_cost) - 1.0
            skip_to = highest_passed_milestone(cur_ret)
        except Exception as e:
            log.warning(f"/position add {ticker} 현재가 fetch 실패: {e}")

    db = SessionLocal()
    try:
        row = crud.upsert_position(
            db, ticker=ticker, qty=qty, avg_cost=avg_cost,
            highest_milestone=skip_to, notes=notes,
        )
    finally:
        db.close()

    qty_str = f"{qty:.4f}".rstrip("0").rstrip(".")
    skip_str = f" (이미 +{skip_to*100:.0f}% 지나감 — 다음 마일스톤만 감시)" if skip_to > 0 else ""
    return f"✅ 포지션 등록: <b>{_esc(ticker)}</b> {qty_str}주 @ ${avg_cost:.2f}{skip_str}"


def _cmd_position_update(args: list[str], ctx: BotContext) -> str:
    """qty/avg_cost 변경 시 마일스톤 0으로 초기화 (의도적 — 평단 바뀌었으니 재계산)."""
    return _cmd_position_add(args, ctx, force_milestone_zero=False)
    # 동작은 add와 동일. add 자체가 이미 지나간 마일스톤을 자동 스킵하므로 OK.


def _cmd_position_remove(args: list[str]) -> str:
    if not args:
        return "사용법: <code>/position remove TICKER</code>"
    ticker = args[0].upper()
    db = SessionLocal()
    try:
        ok = crud.delete_position(db, ticker)
    finally:
        db.close()
    return f"🗑️ 포지션 삭제: <b>{_esc(ticker)}</b>" if ok else f"❌ 없음: <b>{_esc(ticker)}</b>"


def cmd_position(args: list[str], ctx: BotContext) -> str:
    if not args:
        return _POSITION_HELP
    sub = args[0].lower()
    rest = args[1:]
    if sub == "list" or sub == "ls":
        return _cmd_position_list(ctx)
    if sub == "add":
        return _cmd_position_add(rest, ctx)
    if sub == "update":
        return _cmd_position_update(rest, ctx)
    if sub in ("remove", "rm", "delete"):
        return _cmd_position_remove(rest)
    if sub in ("help", "?"):
        return _POSITION_HELP
    return f"알 수 없는 하위 명령: <code>{_esc(sub)}</code>\n\n" + _POSITION_HELP


# ──────────────────────────────────────────────
# /why — 매크로/티커 해설 (LLM + web_search, on-demand)
# ──────────────────────────────────────────────

def cmd_why(args: list[str], ctx: BotContext) -> str:
    """
    /why            → 현재 시장 매크로 해설
    /why TICKER     → 특정 종목 최근 동향 해설
    """
    if ctx.llm is None:
        return ("⚠️ LLM 비활성 (ANTHROPIC_API_KEY 없음).\n"
                "<code>backend/.env</code> 에 키 추가 후 봇 재시작.")

    # 인자 없음 → 매크로 해설
    if not args:
        snap = ctx.market.fetch()
        result = research_macro_now(ctx.llm, snap)
        if not result:
            return "⚠️ 해설 생성 실패 (API 과부하 또는 예산 초과). <code>/cost</code> 확인"
        return format_research(result)

    # /why TICKER
    ticker_raw = args[0].strip()
    ticker = ticker_raw.upper() if ("/" not in ticker_raw and "-" not in ticker_raw) else ticker_raw

    # 일봉 OHLCV (최대 1년) — fetch 실패해도 LLM은 돌림
    df = None
    try:
        df = ctx.provider.get_ohlcv(ticker, interval="1d", period="1y")
    except Exception as e:
        log.warning(f"/why {ticker} OHLCV fetch 실패: {type(e).__name__}: {e}")

    # 매크로 스냅샷도 컨텍스트로 (실패해도 진행)
    snap = None
    try:
        snap = ctx.market.fetch()
    except Exception as e:
        log.warning(f"/why {ticker} market snap 실패: {type(e).__name__}: {e}")

    result = research_ticker(ctx.llm, ticker, df, snap)
    if not result:
        return f"⚠️ {_esc(ticker)} 해설 생성 실패 (API 과부하 또는 예산 초과)"
    return format_research(result)


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
    "market": cmd_market,
    "alert":  cmd_alert,
    "position": cmd_position,
    "pos":      cmd_position,
    "why":    cmd_why,
    "test":   cmd_test,
}

# Telegram 자동완성 메뉴에 노출할 명령어 (alias는 제외 — 깔끔하게)
COMMAND_DESCRIPTIONS: list[tuple[str, str]] = [
    ("list",   "워치리스트 + 현재 RSI/신호"),
    ("market", "시장 현황 (지수/채권/심리/크립토)"),
    ("add",    "종목 추가 (예: /add NVDA)"),
    ("remove", "종목 삭제"),
    ("cost",   "LLM 비용 + 알람 카운트"),
    ("alert",  "가격/VIX/F&G 임계치 알림 (/alert 로 사용법)"),
    ("position", "보유 포지션 + 익절 룰 (/position 로 사용법)"),
    ("why",    "왜 움직이는가 — /why TICKER 또는 /why (매크로)"),
    ("test",   "가짜 STRONG 알람 (헬스체크)"),
    ("help",   "명령어 목록"),
]


def register_commands(ctx: BotContext) -> None:
    """Telegram에 명령어 메뉴 등록 (setMyCommands)."""
    commands = [{"command": cmd, "description": desc} for cmd, desc in COMMAND_DESCRIPTIONS]
    result = _api(ctx, "setMyCommands", commands=commands)
    if result.get("ok"):
        log.info(f"명령어 메뉴 등록 완료 ({len(commands)}개)")
    else:
        log.warning(f"setMyCommands 실패: {result}")


def _start_daily_scheduler(ctx: BotContext) -> None:
    """
    매일 발송 작업을 별도 데몬 스레드로 실행.
    schedule 1.2+ 의 timezone 인자 사용 (Asia/Seoul 06:00).
    """
    daily_at = os.getenv("MARKET_DAILY_AT_KST", "06:00")
    try:
        schedule.every().day.at(daily_at, "Asia/Seoul").do(send_market_report, ctx)
    except Exception as e:
        # schedule 구버전 폴백 — UTC로 환산해서 등록
        log.warning(f"timezone 인자 미지원 — UTC로 환산: {e}")
        h, m = map(int, daily_at.split(":"))
        utc_h = (h - 9) % 24                                  # KST → UTC
        schedule.every().day.at(f"{utc_h:02d}:{m:02d}").do(send_market_report, ctx)

    def loop():
        log.info(f"[scheduler] 매일 시장 리포트 KST {daily_at}")
        while True:
            try:
                schedule.run_pending()
            except Exception as e:
                log.error(f"[scheduler] 실행 실패: {e}")
            time.sleep(30)

    threading.Thread(target=loop, daemon=True, name="market-scheduler").start()


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
    market = MarketFetcher()

    # LLMClient — 매크로 해설 / on-demand 분석용. ANTHROPIC_API_KEY 없으면 None.
    llm: Optional[LLMClient] = None
    if os.getenv("ANTHROPIC_API_KEY"):
        llm = LLMClient(
            max_daily_usd=float(os.getenv("MAX_DAILY_LLM_USD", "2.0")),
            on_call=_persist_llm_call,
        )
        log.info("LLMClient 활성 — 매크로 해설/on-demand 사용 가능")
    else:
        log.info("ANTHROPIC_API_KEY 없음 — 매크로 해설 비활성")

    ctx = BotContext(
        token=token, allowed_chat_id=chat_id,
        provider=provider, strategy=strategy, alerter=alerter, market=market,
        llm=llm,
    )

    register_commands(ctx)
    _start_daily_scheduler(ctx)
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
