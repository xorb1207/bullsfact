"""
DB CRUD 헬퍼 — 라우트와 Scanner가 공통으로 쓰는 얇은 레이어.
"""
from datetime import datetime
from typing import Optional, Sequence

from sqlalchemy import select, delete
from sqlalchemy.orm import Session

from .models import (
    Watchlist, AlertLog, BacktestResult, LLMCallLog, ThresholdAlert,
    Position, EventCalibration, SellReminder, LLMCache, User,
)


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
# SellReminder
# ──────────────────────────────────────────────

def list_reminders(
    db: Session, *, active_only: bool = True,
) -> Sequence[SellReminder]:
    stmt = select(SellReminder)
    if active_only:
        stmt = stmt.where(SellReminder.active.is_(True))
    stmt = stmt.order_by(SellReminder.target_date.asc())
    return db.execute(stmt).scalars().all()


def get_reminder(db: Session, rid: int) -> Optional[SellReminder]:
    return db.get(SellReminder, rid)


def insert_reminder(
    db: Session,
    *,
    title: str,
    target_date: datetime,
    notes: Optional[str] = None,
    days_before: int = 7,
) -> SellReminder:
    row = SellReminder(
        title=title,
        target_date=target_date,
        notes=notes,
        days_before=days_before,
        active=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def mark_reminder_done(db: Session, rid: int) -> bool:
    row = db.get(SellReminder, rid)
    if not row:
        return False
    row.active = False
    row.done_at = datetime.utcnow()
    db.commit()
    return True


def delete_reminder(db: Session, rid: int) -> bool:
    row = db.get(SellReminder, rid)
    if not row:
        return False
    db.delete(row)
    db.commit()
    return True


def auto_expire_past_reminders(db: Session) -> int:
    """D+1 지난 미완료 리마인더 자동 비활성. 반환: 처리 개수."""
    from datetime import timedelta
    cutoff = datetime.utcnow() - timedelta(days=1)
    rows = db.execute(
        select(SellReminder).where(
            SellReminder.active.is_(True),
            SellReminder.target_date < cutoff,
        )
    ).scalars().all()
    n = 0
    for r in rows:
        r.active = False
        n += 1
    if n:
        db.commit()
    return n


# ──────────────────────────────────────────────
# EventCalibration
# ──────────────────────────────────────────────

def upsert_event_calibration(
    db: Session,
    *,
    event_type: str,
    ticker: str,
    rsi_threshold: float,
    hit_rate: float,
    sample_count: int,
    forward_days: int = 5,
    target_return: float = 0.02,
    lookback_days: int = 730,
    bb_std: Optional[float] = None,
) -> EventCalibration:
    existing = db.execute(
        select(EventCalibration).where(
            EventCalibration.event_type == event_type,
            EventCalibration.ticker == ticker,
        )
    ).scalar_one_or_none()
    if existing:
        existing.rsi_threshold = rsi_threshold
        existing.hit_rate = hit_rate
        existing.sample_count = sample_count
        existing.forward_days = forward_days
        existing.target_return = target_return
        existing.lookback_days = lookback_days
        existing.bb_std = bb_std
        existing.last_calibrated_at = datetime.utcnow()
        db.commit()
        db.refresh(existing)
        return existing
    row = EventCalibration(
        event_type=event_type, ticker=ticker, rsi_threshold=rsi_threshold,
        hit_rate=hit_rate, sample_count=sample_count, forward_days=forward_days,
        target_return=target_return, lookback_days=lookback_days, bb_std=bb_std,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def get_event_calibration(
    db: Session, event_type: str, ticker: str,
) -> Optional[EventCalibration]:
    return db.execute(
        select(EventCalibration).where(
            EventCalibration.event_type == event_type,
            EventCalibration.ticker == ticker,
        )
    ).scalar_one_or_none()


def list_event_calibrations(db: Session) -> Sequence[EventCalibration]:
    return db.execute(
        select(EventCalibration).order_by(
            EventCalibration.event_type.asc(), EventCalibration.ticker.asc()
        )
    ).scalars().all()


# ──────────────────────────────────────────────
# LLMCache
# ──────────────────────────────────────────────

def get_llm_cache(db: Session, *, purpose: str, cache_key: str) -> Optional[LLMCache]:
    """만료 안 된 캐시만 반환."""
    now = datetime.utcnow()
    return db.execute(
        select(LLMCache).where(
            LLMCache.purpose == purpose,
            LLMCache.cache_key == cache_key,
            LLMCache.expires_at > now,
        ).order_by(LLMCache.created_at.desc()).limit(1)
    ).scalar_one_or_none()


def put_llm_cache(
    db: Session,
    *,
    purpose: str,
    cache_key: str,
    result_text: dict,
    cost_usd: float,
    ttl_seconds: int,
) -> LLMCache:
    from datetime import timedelta
    now = datetime.utcnow()
    row = LLMCache(
        purpose=purpose,
        cache_key=cache_key,
        result_text=result_text,
        cost_usd=cost_usd,
        created_at=now,
        expires_at=now + timedelta(seconds=ttl_seconds),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def cleanup_expired_cache(db: Session) -> int:
    now = datetime.utcnow()
    rows = db.execute(
        select(LLMCache).where(LLMCache.expires_at <= now)
    ).scalars().all()
    n = len(rows)
    for r in rows:
        db.delete(r)
    if n:
        db.commit()
    return n


def cache_stats(db: Session, *, since_days: int = 30) -> dict:
    """캐시 hit 통계 — /cost 출력용. cost_usd 합 = 절감 추정."""
    from datetime import timedelta
    from sqlalchemy import func
    since = datetime.utcnow() - timedelta(days=since_days)
    row = db.execute(
        select(
            func.count(LLMCache.id).label("entries"),
            func.coalesce(func.sum(LLMCache.cost_usd), 0.0).label("savings_potential"),
        ).where(LLMCache.created_at >= since)
    ).one()
    return {
        "entries": int(row.entries or 0),
        "savings_potential_usd": float(row.savings_potential or 0.0),
    }


# ──────────────────────────────────────────────
# User
# ──────────────────────────────────────────────

# tier 별 default 일일 캡 (USD)
TIER_DEFAULT_CAP = {
    "OWNER":   None,         # None = env MAX_DAILY_LLM_USD 사용
    "TRUSTED": 0.30,
    "LIMITED": 0.10,
}


def get_user_by_chat_id(db: Session, chat_id: str) -> Optional[User]:
    return db.execute(
        select(User).where(User.telegram_chat_id == str(chat_id))
    ).scalar_one_or_none()


def get_or_create_owner(db: Session, chat_id: str, name: Optional[str] = None) -> User:
    """env TELEGRAM_CHAT_ID 와 매칭되는 사용자를 OWNER로 자동 생성/보장."""
    existing = get_user_by_chat_id(db, chat_id)
    if existing:
        if existing.tier != "OWNER":
            existing.tier = "OWNER"
            db.commit()
            db.refresh(existing)
        return existing
    user = User(
        telegram_chat_id=str(chat_id),
        name=name or "Owner",
        tier="OWNER",
        active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def list_users(db: Session, *, active_only: bool = True) -> Sequence[User]:
    stmt = select(User)
    if active_only:
        stmt = stmt.where(User.active.is_(True))
    return db.execute(stmt).scalars().all()


def upsert_user(
    db: Session,
    *,
    telegram_chat_id: str,
    tier: str = "LIMITED",
    name: Optional[str] = None,
    llm_daily_cap_usd: Optional[float] = None,
) -> User:
    existing = get_user_by_chat_id(db, telegram_chat_id)
    if existing:
        existing.tier = tier
        if name is not None:
            existing.name = name
        if llm_daily_cap_usd is not None:
            existing.llm_daily_cap_usd = llm_daily_cap_usd
        db.commit()
        db.refresh(existing)
        return existing
    user = User(
        telegram_chat_id=str(telegram_chat_id),
        name=name,
        tier=tier,
        llm_daily_cap_usd=llm_daily_cap_usd,
        active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def user_llm_spent_today(db: Session, user_id: int) -> float:
    """오늘 (UTC 자정 이후) 누적 LLM 비용 (USD)."""
    from sqlalchemy import func
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    row = db.execute(
        select(func.coalesce(func.sum(LLMCallLog.cost_cents), 0.0))
        .where(LLMCallLog.user_id == user_id, LLMCallLog.called_at >= today_start)
    ).scalar_one()
    return float(row or 0.0) / 100.0


def effective_daily_cap(user: User, env_cap: float) -> float:
    """사용자 tier + 개별 override 기반 일일 캡."""
    if user.llm_daily_cap_usd is not None:
        return float(user.llm_daily_cap_usd)
    default = TIER_DEFAULT_CAP.get(user.tier)
    if default is None:
        return env_cap
    return float(default)


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
    user_id: Optional[int] = None,
) -> LLMCallLog:
    row = LLMCallLog(
        model=model,
        purpose=purpose,
        ticker=ticker,
        user_id=user_id,
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
