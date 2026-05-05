"""
Binance 데이터 소스 — ccxt 라이브러리 사용 (Freqtrade와 동일한 접근)
크립토 24/7, 별도 API 키 없이 퍼블릭 OHLCV 조회 가능.
"""
import pandas as pd
import ccxt

from .base import DataSource

# ccxt interval 포맷
_INTERVAL_MAP = {
    "1m":  "1m",
    "5m":  "5m",
    "15m": "15m",
    "30m": "30m",
    "1h":  "1h",
    "4h":  "4h",
    "1d":  "1d",
}

# period → 가져올 캔들 개수 변환
_PERIOD_TO_LIMIT = {
    "7d":   7   * 24,
    "30d":  30  * 24,
    "60d":  60  * 24,
    "90d":  90  * 24,
    "180d": 180 * 24,
    "1y":   365 * 24,
}


class BinanceSource(DataSource):

    def __init__(self, api_key: str = "", api_secret: str = ""):
        """
        API 키 없이도 퍼블릭 데이터 조회 가능.
        자동 매수 연동 시에는 키 필요.
        """
        config = {"enableRateLimit": True}
        if api_key and api_secret:
            config["apiKey"] = api_key
            config["secret"] = api_secret
        self._exchange = ccxt.binance(config)

    @property
    def source_name(self) -> str:
        return "binance"

    def get_ohlcv(
        self,
        ticker: str,
        interval: str = "1h",
        period: str = "60d",
    ) -> pd.DataFrame:
        ccxt_interval = _INTERVAL_MAP.get(interval, "1h")

        # period → limit 계산 (interval이 1h 기준)
        limit = _PERIOD_TO_LIMIT.get(period, 60 * 24)
        if interval == "1d":
            limit = limit // 24
        elif interval in ("4h",):
            limit = limit // 4

        ohlcv = self._exchange.fetch_ohlcv(ticker, ccxt_interval, limit=limit)
        if not ohlcv:
            raise ValueError(f"[Binance] {ticker} 데이터 없음")

        df = pd.DataFrame(
            ohlcv,
            columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df.set_index("timestamp", inplace=True)
        return df[["open", "high", "low", "close", "volume"]]

    def get_price(self, ticker: str) -> float:
        ticker_data = self._exchange.fetch_ticker(ticker)
        return float(ticker_data["last"])

    def is_market_open(self, ticker: str) -> bool:
        # 크립토는 24/7
        return True
