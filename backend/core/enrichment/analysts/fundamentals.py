"""
Fundamentals Analyst — 종목별 펀더멘털 (실적 캘린더, P/E, 섹터 등) 요약.

주식만 의미 있음. 크립토 시그널엔 빈 결과 반환.
Day 1: stub. Day 5에서 실제 yfinance.info / earnings_dates 연동.
"""
import logging
from ..base import Analyst
from ..types import AnalystResult
from ...strategy.dip_buy import Signal

log = logging.getLogger(__name__)


class FundamentalsAnalyst(Analyst):
    name = "fundamentals"

    def analyze(self, signal: Signal, source: str) -> AnalystResult:
        if source == "binance":
            return AnalystResult(name=self.name, summary="", citations=[])
        log.debug(f"[FundamentalsAnalyst stub] {signal.ticker}")
        return AnalystResult(
            name=self.name,
            summary=f"(stub) {signal.ticker} 실적/지표 요약 자리.",
            citations=[],
        )
