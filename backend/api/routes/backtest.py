"""
백테스트 라우트 — BacktestEngine 사용.
"""
import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.db import get_db, crud
from backend.core.datasource import DataProvider
from backend.core.strategy import DipBuyStrategy
from backend.core.backtest import BacktestEngine, BacktestConfig, build_exit_rule
from ..schemas import BacktestRequest, BacktestOut
from ..deps import get_provider

router = APIRouter(prefix="/backtest", tags=["backtest"])


def _period_for_span(days: int) -> str:
    if days <= 7: return "7d"
    if days <= 30: return "30d"
    if days <= 60: return "60d"
    if days <= 90: return "90d"
    if days <= 180: return "180d"
    return "1y"


@router.post("", response_model=BacktestOut)
def run_backtest(
    body: BacktestRequest,
    db: Session = Depends(get_db),
    provider: DataProvider = Depends(get_provider),
):
    if body.end_date <= body.start_date:
        raise HTTPException(status_code=400, detail="end_date must be after start_date")

    strategy = DipBuyStrategy(
        rsi_period=body.rsi_period,
        rsi_threshold=body.rsi_threshold,
        bb_period=body.bb_period,
        bb_std=body.bb_std,
    )

    try:
        exit_rule = build_exit_rule(body.exit_rule, body.exit_params)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    config = BacktestConfig(
        fee_bps=body.fee_bps,
        slippage_bps=body.slippage_bps,
        allow_overlap=body.allow_overlap,
    )

    span_days = (body.end_date - body.start_date).days
    period = _period_for_span(span_days)

    try:
        df = provider.get_ohlcv(body.ticker, interval=body.interval, period=period)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"데이터 조회 실패: {e}")

    start = pd.Timestamp(body.start_date, tz="UTC")
    end = pd.Timestamp(body.end_date, tz="UTC")
    df = df[(df.index >= start) & (df.index <= end)]
    min_required = max(body.rsi_period, body.bb_period) + 5
    if df.empty or len(df) < min_required:
        raise HTTPException(status_code=400, detail="기간 내 데이터 부족")

    engine = BacktestEngine(strategy=strategy, exit_rule=exit_rule, config=config)
    report = engine.run(df)

    params = {
        "rsi_period": body.rsi_period,
        "rsi_threshold": body.rsi_threshold,
        "bb_period": body.bb_period,
        "bb_std": body.bb_std,
        "interval": body.interval,
        "exit_rule": body.exit_rule,
        "exit_params": body.exit_params,
        "fee_bps": body.fee_bps,
        "slippage_bps": body.slippage_bps,
        "allow_overlap": body.allow_overlap,
    }

    m = report.metrics
    saved = crud.insert_backtest(
        db,
        ticker=body.ticker,
        start_date=body.start_date,
        end_date=body.end_date,
        strategy_params=params,
        win_rate=m["win_rate"],
        mdd=m["mdd"],
        total_return=m["total_return"],
        trade_count=m["trade_count"],
        details={
            "metrics": m,
            "trades": report.trades[:500],          # 응답 크기 보호
            "equity_curve": report.equity_curve,
            "exit_rule_resolved": report.exit_rule,
            "config": report.config,
        },
    )
    return saved


@router.get("/{backtest_id}", response_model=BacktestOut)
def get_backtest(backtest_id: int, db: Session = Depends(get_db)):
    item = crud.get_backtest(db, backtest_id)
    if not item:
        raise HTTPException(status_code=404, detail="backtest not found")
    return item


@router.get("", response_model=list[BacktestOut])
def list_backtests(
    ticker: str | None = None,
    limit: int = 20,
    db: Session = Depends(get_db),
):
    return crud.list_backtests(db, ticker=ticker, limit=limit)
