"""
yfinance 데이터 소스 — 미국 주식 / ETF / 야후 크립토 (ETH-USD 등)
"""
import pandas as pd
import yfinance as yf
from datetime import datetime
from zoneinfo import ZoneInfo

from .base import DataSource

ET = ZoneInfo("America/New_York")

# yfinance interval → period 최소값 매핑
_MIN_PERIOD = {
    "1m":  "7d",
    "5m":  "60d",
    "15m": "60d",
    "30m": "60d",
    "1h":  "730d",
    "1d":  "5y",
}


class YFinanceSource(DataSource):

    @property
    def source_name(self) -> str:
        return "yfinance"

    def get_ohlcv(
        self,
        ticker: str,
        interval: str = "1h",
        period: str = "60d",
    ) -> pd.DataFrame:
        df = yf.download(
            ticker,
            period=period,
            interval=interval,
            progress=False,
            auto_adjust=True,
        )
        if df is None or df.empty:
            raise ValueError(f"[yfinance] {ticker} 데이터 없음")

        # 컬럼 정규화 (MultiIndex 대응)
        df.columns = [c[0].lower() if isinstance(c, tuple) else c.lower()
                      for c in df.columns]
        df.index = pd.to_datetime(df.index, utc=True)
        return df[["open", "high", "low", "close", "volume"]]

    def get_price(self, ticker: str) -> float:
        t = yf.Ticker(ticker)
        info = t.fast_info
        price = getattr(info, "last_price", None)
        if price is None:
            # fallback: 최근 1분봉 close
            hist = t.history(period="1d", interval="1m")
            if hist.empty:
                raise ValueError(f"[yfinance] {ticker} 현재가 조회 실패")
            price = float(hist["Close"].iloc[-1])
        return float(price)

    def is_market_open(self, ticker: str) -> bool:
        # 크립토 (야후 포맷: ETH-USD 등) → 24/7
        if "-" in ticker and ticker.split("-")[-1].upper() in ("USD", "USDT", "BTC", "ETH"):
            return True
        # 미국 주식 장 시간 체크 (ET 기준)
        now = datetime.now(ET)
        if now.weekday() >= 5:
            return False
        open_t  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
        close_t = now.replace(hour=16, minute=0,  second=0, microsecond=0)
        return open_t <= now <= close_t
