"""
DB CRUD 헬퍼 — 라우트와 Scanner가 공통으로 쓰는 얇은 레이어.
"""
from datetime import datetime
from typing import Optional, Sequence

from sqlalchemy import select, delete
from sqlalchemy.orm import Session

from .models import Watchlist, AlertLog, BacktestResult, LLMCallLog, ThresholdAlert, Position


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


def add_watchlist(db: Session, ticker: str, source: str, name: Optional[str] = None) -> Watchlist:
    existing = get_watchlist_item(db, ticker)
    if existing:
        existing.active = True
        existing.source = source
        if name and not existing.name:
            existing.name = name
        db.commit()
        db.refresh(existing)
        return existing
    item = Watchlist(ticker=ticker, source=source, name=name, active=True)
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def update_watchlist_name(db: Session, ticker: str, name: str) -> bool:
    item = get_watchlist_item(db, ticker)
    if not item:
        return False
    item.name = name
    db.commit()
    return True


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


# ──────────────────────────────────────────────
# Position
# ──────────────────────────────────────────────

def list_positions(db: Session) -> Sequence[Position]:
    return db.execute(
        select(Position).order_by(Position.added_at.asc())
    ).scalars().all()


def get_position(db: Session, ticker: str) -> Optional[Position]:
    return db.execute(
        select(Position).where(Position.ticker == ticker)
    ).scalar_one_or_none()


def upsert_position(
    db: Session,
    *,
    ticker: str,
    qty: float,
    avg_cost: float,
    highest_milestone: float = 0.0,
    notes: Optional[str] = None,
) -> Position:
    """있으면 갱신 (qty/avg_cost/notes/highest_milestone 모두), 없으면 추가."""
    row = get_position(db, ticker)
    if row:
        row.qty = qty
        row.avg_cost = avg_cost
        row.highest_milestone = highest_milestone
        if notes is not None:
            row.notes = notes
    else:
        row = Position(
            ticker=ticker, qty=qty, avg_cost=avg_cost,
            highest_milestone=highest_milestone, notes=notes,
        )
        db.add(row)
    db.commit()
    db.refresh(row)
    return row


def update_position_milestone(db: Session, ticker: str, milestone: float) -> Optional[Position]:
    row = get_position(db, ticker)
    if not row:
        return None
    row.highest_milestone = milestone
    db.commit()
    db.refresh(row)
    return row


def delete_position(db: Session, ticker: str) -> bool:
    row = get_position(db, ticker)
    if not row:
        return False
    db.delete(row)
    db.commit()
    return True


# ──────────────────────────────────────────────
# ThresholdAlert
# ──────────────────────────────────────────────

def list_threshold_alerts(
    db: Session,
    *,
    active_only: bool = True,
    metric_type: Optional[str] = None,
    ticker: Optional[str] = None,
) -> Sequence[ThresholdAlert]:
    stmt = select(ThresholdAlert)
    if active_only:
        stmt = stmt.where(ThresholdAlert.active.is_(True))
    if metric_type:
        stmt = stmt.where(ThresholdAlert.metric_type == metric_type)
    if ticker:
        stmt = stmt.where(ThresholdAlert.ticker == ticker)
    stmt = stmt.order_by(ThresholdAlert.created_at.asc())
    return db.execute(stmt).scalars().all()


def get_threshold_alert(db: Session, alert_id: int) -> Optional[ThresholdAlert]:
    return db.get(ThresholdAlert, alert_id)


def insert_threshold_alert(
    db: Session,
    *,
    metric_type: str,
    direction: str,
    ticker: Optional[str] = None,
    abs_value: Optional[float] = None,
    ref_window: Optional[str] = None,
    ref_pct: Optional[float] = None,
    tier: Optional[str] = None,
    priority: str = "MED",
    note: Optional[str] = None,
    re_arm_after_h: Optional[int] = None,
) -> ThresholdAlert:
    if abs_value is None and (ref_window is None or ref_pct is None):
        raise ValueError("abs_value 또는 (ref_window + ref_pct) 둘 중 하나는 필수")
    if abs_value is not None and (ref_window is not None or ref_pct is not None):
        raise ValueError("abs_value 와 (ref_window/ref_pct)는 동시에 지정 불가")
    if direction not in ("above", "below"):
        raise ValueError(f"direction은 'above'|'below' 중 하나: {direction}")
    if metric_type == "price" and not ticker:
        raise ValueError("metric_type='price'는 ticker 필수")

    row = ThresholdAlert(
        metric_type=metric_type,
        ticker=ticker,
        direction=direction,
        abs_value=abs_value,
        ref_window=ref_window,
        ref_pct=ref_pct,
        tier=tier,
        priority=priority,
        note=note,
        re_arm_after_h=re_arm_after_h,
        active=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def mark_threshold_triggered(
    db: Session,
    alert_id: int,
    *,
    last_value: float,
) -> Optional[ThresholdAlert]:
    row = db.get(ThresholdAlert, alert_id)
    if not row:
        return None
    row.triggered_at = datetime.utcnow()
    row.last_value = last_value
    row.active = False
    db.commit()
    db.refresh(row)
    return row


def update_threshold_last_value(
    db: Session,
    alert_id: int,
    value: float,
) -> None:
    """디버깅/모니터링용 — 매 평가 사이클마다 현재값 저장."""
    row = db.get(ThresholdAlert, alert_id)
    if not row:
        return
    row.last_value = value
    db.commit()


def set_threshold_active(db: Session, alert_id: int, active: bool) -> bool:
    row = db.get(ThresholdAlert, alert_id)
    if not row:
        return False
    row.active = active
    if active:
        row.triggered_at = None
    db.commit()
    return True


def delete_threshold_alert(db: Session, alert_id: int) -> bool:
    row = db.get(ThresholdAlert, alert_id)
    if not row:
        return False
    db.delete(row)
    db.commit()
    return True


def re_arm_due_alerts(db: Session) -> int:
    """re_arm_after_h가 설정된 발동 완료 알림 중, 시간 경과한 것 자동 재활성화. 재활성화 개수 반환."""
    from datetime import timedelta
    now = datetime.utcnow()
    candidates = db.execute(
        select(ThresholdAlert).where(
            ThresholdAlert.active.is_(False),
            ThresholdAlert.re_arm_after_h.isnot(None),
            ThresholdAlert.triggered_at.isnot(None),
        )
    ).scalars().all()
    count = 0
    for row in candidates:
        if row.triggered_at + timedelta(hours=row.re_arm_after_h) <= now:
            row.active = True
            row.triggered_at = None
            count += 1
    if count:
        db.commit()
    return count


# ──────────────────────────────────────────────
# LLMCallLog
# ──────────────────────────────────────────────

def insert_llm_call(
    db: Session,
    *,
    model: str,
    purpose: str,
    ticker: Optional[str],
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_creation_tokens: int,
    cost_cents: float,
    latency_ms: Optional[int] = None,
) -> LLMCallLog:
    row = LLMCallLog(
        model=model,
        purpose=purpose,
        ticker=ticker,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_creation_tokens=cache_creation_tokens,
        cost_cents=cost_cents,
        latency_ms=latency_ms,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def llm_cost_summary(
    db: Session,
    *,
    since: datetime,
    until: Optional[datetime] = None,
) -> dict:
    """기간 내 LLM 호출 집계. cost_report에서 사용."""
    from sqlalchemy import func
    stmt = select(
        func.count(LLMCallLog.id).label("calls"),
        func.coalesce(func.sum(LLMCallLog.cost_cents), 0.0).label("cents"),
        func.coalesce(func.sum(LLMCallLog.input_tokens), 0).label("in_tokens"),
        func.coalesce(func.sum(LLMCallLog.output_tokens), 0).label("out_tokens"),
    ).where(LLMCallLog.called_at >= since)
    if until:
        stmt = stmt.where(LLMCallLog.called_at < until)
    row = db.execute(stmt).one()
    return {
        "calls": row.calls,
        "cost_usd": row.cents / 100.0,
        "input_tokens": row.in_tokens,
        "output_tokens": row.out_tokens,
    }
