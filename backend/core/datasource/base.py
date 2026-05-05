"""
DataSource 추상 기반 클래스
Freqtrade의 Exchange 추상화 패턴을 참고하여 설계.
모든 데이터 소스는 이 인터페이스를 구현해야 함.
"""
from abc import ABC, abstractmethod
import pandas as pd


class DataSource(ABC):
    """
    모든 데이터 소스의 공통 인터페이스.
    전략/엔진은 이 클래스만 알면 됨 — yfinance인지 Binance인지 몰라도 됨.
    """

    @abstractmethod
    def get_ohlcv(
        self,
        ticker: str,
        interval: str = "1h",
        period: str = "60d",
    ) -> pd.DataFrame:
        """
        OHLCV 캔들 데이터 반환.

        Returns:
            columns: [open, high, low, close, volume]
            index: DatetimeIndex (UTC)
        """
        ...

    @abstractmethod
    def get_price(self, ticker: str) -> float:
        """현재가 반환."""
        ...

    @abstractmethod
    def is_market_open(self, ticker: str) -> bool:
        """장이 열려 있는지 여부."""
        ...

    @property
    @abstractmethod
    def source_name(self) -> str:
        """데이터 소스 이름 (로깅용)."""
        ...
