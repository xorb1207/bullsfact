"""
워치리스트 라우트.
GET 시 현재 지표(RSI, BB)를 함께 반환.
"""
import logging
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from backend.db import get_db, crud
from backend.core.datasource import DataProvider
from backend.core.strategy import DipBuyStrategy
from ..schemas import WatchlistCreate, WatchlistOut, WatchlistWithIndicators
from ..deps import get_provider, get_strategy

log = logging.getLogger(__name__)

router = APIRouter(prefix="/watchlist", tags=["watchlist"])


@router.get("", response_model=list[WatchlistWithIndicators])
def list_watchlist(
    include_indicators: bool = True,
    db: Session = Depends(get_db),
    provider: DataProvider = Depends(get_provider),
    strategy: DipBuyStrategy = Depends(get_strategy),
):
    items = crud.list_watchlist(db, active_only=True)
    out: list[WatchlistWithIndicators] = []
    for item in items:
        row = WatchlistWithIndicators.model_validate(item)
        if include_indicators:
            try:
                df = provider.get_ohlcv(item.ticker, interval="1h", period="60d")
                signal = strategy.generate_signal(df, item.ticker)
                row.price = signal.price
                row.rsi = signal.indicators.get("rsi")
                row.bb_lower = signal.indicators.get("bb_lower")
                row.bb_mid = signal.indicators.get("bb_mid")
                row.bb_upper = signal.indicators.get("bb_upper")
                row.signal = signal.strength.value
            except Exception as e:
                log.warning(f"[/watchlist] {item.ticker} 지표 조회 실패: {e}")
                row.error = str(e)
        out.append(row)
    return out


@router.post("", response_model=WatchlistOut, status_code=status.HTTP_201_CREATED)
def add_to_watchlist(
    body: WatchlistCreate,
    db: Session = Depends(get_db),
    provider: DataProvider = Depends(get_provider),
):
    ticker = body.ticker.strip()
    if not ticker:
        raise HTTPException(status_code=400, detail="ticker is empty")
    source = provider.source_of(ticker)
    item = crud.add_watchlist(db, ticker=ticker, source=source)
    return item


@router.delete("/{ticker}", status_code=status.HTTP_204_NO_CONTENT)
def remove_from_watchlist(ticker: str, db: Session = Depends(get_db)):
    ok = crud.remove_watchlist(db, ticker=ticker)
    if not ok:
        raise HTTPException(status_code=404, detail=f"{ticker} not in watchlist")
    return None
