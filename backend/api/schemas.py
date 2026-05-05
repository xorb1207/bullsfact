"""
Pydantic 스키마 — 요청/응답 모델.
"""
from datetime import datetime
from typing import Any, Optional
from pydantic import BaseModel, Field, ConfigDict


# ──────────────────────────────────────────────
# Watchlist
# ──────────────────────────────────────────────

class WatchlistCreate(BaseModel):
    ticker: str = Field(..., min_length=1, max_length=32)


class WatchlistOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ticker: str
    source: str
    added_at: datetime
    active: bool


class WatchlistWithIndicators(WatchlistOut):
    price: Optional[float] = None
    rsi: Optional[float] = None
    bb_lower: Optional[float] = None
    bb_mid: Optional[float] = None
    bb_upper: Optional[float] = None
    signal: Optional[str] = None
    error: Optional[str] = None


# ──────────────────────────────────────────────
# Alerts
# ──────────────────────────────────────────────

class AlertOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ticker: str
    strength: str
    price: float
    rsi: Optional[float]
    bb_lower: Optional[float]
    source: str
    reasons: Optional[list[str]] = None
    sent_at: datetime


class AlertsPage(BaseModel):
    items: list[AlertOut]
    total: int
    limit: int
    offset: int


# ──────────────────────────────────────────────
# Backtest
# ──────────────────────────────────────────────

class BacktestRequest(BaseModel):
    ticker: str
    start_date: datetime
    end_date: datetime
    rsi_threshold: float = 35.0
    bb_std: float = 2.0
    rsi_period: int = 14
    bb_period: int = 20
    interval: str = "1h"

    # 청산 룰 — "holding_bars" | "bb_revert" | "rsi_revert" | "tp_sl"
    exit_rule: str = "holding_bars"
    exit_params: dict[str, Any] = Field(default_factory=dict)

    fee_bps: float = 5.0
    slippage_bps: float = 2.0
    allow_overlap: bool = False


class BacktestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ticker: str
    start_date: datetime
    end_date: datetime
    strategy_params: dict[str, Any]
    win_rate: Optional[float]
    mdd: Optional[float]
    total_return: Optional[float]
    trade_count: Optional[int]
    details: Optional[dict[str, Any]] = None
    created_at: datetime
