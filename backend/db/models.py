"""
SQLAlchemy ORM 모델.
"""
from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Float, Boolean, DateTime, JSON, Index
)
from .database import Base


class Watchlist(Base):
    __tablename__ = "watchlist"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(32), nullable=False, unique=True, index=True)
    source = Column(String(16), nullable=False)  # "yfinance" | "binance"
    added_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    active = Column(Boolean, nullable=False, default=True)


class AlertLog(Base):
    __tablename__ = "alert_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(32), nullable=False, index=True)
    strength = Column(String(16), nullable=False)  # "weak" | "strong"
    price = Column(Float, nullable=False)
    rsi = Column(Float, nullable=True)
    bb_lower = Column(Float, nullable=True)
    source = Column(String(16), nullable=False)
    reasons = Column(JSON, nullable=True)
    sent_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)


class BacktestResult(Base):
    __tablename__ = "backtest_result"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(32), nullable=False, index=True)
    start_date = Column(DateTime, nullable=False)
    end_date = Column(DateTime, nullable=False)
    strategy_params = Column(JSON, nullable=False)
    win_rate = Column(Float, nullable=True)
    mdd = Column(Float, nullable=True)
    total_return = Column(Float, nullable=True)
    trade_count = Column(Integer, nullable=True)
    details = Column(JSON, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


Index("ix_alert_log_ticker_sent", AlertLog.ticker, AlertLog.sent_at.desc())


class LLMCallLog(Base):
    """Synthesizer/Analyst의 LLM 호출 기록. 일일 비용 리포트에서 집계."""
    __tablename__ = "llm_call_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    model = Column(String(64), nullable=False)
    purpose = Column(String(32), nullable=False)             # "synthesizer" | "analyst:news" | ...
    ticker = Column(String(32), nullable=True, index=True)
    input_tokens = Column(Integer, nullable=False)
    output_tokens = Column(Integer, nullable=False)
    cache_read_tokens = Column(Integer, nullable=False, default=0)
    cache_creation_tokens = Column(Integer, nullable=False, default=0)
    cost_cents = Column(Float, nullable=False)
    latency_ms = Column(Integer, nullable=True)
    called_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
