"""
백테스트 메트릭 계산.

trades: 진입/청산 가격 + 수익률 리스트
equity_curve: 시점별 누적 자산 (1.0 시작, 복리)
"""
from __future__ import annotations
import math
from typing import Optional


def compute_metrics(
    trades: list[dict],
    equity_curve: list[tuple[str, float]],
    bars_per_year: int = 24 * 365,  # 1h 봉 기준 기본값
) -> dict:
    """
    Returns:
        win_rate, total_return, mdd, sharpe, profit_factor,
        trade_count, avg_win, avg_loss, avg_holding_bars
    """
    n = len(trades)
    if n == 0 or not equity_curve:
        return {
            "win_rate": 0.0,
            "total_return": 0.0,
            "mdd": 0.0,
            "sharpe": None,
            "profit_factor": None,
            "trade_count": 0,
            "avg_win": None,
            "avg_loss": None,
            "avg_holding_bars": None,
        }

    wins = [t["return"] for t in trades if t["return"] > 0]
    losses = [t["return"] for t in trades if t["return"] <= 0]
    win_rate = len(wins) / n

    final_equity = equity_curve[-1][1]
    total_return = final_equity - 1.0

    # MDD
    peak = equity_curve[0][1]
    mdd = 0.0
    for _, eq in equity_curve:
        peak = max(peak, eq)
        dd = (eq - peak) / peak
        mdd = min(mdd, dd)

    # Sharpe — 거래 단위 기준 (단순 근사). 거래 빈도가 다양하므로 참고용.
    rets = [t["return"] for t in trades]
    if len(rets) >= 2:
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        std = math.sqrt(var)
        sharpe = (mean / std) * math.sqrt(len(rets)) if std > 0 else None
    else:
        sharpe = None

    # Profit factor = sum(gains) / |sum(losses)|
    sum_gain = sum(wins) if wins else 0.0
    sum_loss = abs(sum(losses)) if losses else 0.0
    profit_factor = (sum_gain / sum_loss) if sum_loss > 0 else (float("inf") if sum_gain > 0 else None)
    if profit_factor == float("inf"):
        profit_factor = None  # JSON에 inf 못 넣음

    avg_win: Optional[float] = (sum(wins) / len(wins)) if wins else None
    avg_loss: Optional[float] = (sum(losses) / len(losses)) if losses else None

    holding_bars = [t["holding_bars"] for t in trades if "holding_bars" in t]
    avg_holding_bars = (sum(holding_bars) / len(holding_bars)) if holding_bars else None

    return {
        "win_rate": win_rate,
        "total_return": total_return,
        "mdd": mdd,
        "sharpe": sharpe,
        "profit_factor": profit_factor,
        "trade_count": n,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "avg_holding_bars": avg_holding_bars,
    }
