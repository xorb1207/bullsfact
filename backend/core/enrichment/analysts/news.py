"""
News Analyst — yfinance.news로 종목 관련 최근 헤드라인 fetch.

LLM 호출 없음. 사실 그대로 모아서 Synthesizer에 넘김.
크립토(예: ETH/USDT)는 ETH-USD로 변환해서 야후에서 검색.
"""
import logging
from datetime import datetime, timezone

import yfinance as yf

from ..base import Analyst
from ..types import AnalystResult
from ...strategy.dip_buy import Signal

log = logging.getLogger(__name__)

MAX_HEADLINES = 6
MAX_AGE_HOURS = 72


def _normalize_for_news(ticker: str) -> str:
    """
    ETH/USDT, BTC/USDT → ETH-USD, BTC-USD (야후에서 뉴스 검색 가능한 포맷).
    이미 -USD 형태면 그대로.
    """
    if "/" in ticker:
        base = ticker.split("/", 1)[0]
        return f"{base}-USD"
    return ticker


def _parse_pub_date(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


class NewsAnalyst(Analyst):
    name = "news"

    def analyze(self, signal: Signal, source: str) -> AnalystResult:
        symbol = _normalize_for_news(signal.ticker)
        try:
            raw = yf.Ticker(symbol).news or []
        except Exception as e:
            return AnalystResult(name=self.name, summary="", error=f"fetch: {e}")

        cutoff = datetime.now(timezone.utc).timestamp() - MAX_AGE_HOURS * 3600
        items: list[tuple[str, str, str]] = []  # (title, publisher, url)
        for entry in raw:
            content = entry.get("content") or entry
            title = (content.get("title") or "").strip()
            if not title:
                continue
            pub_date = _parse_pub_date(content.get("pubDate") or content.get("displayTime") or "")
            if pub_date and pub_date.timestamp() < cutoff:
                continue
            publisher = (content.get("provider") or {}).get("displayName", "")
            url = (content.get("clickThroughUrl") or content.get("canonicalUrl") or {}).get("url", "")
            items.append((title, publisher, url))
            if len(items) >= MAX_HEADLINES:
                break

        if not items:
            return AnalystResult(
                name=self.name,
                summary=f"{symbol}: 최근 {MAX_AGE_HOURS}h 내 뉴스 없음.",
                citations=[],
            )

        lines = [
            f"- [{pub}] {title}" if pub else f"- {title}"
            for title, pub, _ in items
        ]
        summary = f"{symbol} 최근 {MAX_AGE_HOURS}h 헤드라인 ({len(items)}건):\n" + "\n".join(lines)
        citations = [url for _, _, url in items if url]
        log.info(f"[NewsAnalyst] {signal.ticker} → {len(items)}건 수집")
        return AnalystResult(name=self.name, summary=summary, citations=citations)
