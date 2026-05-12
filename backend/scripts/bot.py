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
from backend.db.models import User
from backend.core import wizard
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
from backend.core.datasource.calendar_fetcher import CalendarFetcher
from backend.core.exposure import compute_exposure, PortfolioExposure
from backend.core.reminders import get_due_reminders, format_section as format_reminders
from backend.core.portfolio_parser import parse_free_form
from backend.core.daily_summary import build_personal_section
from backend.core.post_mortem import update_returns, compute_statistics, format_stats_section

log = logging.getLogger("bot")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


@dataclass
class BotContext:
    token: str
    allowed_chat_id: str                     # OWNER chat_id (브로드캐스트 디폴트)
    provider: DataProvider
    strategy: DipBuyStrategy
    alerter: AlertEngine
    market: MarketFetcher
    llm: Optional[LLMClient] = None         # 매크로 해설 / on-demand 분석에 사용
    calendar: Optional[CalendarFetcher] = None  # M1: 이벤트 캘린더 (어닝/매크로/공시)
    # Request-scoped — _process_update 에서 채워짐. handler 안에서 본인 user/chat_id 식별용
    current_user: Optional[User] = None
    current_chat_id: Optional[str] = None


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


def send(
    ctx: BotContext,
    text: str,
    chat_id: str | None = None,
    reply_markup: dict | None = None,
) -> None:
    """
    명시적 chat_id 우선. 없으면 현재 요청자(ctx.current_chat_id), 그것도 없으면 OWNER.
    reply_markup: Telegram inline keyboard 등 (선택).
    """
    target = chat_id or getattr(ctx, "current_chat_id", None) or ctx.allowed_chat_id
    payload = {
        "chat_id": target,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    _api(ctx, "sendMessage", **payload)


def _kb_button(text: str, callback_data: str) -> dict:
    return {"text": text, "callback_data": callback_data}


def _inline_kb(rows: list[list[dict]]) -> dict:
    return {"inline_keyboard": rows}


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
            user_id=usage.user_id,
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
        "<b>🤖 bullsfact 명령어</b>\n"
        "━━━━━━━━━━━━━━━━━\n"
        "\n"
        "<b>📊 시장 정보</b>\n"
        "  /brief — 지금 브리핑 받기 (모닝 브리핑 즉시 재발송)\n"
        "    매일 06:00 KST 자동 발송과 동일 내용\n"
        "    매크로 해설은 24h 캐시 → 비용 0\n"
        "  /market — 시장 스냅샷 (지수/VIX/F&amp;G/상관)\n"
        "  /list — 워치리스트 + RSI + 임박 이벤트\n"
        "  /why [TICKER] — LLM 매크로/티커 해설\n"
        "    예: /why SOXL, /why (인자 없으면 매크로)\n"
        "\n"
        "<b>💼 포트폴리오</b>\n"
        "  /portfolio — 통합 (서브명령 안내)\n"
        "    list / exposure / add / update / remove\n"
        "    예: /portfolio add SOXL 23 21.47\n"
        "    여러 라인 일괄 입력 OK\n"
        "\n"
        "<b>📋 워치리스트</b>\n"
        "  /add TICKER — 추가 (예: /add NVDA, /add ETH/USDT)\n"
        "  /remove TICKER — 삭제\n"
        "\n"
        "<b>🔔 알림</b>\n"
        "  /alert — 가격/VIX/F&amp;G 임계치 (서브명령)\n"
        "    add / list / remove / pause / resume\n"
        "    여러 라인 일괄 입력 OK\n"
        "\n"
        "<b>📅 매도 캘린더</b>\n"
        "  /reminder — 양도세 분할 매도 일정 (서브명령)\n"
        "    add / list / done / remove\n"
        "    D-7부터 매일 06:00 KST 브리핑에 자동 첨부\n"
        "\n"
        "<b>⚙️ 시스템</b>\n"
        "  /cost — LLM 비용 + 알람 카운트\n"
        "  /test [TICKER] — 헬스체크 (가짜 알림)\n"
        "\n"
        "<i>구버전 alias: /position, /pos, /exposure, /expo 도 동작</i>"
    )


def cmd_list(args: list[str], ctx: BotContext) -> str:
    user_id = _resolve_user_id(ctx)
    db = SessionLocal()
    try:
        items = list(crud.list_watchlist(db, active_only=False, user_id=user_id))
    finally:
        db.close()

    if not items:
        return "워치리스트 비어있음. <code>/add TICKER</code> 로 추가."

    interval = os.getenv("DATA_INTERVAL", "1d")
    # /list 는 RSI 14 + BB 20 + 일일 변동률만 필요 — 60일이면 충분
    period = "60d"

    # 활성 종목 병렬 fetch + 신호 계산
    from concurrent.futures import ThreadPoolExecutor

    def _evaluate(w):
        try:
            df = ctx.provider.get_ohlcv(w.ticker, interval=interval, period=period)
            sig = ctx.strategy.generate_signal(df, w.ticker)
            close = df["close"] if df is not None and "close" in df.columns else None
            change_pct = None
            if close is not None and len(close) >= 2:
                prev = float(close.iloc[-2])
                cur = float(close.iloc[-1])
                if prev:
                    change_pct = (cur - prev) / prev * 100
            cal_suffix = _ticker_calendar_suffix(ctx, w.ticker)
            return (w, sig, change_pct, cal_suffix, None)
        except Exception as e:
            return (w, None, None, None, e)

    active = [w for w in items if w.active]
    inactive = [w for w in items if not w.active]

    results: list = []
    if active:
        with ThreadPoolExecutor(max_workers=8, thread_name_prefix="list") as ex:
            results = list(ex.map(_evaluate, active))

    rows = [
        "<b>워치리스트</b>  <i>⚪정상 · 🟡약신호 · 🔴강신호</i>",
        "━━━━━━━━━━━━━━━━━",
    ]

    for w, sig, change_pct, cal_suffix, err in results:
        short_name = _short_company_name(w.name)
        name_str = f"  <i>{_esc(short_name)}</i>" if short_name else ""
        if err is not None:
            rows.append(f"⚠️ <b>{_esc(w.ticker)}</b>{name_str}: {_esc(type(err).__name__)}")
            continue
        rsi = sig.indicators.get("rsi") if sig else None
        rsi_str = f"{rsi:.1f}" if isinstance(rsi, float) and not math.isnan(rsi) else "N/A"
        emoji = {"strong": "🔴", "weak": "🟡", "none": "⚪"}.get(
            sig.strength.value if sig else "none", "⚪"
        )
        price_str = format_money(sig.price if sig else 0.0, w.ticker)
        change_str = f"{change_pct:+.2f}%" if change_pct is not None else ""

        rows.append(f"{emoji} <b>{_esc(w.ticker)}</b>{name_str}")
        metric_parts = [price_str]
        if change_str:
            metric_parts.append(change_str)
        metric_parts.append(f"RSI {rsi_str}")
        rows.append("   " + "  ·  ".join(metric_parts))
        if cal_suffix:
            rows.append(f"   {cal_suffix}")

    for w in inactive:
        short_name = _short_company_name(w.name)
        name_str = f"  <i>{_esc(short_name)}</i>" if short_name else ""
        rows.append(f"⚪ {_esc(w.ticker)}{name_str} — 비활성")

    return "\n".join(rows)


def _ticker_calendar_suffix(ctx: BotContext, ticker: str, lookahead_days: int = 14) -> str:
    """
    종목 한정 이벤트 (어닝/공시) 한 줄 요약. 매크로는 /market 에서 따로 표시.

    공시는 같은 날에 여러 건이 흔함(특수관계인 거래·배당·실적 등 동시 공시).
    날짜별로 group → "📑 공시 D+N (M건)" 통합 표시.
    """
    if ctx.calendar is None:
        return ""
    try:
        events = ctx.calendar.get_events(ticker, lookahead_days=lookahead_days)
    except Exception:
        return ""
    ticker_events = [e for e in events if e.ticker]
    if not ticker_events:
        return ""

    parts: list[str] = []

    # 어닝스 (보통 분기 1회라 dedupe 불필요)
    for ev in ticker_events:
        if ev.event_type == "earnings":
            if ev.days_until == 0:
                tag = "오늘"
            elif ev.days_until > 0:
                tag = f"D-{ev.days_until}"
            else:
                tag = f"{-ev.days_until}일 전"
            parts.append(f"🎯 어닝 {tag}")
            break  # 가장 가까운 어닝만

    # 공시 — 날짜별로 그룹화
    dart_events = [e for e in ticker_events if e.event_type == "dart"]
    if dart_events:
        from collections import Counter
        date_counts = Counter(e.event_date for e in dart_events)
        # 가까운 날짜 우선 (음수=과거, 절댓값 작은 순)
        for ev_date, count in sorted(date_counts.items(), key=lambda x: -x[0].toordinal())[:2]:
            days = (ev_date - dart_events[0].event_date).days  # 그냥 reference
            # 명시적으로 다시 계산 (이벤트 객체에서 days_until 가져오기)
            d_until = next(e.days_until for e in dart_events if e.event_date == ev_date)
            if d_until == 0:
                tag = "오늘"
            elif d_until > 0:
                tag = f"D-{d_until}"
            else:
                tag = f"{-d_until}일 전"
            count_str = f" ({count}건)" if count > 1 else ""
            parts.append(f"📑 공시 {tag}{count_str}")

    return " · ".join(parts)


def _short_company_name(name: Optional[str], max_len: int = 26) -> str:
    """회사명 긴 거 잘라서 짧게. ETF 후미·법인격 제거 + cutoff."""
    if not name:
        return ""
    # 흔한 후미 제거 (정보가치 낮음)
    suffixes = [
        ", Ltd.", " Ltd.", ", Inc.", " Inc.", " Incorporated",
        " Corporation", " Corp.", " Co., Ltd.", " Company",
        " Trust", " Trust ETF", " Mini Trust ETF",
    ]
    cleaned = name
    # 가장 긴 매칭부터 (Co., Ltd. → Inc. 순 들어감)
    for s in sorted(suffixes, key=len, reverse=True):
        if cleaned.endswith(s):
            cleaned = cleaned[: -len(s)].rstrip(",. ")
            break
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[: max_len - 1].rstrip() + "…"


def _fetch_ticker_name(ticker: str, source: str) -> Optional[str]:
    """
    회사명 가져오기. 실패는 조용히 None.
    - 한국 종목 (.KS/.KQ): DART corpCode.xml 한글명 우선, 실패 시 yfinance 영문 fallback
    - 그 외: yfinance.info.longName/shortName
    """
    if source != "yfinance":
        return None
    # 한국 종목은 한글명 우선
    upper = ticker.upper()
    if upper.endswith(".KS") or upper.endswith(".KQ"):
        try:
            from backend.core.datasource.krx_names import resolve_korean_name
            kr = resolve_korean_name(ticker)
            if kr:
                return kr
        except Exception as e:
            log.debug(f"[name] KR resolve 실패 ({ticker}): {type(e).__name__}: {e}")
        # fallthrough → 영문 fallback
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
    if "/" not in ticker and "-" not in ticker:
        ticker = ticker.upper()
    user_id = _resolve_user_id(ctx)

    db = SessionLocal()
    try:
        existing = crud.get_watchlist_item(db, ticker=ticker, user_id=user_id)
        if existing:
            if existing.active:
                return f"이미 등록됨: <b>{_esc(ticker)}</b>"
            return f"이미 있음 (비활성): <b>{_esc(ticker)}</b> — 직접 활성화 필요"
        try:
            source = ctx.provider.source_of(ticker)
        except Exception:
            return f"⚠️ <b>{_esc(ticker)}</b> 라우팅 불가 — yfinance/binance 어느 쪽도 매칭 안 됨"

        # 회사명 캐싱 — 다른 사용자가 이미 캐시했으면 재사용
        name = None
        any_existing = crud.get_any_watchlist_item(db, ticker)
        if any_existing and any_existing.name:
            name = any_existing.name
        else:
            name = _fetch_ticker_name(ticker, source)
        crud.add_watchlist(db, ticker=ticker, source=source, name=name, user_id=user_id)
        name_str = f"  <i>{_esc(name)}</i>" if name else ""
        return f"✅ 추가됨: <b>{_esc(ticker)}</b> ({source}){name_str}"
    finally:
        db.close()


def cmd_remove(args: list[str], ctx: BotContext) -> str:
    if not args:
        return "사용법: <code>/remove TICKER</code>"
    ticker = args[0].strip().upper()
    user_id = _resolve_user_id(ctx)
    db = SessionLocal()
    try:
        ok = crud.remove_watchlist(db, ticker=ticker, user_id=user_id)
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
        env_cap = float(os.getenv("MAX_DAILY_LLM_USD", "2.0"))
        today_pct = today["cost_usd"] / env_cap * 100 if env_cap else 0

        # 사용자별 사용량 (간략)
        users = crud.list_users(db, active_only=True)
        user_lines = []
        for u in users:
            spent = crud.user_llm_spent_today(db, u.id)
            cap = crud.effective_daily_cap(u, env_cap)
            pct = (spent / cap * 100) if cap else 0
            label = u.name or f"#{u.id}"
            user_lines.append(
                f"  {u.tier:8s} {_esc(label)} ${spent:.4f} / ${cap:.2f} ({pct:.0f}%)"
            )

        # 캐시 통계
        cstats = crud.cache_stats(db, since_days=30)

        body_parts = [
            "<b>💰 bullsfact 비용 리포트</b>",
            "━━━━━━━━━━━━━━━━━",
            f"<b>오늘 (전체)</b>: ${today['cost_usd']:.4f} / 글로벌 캡 ${env_cap:.2f} ({today_pct:.1f}%)",
            f"  LLM {today['calls']}회, 알람 STRONG {alerts['strong']} / WEAK {alerts['weak']}",
            "",
            f"<b>어제</b>: ${yest['cost_usd']:.4f} ({yest['calls']}회)",
            f"<b>최근 7일</b>: ${wk['cost_usd']:.4f} ({wk['calls']}회)",
        ]
        if user_lines:
            body_parts.append("")
            body_parts.append("<b>👥 사용자별 (오늘)</b>")
            body_parts.extend(user_lines)
        body_parts.append("")
        body_parts.append(
            f"<b>📦 LLM 캐시</b> (최근 30일): "
            f"항목 {cstats['entries']}건, "
            f"절감 추정 ${cstats['savings_potential_usd']:.2f}"
        )
        body = "\n".join(body_parts)
    finally:
        db.close()

    # M3 부가: 자기 검증 통계 (누적 데이터 있을 때만 노출)
    try:
        stats = compute_statistics(lookback_days=90)
        body += format_stats_section(stats, lookback_days=90)
    except Exception as e:
        log.warning(f"[/cost] stats 계산 실패 (무시): {type(e).__name__}: {e}")

    return body


def _format_upcoming_events_section(ctx: BotContext, lookahead_days: int = 21) -> str:
    """매크로 임박 이벤트 섹션 (FOMC + FRED CPI/PPI/NFP). 종목 무관."""
    if ctx.calendar is None:
        return ""
    try:
        events = ctx.calendar.get_events("", lookahead_days=lookahead_days)
    except Exception:
        return ""
    if not events:
        return ""
    lines = [f"📅 <b>임박 이벤트</b> (D-{lookahead_days} 이내)"]
    for ev in events[:8]:  # 너무 길어지지 않게
        if ev.days_until == 0:
            tag = "오늘"
        elif ev.days_until == 1:
            tag = f"⚠️ D-1 ({ev.event_date.isoformat()})"
        else:
            tag = f"D-{ev.days_until} ({ev.event_date.isoformat()})"
        lines.append(f"  {_esc(ev.description)} — {tag}")
    # 본문 뒤에 빈 줄 한 칸 띄워서 시각적 분리
    return "\n\n" + "\n".join(lines)


def cmd_market(args: list[str], ctx: BotContext) -> str:
    """시장 현황 스냅샷 — 지수/채권/심리/크립토/원자재 + 임박 이벤트."""
    snap = ctx.market.fetch()
    body = format_market(snap)
    events_section = _format_upcoming_events_section(ctx)
    return body + events_section


def build_briefing(ctx: BotContext, user: User, header: str = "🌅 <b>모닝 브리핑</b>") -> str:
    """
    단일 사용자용 브리핑 텍스트 빌드.
    - 시장 스냅샷
    - 매크로 해설 (LLM, 24h 캐시 → 같은 날 반복 호출은 비용 0)
    - 개인 포트폴리오 + 워치리스트 (사용자별)
    - 매도 리마인더 임박 항목

    스케줄러(send_market_report)도 /brief 도 이 함수 호출.
    """
    snap = ctx.market.fetch()
    text = header + "\n\n" + format_market(snap)

    # 매크로 해설 (LLM 캐시 hit이면 즉답)
    if ctx.llm is not None:
        try:
            briefing = generate_daily_recap(ctx.llm, snap, user_id=user.id)
            if briefing:
                text += "\n\n" + format_macro(briefing)
        except Exception as e:
            log.error(f"[briefing] 매크로 해설 실패 user={user.id} (무시): {type(e).__name__}: {e}")

    # 개인 일일 요약
    try:
        personal = build_personal_section(ctx.provider, user.id)
        if personal:
            text += "\n\n" + personal
    except Exception as e:
        log.error(f"[briefing] 개인 요약 user={user.id} 실패 (무시): {e}")

    # 매도 리마인더
    try:
        due = get_due_reminders(user_id=user.id)
        if due:
            text += "\n" + format_reminders(due)
    except Exception as e:
        log.error(f"[briefing] reminders user={user.id} 실패 (무시): {e}")

    return text


def send_market_report(ctx: BotContext) -> None:
    """
    매일 06:00 KST 스케줄러 — 모든 active user 에게 브리핑 broadcast.
    매크로 해설 캐시(24h) 덕에 LLM 1회 호출 → N명에게 같은 본문.
    """
    try:
        log.info("[scheduler] 매일 시장 리포트 발송")
        try:
            n = update_returns()
            if n:
                log.info(f"[scheduler] 알림 후속 데이터 {n}건 갱신")
        except Exception as e:
            log.error(f"[scheduler] 후속 추적 실패 (무시): {type(e).__name__}: {e}")

        db = SessionLocal()
        try:
            users = list(crud.list_users(db, active_only=True))
        finally:
            db.close()

        for user in users:
            try:
                text = build_briefing(ctx, user)
                send(ctx, text, chat_id=user.telegram_chat_id)
                log.info(f"[scheduler] 브리핑 → user={user.id} ({user.name})")
            except Exception as e:
                log.error(f"[scheduler] 브리핑 발송 실패 user={user.id}: {e}")
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


def _cmd_alert_add(args: list[str], user_id: Optional[int] = None) -> str:
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
                tier=tier, priority=priority, note=note, user_id=user_id, **kwargs,
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
                priority=priority, note=note, user_id=user_id,
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


def _cmd_alert_list(args: list[str], user_id: Optional[int] = None) -> str:
    show_all = bool(args and args[0].lower() == "all")
    db = SessionLocal()
    try:
        rows = crud.list_threshold_alerts(db, active_only=not show_all, user_id=user_id)
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
    user_id = _resolve_user_id(ctx)
    sub = args[0].lower()
    rest = args[1:]
    if sub == "list":
        return _cmd_alert_list(rest, user_id=user_id)
    if sub == "add":
        return _cmd_alert_add(rest, user_id=user_id)
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

_PORTFOLIO_HELP = (
    "<b>📊 포트폴리오 — 보유 + 익절 룰 + 노출도</b>\n"
    "━━━━━━━━━━━━━━━━━\n"
    "<b>/portfolio list</b>  현재 P&amp;L + 다음 마일스톤\n"
    "<b>/portfolio exposure</b>  SPY/SOXX 베타 + R² (집중도)\n"
    "<b>/portfolio add TICKER QTY AVG_COST [메모]</b>\n"
    "  예: <code>/portfolio add SOXL 23 21.47</code>\n"
    "  여러 라인 일괄 입력 가능. 같은 종목 다른 값이면 거부됨\n\n"
    "<b>/portfolio update TICKER QTY AVG_COST</b>\n"
    "  명시적 갱신 (덮어쓰기 허용)\n\n"
    "<b>/portfolio remove TICKER</b>\n\n"
    "<b>익절 룰 (참고):</b>\n"
    "  +50% → 20% 매도 (누적 20%)\n"
    "  +100% → 30% 매도 (누적 50%, 원금 회수)\n"
    "  +200% → 25% 매도 (누적 75%)\n"
    "  +400% → 15% 매도 (누적 90%)\n"
    "  +600% → 재량 매도 (공짜 칩)\n\n"
    "<i>구버전 alias: /position, /exposure 도 동작합니다</i>"
)
_POSITION_HELP = _PORTFOLIO_HELP   # backcompat


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
    user_id = _resolve_user_id(ctx)
    db = SessionLocal()
    try:
        rows = crud.list_positions(db, user_id=user_id)
        if not rows:
            return "등록된 포지션 없음. <code>/portfolio add TICKER QTY AVG_COST</code> 로 추가."
        return _format_position_list(rows, db, ctx)
    finally:
        db.close()


def _format_position_list(rows, db, ctx: BotContext) -> str:
    from concurrent.futures import ThreadPoolExecutor

    lines = [
        "<b>보유 포지션</b>  <i>🟢이익 · 🔴손실</i>",
        "━━━━━━━━━━━━━━━━━",
    ]
    totals: dict[str, dict] = {}  # cur → {"value": float, "pnl": float}

    # 회사명 lookup — 한 번 모음 (사용자 무관 메타)
    name_map: dict[str, str] = {}
    for p in rows:
        wl = crud.get_any_watchlist_item(db, p.ticker)
        if wl and wl.name:
            name_map[p.ticker] = wl.name

    # 현재가 병렬 fetch (provider 60s 캐시 + thread pool)
    def _fetch_price(p):
        try:
            df = ctx.provider.get_ohlcv(p.ticker, interval="1d", period="5d")
            return (p, float(df["close"].iloc[-1]), None)
        except Exception as e:
            return (p, None, e)

    with ThreadPoolExecutor(max_workers=8, thread_name_prefix="port") as ex:
        results = list(ex.map(_fetch_price, list(rows)))

    for p, cur, err in results:
        if err is not None or cur is None:
            lines.append(
                f"⚠️ <b>{_esc(p.ticker)}</b>: "
                f"{_esc(type(err).__name__) if err else 'fetch 실패'} (현재가 미확보)"
            )
            continue

        ret = (cur / p.avg_cost) - 1.0 if p.avg_cost > 0 else 0.0
        pnl = (cur - p.avg_cost) * p.qty
        value = cur * p.qty

        cur_code = currency_for(p.ticker)
        slot = totals.setdefault(cur_code, {"value": 0.0, "pnl": 0.0})
        slot["value"] += value
        slot["pnl"] += pnl

        short_name = _short_company_name(name_map.get(p.ticker))
        name_str = f"  <i>{_esc(short_name)}</i>" if short_name else ""

        emoji = "🟢" if ret >= 0 else "🔴"
        ms_str = _next_milestone_label(p.highest_milestone, ret)
        qty_str = f"{p.qty:.4f}".rstrip("0").rstrip(".")

        price_str = format_money(cur, p.ticker)
        avg_str = format_money(p.avg_cost, p.ticker)
        value_str = format_money(value, p.ticker)
        pnl_str = format_money_signed(pnl, p.ticker)

        lines.append(f"{emoji} <b>{_esc(p.ticker)}</b>{name_str}")
        lines.append(f"   <b>{ret*100:+.1f}%</b>  ·  {value_str} ({qty_str}주)  ·  손익 {pnl_str}")
        lines.append(f"   평단 {avg_str}  ·  현재 {price_str}  ·  {_esc(ms_str)}")
        if p.notes:
            lines.append(f"   📝 {_esc(p.notes)}")

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


def _cmd_position_add(
    args: list[str],
    ctx: BotContext,
    *,
    force_milestone_zero: bool = False,
    allow_overwrite: bool = False,
) -> str:
    """
    /position add TICKER QTY AVG_COST [메모...]

    중복 정책 (allow_overwrite=False, /position add 기본):
      - 동일 ticker + 동일 qty/avg_cost  → 스킵 (idempotent)
      - 동일 ticker + 다른 qty/avg_cost → 거부 (기존값 보존, /position update 안내)
      - 신규 ticker                       → 추가

    /position update 는 allow_overwrite=True 로 호출 → 항상 덮어씀.
    """
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
    user_id = _resolve_user_id(ctx)

    # 중복 검사
    db = SessionLocal()
    try:
        existing = crud.get_position(db, ticker, user_id=user_id)
    finally:
        db.close()

    if existing is not None and not allow_overwrite:
        same_qty = abs(existing.qty - qty) < 1e-9
        same_avg = abs(existing.avg_cost - avg_cost) < 1e-9
        same_notes = (existing.notes or "") == (notes or "")
        if same_qty and same_avg and same_notes:
            return f"⚪ 이미 등록됨 (동일): <b>{_esc(ticker)}</b>"
        # 다른 값 — 거부, 기존값 보존
        return (
            f"⚠️ 이미 있음: <b>{_esc(ticker)}</b> "
            f"기존 {existing.qty:g}주 @ ${existing.avg_cost:.2f}\n"
            f"   갱신하려면 <code>/position update {ticker} {qty:g} {avg_cost}</code>"
        )

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
        crud.upsert_position(
            db, ticker=ticker, qty=qty, avg_cost=avg_cost,
            highest_milestone=skip_to, notes=notes, user_id=user_id,
        )
    finally:
        db.close()

    qty_str = f"{qty:.4f}".rstrip("0").rstrip(".")
    skip_str = f" (이미 +{skip_to*100:.0f}% 지나감 — 다음 마일스톤만 감시)" if skip_to > 0 else ""
    action = "갱신" if existing is not None else "등록"
    return f"✅ 포지션 {action}: <b>{_esc(ticker)}</b> {qty_str}주 @ ${avg_cost:.2f}{skip_str}"


def _cmd_position_update(args: list[str], ctx: BotContext) -> str:
    """명시적 갱신 — 기존 값을 항상 덮어쓰고 마일스톤도 재계산."""
    return _cmd_position_add(args, ctx, allow_overwrite=True)


def _cmd_position_remove(args: list[str], ctx: BotContext) -> str:
    if not args:
        return "사용법: <code>/portfolio remove TICKER</code>"
    ticker = args[0].upper()
    user_id = _resolve_user_id(ctx)
    db = SessionLocal()
    try:
        ok = crud.delete_position(db, ticker, user_id=user_id)
    finally:
        db.close()
    return f"🗑️ 포지션 삭제: <b>{_esc(ticker)}</b>" if ok else f"❌ 없음: <b>{_esc(ticker)}</b>"


# ──────────────────────────────────────────────
# Wizard / Callback 처리
# ──────────────────────────────────────────────

def _ack_callback(ctx: BotContext, callback_id: str, text: str = "") -> None:
    """Telegram 인라인 키보드 클릭 응답 (로딩 스피너 제거)."""
    payload = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
    _api(ctx, "answerCallbackQuery", **payload)


def _process_callback(callback: dict, ctx: BotContext) -> None:
    cb_id = callback.get("id", "")
    data = callback.get("data", "")
    msg = callback.get("message") or {}
    chat_id = str((msg.get("chat") or {}).get("id", ""))
    if not chat_id or not data:
        _ack_callback(ctx, cb_id)
        return

    # 인증
    db = SessionLocal()
    try:
        user = crud.get_user_by_chat_id(db, chat_id)
    finally:
        db.close()
    if user is None or not user.active:
        _ack_callback(ctx, cb_id, text="등록되지 않은 사용자")
        return

    ctx.current_user = user
    ctx.current_chat_id = chat_id
    _ack_callback(ctx, cb_id)

    # 라우팅 — "namespace:action[:arg]"
    parts = data.split(":", 2)
    ns = parts[0]
    action = parts[1] if len(parts) > 1 else ""
    arg = parts[2] if len(parts) > 2 else ""

    if ns == "port":
        _handle_portfolio_callback(action, arg, ctx, user)
    else:
        log.warning(f"unknown callback ns={ns} data={data}")


def _handle_portfolio_callback(action: str, arg: str, ctx: BotContext, user: User) -> None:
    chat_id = ctx.current_chat_id

    if action == "list":
        send(ctx, _cmd_position_list(ctx))
        return
    if action == "exposure":
        send(ctx, _cmd_exposure_inner(ctx))
        return
    if action == "import":
        send(ctx, _cmd_portfolio_import_help())
        return
    if action == "add":
        # tier 권한 체크 (TRUSTED 이상)
        if not _can_use(user.tier, "portfolio"):
            send(ctx, "⚠️ 권한 없음")
            return
        wizard.start(chat_id, "portfolio_add", "ticker", user_id=user.id)
        send(ctx, (
            "<b>➕ 종목 추가</b> — 1/3\n"
            "종목 코드는?\n"
            "<i>예: SOXL, NVDA, 005930.KS, ETH/USDT</i>"
        ))
        return
    if action == "update":
        if not _can_use(user.tier, "portfolio"):
            send(ctx, "⚠️ 권한 없음")
            return
        wizard.start(chat_id, "portfolio_update", "ticker", user_id=user.id)
        send(ctx, (
            "<b>✏️ 평단/수량 수정</b> — 1/3\n"
            "수정할 종목 코드는?"
        ))
        return
    if action == "delete":
        if not _can_use(user.tier, "portfolio"):
            send(ctx, "⚠️ 권한 없음")
            return
        _send_delete_menu(ctx, user)
        return
    if action == "del":
        # arg = ticker
        _confirm_delete(ctx, user, arg)
        return
    if action == "delconfirm":
        ticker = arg
        db = SessionLocal()
        try:
            ok = crud.delete_position(db, ticker, user_id=user.id)
        finally:
            db.close()
        send(ctx, f"🗑 {ticker} 삭제 완료" if ok else f"❌ {ticker} 없음")
        return
    if action == "delcancel":
        send(ctx, "❌ 취소됐습니다.")
        return

    send(ctx, f"⚠️ 알 수 없는 액션: {action}")


def _send_delete_menu(ctx: BotContext, user: User) -> None:
    db = SessionLocal()
    try:
        rows = crud.list_positions(db, user_id=user.id)
    finally:
        db.close()
    if not rows:
        send(ctx, "삭제할 포지션 없음")
        return
    buttons: list[list[dict]] = []
    line: list[dict] = []
    for p in rows:
        line.append(_kb_button(f"{p.ticker} ({p.qty:g})", f"port:del:{p.ticker}"))
        if len(line) == 2:
            buttons.append(line)
            line = []
    if line:
        buttons.append(line)
    send(ctx, "<b>🗑 삭제할 종목 선택</b>", reply_markup=_inline_kb(buttons))


def _confirm_delete(ctx: BotContext, user: User, ticker: str) -> None:
    db = SessionLocal()
    try:
        p = crud.get_position(db, ticker, user_id=user.id)
    finally:
        db.close()
    if not p:
        send(ctx, f"❌ {ticker} 없음")
        return
    kb = _inline_kb([[
        _kb_button("✅ 삭제", f"port:delconfirm:{ticker}"),
        _kb_button("❌ 취소", "port:delcancel"),
    ]])
    send(ctx, (
        f"<b>{_esc(ticker)}</b> 정말 삭제할까요?\n"
        f"보유 {p.qty:g}주 @ ${p.avg_cost:.2f}"
    ), reply_markup=kb)


# ──────────────────────────────────────────────
# Wizard 단계별 핸들러
# ──────────────────────────────────────────────

def _wizard_handle_text(text: str, ctx: BotContext, state) -> None:
    """진행 중인 wizard 의 다음 입력 처리."""
    flow = state.flow
    chat_id = ctx.current_chat_id

    if flow == "portfolio_add":
        _handle_portfolio_add_step(text, ctx, state)
        return
    if flow == "portfolio_update":
        _handle_portfolio_update_step(text, ctx, state)
        return

    # 알 수 없는 흐름 — 안전하게 종료
    wizard.cancel(chat_id)
    send(ctx, "⚠️ 알 수 없는 wizard. 취소됐습니다.")


def _handle_portfolio_add_step(text: str, ctx: BotContext, state) -> None:
    chat_id = ctx.current_chat_id
    user = ctx.current_user

    if state.step == "ticker":
        ticker = text.strip().upper()
        if not ticker or len(ticker) > 32:
            send(ctx, "⚠️ 잘못된 종목 코드. 다시 입력해주세요.")
            return
        wizard.advance(chat_id, "qty", ticker=ticker)
        send(ctx, f"<b>➕ 종목 추가 — 2/3</b>\n<b>{_esc(ticker)}</b> 보유 수량은?")
        return

    if state.step == "qty":
        try:
            qty = float(text.replace(",", "").strip())
        except ValueError:
            send(ctx, "⚠️ 숫자만 입력. 예: 23 또는 0.5")
            return
        if qty <= 0:
            send(ctx, "⚠️ 양수 수량만 가능")
            return
        wizard.advance(chat_id, "avg_cost", qty=qty)
        ticker = state.data.get("ticker", "")
        send(ctx, f"<b>➕ 종목 추가 — 3/3</b>\n<b>{_esc(ticker)}</b> 평단가는?\n<i>예: 21.47 또는 70000 (한국 종목)</i>")
        return

    if state.step == "avg_cost":
        cleaned = text.replace(",", "").replace("$", "").replace("₩", "").strip()
        try:
            avg_cost = float(cleaned)
        except ValueError:
            send(ctx, "⚠️ 숫자만 입력. 예: 21.47")
            return
        if avg_cost <= 0:
            send(ctx, "⚠️ 양수 평단만 가능")
            return

        ticker = state.data["ticker"]
        qty = state.data["qty"]
        result = _cmd_position_add([ticker, str(qty), str(avg_cost)], ctx)
        wizard.cancel(chat_id)
        send(ctx, result)
        return


def _handle_portfolio_update_step(text: str, ctx: BotContext, state) -> None:
    """add 와 거의 동일하나 마지막에 update (allow_overwrite=True) 사용."""
    chat_id = ctx.current_chat_id
    user = ctx.current_user

    if state.step == "ticker":
        ticker = text.strip().upper()
        # 기존 포지션 있는지 확인
        db = SessionLocal()
        try:
            existing = crud.get_position(db, ticker, user_id=user.id) if user else None
        finally:
            db.close()
        if not existing:
            wizard.cancel(chat_id)
            send(ctx, f"⚠️ <b>{_esc(ticker)}</b> 보유 안 함. 새로 추가하려면 메뉴의 ➕ 추가 사용.")
            return
        wizard.advance(chat_id, "qty", ticker=ticker, prev_qty=existing.qty, prev_avg=existing.avg_cost)
        send(ctx, (
            f"<b>✏️ 수정 — 2/3</b>\n"
            f"<b>{_esc(ticker)}</b> 새 수량은?\n"
            f"<i>현재: {existing.qty:g}주 @ ${existing.avg_cost:.2f}</i>"
        ))
        return

    if state.step == "qty":
        try:
            qty = float(text.replace(",", "").strip())
        except ValueError:
            send(ctx, "⚠️ 숫자만 입력")
            return
        wizard.advance(chat_id, "avg_cost", qty=qty)
        send(ctx, "<b>✏️ 수정 — 3/3</b>\n새 평단가는?")
        return

    if state.step == "avg_cost":
        cleaned = text.replace(",", "").replace("$", "").replace("₩", "").strip()
        try:
            avg_cost = float(cleaned)
        except ValueError:
            send(ctx, "⚠️ 숫자만 입력")
            return
        ticker = state.data["ticker"]
        qty = state.data["qty"]
        result = _cmd_position_update([ticker, str(qty), str(avg_cost)], ctx)
        wizard.cancel(chat_id)
        send(ctx, result)
        return


def _send_portfolio_menu(ctx: BotContext) -> None:
    """/portfolio (인자 없음) — 인라인 키보드 메뉴 발송."""
    kb = _inline_kb([
        [_kb_button("➕ 추가", "port:add"),       _kb_button("✏️ 수정", "port:update")],
        [_kb_button("📋 목록", "port:list"),       _kb_button("📊 노출도", "port:exposure")],
        [_kb_button("🗑 삭제", "port:delete"),     _kb_button("📦 일괄 입력", "port:import")],
    ])
    text = (
        "<b>📊 포트폴리오 관리</b>\n"
        "━━━━━━━━━━━━━━━━━\n"
        "버튼을 눌러 작업을 선택하세요.\n"
        "<i>대화는 5분 무응답 시 자동 종료. /cancel 로 중단 가능.</i>"
    )
    send(ctx, text, reply_markup=kb)


def cmd_portfolio(args: list[str], ctx: BotContext) -> str:
    """
    통합 포트폴리오 명령. /position 과 /exposure 의 슈퍼셋.
    인자 없으면 인라인 메뉴 (Wizard), 인자 있으면 텍스트 모드 (기존).
    """
    if not args:
        _send_portfolio_menu(ctx)
        return ""    # 빈 문자열 반환 → 추가 send 안 함
    sub = args[0].lower()
    rest = args[1:]
    if sub in ("list", "ls"):
        return _cmd_position_list(ctx)
    if sub in ("exposure", "expo"):
        return _cmd_exposure_inner(ctx)
    if sub == "add":
        return _cmd_position_add(rest, ctx)
    if sub == "update":
        return _cmd_position_update(rest, ctx)
    if sub in ("remove", "rm", "delete"):
        return _cmd_position_remove(rest, ctx)
    if sub == "import":
        return _cmd_portfolio_import_help()
    if sub in ("help", "?"):
        return _PORTFOLIO_HELP
    return f"알 수 없는 하위 명령: <code>{_esc(sub)}</code>\n\n" + _PORTFOLIO_HELP


def _cmd_portfolio_import_help() -> str:
    return (
        "<b>📦 일괄 입력 — 자유 형식</b>\n"
        "━━━━━━━━━━━━━━━━━\n"
        "<code>/portfolio import</code> 다음 줄에 보유 종목을 자유롭게 적으면 LLM이 파싱.\n\n"
        "예시:\n"
        "<code>/portfolio import\n"
        "삼성전자 10주 평단 70000\n"
        "SOXL 23주 21.47\n"
        "TQQQ 155 19.87\n"
        "NVDA 50주 평균 $10.38</code>\n\n"
        "<i>한국어/영어/숫자 형식 자유. 비용 ~$0.01 / 회</i>"
    )


# Backcompat — 기존 /position muscle memory 보존
def cmd_position(args: list[str], ctx: BotContext) -> str:
    return cmd_portfolio(args, ctx)


# ──────────────────────────────────────────────
# /exposure — 포트폴리오 베타 + R² (M2-B)
# ──────────────────────────────────────────────

def _fmt_metric(beta: Optional[float], r2: Optional[float]) -> str:
    if beta is None or r2 is None:
        return "데이터 부족"
    return f"β {beta:+.2f}  ·  R² {r2:.2f}"


def _r2_indicator(r2: Optional[float]) -> str:
    """R² 강도 시각화."""
    if r2 is None:
        return ""
    if r2 >= 0.90:
        return "🔴"   # 강한 동조 (분산 효과 거의 없음)
    if r2 >= 0.70:
        return "🟡"   # 중간 동조
    return "🟢"       # 약한 동조 (분산 효과 있음)


def _format_exposure(expo: PortfolioExposure) -> str:
    bench_syms = [s for s, _ in expo.benchmarks]
    lines = [
        "<b>📊 포트폴리오 노출도</b>  <i>🟢약 · 🟡중 · 🔴강 동조</i>",
        "━━━━━━━━━━━━━━━━━",
        f"벤치마크: {' · '.join(s for s, _ in expo.benchmarks)} (1년 일봉, β = 민감도 / R² = 동조도)",
        "",
    ]

    # 종목별 (가중치 큰 순)
    sorted_tx = sorted(expo.tickers, key=lambda t: expo.weights.get(t.ticker, 0), reverse=True)
    for tx in sorted_tx:
        weight_pct = expo.weights.get(tx.ticker, 0) * 100
        ret_str = f"{tx.return_pct*100:+.1f}%"
        cur_tag = "" if tx.currency == "USD" else f" <i>({tx.currency})</i>"
        lines.append(
            f"<b>{_esc(tx.ticker)}</b>{cur_tag}  "
            f"비중 <b>{weight_pct:.1f}%</b>  ·  {ret_str}"
        )
        for sym in bench_syms:
            beta, r2 = tx.metrics.get(sym, (None, None))
            indicator = _r2_indicator(r2)
            lines.append(f"   {sym:5s} {_fmt_metric(beta, r2)}  {indicator}")

    # 포트폴리오 합계
    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━")
    lines.append("<b>포트폴리오 종합</b>")
    for sym, _ in expo.benchmarks:
        beta, r2 = expo.portfolio_metrics.get(sym, (None, None))
        indicator = _r2_indicator(r2)
        lines.append(f"  {sym:5s} {_fmt_metric(beta, r2)}  {indicator}")

    if expo.warnings:
        lines.append("")
        for w in expo.warnings:
            lines.append(f"⚠️ {_esc(w)}")

    return "\n".join(lines)


def _cmd_exposure_inner(ctx: BotContext) -> str:
    """exposure 본문 — /portfolio exposure 와 /exposure (backcompat) 둘 다에서 호출."""
    user_id = _resolve_user_id(ctx)
    db = SessionLocal()
    try:
        positions = crud.list_positions(db, user_id=user_id)
    finally:
        db.close()
    if not positions:
        return ("등록된 포지션 없음.\n"
                "<code>/portfolio add SOXL 23 21.47</code> 로 추가 후 재호출.")

    try:
        expo = compute_exposure(ctx.provider, positions)
    except Exception as e:
        log.error(f"[/exposure] 계산 실패: {type(e).__name__}: {e}")
        return f"⚠️ 노출도 계산 실패: <code>{_esc(type(e).__name__)}</code>"

    if expo is None:
        return "⚠️ 노출도 계산 실패 — 데이터 부족 또는 벤치마크 fetch 실패"

    return _format_exposure(expo)


# Backcompat — 기존 /exposure muscle memory 보존
def cmd_exposure(args: list[str], ctx: BotContext) -> str:
    return _cmd_exposure_inner(ctx)


# ──────────────────────────────────────────────
# /why — 매크로/티커 해설 (LLM + web_search, on-demand)
# ──────────────────────────────────────────────

def _resolve_user_id(ctx: BotContext) -> Optional[int]:
    """현재 요청자(ctx.current_user) → User.id. _process_update가 채워두지 못했으면 OWNER fallback."""
    if ctx.current_user is not None:
        return ctx.current_user.id
    # 스케줄러나 fallback 경로 — OWNER
    db = SessionLocal()
    try:
        u = crud.get_user_by_chat_id(db, ctx.allowed_chat_id)
        return u.id if u else None
    finally:
        db.close()


def cmd_why(args: list[str], ctx: BotContext) -> str:
    """
    /why            → 현재 시장 매크로 해설
    /why TICKER     → 특정 종목 최근 동향 해설
    """
    if ctx.llm is None:
        return ("⚠️ LLM 비활성 (ANTHROPIC_API_KEY 없음).\n"
                "<code>backend/.env</code> 에 키 추가 후 봇 재시작.")

    user_id = _resolve_user_id(ctx)

    # 인자 없음 → 매크로 해설
    if not args:
        snap = ctx.market.fetch()
        result = research_macro_now(ctx.llm, snap, user_id=user_id)
        if not result:
            return "⚠️ 해설 생성 실패 (한도 초과 또는 API 과부하). <code>/cost</code> 확인"
        return format_research(result)

    # /why TICKER
    ticker_raw = args[0].strip()
    ticker = ticker_raw.upper() if ("/" not in ticker_raw and "-" not in ticker_raw) else ticker_raw

    df = None
    try:
        df = ctx.provider.get_ohlcv(ticker, interval="1d", period="1y")
    except Exception as e:
        log.warning(f"/why {ticker} OHLCV fetch 실패: {type(e).__name__}: {e}")

    snap = None
    try:
        snap = ctx.market.fetch()
    except Exception as e:
        log.warning(f"/why {ticker} market snap 실패: {type(e).__name__}: {e}")

    result = research_ticker(ctx.llm, ticker, df, snap, user_id=user_id)
    if not result:
        return f"⚠️ {_esc(ticker)} 해설 생성 실패 (한도 초과 또는 API 과부하)"
    return format_research(result)


# ──────────────────────────────────────────────
# /reminder — 매도 캘린더 (양도세 분할 매도 등)
# ──────────────────────────────────────────────

_REMINDER_HELP = (
    "<b>📅 매도 캘린더 리마인더</b>\n"
    "━━━━━━━━━━━━━━━━━\n"
    "<b>/reminder list</b>  활성 리마인더 목록\n"
    "<b>/reminder list all</b>  완료/만료 포함\n\n"
    "<b>/reminder add YYYY-MM-DD 제목 [메모...]</b>\n"
    "  예: <code>/reminder add 2026-06-15 1차 매도 (TQQQ 15+SOXL 5)</code>\n"
    "  D-7 부터 매일 06:00 KST 브리핑에 첨부\n\n"
    "<b>/reminder done ID</b>  완료 처리 (알림 종료)\n"
    "<b>/reminder remove ID</b>  영구 삭제\n\n"
    "<i>매매전략 §1.2 양도세 250만원 공제 분할 매도 일정 관리용</i>"
)


def _parse_date(s: str):
    from datetime import datetime as _dt
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return _dt.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _cmd_reminder_list(args: list[str], user_id: Optional[int] = None) -> str:
    show_all = bool(args and args[0].lower() == "all")
    db = SessionLocal()
    try:
        rows = crud.list_reminders(db, active_only=not show_all, user_id=user_id)
    finally:
        db.close()
    if not rows:
        return "등록된 리마인더 없음. <code>/reminder</code> 로 사용법 확인."
    from datetime import datetime as _dt
    today = _dt.utcnow().date()
    header = "<b>매도 캘린더</b>" + (" (전체)" if show_all else " (활성)")
    lines = [header, "━━━━━━━━━━━━━━━━━"]
    for r in rows:
        target = r.target_date.date() if hasattr(r.target_date, "date") else r.target_date
        delta = (target - today).days
        if not r.active:
            status = "✅ 완료" if r.done_at else "⚪ 만료"
            tag = f"{status} ({target})"
        elif delta < 0:
            tag = f"⚠️ 지남 D+{-delta} ({target})"
        elif delta == 0:
            tag = f"🔴 오늘 ({target})"
        elif delta <= r.days_before:
            tag = f"🟡 D-{delta} ({target})"
        else:
            tag = f"D-{delta} ({target})"
        notes_str = f"\n   📝 {_esc(r.notes)}" if r.notes else ""
        lines.append(f"<b>#{r.id}</b> {_esc(r.title)} — {tag}{notes_str}")
    return "\n".join(lines)


def _cmd_reminder_add(args: list[str], user_id: Optional[int] = None) -> str:
    if len(args) < 2:
        return "사용법: <code>/reminder add 2026-06-15 1차 매도 (TQQQ 15+SOXL 5)</code>"
    target = _parse_date(args[0])
    if target is None:
        return f"날짜 형식 인식 실패: <code>{_esc(args[0])}</code> (YYYY-MM-DD 권장)"
    title_parts = args[1:]
    title = " ".join(title_parts).strip()
    if not title:
        return "제목 필수"
    # title 너무 길면 일부를 notes 로 분리
    if len(title) > 80 and "—" in title:
        parts = title.split("—", 1)
        title = parts[0].strip()[:80]
        notes = parts[1].strip()
    else:
        notes = None
    db = SessionLocal()
    try:
        row = crud.insert_reminder(
            db, title=title, target_date=target, notes=notes, user_id=user_id,
        )
    finally:
        db.close()
    return f"✅ 리마인더 #{row.id} 등록: <b>{_esc(title)}</b> ({target.strftime('%Y-%m-%d')})"


def _cmd_reminder_done(args: list[str]) -> str:
    if not args:
        return "사용법: <code>/reminder done 42</code>"
    try:
        rid = int(args[0])
    except ValueError:
        return f"ID 파싱 실패: <code>{_esc(args[0])}</code>"
    db = SessionLocal()
    try:
        ok = crud.mark_reminder_done(db, rid)
    finally:
        db.close()
    return f"✅ 리마인더 #{rid} 완료 처리" if ok else f"❌ 없음: #{rid}"


def _cmd_reminder_remove(args: list[str]) -> str:
    if not args:
        return "사용법: <code>/reminder remove 42</code>"
    try:
        rid = int(args[0])
    except ValueError:
        return f"ID 파싱 실패: <code>{_esc(args[0])}</code>"
    db = SessionLocal()
    try:
        ok = crud.delete_reminder(db, rid)
    finally:
        db.close()
    return f"🗑️ 리마인더 #{rid} 삭제됨" if ok else f"❌ 없음: #{rid}"


def cmd_reminder(args: list[str], ctx: BotContext) -> str:
    if not args:
        return _REMINDER_HELP
    user_id = _resolve_user_id(ctx)
    sub = args[0].lower()
    rest = args[1:]
    if sub == "list":
        return _cmd_reminder_list(rest, user_id=user_id)
    if sub == "add":
        return _cmd_reminder_add(rest, user_id=user_id)
    if sub == "done":
        return _cmd_reminder_done(rest)
    if sub in ("remove", "rm", "delete"):
        return _cmd_reminder_remove(rest)
    if sub in ("help", "?"):
        return _REMINDER_HELP
    return f"알 수 없는 하위 명령: <code>{_esc(sub)}</code>\n\n" + _REMINDER_HELP


# ──────────────────────────────────────────────
# Tier 권한 매핑
# ──────────────────────────────────────────────

# OWNER 만 허용되는 명령 — 시스템/관리/시드 변경
_OWNER_ONLY = {"admin", "test"}

# OWNER + TRUSTED 허용 — 데이터 변경 (자기 데이터)
_OWNER_TRUSTED = {
    "add", "remove", "rm",
    "portfolio", "port", "position", "pos",
    "alert", "reminder", "remind",
    "feedback",
}

# 모든 tier 허용 (LIMITED 포함) — 정보 조회 only
# (그 외 명령은 모두 LIMITED 도 가능)


def _can_use(user_tier: str, cmd: str) -> bool:
    if cmd in _OWNER_ONLY:
        return user_tier == "OWNER"
    if cmd in _OWNER_TRUSTED:
        return user_tier in ("OWNER", "TRUSTED")
    # 정보 조회 명령 — 모두 OK
    return True


# ──────────────────────────────────────────────
# /admin — OWNER 전용 사용자/피드백 관리
# ──────────────────────────────────────────────

_ADMIN_HELP = (
    "<b>🛠 관리자 명령</b>\n"
    "━━━━━━━━━━━━━━━━━\n"
    "<b>/admin user list</b>\n"
    "<b>/admin user add CHAT_ID TIER [이름]</b>\n"
    "  TIER: OWNER | TRUSTED | LIMITED\n"
    "<b>/admin user tier ID NEW_TIER</b>\n"
    "<b>/admin user remove ID</b>\n\n"
    "<b>/admin feedback list</b>  (대기만)\n"
    "<b>/admin feedback all</b>  (전체)\n"
    "<b>/admin feedback done ID</b>"
)


def _send_welcome(ctx: BotContext, user: User) -> None:
    """신규 사용자 등록 시 환영 + 시작 가이드 발송. tier별 명령 차등."""
    name = user.name or "님"
    tier_desc = {
        "OWNER":   "전체 권한",
        "TRUSTED": "데이터 추가/수정 가능",
        "LIMITED": "정보 조회만 가능",
    }.get(user.tier, user.tier)

    if user.tier == "LIMITED":
        cmds = (
            "  /brief — 지금 모닝 브리핑 받기\n"
            "  /market — 시장 스냅샷 (지수/VIX/F&amp;G)\n"
            "  /list — 워치리스트 + RSI\n"
            "  /why TICKER — 종목 해설 (예: /why SOXL)\n"
            "  /portfolio list — 포지션 조회"
        )
    else:  # OWNER / TRUSTED
        cmds = (
            "  /brief — 지금 모닝 브리핑 받기\n"
            "  /portfolio — 보유 종목 관리 (메뉴/버튼)\n"
            "  /add TICKER — 워치리스트 추가 (예: /add NVDA)\n"
            "  /alert — 가격/VIX/F&amp;G 임계치 알림\n"
            "  /why TICKER — 종목 해설\n"
            "  /help — 전체 명령 안내"
        )

    msg = (
        f"🎉 <b>등록 완료!</b>\n"
        f"환영합니다, <b>{_esc(name)}</b>.\n"
        f"권한: <b>{user.tier}</b> ({tier_desc})\n\n"
        f"<b>추천 시작 흐름:</b>\n"
        f"{cmds}\n\n"
        f"📅 매일 <b>06:00 KST</b> 모닝 브리핑이 자동 발송됩니다.\n"
        f"의견·제안은 <code>/feedback 내용</code> 으로 보내주세요."
    )
    send(ctx, msg, chat_id=user.telegram_chat_id)


def _cmd_admin_user(args: list[str], ctx: BotContext) -> str:
    if not args:
        return _ADMIN_HELP
    sub = args[0].lower()
    rest = args[1:]
    db = SessionLocal()
    try:
        if sub == "list":
            users = crud.list_users(db, active_only=False)
            if not users:
                return "사용자 없음"
            lines = ["<b>사용자 목록</b>", "━━━━━━━━━━━━━━━━━"]
            for u in users:
                status = "" if u.active else " <i>(비활성)</i>"
                cap_override = f" cap=${u.llm_daily_cap_usd:.2f}" if u.llm_daily_cap_usd else ""
                lines.append(
                    f"#{u.id} <b>{_esc(u.name or '?')}</b> "
                    f"[{u.tier}] chat={u.telegram_chat_id}{cap_override}{status}"
                )
            return "\n".join(lines)
        if sub == "add":
            if len(rest) < 2:
                return "사용법: <code>/admin user add CHAT_ID TIER [이름]</code>"
            chat_id, tier = rest[0], rest[1].upper()
            if tier not in ("OWNER", "TRUSTED", "LIMITED"):
                return f"⚠️ TIER 오류: <code>{_esc(tier)}</code> (OWNER/TRUSTED/LIMITED)"
            name = " ".join(rest[2:]) if len(rest) > 2 else None

            # 신규 vs 갱신 구분 — 환영 메시지는 신규만 발송 (재등록 spam 방지)
            existing = crud.get_user_by_chat_id(db, chat_id)
            is_new = existing is None

            row = crud.upsert_user(db, telegram_chat_id=chat_id, tier=tier, name=name)

            welcome_status = ""
            if is_new:
                try:
                    _send_welcome(ctx, row)
                    welcome_status = "  · 환영 메시지 발송 ✓"
                except Exception as e:
                    log.warning(f"환영 메시지 발송 실패 user={row.id}: {type(e).__name__}: {e}")
                    welcome_status = "  · ⚠️ 환영 메시지 발송 실패"

            suffix = "" if is_new else "  (기존 사용자 갱신)"
            return (
                f"✅ 사용자 #{row.id} 등록: <b>{_esc(name or '?')}</b> "
                f"[{tier}] chat={chat_id}{suffix}{welcome_status}"
            )
        if sub == "tier":
            if len(rest) < 2:
                return "사용법: <code>/admin user tier ID NEW_TIER</code>"
            try:
                uid = int(rest[0])
            except ValueError:
                return f"ID 파싱 실패: {_esc(rest[0])}"
            new_tier = rest[1].upper()
            if new_tier not in ("OWNER", "TRUSTED", "LIMITED"):
                return f"⚠️ TIER 오류: <code>{_esc(new_tier)}</code>"
            user = db.get(User, uid)
            if not user:
                return f"❌ 없음: #{uid}"
            user.tier = new_tier
            db.commit()
            return f"✅ #{uid} → {new_tier}"
        if sub == "remove":
            if not rest:
                return "사용법: <code>/admin user remove ID</code>"
            try:
                uid = int(rest[0])
            except ValueError:
                return f"ID 파싱 실패: {_esc(rest[0])}"
            user = db.get(User, uid)
            if not user:
                return f"❌ 없음: #{uid}"
            if user.tier == "OWNER":
                return "⚠️ OWNER는 삭제 불가 (안전장치)"
            user.active = False
            db.commit()
            return f"🗑️ #{uid} 비활성"
        return _ADMIN_HELP
    finally:
        db.close()


def _cmd_admin_feedback(args: list[str], ctx: BotContext) -> str:
    if not args:
        args = ["list"]
    sub = args[0].lower()
    rest = args[1:]
    db = SessionLocal()
    try:
        if sub in ("list", "all"):
            pending_only = (sub == "list")
            rows = crud.list_feedback(db, pending_only=pending_only)
            if not rows:
                return "피드백 없음" if pending_only else "전체 피드백 없음"
            lines = [f"<b>피드백</b> {'(대기)' if pending_only else '(전체)'}",
                     "━━━━━━━━━━━━━━━━━"]
            for f in rows[:20]:
                user = db.get(User, f.user_id) if f.user_id else None
                who = (user.name or f"#{f.user_id}") if user else "anonymous"
                done = "✅ " if f.done_at else ""
                lines.append(f"{done}<b>#{f.id}</b> [{_esc(who)}] {f.created_at:%m-%d %H:%M}")
                lines.append(f"   {_esc(f.text)}")
            return "\n".join(lines)
        if sub == "done":
            if not rest:
                return "사용법: <code>/admin feedback done ID</code>"
            try:
                fid = int(rest[0])
            except ValueError:
                return f"ID 파싱 실패: {_esc(rest[0])}"
            ok = crud.mark_feedback_done(db, fid)
            return f"✅ 피드백 #{fid} 완료" if ok else f"❌ 없음: #{fid}"
        return _ADMIN_HELP
    finally:
        db.close()


def cmd_admin(args: list[str], ctx: BotContext) -> str:
    if not args:
        return _ADMIN_HELP
    sub = args[0].lower()
    rest = args[1:]
    if sub == "user":
        return _cmd_admin_user(rest, ctx)
    if sub == "feedback":
        return _cmd_admin_feedback(rest, ctx)
    return _ADMIN_HELP


# ──────────────────────────────────────────────
# /feedback — 가족 needs 발굴
# ──────────────────────────────────────────────

def cmd_feedback(args: list[str], ctx: BotContext) -> str:
    if not args:
        return ("사용법: <code>/feedback 의견 또는 제안</code>\n"
                "예: <code>/feedback /list 에 한국 시장 분리 표시 원함</code>")
    text = " ".join(args).strip()
    if not text:
        return "내용을 적어주세요"
    user_id = ctx.current_user.id if ctx.current_user else None
    db = SessionLocal()
    try:
        row = crud.insert_feedback(db, text=text, user_id=user_id)
    finally:
        db.close()
    return f"✅ 피드백 #{row.id} 접수. 검토 후 반영합니다 🙏"


def cmd_brief(args: list[str], ctx: BotContext) -> str:
    """
    /brief — 06:00 KST 모닝 브리핑을 지금 즉시 다시 받기.
    매크로 해설은 24h 캐시 hit이라 같은 날에는 비용 0.
    """
    user = ctx.current_user
    if user is None:
        return "⚠️ 사용자 인증 실패"
    return build_briefing(ctx, user, header="📨 <b>요청 브리핑</b>")


def cmd_test(args: list[str], ctx: BotContext) -> str:
    """가짜 STRONG 시그널을 raw 모드로 발사 (LLM 비용 0)."""
    ticker = args[0].upper() if args else "ETH/USDT"
    if "/" in ticker:
        source = "binance"
        price, bb_lower = 3200.0, 3250.0
    else:
        source = "yfinance"
        price, bb_lower = 18.42, 18.65

    reasons = ["RSI=31.2 < 35", f"가격 ${price:.2f} < BB하단 ${bb_lower:.2f}"]
    # M1: 실제 발동 경로와 동일하게 캘린더 컨텍스트 주입 (검증 목적)
    if ctx.calendar is not None:
        try:
            reasons.extend(ctx.calendar.get_context_strings(ticker))
        except Exception as e:
            log.debug(f"[/test] calendar 실패 (무시): {type(e).__name__}: {e}")

    sig = Signal(
        ticker=ticker, strength=SignalStrength.STRONG, price=price,
        reasons=reasons,
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
    # 시장 정보
    "market": cmd_market,
    "list":   cmd_list,
    "ls":     cmd_list,            # alias
    "why":    cmd_why,
    "brief":  cmd_brief,
    "digest": cmd_brief,           # alias
    "morning": cmd_brief,          # alias

    # 워치리스트
    "add":    cmd_add,
    "remove": cmd_remove,
    "rm":     cmd_remove,          # alias

    # 포트폴리오 (통합)
    "portfolio": cmd_portfolio,
    "port":      cmd_portfolio,    # alias
    "position":  cmd_position,     # backcompat alias
    "pos":       cmd_position,     # backcompat alias
    "exposure":  cmd_exposure,     # backcompat alias
    "expo":      cmd_exposure,     # backcompat alias

    # 알림
    "alert":  cmd_alert,

    # 매도 캘린더
    "reminder": cmd_reminder,
    "remind":   cmd_reminder,    # alias

    # 시스템 (자동완성 메뉴 숨김)
    "cost":   cmd_cost,
    "test":   cmd_test,
    "help":   cmd_help,
    "start":  cmd_help,            # alias

    # 멀티유저
    "feedback": cmd_feedback,
    "admin":  cmd_admin,           # OWNER 전용 (권한 체크는 _can_use)
}

# Telegram 자동완성 메뉴 — 핵심 8개만. 시스템성/alias 는 제외.
COMMAND_DESCRIPTIONS: list[tuple[str, str]] = [
    ("brief",     "📨 지금 브리핑 받기 (모닝 브리핑 즉시 재발송)"),
    ("market",    "🌐 시장 스냅샷 (지수/VIX/F&G/상관)"),
    ("list",      "📋 워치리스트 + RSI + 임박 이벤트"),
    ("why",       "🔍 왜 움직이는가 — /why TICKER 또는 /why"),
    ("portfolio", "💼 보유 + 익절 룰 + 노출도"),
    ("alert",     "🔔 가격/VIX/F&G 임계치 (서브명령)"),
    ("reminder",  "📅 매도 캘린더 (양도세 분할 매도)"),
    ("add",       "➕ 워치리스트 추가"),
    ("remove",    "➖ 워치리스트 제거"),
    ("help",      "❓ 명령어 안내"),
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

def _try_multiline_bulk(text: str, ctx: BotContext) -> Optional[str]:
    """
    멀티라인 메시지 일괄 처리.

    지원 패턴 (첫 줄에 명령, 이후 줄마다 인자):
      /position add [TICKER QTY AVG_COST [메모]]
      LINE2 ...
      LINE3 ...

      /alert add [...]
      LINE2 ...

    매칭 안 되면 None 반환 → 일반 처리 흐름.
    """
    if "\n" not in text:
        return None
    lines = [l for l in (line.strip() for line in text.split("\n")) if l]
    if len(lines) < 2:
        return None

    first = lines[0]
    if not first.startswith("/"):
        return None
    first_parts = first[1:].split()
    if len(first_parts) < 2:
        return None
    cmd = first_parts[0].split("@")[0].lower()
    sub = first_parts[1].lower()

    # 모든 멀티라인 일괄 처리는 user-scoped
    user_id = _resolve_user_id(ctx)

    # /portfolio import — 자유 형식 LLM 파싱
    if cmd in ("portfolio", "port") and sub == "import":
        if ctx.llm is None:
            return "⚠️ LLM 비활성 — /portfolio import 사용 불가"
        # 첫 줄 외 나머지가 본문
        body = "\n".join(lines[1:]).strip()
        if not body:
            return _cmd_portfolio_import_help()
        result = parse_free_form(ctx.llm, body, user_id=user_id)
        if result is None:
            return "⚠️ 파싱 실패 — 형식 다시 확인하거나 /portfolio add 명령 사용"
        if not result.positions:
            return ("⚠️ 인식된 종목 없음. 예시:\n"
                    "<code>삼성전자 10주 70000\nSOXL 23 21.47</code>")
        # 파싱 결과 → 기존 add 흐름 (중복 정책 자동 적용)
        results: list[str] = []
        for p in result.positions:
            args2 = [p.ticker, str(p.qty), str(p.avg_cost)]
            if p.note:
                args2.extend(p.note.split())
            results.append(_cmd_position_add(args2, ctx))
        summary = _summarize_bulk(results, label="포지션 (LLM 파싱)")
        if result.warnings:
            summary += "\n\n⚠️ <b>경고</b>:\n" + "\n".join(f"  • {_esc(w)}" for w in result.warnings)
        summary += f"\n\n<i>LLM 비용 ${result.cost_usd:.4f}</i>"
        return summary

    # /portfolio add (또는 /position add, /pos add)
    if cmd in ("portfolio", "port", "position", "pos") and sub == "add":
        results: list[str] = []
        first_extra = first_parts[2:]
        if first_extra:
            results.append(_cmd_position_add(first_extra, ctx))
        for line in lines[1:]:
            tokens = line.split()
            if not tokens:
                continue
            results.append(_cmd_position_add(tokens, ctx))
        return _summarize_bulk(results, label="포지션")

    # /alert add
    if cmd == "alert" and sub == "add":
        results = []
        first_extra = first_parts[2:]
        if first_extra:
            results.append(_cmd_alert_add(first_extra, user_id=user_id))
        for line in lines[1:]:
            tokens = line.split()
            if not tokens:
                continue
            results.append(_cmd_alert_add(tokens, user_id=user_id))
        return _summarize_bulk(results, label="알림")

    return None


def _summarize_bulk(results: list[str], label: str) -> str:
    """일괄 처리 결과 — 성공/스킵/실패 카운트 + 각 라인 응답."""
    if not results:
        return f"⚠️ {label} 입력 라인 0건"
    ok = sum(1 for r in results if r.startswith("✅"))
    skipped = sum(1 for r in results if r.startswith("⚪"))
    fail = len(results) - ok - skipped
    parts = [f"✅ {ok}건"]
    if skipped:
        parts.append(f"⚪ {skipped}건")
    if fail:
        parts.append(f"⚠️ {fail}건")
    head = f"<b>{label} 일괄 처리</b>: " + " / ".join(parts)
    return head + "\n━━━━━━━━━━━━━━━━━\n" + "\n".join(results)


def _process_update(update: dict, ctx: BotContext) -> None:
    # 1. callback_query (인라인 키보드 클릭)
    callback = update.get("callback_query")
    if callback:
        _process_callback(callback, ctx)
        return

    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return
    text = (msg.get("text") or "").strip()
    chat_id = str((msg.get("chat") or {}).get("id", ""))
    if not text:
        return

    # 사용자 인증 — DB User 기반 (멀티유저)
    db = SessionLocal()
    try:
        user = crud.get_user_by_chat_id(db, chat_id)
    finally:
        db.close()

    if user is None or not user.active:
        # 미등록 사용자 — onboarding 안내 (공격자 spam 방지 위해 짧게)
        if text.startswith("/start") or text.startswith("/help"):
            try:
                _api(ctx, "sendMessage", chat_id=chat_id, parse_mode="HTML",
                     text=(
                         "<b>👋 bullsfact</b>\n"
                         f"이 채팅 ID: <code>{chat_id}</code>\n"
                         "관리자에게 위 ID를 전달하시면 등록해드립니다."
                     ))
            except Exception as e:
                log.warning(f"onboarding 응답 실패: {e}")
        else:
            log.info(f"unauthorized chat_id={chat_id} text={text[:30]!r}")
        return

    ctx.current_user = user
    ctx.current_chat_id = chat_id

    # 진행 중인 Wizard 가 있으면 단계 진행 (명령어 아니어도 OK)
    state = wizard.get(chat_id)
    if state is not None:
        # /cancel 로 중단
        if text.startswith("/cancel"):
            wizard.cancel(chat_id)
            send(ctx, "❌ 취소됐습니다.")
            return
        # 다른 명령으로 새 흐름 시작 시 wizard 끝내고 명령 처리로 이동
        if not text.startswith("/"):
            _wizard_handle_text(text, ctx, state)
            return
        wizard.cancel(chat_id)  # 새 명령 들어오면 wizard 종료

    if not text.startswith("/"):
        return  # 명령어 아니면 무시

    # 멀티라인 일괄 처리 (/position add, /alert add) 시도
    bulk_reply = _try_multiline_bulk(text, ctx)
    if bulk_reply is not None:
        send(ctx, bulk_reply)
        return

    parts = text[1:].split()
    cmd = parts[0].split("@")[0].lower()  # /add@bot_name 같은 형태 정리
    args = parts[1:]

    # tier 권한 체크
    if not _can_use(user.tier, cmd):
        send(ctx, f"⚠️ 권한 없음 — <code>/{_esc(cmd)}</code> 는 {user.tier} 등급에서 사용 불가")
        return

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

    # M1: 이벤트 캘린더 (FINNHUB/FRED/DART 키 없으면 자동 silent skip)
    calendar = CalendarFetcher()

    # OWNER 사용자 자동 보장 (멀티유저 진입 골격)
    try:
        db = SessionLocal()
        try:
            owner = crud.get_or_create_owner(db, chat_id=chat_id, name="Owner")
            log.info(f"User OWNER 보장: #{owner.id} (chat_id={chat_id})")
        finally:
            db.close()
        # OWNER 생성 후 init_db 재호출 → orphan(user_id NULL) 행을 OWNER 로 매핑
        init_db()
    except Exception as e:
        log.error(f"User OWNER 생성 실패 (무시): {type(e).__name__}: {e}")

    # 사용자별 캡 resolver — chat_id 기반으로 user 조회
    env_cap = float(os.getenv("MAX_DAILY_LLM_USD", "2.0"))

    def _user_cap(user_id: int) -> float:
        db = SessionLocal()
        try:
            user = db.get(User, user_id)
            if user is None:
                return env_cap
            return crud.effective_daily_cap(user, env_cap)
        finally:
            db.close()

    def _user_spent(user_id: int) -> float:
        db = SessionLocal()
        try:
            return crud.user_llm_spent_today(db, user_id)
        finally:
            db.close()

    # LLMClient — 매크로 해설 / on-demand 분석용. ANTHROPIC_API_KEY 없으면 None.
    llm: Optional[LLMClient] = None
    if os.getenv("ANTHROPIC_API_KEY"):
        llm = LLMClient(
            max_daily_usd=env_cap,
            on_call=_persist_llm_call,
            user_cap_resolver=_user_cap,
            user_spent_resolver=_user_spent,
        )
        log.info("LLMClient 활성 — 매크로 해설/on-demand 사용 가능 (per-user cap 활성)")
    else:
        log.info("ANTHROPIC_API_KEY 없음 — 매크로 해설 비활성")

    ctx = BotContext(
        token=token, allowed_chat_id=chat_id,
        provider=provider, strategy=strategy, alerter=alerter, market=market,
        llm=llm, calendar=calendar,
    )

    register_commands(ctx)
    _start_daily_scheduler(ctx)
    log.info("🤖 봇 시작 — 폴링 모드")
    offset: int | None = None
    backoff = 1
    while True:
        try:
            params = {"timeout": 30, "allowed_updates": ["message", "callback_query"]}
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
