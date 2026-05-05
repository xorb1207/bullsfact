"""
DataProvider — 티커를 보고 적절한 DataSource로 자동 라우팅.
Freqtrade의 DataProvider 패턴 참고.

라우팅 규칙:
  ETH/USDT, BTC/USDT  →  Binance  (슬래시 포함 + 알려진 크립토 쌍)
  ETH-USD, BTC-USD    →  yfinance (야후 크립토 포맷)
  SOXL, TQQQ, NVDA   →  yfinance (미국 주식/ETF)
"""
import logging
import pandas as pd

from .base import DataSource
from .yfinance_source import YFinanceSource
from .binance_source import BinanceSource

log = logging.getLogger(__name__)

# 슬래시 포함 + 이 quote currency 목록이면 Binance로 라우팅
_BINANCE_QUOTES = {"USDT", "BUSD", "BTC", "ETH", "BNB", "USDC"}


def _is_binance_pair(ticker: str) -> bool:
    """ETH/USDT 형식이면 True"""
    if "/" not in ticker:
        return False
    _, quote = ticker.upper().split("/", 1)
    return quote in _BINANCE_QUOTES


class DataProvider:
    """
    전략/엔진이 직접 사용하는 단일 진입점.
    내부적으로 yfinance / Binance를 자동 선택.
    """

    def __init__(
        self,
        binance_api_key: str = "",
        binance_api_secret: str = "",
    ):
        self._yf = YFinanceSource()
        self._binance = BinanceSource(binance_api_key, binance_api_secret)

    def _route(self, ticker: str) -> DataSource:
        source = self._binance if _is_binance_pair(ticker) else self._yf
        log.debug(f"[DataProvider] {ticker} → {source.source_name}")
        return source

    def get_ohlcv(
        self,
        ticker: str,
        interval: str = "1h",
        period: str = "60d",
    ) -> pd.DataFrame:
        return self._route(ticker).get_ohlcv(ticker, interval, period)

    def get_price(self, ticker: str) -> float:
        return self._route(ticker).get_price(ticker)

    def is_market_open(self, ticker: str) -> bool:
        return self._route(ticker).is_market_open(ticker)

    def source_of(self, ticker: str) -> str:
        """디버깅/로깅용: 어느 소스인지 반환"""
        return self._route(ticker).source_name
