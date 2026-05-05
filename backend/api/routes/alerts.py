"""
알람 히스토리 라우트.
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from backend.db import get_db, crud
from ..schemas import AlertsPage, AlertOut

router = APIRouter(prefix="/alerts", tags=["alerts"])


@router.get("", response_model=AlertsPage)
def list_alerts(
    ticker: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    items = crud.list_alerts(db, ticker=ticker, limit=limit, offset=offset)
    total = crud.count_alerts(db, ticker=ticker)
    return AlertsPage(
        items=[AlertOut.model_validate(i) for i in items],
        total=total,
        limit=limit,
        offset=offset,
    )
