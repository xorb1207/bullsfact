"""
포트폴리오 노출도 계산 — 베타 + R² (M2-B).

목적: 보유 포지션이 SPY/SOXX 같은 벤치마크와 얼마나 동조하는지 정량 측정.
사용자 포트가 NVDA/SOXL/AMD/TQQQ 등 반도체 집중이라 "사실상 반도체 ETF와
R²=0.94" 같은 한 줄이 과잉확신 방지에 핵심.

해석:
- β > 1: 벤치마크보다 변동성 큼 (레버리지 효과)
- R² > 0.85: 벤치마크와 강한 동조 (사실상 같이 움직임)
- β + R² 둘 다 높음: 분산 효과 거의 없음
"""
from __future__ import annotations

import logging
import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Optional, Sequence

import pandas as pd

from .datasource.provider import DataProvider
from .money import currency_for

log = logging.getLogger(__name__)


# 벤치마크 — yfinance ticker
DEFAULT_BENCHMARKS: list[tuple[str, str]] = [
    ("SPY",  "S&P 500"),
    ("SOXX", "반도체"),
]

# 분석 기간 (일봉)
PERIOD = "2y"
INTERVAL = "1d"
LOOKBACK_DAYS = 252            # 약 1년 (영업일)
MIN_OVERLAP = 60               # 회귀에 필요한 최소 공통 거래일


@dataclass
class TickerExposure:
    ticker: str
    qty: float
    current_price: float
    value: float                                       # qty × current_price (티커 통화)
    currency: str
    return_pct: float                                  # 평단 대비 수익률
    # bench_symbol → (beta, r2)
    metrics: dict[str, tuple[Optional[float], Optional[float]]] = field(default_factory=dict)


@dataclass
class PortfolioExposure:
    tickers: list[TickerExposure]
    total_value_usd_proxy: float                       # 통화 무관 단순 합 (해석 주의)
    # bench_symbol → (beta, r2) — 가중 시계열 기반
    portfolio_metrics: dict[str, tuple[Optional[float], Optional[float]]]
    weights: dict[str, float]                          # ticker → weight (0~1)
    benchmarks: list[tuple[str, str]]                  # (symbol, label)
    warnings: list[str] = field(default_factory=list)


# ──────────────────────────────────────────────
# 핵심 통계
# ──────────────────────────────────────────────

def _daily_returns(close: pd.Series) -> pd.Series:
    """단순 daily return. NaN drop은 회귀 단계에서."""
    if close is None or close.empty:
        return pd.Series(dtype=float)
    return close.pct_change()


def beta_and_r2(
    stock_returns: pd.Series,
    bench_returns: pd.Series,
) -> tuple[Optional[float], Optional[float]]:
    """β = cov/var, R² = corr². 공통 거래일이 부족하면 (None, None)."""
    if stock_returns.empty or bench_returns.empty:
        return None, None
    df = pd.concat([stock_returns, bench_returns], axis=1, join="inner").dropna()
    df.columns = ["s", "b"]
    if len(df) < MIN_OVERLAP:
        return None, None
    var_b = df["b"].var()
    if var_b == 0 or math.isnan(var_b):
        return None, None
    beta = df["s"].cov(df["b"]) / var_b
    corr = df["s"].corr(df["b"])
    if corr is None or math.isnan(corr):
        return None, None
    return float(beta), float(corr ** 2)


# ──────────────────────────────────────────────
# 데이터 fetch
# ──────────────────────────────────────────────

def _fetch_close(provider: DataProvider, ticker: str) -> Optional[pd.Series]:
    """일봉 close 시리즈만 추출. 실패 시 None."""
    try:
        df = provider.get_ohlcv(ticker, interval=INTERVAL, period=PERIOD)
        if df is None or df.empty or "close" not in df.columns:
            return None
        close = df["close"].dropna()
        # 인덱스 정규화 — 같은 날짜는 같은 인덱스로 join 되도록
        if hasattr(close.index, "tz") and close.index.tz is not None:
            close.index = close.index.tz_localize(None)
        # 시간 부분 제거 (날짜만)
        close.index = pd.to_datetime(close.index).normalize()
        # 마지막 LOOKBACK_DAYS 만 사용
        return close.tail(LOOKBACK_DAYS)
    except Exception as e:
        log.warning(f"[Exposure] {ticker} fetch 실패: {type(e).__name__}: {e}")
        return None


def _fetch_all_closes(
    provider: DataProvider,
    tickers: Sequence[str],
) -> dict[str, pd.Series]:
    """병렬 fetch — 실패한 티커는 dict에서 제외."""
    out: dict[str, pd.Series] = {}
    with ThreadPoolExecutor(max_workers=8, thread_name_prefix="exposure-fetch") as ex:
        futures = {ex.submit(_fetch_close, provider, t): t for t in tickers}
        for fut in futures:
            t = futures[fut]
            close = fut.result()
            if close is not None and not close.empty:
                out[t] = close
            else:
                log.info(f"[Exposure] {t} 데이터 없음 — 노출도 계산에서 제외")
    return out


# ──────────────────────────────────────────────
# 메인
# ──────────────────────────────────────────────

def compute_exposure(
    provider: DataProvider,
    positions,                                          # Sequence[Position]
    benchmarks: list[tuple[str, str]] = DEFAULT_BENCHMARKS,
) -> Optional[PortfolioExposure]:
    """
    positions: db.crud.list_positions(...) 결과.
    가격은 fetch한 close의 마지막 값 (스캐너와 같은 일봉 기준).

    Returns:
      None: 포지션 없음 또는 유효 데이터 부족
    """
    if not positions:
        return None

    # 종목 + 벤치마크 모두 한 번에 병렬 fetch
    pos_tickers = [p.ticker for p in positions]
    bench_tickers = [sym for sym, _ in benchmarks]
    all_tickers = list(set(pos_tickers + bench_tickers))
    closes = _fetch_all_closes(provider, all_tickers)

    # 벤치마크 returns
    bench_returns: dict[str, pd.Series] = {}
    for sym, _ in benchmarks:
        c = closes.get(sym)
        if c is not None:
            bench_returns[sym] = _daily_returns(c)
        else:
            log.warning(f"[Exposure] 벤치마크 {sym} 데이터 없음")

    if not bench_returns:
        return None

    # 종목별 노출
    ticker_exposures: list[TickerExposure] = []
    weighted_returns_components: list[tuple[float, pd.Series]] = []  # (weight_value, returns)

    for p in positions:
        close = closes.get(p.ticker)
        if close is None or close.empty:
            continue
        cur_price = float(close.iloc[-1])
        value = cur_price * p.qty
        ret_pct = (cur_price / p.avg_cost - 1.0) if p.avg_cost > 0 else 0.0

        stock_ret = _daily_returns(close)

        metrics: dict[str, tuple[Optional[float], Optional[float]]] = {}
        for sym, br in bench_returns.items():
            metrics[sym] = beta_and_r2(stock_ret, br)

        ticker_exposures.append(TickerExposure(
            ticker=p.ticker,
            qty=p.qty,
            current_price=cur_price,
            value=value,
            currency=currency_for(p.ticker),
            return_pct=ret_pct,
            metrics=metrics,
        ))
        weighted_returns_components.append((value, stock_ret))

    if not ticker_exposures:
        return None

    # 통화별 합산은 명시적이지만, 가중치 계산용으로 단순 합 (currency 무관 numeric)
    # — 정확한 USD 환산은 환율 fetch 필요. 일단 나타난 numeric value 기준 가중.
    total_value = sum(v for v, _ in weighted_returns_components)
    weights = {te.ticker: (te.value / total_value if total_value > 0 else 0.0)
               for te in ticker_exposures}

    # 포트폴리오 가중 return series
    if total_value > 0:
        weighted_returns = sum(
            (w_val / total_value) * ret_series
            for w_val, ret_series in weighted_returns_components
        )
    else:
        weighted_returns = pd.Series(dtype=float)

    portfolio_metrics: dict[str, tuple[Optional[float], Optional[float]]] = {}
    for sym, br in bench_returns.items():
        portfolio_metrics[sym] = beta_and_r2(weighted_returns, br)

    # 경고 라벨 자동 생성
    warnings_: list[str] = []
    for sym, (beta, r2) in portfolio_metrics.items():
        if r2 is not None and r2 > 0.90:
            warnings_.append(f"{sym} R² {r2:.2f} — 사실상 단일 섹터 노출")
        if beta is not None and beta > 1.5:
            warnings_.append(f"{sym} β {beta:.2f} — 공격적 레버리지 노출")

    # 통화 혼용 경고
    currencies = {te.currency for te in ticker_exposures}
    if len(currencies) > 1:
        warnings_.append(
            f"통화 혼용 ({', '.join(sorted(currencies))}) — 가중치는 환율 미고려, 참고치"
        )

    return PortfolioExposure(
        tickers=ticker_exposures,
        total_value_usd_proxy=total_value,
        portfolio_metrics=portfolio_metrics,
        weights=weights,
        benchmarks=benchmarks,
        warnings=warnings_,
    )
