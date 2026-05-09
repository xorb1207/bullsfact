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
from backend.core.datasource.calendar_fetcher import CalendarFetcher
from backend.core.exposure import compute_exposure, PortfolioExposure
from backend.core.reminders import get_due_reminders, format_section as format_reminders
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
    allowed_chat_id: str
    provider: DataProvider
    strategy: DipBuyStrategy
    alerter: AlertEngine
    market: MarketFetcher
    llm: Optional[LLMClient] = None         # 매크로 해설 / on-demand 분석에 사용
    calendar: Optional[CalendarFetcher] = None  # M1: 이벤트 캘린더 (어닝/매크로/공시)


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
        "<b>🤖 bullsfact 명령어</b>\n"
        "━━━━━━━━━━━━━━━━━\n"
        "\n"
        "<b>📊 시장 정보</b>\n"
        "  /market — 시장 스냅샷 (지수/VIX/F&amp;G/상관)\n"
        "    매일 06:00 KST 자동 발송\n"
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
    db = SessionLocal()
    try:
        items = crud.list_watchlist(db, active_only=False)
    finally:
        db.close()

    if not items:
        return "워치리스트 비어있음. <code>/add TICKER</code> 로 추가."

    # 스캐너와 동일 interval/period 사용 — RSI 일관성 확보
    interval = os.getenv("DATA_INTERVAL", "1d")
    period = os.getenv("DATA_PERIOD", "2y")

    rows = [
        "<b>워치리스트</b>  <i>⚪정상 · 🟡약신호 · 🔴강신호</i>",
        "━━━━━━━━━━━━━━━━━",
    ]
    for w in items:
        short_name = _short_company_name(w.name)
        name_str = f"  <i>{_esc(short_name)}</i>" if short_name else ""
        if not w.active:
            rows.append(f"⚪ {_esc(w.ticker)}{name_str} — 비활성")
            continue
        try:
            df = ctx.provider.get_ohlcv(w.ticker, interval=interval, period=period)
            sig = ctx.strategy.generate_signal(df, w.ticker)
            rsi = sig.indicators.get("rsi")
            rsi_str = f"{rsi:.1f}" if isinstance(rsi, float) and not math.isnan(rsi) else "N/A"
            emoji = {"strong": "🔴", "weak": "🟡", "none": "⚪"}.get(sig.strength.value, "⚪")
            price_str = format_money(sig.price, w.ticker)

            # 일일 변동률 (마지막 캔들 vs 그 직전)
            close = df["close"]
            if len(close) >= 2:
                prev = float(close.iloc[-2])
                cur = float(close.iloc[-1])
                change_pct = (cur - prev) / prev * 100 if prev else 0.0
                change_str = f"{change_pct:+.2f}%"
            else:
                change_str = ""

            # 2줄 구조 — 모바일 가독성
            rows.append(f"{emoji} <b>{_esc(w.ticker)}</b>{name_str}")
            metric_parts = [price_str]
            if change_str:
                metric_parts.append(change_str)
            metric_parts.append(f"RSI {rsi_str}")
            rows.append("   " + "  ·  ".join(metric_parts))

            # M1: 캘린더 suffix는 추가 라인
            cal_suffix = _ticker_calendar_suffix(ctx, w.ticker)
            if cal_suffix:
                rows.append(f"   {cal_suffix}")
        except Exception as e:
            rows.append(f"⚠️ <b>{_esc(w.ticker)}</b>{name_str}: {_esc(type(e).__name__)}")
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
        body = (
            "<b>💰 bullsfact 비용 리포트</b>\n"
            "━━━━━━━━━━━━━━━━━\n"
            f"<b>오늘</b>: ${today['cost_usd']:.4f} / 캡 ${cap:.2f} ({today_pct:.1f}%)\n"
            f"  LLM {today['calls']}회, "
            f"알람 STRONG {alerts['strong']} / WEAK {alerts['weak']}\n\n"
            f"<b>어제</b>: ${yest['cost_usd']:.4f} ({yest['calls']}회)\n"
            f"<b>최근 7일</b>: ${wk['cost_usd']:.4f} ({wk['calls']}회)"
        )
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


def send_market_report(ctx: BotContext) -> None:
    """매일 자동 발송 (스케줄러 호출). 시장 스냅샷 + LLM 매크로 해설."""
    try:
        log.info("[scheduler] 매일 시장 리포트 발송")
        # M3 부가: 알림 후속 추적 — 매일 1회 갱신 (브리핑 발송 전, 무관 실패 격리)
        try:
            n = update_returns()
            if n:
                log.info(f"[scheduler] 알림 후속 데이터 {n}건 갱신")
        except Exception as e:
            log.error(f"[scheduler] 후속 추적 실패 (무시): {type(e).__name__}: {e}")

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

        # 매도 캘린더 임박 항목 (양도세 분할 매도 등) — 실패해도 본 브리핑은 발송
        try:
            due = get_due_reminders()
            if due:
                text += "\n" + format_reminders(due)
                log.info(f"[scheduler] 매도 리마인더 {len(due)}건 첨부")
        except Exception as e:
            log.error(f"[scheduler] 리마인더 예외 (무시): {type(e).__name__}: {e}")

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
    db = SessionLocal()
    try:
        rows = crud.list_positions(db)
        if not rows:
            return "등록된 포지션 없음. <code>/position add TICKER QTY AVG_COST</code> 로 추가."
        return _format_position_list(rows, db, ctx)
    finally:
        db.close()


def _format_position_list(rows, db, ctx: BotContext) -> str:
    lines = [
        "<b>보유 포지션</b>  <i>🟢이익 · 🔴손실</i>",
        "━━━━━━━━━━━━━━━━━",
    ]
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

        # 회사명 (Watchlist 캐시 활용, short form)
        wl = crud.get_watchlist_item(db, p.ticker)
        short_name = _short_company_name(wl.name) if wl else ""
        name_str = f"  <i>{_esc(short_name)}</i>" if short_name else ""

        emoji = "🟢" if ret >= 0 else "🔴"
        ms_str = _next_milestone_label(p.highest_milestone, ret)
        qty_str = f"{p.qty:.4f}".rstrip("0").rstrip(".")

        price_str = format_money(cur, p.ticker)
        avg_str = format_money(p.avg_cost, p.ticker)
        value_str = format_money(value, p.ticker)
        pnl_str = format_money_signed(pnl, p.ticker)

        # 멀티라인 구조 — 모바일 가독성 통일
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

    # 중복 검사
    db = SessionLocal()
    try:
        existing = crud.get_position(db, ticker)
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
            highest_milestone=skip_to, notes=notes,
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


def cmd_portfolio(args: list[str], ctx: BotContext) -> str:
    """
    통합 포트폴리오 명령. /position 과 /exposure 의 슈퍼셋.
    하위:
      list     보유 + P&L + 다음 마일스톤
      exposure 베타 + R² (M2-B)
      add      포지션 추가
      update   강제 갱신
      remove   삭제
    """
    if not args:
        return _PORTFOLIO_HELP
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
        return _cmd_position_remove(rest)
    if sub in ("help", "?"):
        return _PORTFOLIO_HELP
    return f"알 수 없는 하위 명령: <code>{_esc(sub)}</code>\n\n" + _PORTFOLIO_HELP


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
    db = SessionLocal()
    try:
        positions = crud.list_positions(db)
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


def _cmd_reminder_list(args: list[str]) -> str:
    show_all = bool(args and args[0].lower() == "all")
    db = SessionLocal()
    try:
        rows = crud.list_reminders(db, active_only=not show_all)
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


def _cmd_reminder_add(args: list[str]) -> str:
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
        row = crud.insert_reminder(db, title=title, target_date=target, notes=notes)
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
    sub = args[0].lower()
    rest = args[1:]
    if sub == "list":
        return _cmd_reminder_list(rest)
    if sub == "add":
        return _cmd_reminder_add(rest)
    if sub == "done":
        return _cmd_reminder_done(rest)
    if sub in ("remove", "rm", "delete"):
        return _cmd_reminder_remove(rest)
    if sub in ("help", "?"):
        return _REMINDER_HELP
    return f"알 수 없는 하위 명령: <code>{_esc(sub)}</code>\n\n" + _REMINDER_HELP


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
}

# Telegram 자동완성 메뉴 — 핵심 8개만. 시스템성/alias 는 제외.
COMMAND_DESCRIPTIONS: list[tuple[str, str]] = [
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

    # /position add (또는 /pos add)
    if cmd in ("position", "pos") and sub == "add":
        results: list[str] = []
        # 첫 줄에 인자가 더 있으면 그것도 처리 (e.g. "/position add SOXL 23 21.47")
        first_extra = first_parts[2:]
        if first_extra:
            results.append(_cmd_position_add(first_extra, ctx))
        for line in lines[1:]:
            tokens = line.split()
            if not tokens:
                continue
            results.append(_cmd_position_add(tokens, ctx))
        return _summarize_bulk(results, label="포지션")

    # /alert add price ... (멀티라인은 'price' 모드에서만 의미. vix/fg는 단발이라 굳이)
    if cmd == "alert" and sub == "add":
        results = []
        first_extra = first_parts[2:]
        if first_extra:
            results.append(_cmd_alert_add(first_extra))
        for line in lines[1:]:
            tokens = line.split()
            if not tokens:
                continue
            results.append(_cmd_alert_add(tokens))
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

    # 멀티라인 일괄 처리 (/position add, /alert add) 시도
    bulk_reply = _try_multiline_bulk(text, ctx)
    if bulk_reply is not None:
        send(ctx, bulk_reply)
        return

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

    # M1: 이벤트 캘린더 (FINNHUB/FRED/DART 키 없으면 자동 silent skip)
    calendar = CalendarFetcher()

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
        llm=llm, calendar=calendar,
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
