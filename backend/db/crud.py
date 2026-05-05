"""
DB CRUD 헬퍼 — 라우트와 Scanner가 공통으로 쓰는 얇은 레이어.
"""
from datetime import datetime
from typing import Optional, Sequence

from sqlalchemy import select, delete
from sqlalchemy.orm import Session

from .models import Watchlist, AlertLog, BacktestResult


# ──────────────────────────────────────────────
# Watchlist
# ──────────────────────────────────────────────

def list_watchlist(db: Session, active_only: bool = True) -> Sequence[Watchlist]:
    stmt = select(Watchlist)
    if active_only:
        stmt = stmt.where(Watchlist.active.is_(True))
    stmt = stmt.order_by(Watchlist.added_at.asc())
    return db.execute(stmt).scalars().all()


def get_watchlist_item(db: Session, ticker: str) -> Optional[Watchlist]:
    return db.execute(
        select(Watchlist).where(Watchlist.ticker == ticker)
    ).scalar_one_or_none()


def add_watchlist(db: Session, ticker: str, source: str) -> Watchlist:
    existing = get_watchlist_item(db, ticker)
    if existing:
        existing.active = True
        existing.source = source
        db.commit()
        db.refresh(existing)
        return existing
    item = Watchlist(ticker=ticker, source=source, active=True)
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def remove_watchlist(db: Session, ticker: str) -> bool:
    """soft delete — active=False. 완전 삭제가 아니어서 알람 로그와의 의미적 일관성 유지."""
    item = get_watchlist_item(db, ticker)
    if not item:
        return False
    item.active = False
    db.commit()
    return True


# ──────────────────────────────────────────────
# AlertLog
# ──────────────────────────────────────────────

def insert_alert(
    db: Session,
    *,
    ticker: str,
    strength: str,
    price: float,
    rsi: Optional[float],
    bb_lower: Optional[float],
    source: str,
    reasons: Optional[list[str]] = None,
) -> AlertLog:
    log = AlertLog(
        ticker=ticker,
        strength=strength,
        price=price,
        rsi=rsi,
        bb_lower=bb_lower,
        source=source,
        reasons=reasons or [],
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    return log


def list_alerts(
    db: Session,
    *,
    ticker: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> Sequence[AlertLog]:
    stmt = select(AlertLog)
    if ticker:
        stmt = stmt.where(AlertLog.ticker == ticker)
    stmt = stmt.order_by(AlertLog.sent_at.desc()).limit(limit).offset(offset)
    return db.execute(stmt).scalars().all()


def count_alerts(db: Session, ticker: Optional[str] = None) -> int:
    from sqlalchemy import func
    stmt = select(func.count(AlertLog.id))
    if ticker:
        stmt = stmt.where(AlertLog.ticker == ticker)
    return db.execute(stmt).scalar_one()


# ──────────────────────────────────────────────
# BacktestResult
# ──────────────────────────────────────────────

def insert_backtest(
    db: Session,
    *,
    ticker: str,
    start_date: datetime,
    end_date: datetime,
    strategy_params: dict,
    win_rate: Optional[float],
    mdd: Optional[float],
    total_return: Optional[float],
    trade_count: Optional[int],
    details: Optional[dict] = None,
) -> BacktestResult:
    result = BacktestResult(
        ticker=ticker,
        start_date=start_date,
        end_date=end_date,
        strategy_params=strategy_params,
        win_rate=win_rate,
        mdd=mdd,
        total_return=total_return,
        trade_count=trade_count,
        details=details,
    )
    db.add(result)
    db.commit()
    db.refresh(result)
    return result


def get_backtest(db: Session, backtest_id: int) -> Optional[BacktestResult]:
    return db.get(BacktestResult, backtest_id)


def list_backtests(db: Session, ticker: Optional[str] = None, limit: int = 20) -> Sequence[BacktestResult]:
    stmt = select(BacktestResult)
    if ticker:
        stmt = stmt.where(BacktestResult.ticker == ticker)
    stmt = stmt.order_by(BacktestResult.created_at.desc()).limit(limit)
    return db.execute(stmt).scalars().all()
