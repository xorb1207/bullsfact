"""
News Analyst — yfinance.news로 종목 관련 최근 헤드라인 fetch.

LLM 호출 없음. 사실 그대로 모아서 Synthesizer에 넘김.

수집 전략:
  1) 종목 자체 뉴스 fetch
  2) 레버리지 ETF인데 결과가 빈약하면(< 3건) 모종목/모지수 뉴스 추가 fetch
  3) URL 기준 중복 제거 후 최신순 N건 반환
"""
import logging
import os
from datetime import datetime, timezone

import yfinance as yf

from ..base import Analyst
from ..types import AnalystResult
from ...strategy.dip_buy import Signal

log = logging.getLogger(__name__)

MAX_HEADLINES = int(os.getenv("NEWS_MAX_HEADLINES", "6"))
MAX_AGE_HOURS = int(os.getenv("NEWS_MAX_AGE_HOURS", "168"))   # 7일
MIN_DIRECT_BEFORE_FALLBACK = 3

# 레버리지/인버스 ETF → 모지수/모ETF (뉴스가 풍부한 쪽)
# 너 워치리스트(SOXL, TQQQ)에 흔히 쓰이는 것들 위주로만. 필요하면 늘림.
UNDERLYING_MAP: dict[str, str] = {
    # 반도체 3x
    "SOXL": "SMH",  "SOXS": "SMH",
    # 나스닥-100 3x
    "TQQQ": "QQQ",  "SQQQ": "QQQ",
    # FANG+ 3x
    "FNGU": "QQQ",  "FNGD": "QQQ",
    # 러셀 2000 3x
    "TNA":  "IWM",  "TZA":  "IWM",
    # S&P 500 3x
    "UPRO": "SPY",  "SPXU": "SPY",
    # 다우 3x
    "UDOW": "DIA",  "SDOW": "DIA",
    # 금광 3x
    "NUGT": "GDX",  "DUST": "GDX",
    # 바이오 3x
    "LABU": "XBI",  "LABD": "XBI",
}


def _normalize_for_news(ticker: str) -> str:
    """ETH/USDT, BTC/USDT → ETH-USD, BTC-USD."""
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


def _fetch_filtered(symbol: str, cutoff_ts: float, via: str | None = None) -> list[dict]:
    """
    yfinance에서 특정 심볼의 뉴스 fetch + 시간 필터.
    via가 있으면 헤드라인 앞에 [via XXX] 표시 (모종목 뉴스 구분용).
    각 항목: {ts, title, publisher, url, via}
    """
    try:
        raw = yf.Ticker(symbol).news or []
    except Exception as e:
        log.warning(f"[NewsAnalyst] {symbol} fetch 실패: {e}")
        return []

    out = []
    for entry in raw:
        content = entry.get("content") or entry
        title = (content.get("title") or "").strip()
        if not title:
            continue
        pub_date = _parse_pub_date(content.get("pubDate") or content.get("displayTime") or "")
        if not pub_date:
            continue
        ts = pub_date.timestamp()
        if ts < cutoff_ts:
            continue
        publisher = (content.get("provider") or {}).get("displayName", "")
        url = (content.get("clickThroughUrl") or content.get("canonicalUrl") or {}).get("url", "")
        out.append({
            "ts": ts,
            "title": title,
            "publisher": publisher,
            "url": url,
            "via": via,
        })
    return out


class NewsAnalyst(Analyst):
    name = "news"

    def analyze(self, signal: Signal, source: str) -> AnalystResult:
        symbol = _normalize_for_news(signal.ticker)
        cutoff = datetime.now(timezone.utc).timestamp() - MAX_AGE_HOURS * 3600

        # 1) 직접 뉴스
        direct = _fetch_filtered(symbol, cutoff)

        # 2) 모종목 폴백 (레버리지 ETF + 직접 뉴스 빈약할 때만)
        items = list(direct)
        underlying = UNDERLYING_MAP.get(symbol.upper())
        if underlying and len(direct) < MIN_DIRECT_BEFORE_FALLBACK:
            log.info(
                f"[NewsAnalyst] {symbol} 직접 뉴스 {len(direct)}건 (< {MIN_DIRECT_BEFORE_FALLBACK}) "
                f"→ 모종목 {underlying} 추가 fetch"
            )
            items.extend(_fetch_filtered(underlying, cutoff, via=underlying))

        # 3) URL 기준 dedup, 최신순, 상위 N건
        seen_urls = set()
        deduped = []
        for it in sorted(items, key=lambda x: x["ts"], reverse=True):
            key = it["url"] or it["title"]
            if key in seen_urls:
                continue
            seen_urls.add(key)
            deduped.append(it)
            if len(deduped) >= MAX_HEADLINES:
                break

        if not deduped:
            return AnalystResult(
                name=self.name,
                summary=f"{symbol}: 최근 {MAX_AGE_HOURS}h 내 뉴스 없음.",
                citations=[],
            )

        lines = []
        for it in deduped:
            tag = f"[via {it['via']}] " if it.get("via") else ""
            pub = f"[{it['publisher']}] " if it["publisher"] else ""
            lines.append(f"- {tag}{pub}{it['title']}")
        summary = (
            f"{symbol} 최근 {MAX_AGE_HOURS}h 헤드라인 ({len(deduped)}건"
            + (f", 모종목 {underlying} 포함" if underlying and any(d.get('via') for d in deduped) else "")
            + "):\n"
            + "\n".join(lines)
        )
        citations = [it["url"] for it in deduped if it["url"]]
        via_count = sum(1 for d in deduped if d.get("via"))
        log.info(
            f"[NewsAnalyst] {signal.ticker} → {len(deduped)}건 "
            f"(직접 {len(deduped)-via_count} + via {via_count})"
        )
        return AnalystResult(name=self.name, summary=summary, citations=citations)
