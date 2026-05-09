"""
이벤트별 RSI 임계치 캘리브레이션 (M2-A).

목적: "RSI 35 < 매수" 같은 직관 임계치를 백테스트로 보정.
이벤트 임박일(D-3 ~ D+0) 구간만 필터링 → RSI grid 적중률 grid search →
적중률 최고 RSI를 EventCalibration 테이블에 저장.

적중 정의: RSI < threshold 인 날 D 에서, D+forward_days 종가가 +target_return% 이상.

설계 원칙:
- FRED historical 발표일 활용 (CalendarFetcher 의 lookback 모드)
- yfinance 일봉 2년치 (LOOKBACK_DAYS = 730)
- 결과 없거나 sample 부족 → 저장 안 함 (default fallback 유지)
- 종목별/이벤트별로 독립 row
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional, Sequence

import pandas as pd
import pandas_ta as ta
import requests
import yfinance as yf

log = logging.getLogger(__name__)


# 캘리브레이션 대상
DEFAULT_TICKERS = ["SPY", "QQQ", "SOXL", "TQQQ", "NVDA"]
DEFAULT_EVENT_TYPES = ["cpi", "nfp", "fomc"]

# Grid + 윈도
RSI_GRID = [30.0, 31.0, 32.0, 33.0, 34.0, 35.0, 36.0, 37.0, 38.0, 39.0, 40.0]
EVENT_LOOKBACK_DAYS = 3       # 이벤트 D-3 ~ D+0 구간만 후보
FORWARD_DAYS = 5              # 시그널 발생 후 D+5 종가 평가
TARGET_RETURN = 0.02          # +2% 도달이 "적중"
LOOKBACK_DAYS = 730           # 백테스트 기간 (~2년)
MIN_SAMPLES = 5               # 이 미만이면 저장 X (통계 의미 없음)

# FRED release IDs (CalendarFetcher 와 동일)
_FRED_RELEASES = {
    "cpi": 10,
    "nfp": 50,
    # ppi는 일단 제외 — CPI/NFP 가 시장 영향 더 큼
}

# FOMC 2024-2026 하드코딩 (Fed 공식, historical 포함)
_FOMC_HISTORICAL: list[date] = [
    # 2024
    date(2024, 1, 31),  date(2024, 3, 20),  date(2024, 5, 1),
    date(2024, 6, 12),  date(2024, 7, 31),  date(2024, 9, 18),
    date(2024, 11, 7),  date(2024, 12, 18),
    # 2025
    date(2025, 1, 29),  date(2025, 3, 19),  date(2025, 5, 7),
    date(2025, 6, 18),  date(2025, 7, 30),  date(2025, 9, 17),
    date(2025, 10, 29), date(2025, 12, 10),
    # 2026 (현재년도)
    date(2026, 1, 28),  date(2026, 3, 18),  date(2026, 4, 29),
]


@dataclass
class CalibrationResult:
    event_type: str
    ticker: str
    rsi_threshold: float
    hit_rate: float
    sample_count: int


# ──────────────────────────────────────────────
# 데이터 fetch
# ──────────────────────────────────────────────

def _fetch_fred_historical(event_type: str, fred_key: str, start: date, end: date) -> list[date]:
    """과거 ~end 까지 발표일 모두 fetch."""
    release_id = _FRED_RELEASES.get(event_type)
    if not release_id:
        return []
    try:
        url = "https://api.stlouisfed.org/fred/release/dates"
        params = {
            "release_id": release_id,
            "api_key": fred_key,
            "file_type": "json",
            "include_release_dates_with_no_data": "true",
            "limit": 200,
            "sort_order": "asc",
            "realtime_start": start.isoformat(),
            "realtime_end": end.isoformat(),
        }
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json() or {}
        out: list[date] = []
        for row in data.get("release_dates", []) or []:
            d_str = row.get("date")
            if not d_str:
                continue
            try:
                d = datetime.strptime(d_str, "%Y-%m-%d").date()
            except ValueError:
                continue
            if start <= d <= end:
                out.append(d)
        return out
    except Exception as e:
        log.warning(f"[Calibration] FRED {event_type} fetch 실패: {type(e).__name__}: {e}")
        return []


def _get_event_dates(event_type: str, fred_key: str, lookback_days: int) -> list[date]:
    end = date.today()
    start = end - timedelta(days=lookback_days)
    if event_type == "fomc":
        return [d for d in _FOMC_HISTORICAL if start <= d <= end]
    if event_type in _FRED_RELEASES:
        if not fred_key:
            log.warning(f"[Calibration] FRED key 없음 → {event_type} 스킵")
            return []
        return _fetch_fred_historical(event_type, fred_key, start, end)
    return []


def _fetch_ohlcv(ticker: str, lookback_days: int) -> Optional[pd.DataFrame]:
    """일봉 OHLCV. RSI 계산용 + 적중률 평가용."""
    try:
        period = "2y" if lookback_days <= 730 else "5y"
        h = yf.Ticker(ticker).history(period=period, interval="1d", auto_adjust=False)
        if h is None or h.empty:
            return None
        df = h[["Open", "High", "Low", "Close", "Volume"]].copy()
        df.columns = [c.lower() for c in df.columns]
        if hasattr(df.index, "tz") and df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        df.index = pd.to_datetime(df.index).normalize()
        df["rsi"] = ta.rsi(df["close"], length=14)
        return df.dropna(subset=["close"])
    except Exception as e:
        log.warning(f"[Calibration] {ticker} OHLCV fetch 실패: {type(e).__name__}: {e}")
        return None


# ──────────────────────────────────────────────
# Grid search
# ──────────────────────────────────────────────

def _evaluate_threshold(
    df: pd.DataFrame,
    event_dates: list[date],
    rsi_threshold: float,
    forward_days: int = FORWARD_DAYS,
    target_return: float = TARGET_RETURN,
    event_lookback: int = EVENT_LOOKBACK_DAYS,
) -> tuple[float, int]:
    """
    이벤트 임박 구간 (D-event_lookback ~ D+0) 만 보고:
      - RSI < rsi_threshold 인 날을 시그널로 수집
      - D+forward_days 종가가 +target_return 이상이면 적중
    Returns: (hit_rate, sample_count)
    """
    signals = 0
    hits = 0
    for ev_date in event_dates:
        # 이벤트 임박 구간 인덱스
        win_start = pd.Timestamp(ev_date - timedelta(days=event_lookback))
        win_end = pd.Timestamp(ev_date)
        win = df.loc[(df.index >= win_start) & (df.index <= win_end)]
        if win.empty:
            continue
        for sig_date, row in win.iterrows():
            rsi = row.get("rsi")
            if rsi is None or pd.isna(rsi):
                continue
            if rsi >= rsi_threshold:
                continue
            # forward 종가 평가
            target_idx = sig_date + pd.Timedelta(days=forward_days * 2)  # 영업일 ≈ 자연일 * 1.4
            future = df.loc[(df.index > sig_date) & (df.index <= target_idx)]
            if len(future) < 1:
                continue
            entry_close = float(row["close"])
            forward_close = float(future["close"].iloc[min(forward_days - 1, len(future) - 1)])
            if entry_close <= 0:
                continue
            ret = (forward_close - entry_close) / entry_close
            signals += 1
            if ret >= target_return:
                hits += 1
    if signals == 0:
        return 0.0, 0
    return hits / signals, signals


def grid_search(
    df: pd.DataFrame,
    event_dates: list[date],
    rsi_grid: list[float] = RSI_GRID,
) -> Optional[CalibrationResult]:
    """모든 RSI threshold에 대해 적중률 평가, 최고값 반환. sample 부족 시 None."""
    best: Optional[tuple[float, float, int]] = None  # (rsi, hit_rate, samples)
    for rsi in rsi_grid:
        hr, n = _evaluate_threshold(df, event_dates, rsi)
        if n < MIN_SAMPLES:
            continue
        if best is None or hr > best[1]:
            best = (rsi, hr, n)
    if best is None:
        return None
    return CalibrationResult(
        event_type="",   # caller 가 채움
        ticker="",       # caller 가 채움
        rsi_threshold=best[0],
        hit_rate=best[1],
        sample_count=best[2],
    )


# ──────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────

def calibrate_one(
    event_type: str,
    ticker: str,
    fred_key: str,
    lookback_days: int = LOOKBACK_DAYS,
) -> Optional[CalibrationResult]:
    event_dates = _get_event_dates(event_type, fred_key, lookback_days)
    if not event_dates:
        log.info(f"[Calibration] {event_type} 발표일 0건 — 스킵")
        return None
    df = _fetch_ohlcv(ticker, lookback_days)
    if df is None or df.empty:
        log.info(f"[Calibration] {ticker} OHLCV 없음 — 스킵")
        return None
    result = grid_search(df, event_dates)
    if result is None:
        log.info(f"[Calibration] {event_type}/{ticker} sample 부족 (<{MIN_SAMPLES})")
        return None
    result.event_type = event_type
    result.ticker = ticker
    return result


def calibrate_all(
    event_types: Sequence[str] = DEFAULT_EVENT_TYPES,
    tickers: Sequence[str] = DEFAULT_TICKERS,
    fred_key: str = "",
    lookback_days: int = LOOKBACK_DAYS,
) -> list[CalibrationResult]:
    """모든 (event, ticker) 조합 캘리브레이션. 결과 없는 조합은 빠짐."""
    out: list[CalibrationResult] = []
    for ev_type in event_types:
        for tk in tickers:
            r = calibrate_one(ev_type, tk, fred_key, lookback_days)
            if r is not None:
                out.append(r)
                log.info(
                    f"[Calibration] {ev_type}/{tk}: RSI<{r.rsi_threshold} "
                    f"hit={r.hit_rate:.1%} (n={r.sample_count})"
                )
    return out
