"""
Dual-window 상관계수 (M2-C).

MarketSnapshot 에 핵심 페어 상관관계를 두 윈도(20d/200d)로 부착.
"현재 레짐 vs 기준선" 비교 → "역상관 심화" 같은 정량 라벨 자동 생성.

CLAUDE.md M2 추가 설계:
- 5일 윈도는 spurious correlation 위험 → 20d 최소
- 20d (현재) / 200d (기준) → delta 로 레짐 변화 감지
- LLM 매크로 해설에서 "금리 우려성 하락" 판단의 정량 근거
"""
from __future__ import annotations

import logging
import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional

import pandas as pd
import yfinance as yf

log = logging.getLogger(__name__)


# 추적할 페어 — (label_a, sym_a, label_b, sym_b)
# 의도:
#   1. SOXL vs 10Y     : 반도체 레버리지 ETF 의 금리 민감도 (사용자 주력 종목)
#   2. S&P vs 10Y       : 시장 전반의 금리 민감도 (벤치마크)
#   3. BTC vs DXY       : 디지털 골드 가설 — 달러 강세 시 BTC 약세 패턴
DEFAULT_PAIRS: list[tuple[str, str, str, str]] = [
    ("SOXL", "SOXL",        "10Y",  "^TNX"),
    ("S&P",  "^GSPC",       "10Y",  "^TNX"),
    ("BTC",  "BTC-USD",     "DXY",  "DX-Y.NYB"),
]

SHORT_WINDOW = 20      # 현재 레짐
LONG_WINDOW = 200      # 기준선
FETCH_PERIOD = "2y"    # LONG_WINDOW(200d) + 여유
DELTA_REGIME_THRESHOLD = 0.20   # |delta| 이 이상이면 "심화" 라벨


@dataclass
class CorrelationPair:
    label_a: str
    label_b: str
    corr_short: Optional[float]    # SHORT_WINDOW 일 상관
    corr_long: Optional[float]     # LONG_WINDOW 일 상관
    delta: Optional[float]         # short - long
    interpretation: str            # 자동 라벨 (한국어)


# ──────────────────────────────────────────────
# 핵심 계산
# ──────────────────────────────────────────────

def _fetch_close(symbol: str) -> Optional[pd.Series]:
    try:
        h = yf.Ticker(symbol).history(period=FETCH_PERIOD, interval="1d", auto_adjust=False)
        if h is None or h.empty:
            return None
        close = h["Close"].dropna()
        if hasattr(close.index, "tz") and close.index.tz is not None:
            close.index = close.index.tz_localize(None)
        close.index = pd.to_datetime(close.index).normalize()
        return close
    except Exception as e:
        log.warning(f"[Correlations] {symbol} fetch 실패: {type(e).__name__}: {e}")
        return None


def _windowed_corr(ret_a: pd.Series, ret_b: pd.Series, window: int) -> Optional[float]:
    """공통 거래일 기준, 마지막 window 일의 pearson corr."""
    if ret_a.empty or ret_b.empty:
        return None
    df = pd.concat([ret_a, ret_b], axis=1, join="inner").dropna()
    df.columns = ["a", "b"]
    if len(df) < window:
        return None
    tail = df.tail(window)
    if tail["a"].std() == 0 or tail["b"].std() == 0:
        return None
    c = tail["a"].corr(tail["b"])
    if c is None or math.isnan(c):
        return None
    return float(c)


def _interpret(short: Optional[float], long: Optional[float], delta: Optional[float]) -> str:
    """레짐 변화 자동 라벨."""
    if short is None or long is None or delta is None:
        return "데이터 부족"
    # 절대 강도
    if abs(short) >= 0.7:
        strength = "강한 " + ("양의 상관" if short > 0 else "역상관")
    elif abs(short) >= 0.4:
        strength = "중간 " + ("양의 상관" if short > 0 else "역상관")
    else:
        strength = "약한 상관"
    # 변화
    if abs(delta) < DELTA_REGIME_THRESHOLD:
        change = "기준선 유지"
    elif delta > 0:
        change = "양의 방향 강화" if long >= 0 else "역상관 약화"
    else:
        change = "역상관 강화" if long >= 0 else "역상관 심화"
    return f"{strength}, {change}"


def compute_pair(label_a: str, sym_a: str, label_b: str, sym_b: str) -> CorrelationPair:
    close_a = _fetch_close(sym_a)
    close_b = _fetch_close(sym_b)
    if close_a is None or close_b is None:
        return CorrelationPair(
            label_a=label_a, label_b=label_b,
            corr_short=None, corr_long=None, delta=None,
            interpretation="데이터 부족",
        )
    ret_a = close_a.pct_change()
    ret_b = close_b.pct_change()
    short = _windowed_corr(ret_a, ret_b, SHORT_WINDOW)
    long_ = _windowed_corr(ret_a, ret_b, LONG_WINDOW)
    delta = (short - long_) if (short is not None and long_ is not None) else None
    return CorrelationPair(
        label_a=label_a,
        label_b=label_b,
        corr_short=short,
        corr_long=long_,
        delta=delta,
        interpretation=_interpret(short, long_, delta),
    )


def compute_all_pairs(
    pairs: list[tuple[str, str, str, str]] = DEFAULT_PAIRS,
) -> list[CorrelationPair]:
    """병렬로 모든 페어 계산."""
    out: list[CorrelationPair] = []
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="corr") as ex:
        futs = [ex.submit(compute_pair, *p) for p in pairs]
        for fut in futs:
            try:
                out.append(fut.result(timeout=20))
            except Exception as e:
                log.warning(f"[Correlations] 페어 계산 실패 (무시): {type(e).__name__}: {e}")
    return out
