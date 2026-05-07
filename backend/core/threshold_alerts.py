"""
ThresholdAlert 평가 엔진.

가격 알림 (스캐너의 OHLCV 재사용) + VIX/F&G 알림 (MarketSnapshot 재사용)
모두 같은 메커니즘으로 평가. 발동 시 ThresholdAlert.active=False 로 자동 비활성화.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Optional, Sequence

import pandas as pd

from backend.db import SessionLocal, crud
from backend.db.models import ThresholdAlert
from .market import MarketSnapshot

log = logging.getLogger(__name__)


METRIC_PRICE = "price"
METRIC_VIX = "vix"
METRIC_FG_CNN = "fg_cnn"
METRIC_FG_CRYPTO = "fg_crypto"

REF_HIGH_252 = "high_252d"
REF_LOW_252 = "low_252d"
REF_EMA_50 = "ema_50d"


# ──────────────────────────────────────────────
# Reference window 계산
# ──────────────────────────────────────────────

def compute_ref_value(df: pd.DataFrame, window: str) -> Optional[float]:
    """일봉 기준 reference value. df는 close 컬럼 보유 (소문자)."""
    if df is None or df.empty or "close" not in df.columns:
        return None
    close = df["close"].dropna()
    if close.empty:
        return None

    if window == REF_HIGH_252:
        # 252 영업일 = 약 1년. 데이터가 부족하면 가용 전부 사용
        return float(close.tail(252).max())
    if window == REF_LOW_252:
        return float(close.tail(252).min())
    if window == REF_EMA_50:
        if len(close) < 50:
            return None
        return float(close.ewm(span=50, adjust=False).mean().iloc[-1])
    return None


def resolve_threshold(alert: ThresholdAlert, df: Optional[pd.DataFrame]) -> Optional[float]:
    """abs_value 또는 (ref_window + ref_pct) → 숫자 threshold."""
    if alert.abs_value is not None:
        return float(alert.abs_value)
    if alert.ref_window and alert.ref_pct is not None:
        if df is None:
            return None
        ref = compute_ref_value(df, alert.ref_window)
        if ref is None:
            return None
        return ref * (1.0 + alert.ref_pct)
    return None


def is_breached(direction: str, current: float, threshold: float) -> bool:
    if direction == "below":
        return current <= threshold
    if direction == "above":
        return current >= threshold
    return False


# ──────────────────────────────────────────────
# Evaluation 결과
# ──────────────────────────────────────────────

@dataclass
class AlertEvaluation:
    alert: ThresholdAlert
    triggered: bool
    current_value: float
    threshold: float
    ref_value: Optional[float] = None        # 상대값 알림인 경우 기준점

    def metric_label(self) -> str:
        if self.alert.metric_type == METRIC_PRICE:
            return self.alert.ticker or "?"
        return {
            METRIC_VIX: "VIX",
            METRIC_FG_CNN: "F&G (CNN)",
            METRIC_FG_CRYPTO: "F&G (Crypto)",
        }.get(self.alert.metric_type, self.alert.metric_type)


# ──────────────────────────────────────────────
# Evaluator
# ──────────────────────────────────────────────

def _safe_float(x) -> Optional[float]:
    if x is None:
        return None
    if isinstance(x, float) and math.isnan(x):
        return None
    return float(x)


class ThresholdAlertEvaluator:
    """
    스캐너 사이클당 1회 instantiation 권장.
    DB 세션은 매 호출마다 새로 열고 닫음 (SessionLocal scope 짧게).
    """

    def evaluate_for_ticker(
        self,
        ticker: str,
        df: pd.DataFrame,
        current_price: float,
    ) -> list[AlertEvaluation]:
        """이 ticker에 걸린 모든 활성 'price' 알림을 평가."""
        results: list[AlertEvaluation] = []
        db = SessionLocal()
        try:
            alerts = crud.list_threshold_alerts(
                db, active_only=True, metric_type=METRIC_PRICE, ticker=ticker
            )
            for a in alerts:
                threshold = resolve_threshold(a, df)
                if threshold is None:
                    log.warning(f"[ThresholdEval] alert#{a.id} threshold 계산 실패")
                    continue
                ref = compute_ref_value(df, a.ref_window) if a.ref_window else None
                triggered = is_breached(a.direction, current_price, threshold)
                results.append(AlertEvaluation(
                    alert=a,
                    triggered=triggered,
                    current_value=current_price,
                    threshold=threshold,
                    ref_value=ref,
                ))
                # 디버깅용 마지막 값 저장
                crud.update_threshold_last_value(db, a.id, current_price)
        finally:
            db.close()
        return results

    def evaluate_market_gauges(self, snap: MarketSnapshot) -> list[AlertEvaluation]:
        """MarketSnapshot에서 VIX/F&G 값을 뽑아 활성 알림과 비교."""
        results: list[AlertEvaluation] = []

        # 메트릭별 현재값 추출
        values: dict[str, Optional[float]] = {
            METRIC_VIX: None,
            METRIC_FG_CNN: None,
            METRIC_FG_CRYPTO: None,
        }
        for q in snap.indices:
            if q.label == "VIX" and not q.error:
                values[METRIC_VIX] = q.price
                break
        for fg in snap.sentiment:
            if fg.source == "cnn":
                values[METRIC_FG_CNN] = fg.score
            elif fg.source == "crypto":
                values[METRIC_FG_CRYPTO] = fg.score

        db = SessionLocal()
        try:
            for metric_type, current in values.items():
                if current is None:
                    continue
                alerts = crud.list_threshold_alerts(
                    db, active_only=True, metric_type=metric_type
                )
                for a in alerts:
                    # 게이지 메트릭은 항상 abs_value 사용 (df가 없으니)
                    threshold = resolve_threshold(a, df=None)
                    if threshold is None:
                        log.warning(
                            f"[ThresholdEval] gauge alert#{a.id} ({metric_type}) "
                            f"threshold 미정 — 상대값은 게이지에 미지원"
                        )
                        continue
                    triggered = is_breached(a.direction, current, threshold)
                    results.append(AlertEvaluation(
                        alert=a,
                        triggered=triggered,
                        current_value=current,
                        threshold=threshold,
                    ))
                    crud.update_threshold_last_value(db, a.id, current)
        finally:
            db.close()
        return results
