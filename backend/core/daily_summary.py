"""
일일 개인 요약 — 매일 06:00 KST 브리핑에 사용자별로 첨부.

목적: 알람 안 뜨는 조용한 시장에도 매일 가치 있는 메시지.
- 포트폴리오: 어제 종가 기준 변동률 + 일일 P&L + 합계
- 워치리스트: 보유 외 관심 종목 한 줄 요약

LLM 호출 없음. provider 가격만 활용 (60s 캐시 hit 가능).
"""
from __future__ import annotations

import html
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional

from .datasource.provider import DataProvider
from .money import format_money, format_money_signed, currency_for
from backend.db import SessionLocal, crud

log = logging.getLogger(__name__)


@dataclass
class TickerDaily:
    ticker: str
    yesterday_close: Optional[float]
    day_before_close: Optional[float]

    @property
    def change_pct(self) -> Optional[float]:
        if (self.yesterday_close is None or self.day_before_close is None
                or self.day_before_close == 0):
            return None
        return (self.yesterday_close - self.day_before_close) / self.day_before_close * 100


def _fetch_recent_closes(
    provider: DataProvider, tickers: list[str],
) -> dict[str, TickerDaily]:
    """병렬로 마지막 2일 종가 fetch (provider 캐시 활용)."""
    def _one(ticker: str) -> TickerDaily:
        try:
            df = provider.get_ohlcv(ticker, interval="1d", period="5d")
            if df is None or df.empty or "close" not in df.columns or len(df) < 2:
                return TickerDaily(ticker, None, None)
            return TickerDaily(
                ticker,
                yesterday_close=float(df["close"].iloc[-1]),
                day_before_close=float(df["close"].iloc[-2]),
            )
        except Exception as e:
            log.debug(f"[daily_summary] {ticker} fetch 실패: {type(e).__name__}: {e}")
            return TickerDaily(ticker, None, None)

    out: dict[str, TickerDaily] = {}
    if not tickers:
        return out
    with ThreadPoolExecutor(max_workers=8, thread_name_prefix="daily") as ex:
        for d in ex.map(_one, tickers):
            out[d.ticker] = d
    return out


def _esc(s: str) -> str:
    return html.escape(s, quote=False)


def _change_emoji(pct: Optional[float]) -> str:
    if pct is None:
        return "⚪"
    if pct > 0.05:
        return "🟢"
    if pct < -0.05:
        return "🔴"
    return "⚪"


def _format_portfolio(positions, closes: dict[str, TickerDaily]) -> Optional[str]:
    """포트폴리오 일일 변동 + 합계 (통화별)."""
    if not positions:
        return None

    lines = ["💼 <b>내 포트폴리오 (어제 종가 기준)</b>"]
    totals: dict[str, dict] = {}   # cur → {"pnl_daily": float, "value": float}
    has_data = False

    for p in positions:
        d = closes.get(p.ticker)
        cur = currency_for(p.ticker)
        if d is None or d.yesterday_close is None:
            lines.append(f"  ⚪ <b>{_esc(p.ticker)}</b>  데이터 없음")
            continue
        change = d.change_pct
        daily_pnl = (
            (d.yesterday_close - d.day_before_close) * p.qty
            if d.day_before_close is not None else 0.0
        )
        value = d.yesterday_close * p.qty
        slot = totals.setdefault(cur, {"pnl_daily": 0.0, "value": 0.0})
        slot["pnl_daily"] += daily_pnl
        slot["value"] += value
        has_data = True

        emoji = _change_emoji(change)
        change_str = f"{change:+.2f}%" if change is not None else "?"
        pnl_str = format_money_signed(daily_pnl, p.ticker)
        qty_str = f"{p.qty:g}"
        lines.append(
            f"  {emoji} <b>{_esc(p.ticker)}</b>  "
            f"{qty_str}주  ·  {change_str}  ·  {pnl_str}"
        )

    if not has_data:
        return None

    # 합계 (통화별)
    if totals:
        lines.append("")
        for cur, slot in totals.items():
            sym = {"USD": "$", "KRW": "₩", "JPY": "¥", "HKD": "HK$"}.get(cur, cur + " ")
            pnl = slot["pnl_daily"]
            val = slot["value"]
            pct = pnl / (val - pnl) * 100 if (val - pnl) > 0 else 0.0
            p_sign = "+" if pnl >= 0 else "-"
            if cur in ("KRW", "JPY"):
                lines.append(
                    f"📈 어제 {p_sign}{sym}{abs(pnl):,.0f}  ({pct:+.2f}%)  ·  평가액 {sym}{val:,.0f}"
                )
            else:
                lines.append(
                    f"📈 어제 {p_sign}{sym}{abs(pnl):,.2f}  ({pct:+.2f}%)  ·  평가액 {sym}{val:,.2f}"
                )
    return "\n".join(lines)


def _format_watchlist_extra(
    watchlist, positions, closes: dict[str, TickerDaily],
) -> Optional[str]:
    """포트에 없는 워치리스트 종목 한 줄 요약."""
    held = {p.ticker for p in positions}
    extras = [w for w in watchlist if w.active and w.ticker not in held]
    if not extras:
        return None

    parts: list[str] = []
    for w in extras:
        d = closes.get(w.ticker)
        if d is None or d.change_pct is None:
            parts.append(f"{_esc(w.ticker)} ?")
            continue
        change = d.change_pct
        if abs(change) < 0.05:
            tag = "보합"
        else:
            tag = f"{change:+.2f}%"
        parts.append(f"{_esc(w.ticker)} {tag}")

    if not parts:
        return None
    return "📋 <b>워치리스트 (어제)</b>\n  " + "  ·  ".join(parts)


def build_personal_section(provider: DataProvider, user_id: int) -> str:
    """
    사용자별 일일 요약 HTML 섹션. 빈 결과면 ""반환 (브리핑에 안 붙음).
    """
    db = SessionLocal()
    try:
        positions = list(crud.list_positions(db, user_id=user_id))
        watchlist = list(crud.list_watchlist(db, active_only=True, user_id=user_id))
    finally:
        db.close()

    if not positions and not watchlist:
        return ""

    all_tickers = sorted({
        *(p.ticker for p in positions),
        *(w.ticker for w in watchlist),
    })
    closes = _fetch_recent_closes(provider, all_tickers)

    parts: list[str] = []

    port_section = _format_portfolio(positions, closes)
    if port_section:
        parts.append(port_section)

    wl_section = _format_watchlist_extra(watchlist, positions, closes)
    if wl_section:
        parts.append(wl_section)

    if not parts:
        return ""
    return "\n\n".join(parts)
