"""
FastAPI 의존성 — DataProvider/Strategy 싱글턴.
"""
import os
from functools import lru_cache

from backend.core.datasource import DataProvider
from backend.core.strategy import DipBuyStrategy


@lru_cache(maxsize=1)
def get_provider() -> DataProvider:
    return DataProvider(
        binance_api_key=os.getenv("BINANCE_API_KEY", ""),
        binance_api_secret=os.getenv("BINANCE_API_SECRET", ""),
    )


@lru_cache(maxsize=1)
def get_strategy() -> DipBuyStrategy:
    return DipBuyStrategy(
        rsi_threshold=float(os.getenv("RSI_THRESHOLD", "35")),
        bb_std=float(os.getenv("BB_STD", "2.0")),
    )
