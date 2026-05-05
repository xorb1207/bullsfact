"""
백테스트 엔진 — Signal Replay.

흐름:
  1. strategy.populate_indicators(df)로 RSI/BB 추가
  2. 매 봉마다 신호 평가 (포지션 없을 때만 진입 후보)
  3. 신호 발생 시 next bar open에 진입 (수수료/슬리피지 반영)
  4. 진입 이후 매 봉마다 ExitRule 호출, True면 청산
  5. trades + equity_curve 산출 → metrics 계산
"""
from __future__ import annotations
import math
from dataclasses import dataclass, field, asdict
from typing import Optional

import pandas as pd

from backend.core.strategy import DipBuyStrategy, SignalStrength
from .rules import ExitRule, HoldingBarsExit
from .metrics import compute_metrics


@dataclass
class BacktestConfig:
    fee_bps: float = 5.0          # 5bp = 0.05% (편도)
    slippage_bps: float = 2.0     # 2bp = 0.02% (편도)
    allow_overlap: bool = False   # 동시 다중 포지션 허용 여부 (False = 단일 포지션)


@dataclass
class Trade:
    entry_time: str
    exit_time: str
    entry_price: float
    exit_price: float
    return_: float           # 수수료/슬리피지 반영 후 순수익률
    gross_return: float      # 비용 반영 전 (참고)
    strength: str
    holding_bars: int

    def to_dict(self) -> dict:
        d = asdict(self)
        d["return"] = d.pop("return_")
        return d


@dataclass
class BacktestReport:
    metrics: dict
    trades: list[dict]
    equity_curve: list[tuple[str, float]]  # (iso_timestamp, equity)
    config: dict
    exit_rule: str


class BacktestEngine:

    def __init__(
        self,
        strategy: DipBuyStrategy,
        exit_rule: Optional[ExitRule] = None,
        config: Optional[BacktestConfig] = None,
    ):
        self.strategy = strategy
        self.exit_rule = exit_rule or HoldingBarsExit(bars=24)
        self.config = config or BacktestConfig()

    def _apply_costs(self, entry_px: float, exit_px: float) -> tuple[float, float]:
        """수수료 + 슬리피지 반영. 진입가는 위로, 청산가는 아래로 밀어냄."""
        fee = self.config.fee_bps / 1e4
        slip = self.config.slippage_bps / 1e4
        eff_entry = entry_px * (1 + fee + slip)
        eff_exit = exit_px * (1 - fee - slip)
        return eff_entry, eff_exit

    def run(self, df: pd.DataFrame) -> BacktestReport:
        df = self.strategy.populate_indicators(df).copy()
        df = df.dropna(subset=["rsi", "bb_lower"])

        trades: list[Trade] = []
        equity = 1.0
        equity_curve: list[tuple[str, float]] = [(df.index[0].isoformat(), 1.0)]

        n = len(df)
        i = 0
        while i < n - 1:
            row = df.iloc[i]
            rsi = float(row["rsi"]) if not pd.isna(row["rsi"]) else math.nan
            bb_lower = float(row["bb_lower"]) if not pd.isna(row["bb_lower"]) else math.nan
            close = float(row["close"])

            cond_rsi = (not math.isnan(rsi)) and rsi < self.strategy.rsi_threshold
            cond_bb = (not math.isnan(bb_lower)) and close < bb_lower

            if not (cond_rsi or cond_bb):
                i += 1
                continue

            strength = "strong" if (cond_rsi and cond_bb) else "weak"

            # 진입: next bar open
            entry_idx = i + 1
            if entry_idx >= n:
                break
            entry_px_raw = float(df.iloc[entry_idx]["open"])
            eff_entry, _ = self._apply_costs(entry_px_raw, entry_px_raw)

            # 청산 시점 탐색
            exit_idx = None
            exit_px_raw = None
            for j in range(entry_idx, n):
                done, px = self.exit_rule.should_exit(df, entry_idx, eff_entry, j)
                if done:
                    exit_idx = j
                    exit_px_raw = px
                    break
            if exit_idx is None:
                # 끝까지 청산 신호 없으면 마지막 봉 close로 강제 청산
                exit_idx = n - 1
                exit_px_raw = float(df.iloc[exit_idx]["close"])

            _, eff_exit = self._apply_costs(eff_entry, exit_px_raw)
            gross = (exit_px_raw - entry_px_raw) / entry_px_raw
            net = (eff_exit - eff_entry) / eff_entry

            trade = Trade(
                entry_time=df.index[entry_idx].isoformat(),
                exit_time=df.index[exit_idx].isoformat(),
                entry_price=entry_px_raw,
                exit_price=exit_px_raw,
                return_=net,
                gross_return=gross,
                strength=strength,
                holding_bars=exit_idx - entry_idx,
            )
            trades.append(trade)

            # equity 업데이트 — 청산 시점에 한 번만 점프
            equity *= (1 + net)
            equity_curve.append((df.index[exit_idx].isoformat(), equity))

            # 다음 탐색 시작점
            i = exit_idx + 1 if not self.config.allow_overlap else i + 1

        # 마지막에 시계열 끝 시점까지 equity 평탄 연장 (차트 보기 좋게)
        if equity_curve and equity_curve[-1][0] != df.index[-1].isoformat():
            equity_curve.append((df.index[-1].isoformat(), equity))

        trade_dicts = [t.to_dict() for t in trades]
        metrics = compute_metrics(trade_dicts, equity_curve)

        return BacktestReport(
            metrics=metrics,
            trades=trade_dicts,
            equity_curve=equity_curve,
            config=asdict(self.config),
            exit_rule=self.exit_rule.name,
        )
