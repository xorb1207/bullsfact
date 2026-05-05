"""
청산 룰 (Exit Rule) — 다형성으로 청산 시점 결정.

각 룰은 진입 시점 i_entry와 현재 검사 시점 j (>= i_entry)를 받아,
청산 여부(bool)와 청산 가격(float, 보통 close)을 반환.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

import pandas as pd


class ExitRule(ABC):
    """진입 후 매 봉마다 호출되어 청산 여부를 판단."""

    @abstractmethod
    def should_exit(
        self,
        df: pd.DataFrame,
        i_entry: int,
        entry_price: float,
        j: int,
    ) -> tuple[bool, Optional[float]]:
        """
        Returns:
            (청산할지, 청산가) — 청산 안 하면 (False, None).
            청산가는 보통 df.iloc[j]['close'].
        """
        ...

    @property
    @abstractmethod
    def name(self) -> str: ...


@dataclass
class HoldingBarsExit(ExitRule):
    """N 봉 보유 후 무조건 청산 (단순/벤치마크용)."""
    bars: int = 24

    def should_exit(self, df, i_entry, entry_price, j):
        if j - i_entry >= self.bars:
            return True, float(df.iloc[j]["close"])
        return False, None

    @property
    def name(self) -> str:
        return f"holding_bars({self.bars})"


@dataclass
class BBRevertExit(ExitRule):
    """가격이 BB 중앙선(이상)으로 복귀하면 청산. max_bars로 강제 청산 캡."""
    max_bars: int = 240

    def should_exit(self, df, i_entry, entry_price, j):
        row = df.iloc[j]
        bb_mid = row.get("bb_mid")
        close = float(row["close"])
        if bb_mid is not None and not pd.isna(bb_mid) and close >= float(bb_mid):
            return True, close
        if j - i_entry >= self.max_bars:
            return True, close
        return False, None

    @property
    def name(self) -> str:
        return f"bb_revert(max={self.max_bars})"


@dataclass
class RSIRevertExit(ExitRule):
    """RSI가 threshold 이상으로 복귀하면 청산."""
    rsi_exit: float = 55.0
    max_bars: int = 240

    def should_exit(self, df, i_entry, entry_price, j):
        row = df.iloc[j]
        rsi = row.get("rsi")
        close = float(row["close"])
        if rsi is not None and not pd.isna(rsi) and float(rsi) >= self.rsi_exit:
            return True, close
        if j - i_entry >= self.max_bars:
            return True, close
        return False, None

    @property
    def name(self) -> str:
        return f"rsi_revert(>={self.rsi_exit}, max={self.max_bars})"


@dataclass
class TPSLExit(ExitRule):
    """익절/손절 % 기반. take_profit/stop_loss 둘 다 양수 비율(0.05 = 5%)."""
    take_profit: float = 0.05
    stop_loss: float = 0.03
    max_bars: int = 240

    def should_exit(self, df, i_entry, entry_price, j):
        row = df.iloc[j]
        high = float(row["high"])
        low = float(row["low"])
        close = float(row["close"])

        # 보수적으로 같은 봉에 둘 다 닿으면 손절 우선 적용 (현실 가정)
        if low <= entry_price * (1 - self.stop_loss):
            return True, entry_price * (1 - self.stop_loss)
        if high >= entry_price * (1 + self.take_profit):
            return True, entry_price * (1 + self.take_profit)
        if j - i_entry >= self.max_bars:
            return True, close
        return False, None

    @property
    def name(self) -> str:
        return f"tp_sl(tp={self.take_profit:.3f}, sl={self.stop_loss:.3f}, max={self.max_bars})"


def build_exit_rule(kind: str, params: Optional[dict] = None) -> ExitRule:
    """API 요청에서 받은 문자열 + 파라미터 → ExitRule 객체."""
    p = params or {}
    if kind == "holding_bars":
        return HoldingBarsExit(bars=int(p.get("bars", 24)))
    if kind == "bb_revert":
        return BBRevertExit(max_bars=int(p.get("max_bars", 240)))
    if kind == "rsi_revert":
        return RSIRevertExit(
            rsi_exit=float(p.get("rsi_exit", 55.0)),
            max_bars=int(p.get("max_bars", 240)),
        )
    if kind == "tp_sl":
        return TPSLExit(
            take_profit=float(p.get("take_profit", 0.05)),
            stop_loss=float(p.get("stop_loss", 0.03)),
            max_bars=int(p.get("max_bars", 240)),
        )
    raise ValueError(f"Unknown exit_rule kind: {kind}")
