"""
알림 후속 추적 (post-mortem) — M3 부가 작업.

목적:
- 발동 알림이 7일/30일 후 어떻게 됐는지 자동 기록
- 누적 후 자기 검증 통계 ("VIX 30+ 발동 시그널 승률 67%")
- /cost 명령 출력에 통합 → 시그널 품질 감각

흐름:
1. update_returns(): sent_at + 7d / +30d 가 지난 NULL 항목 → 종가 fetch → 갱신
2. compute_statistics(): 누적 데이터로 평균 수익률 / 승률 산출

승률 정의: D+N 종가가 발동가 + WIN_THRESHOLD (기본 +2%) 이상.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
import yfinance as yf

from sqlalchemy import select

from backend.db import SessionLocal
from backend.db.models import AlertLog

log = logging.getLogger(__name__)

# 후속 추적 기준일
WINDOWS = (7, 30)              # D+7, D+30
WIN_THRESHOLD = 0.02           # +2% 이상이면 "적중"
LOOKBACK_DAYS_DEFAULT = 90     # 통계 산출 기본 윈도


# ──────────────────────────────────────────────
# 가격 fetch
# ──────────────────────────────────────────────

def _fetch_close_on_or_after(ticker: str, target_date: datetime) -> Optional[float]:
    """
    target_date 또는 그 이후 첫 거래일의 종가. 휴장/주말이면 다음 거래일 사용.
    실패 시 None.
    """
    try:
        # 충분한 윈도 — target_date 전후 ±5일
        start = (target_date - timedelta(days=2)).date()
        end = (target_date + timedelta(days=10)).date()
        h = yf.Ticker(ticker).history(start=start, end=end, interval="1d", auto_adjust=False)
        if h is None or h.empty:
            return None
        # 인덱스 정규화
        idx = pd.to_datetime(h.index)
        if hasattr(idx, "tz") and idx.tz is not None:
            idx = idx.tz_localize(None)
        h = h.copy()
        h.index = idx.normalize()
        # target_date 이상 첫 거래일
        target_naive = pd.Timestamp(target_date.replace(tzinfo=None) if target_date.tzinfo else target_date).normalize()
        on_or_after = h.loc[h.index >= target_naive]
        if on_or_after.empty:
            return None
        return float(on_or_after["Close"].iloc[0])
    except Exception as e:
        log.warning(f"[post-mortem] {ticker} fetch 실패: {type(e).__name__}: {e}")
        return None


# ──────────────────────────────────────────────
# Update returns
# ──────────────────────────────────────────────

def _candidates(db) -> list[AlertLog]:
    """price_7d 또는 price_30d 가 NULL이면서 발동 후 충분히 시간 지난 알림."""
    now = datetime.utcnow()
    rows = db.execute(
        select(AlertLog).where(AlertLog.sent_at <= now - timedelta(days=min(WINDOWS)))
    ).scalars().all()
    out = []
    for r in rows:
        need_7 = (r.price_7d is None and (now - r.sent_at).days >= 7)
        need_30 = (r.price_30d is None and (now - r.sent_at).days >= 30)
        if need_7 or need_30:
            out.append(r)
    return out


def update_returns(max_workers: int = 6) -> int:
    """
    candidates 일괄 갱신. 갱신된 알림 개수 반환.
    매일 1회 호출 가정 — 새로 만료된 항목만 처리되므로 비용 작음.
    """
    db = SessionLocal()
    try:
        cands = _candidates(db)
        if not cands:
            return 0

        def _process(row: AlertLog) -> bool:
            now = datetime.utcnow()
            changed = False
            try:
                if row.price_7d is None and (now - row.sent_at).days >= 7:
                    p7 = _fetch_close_on_or_after(row.ticker, row.sent_at + timedelta(days=7))
                    if p7 is not None and row.price > 0:
                        row.price_7d = p7
                        row.return_7d = p7 / row.price - 1.0
                        changed = True
                if row.price_30d is None and (now - row.sent_at).days >= 30:
                    p30 = _fetch_close_on_or_after(row.ticker, row.sent_at + timedelta(days=30))
                    if p30 is not None and row.price > 0:
                        row.price_30d = p30
                        row.return_30d = p30 / row.price - 1.0
                        changed = True
            except Exception as e:
                log.warning(f"[post-mortem] alert#{row.id} 처리 실패: {type(e).__name__}: {e}")
            return changed

        # ticker별 병렬 fetch (yfinance 안전)
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="post-mortem") as ex:
            results = list(ex.map(_process, cands))

        updated = sum(1 for r in results if r)
        if updated:
            db.commit()
            log.info(f"[post-mortem] {updated}/{len(cands)} 갱신")
        return updated
    finally:
        db.close()


# ──────────────────────────────────────────────
# Statistics
# ──────────────────────────────────────────────

@dataclass
class WindowStats:
    n: int = 0
    avg_return: Optional[float] = None
    win_rate: Optional[float] = None       # WIN_THRESHOLD 초과 비율


@dataclass
class AlertStats:
    total_alerts: int                       # since 시점 이후 발동 총 알림
    window_7d: WindowStats = field(default_factory=WindowStats)
    window_30d: WindowStats = field(default_factory=WindowStats)
    by_strength_7d: dict[str, WindowStats] = field(default_factory=dict)


def _agg(returns: list[float]) -> WindowStats:
    if not returns:
        return WindowStats()
    n = len(returns)
    avg = sum(returns) / n
    wins = sum(1 for r in returns if r > WIN_THRESHOLD)
    return WindowStats(n=n, avg_return=avg, win_rate=wins / n)


def compute_statistics(lookback_days: int = LOOKBACK_DAYS_DEFAULT) -> AlertStats:
    """최근 lookback_days 발동 알림 기준 자기 검증 통계."""
    since = datetime.utcnow() - timedelta(days=lookback_days)
    db = SessionLocal()
    try:
        rows = db.execute(
            select(AlertLog).where(AlertLog.sent_at >= since)
        ).scalars().all()
    finally:
        db.close()

    total = len(rows)
    rets_7 = [r.return_7d for r in rows if r.return_7d is not None]
    rets_30 = [r.return_30d for r in rows if r.return_30d is not None]

    by_strength: dict[str, WindowStats] = {}
    for strength in ("strong", "weak"):
        s_rets = [r.return_7d for r in rows if r.strength == strength and r.return_7d is not None]
        by_strength[strength] = _agg(s_rets)

    return AlertStats(
        total_alerts=total,
        window_7d=_agg(rets_7),
        window_30d=_agg(rets_30),
        by_strength_7d=by_strength,
    )


# ──────────────────────────────────────────────
# Telegram 출력
# ──────────────────────────────────────────────

def format_stats_section(stats: AlertStats, lookback_days: int = LOOKBACK_DAYS_DEFAULT) -> str:
    """Telegram HTML — /cost 안에 부착할 한 단락."""
    if stats.total_alerts == 0:
        return ""
    lines = [
        f"\n📈 <b>자기 검증 통계</b>  <i>(최근 {lookback_days}일)</i>",
        f"  발동 알림: {stats.total_alerts}건",
    ]
    lines.append(
        f"  데이터 확보: 7d {stats.window_7d.n}건 / 30d {stats.window_30d.n}건"
    )

    def _line(label: str, w: WindowStats) -> str:
        if w.n == 0 or w.avg_return is None:
            return f"  {label}: 데이터 부족"
        avg_pct = w.avg_return * 100
        win_pct = (w.win_rate or 0) * 100
        return f"  {label}: 평균 {avg_pct:+.1f}%  ·  승률 {win_pct:.0f}% (n={w.n})"

    lines.append(_line("7d ", stats.window_7d))
    lines.append(_line("30d", stats.window_30d))

    # 강도별 (7d 만)
    strong = stats.by_strength_7d.get("strong")
    weak = stats.by_strength_7d.get("weak")
    if strong and strong.n > 0:
        lines.append(f"  💪 STRONG 7d 승률: {(strong.win_rate or 0)*100:.0f}% (n={strong.n})")
    if weak and weak.n > 0:
        lines.append(f"  🟡 WEAK 7d 승률:   {(weak.win_rate or 0)*100:.0f}% (n={weak.n})")

    lines.append(f"  <i>적중 기준: D+N 수익률 +{WIN_THRESHOLD*100:.0f}% 이상</i>")
    return "\n".join(lines)
