"""
News Analyst — 종목 관련 최근 뉴스 헤드라인 수집 + LLM 요약.

Day 1: stub. 실제 yfinance.news / CryptoPanic 연동은 Day 2.
"""
import logging
from ..base import Analyst
from ..types import AnalystResult
from ...strategy.dip_buy import Signal

log = logging.getLogger(__name__)


class NewsAnalyst(Analyst):
    name = "news"

    def analyze(self, signal: Signal, source: str) -> AnalystResult:
        # Day 1 stub — Day 2에서 실제 뉴스 fetch + LLM 요약으로 교체
        log.debug(f"[NewsAnalyst stub] {signal.ticker}")
        return AnalystResult(
            name=self.name,
            summary=f"(stub) {signal.ticker} 관련 최근 24h 헤드라인 요약 자리.",
            citations=[],
        )
