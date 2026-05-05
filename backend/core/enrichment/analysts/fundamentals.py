"""
Fundamentals Analyst — yfinance.info 에서 종목 메타/실적 캘린더 fetch.

LLM 호출 없음. 사실만 모아서 Synthesizer에 넘김.
크립토(binance source)는 빈 결과 반환.
"""
import logging
from datetime import datetime, timezone

import yfinance as yf

from ..base import Analyst
from ..types import AnalystResult
from ...strategy.dip_buy import Signal

log = logging.getLogger(__name__)


def _earnings_in_days(ticker_obj: yf.Ticker) -> int | None:
    """다음 실적 발표까지 일수. 없거나 모르면 None."""
    try:
        cal = ticker_obj.calendar
        if cal is None:
            return None
        dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
        if not dates:
            return None
        next_date = dates[0] if isinstance(dates, list) else dates
        if hasattr(next_date, "to_pydatetime"):
            next_date = next_date.to_pydatetime()
        if isinstance(next_date, datetime):
            if next_date.tzinfo is None:
                next_date = next_date.replace(tzinfo=timezone.utc)
            delta = (next_date - datetime.now(timezone.utc)).days
            return delta if delta >= 0 else None
    except Exception:
        return None
    return None


class FundamentalsAnalyst(Analyst):
    name = "fundamentals"

    def analyze(self, signal: Signal, source: str) -> AnalystResult:
        if source == "binance":
            return AnalystResult(name=self.name, summary="", citations=[])

        try:
            t = yf.Ticker(signal.ticker)
            info = t.info or {}
        except Exception as e:
            return AnalystResult(name=self.name, summary="", error=f"fetch: {e}")

        bits: list[str] = []

        long_name = info.get("longName") or info.get("shortName")
        if long_name:
            bits.append(f"종목명: {long_name}")

        sector = info.get("sector")
        industry = info.get("industry")
        if sector or industry:
            bits.append(f"섹터/업종: {sector or '-'} / {industry or '-'}")

        quote_type = info.get("quoteType")
        if quote_type:
            bits.append(f"타입: {quote_type}")

        pe = info.get("trailingPE")
        if isinstance(pe, (int, float)):
            bits.append(f"P/E (TTM): {pe:.2f}")

        beta = info.get("beta")
        if isinstance(beta, (int, float)):
            bits.append(f"베타: {beta:.2f}")

        mcap = info.get("marketCap")
        if isinstance(mcap, (int, float)) and mcap > 0:
            bits.append(f"시총: ${mcap/1e9:.2f}B")

        days = _earnings_in_days(t)
        if days is not None:
            bits.append(f"다음 실적 발표까지: D-{days}")

        # 52주 위치
        hi, lo = info.get("fiftyTwoWeekHigh"), info.get("fiftyTwoWeekLow")
        cur = signal.price
        if isinstance(hi, (int, float)) and isinstance(lo, (int, float)) and hi > lo:
            pos = (cur - lo) / (hi - lo) * 100
            bits.append(f"52주 위치: {pos:.0f}% (저 ${lo:.2f} ~ 고 ${hi:.2f})")

        if not bits:
            return AnalystResult(name=self.name, summary=f"{signal.ticker} 펀더멘털 정보 없음.")

        log.info(f"[FundamentalsAnalyst] {signal.ticker} → {len(bits)}개 항목")
        return AnalystResult(
            name=self.name,
            summary=f"{signal.ticker} 펀더멘털:\n" + "\n".join(f"- {b}" for b in bits),
        )
