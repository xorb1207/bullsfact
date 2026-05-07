"""
전략 레이어 — Freqtrade의 Strategy 패턴 참고.

Strategy는 DataProvider에서 받은 OHLCV에
지표를 추가하고 신호(Signal)를 반환.
새 전략 추가 시 BaseStrategy만 상속하면 됨.
"""
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Optional, TYPE_CHECKING

import pandas as pd
import pandas_ta as ta

if TYPE_CHECKING:
    from ..datasource.calendar_fetcher import CalendarFetcher

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# 공통 타입
# ──────────────────────────────────────────────

class SignalStrength(Enum):
    NONE   = "none"
    WEAK   = "weak"    # 조건 1개 충족
    STRONG = "strong"  # 조건 2개 이상 동시 충족


@dataclass
class Signal:
    ticker:   str
    strength: SignalStrength
    price:    float
    reasons:  list[str]         # 충족된 조건 설명
    indicators: dict            # RSI, BB 값 등 (알람 메시지용)


# ──────────────────────────────────────────────
# 추상 기반
# ──────────────────────────────────────────────

class BaseStrategy(ABC):

    @abstractmethod
    def populate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """OHLCV df에 지표 컬럼 추가해서 반환."""
        ...

    @abstractmethod
    def generate_signal(self, df: pd.DataFrame, ticker: str) -> Signal:
        """지표가 추가된 df를 받아 Signal 반환."""
        ...


# ──────────────────────────────────────────────
# DipBuy 전략 — RSI + 볼린저 밴드
# ──────────────────────────────────────────────

class DipBuyStrategy(BaseStrategy):
    """
    매수 신호 조건:
      WEAK   — RSI < rsi_threshold  OR  price < bb_lower
      STRONG — RSI < rsi_threshold  AND price < bb_lower
    """

    def __init__(
        self,
        rsi_period:    int   = 14,
        rsi_threshold: float = 35.0,
        bb_period:     int   = 20,
        bb_std:        float = 2.0,
        calendar_fetcher: Optional["CalendarFetcher"] = None,
    ):
        self.rsi_period       = rsi_period
        self.rsi_threshold    = rsi_threshold
        self.bb_period        = bb_period
        self.bb_std           = bb_std
        self.calendar_fetcher = calendar_fetcher

    def populate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        close = df["close"]

        # RSI
        df["rsi"] = ta.rsi(close, length=self.rsi_period)

        # 볼린저 밴드
        bb = ta.bbands(close, length=self.bb_period, std=self.bb_std)
        if bb is not None and not bb.empty:
            lower_col = next((c for c in bb.columns if "BBL" in c), None)
            mid_col   = next((c for c in bb.columns if "BBM" in c), None)
            upper_col = next((c for c in bb.columns if "BBU" in c), None)
            if lower_col: df["bb_lower"] = bb[lower_col]
            if mid_col:   df["bb_mid"]   = bb[mid_col]
            if upper_col: df["bb_upper"] = bb[upper_col]

        return df

    def generate_signal(self, df: pd.DataFrame, ticker: str) -> Signal:
        df = self.populate_indicators(df)
        last = df.iloc[-1]

        price    = float(last["close"])
        rsi      = float(last.get("rsi",      float("nan")))
        bb_lower = float(last.get("bb_lower", float("nan")))
        bb_mid   = float(last.get("bb_mid",   float("nan")))
        bb_upper = float(last.get("bb_upper", float("nan")))

        reasons: list[str] = []

        if not pd.isna(rsi) and rsi < self.rsi_threshold:
            reasons.append(f"RSI={rsi:.1f} < {self.rsi_threshold}")

        if not pd.isna(bb_lower) and price < bb_lower:
            reasons.append(f"가격 ${price:.2f} < BB하단 ${bb_lower:.2f}")

        if len(reasons) >= 2:
            strength = SignalStrength.STRONG
        elif len(reasons) == 1:
            strength = SignalStrength.WEAK
        else:
            strength = SignalStrength.NONE

        # M1: 이벤트 캘린더 컨텍스트 주입 (graceful, 실패해도 시그널은 정상 발동)
        if self.calendar_fetcher is not None:
            try:
                reasons.extend(self.calendar_fetcher.get_context_strings(ticker))
            except Exception as e:
                log.debug(f"[DipBuy] calendar context 실패 ({ticker}): {type(e).__name__}: {e}")

        return Signal(
            ticker=ticker,
            strength=strength,
            price=price,
            reasons=reasons,
            indicators={
                "rsi":      rsi,
                "bb_lower": bb_lower,
                "bb_mid":   bb_mid,
                "bb_upper": bb_upper,
            },
        )
