"""
매매전략 MD (AI 섹터 매매 전략 2026) §3.1 / §3.2 / §3.3 의 알림 표를 DB에 일괄 등록.

사용:
    python -m backend.scripts.seed_threshold_alerts          # 미존재만 추가
    python -m backend.scripts.seed_threshold_alerts --reset  # 기존 동일 룰 모두 삭제 후 재시드

동일 룰 판별 기준: (metric_type, ticker, direction, abs_value, ref_window, ref_pct)
"""
from __future__ import annotations

import argparse
import logging
import sys

from sqlalchemy import select

from backend.db import SessionLocal, init_db, crud
from backend.db.models import ThresholdAlert

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("seed-alerts")


# ──────────────────────────────────────────────
# 시드 데이터 (매매전략 MD 그대로)
# ──────────────────────────────────────────────

# §3.1 매수 알림 (하향 돌파)
BUY_ALERTS = [
    # SOXL 3-Tier
    dict(metric_type="price", ticker="SOXL", direction="below", abs_value=110.0,
         tier="T1", priority="HIGH", note="T1 매수 검토 + 시장 상황 점검"),
    dict(metric_type="price", ticker="SOXL", direction="below", abs_value=80.0,
         tier="T2", priority="HIGH", note="T2 자동 매수 검토"),
    dict(metric_type="price", ticker="SOXL", direction="below", abs_value=50.0,
         tier="T3", priority="HIGH", note="T3 자동 매수 검토"),
    # TQQQ 3-Tier
    dict(metric_type="price", ticker="TQQQ", direction="below", abs_value=56.0,
         tier="T1", priority="HIGH", note="T1 매수 검토"),
    dict(metric_type="price", ticker="TQQQ", direction="below", abs_value=45.0,
         tier="T2", priority="HIGH", note="T2 매수 검토"),
    dict(metric_type="price", ticker="TQQQ", direction="below", abs_value=32.0,
         tier="T3", priority="HIGH", note="T3 매수 검토"),
    # 코어/분산 종목
    dict(metric_type="price", ticker="NVDA", direction="below", abs_value=170.0,
         priority="MED", note="코어 섹터 약세 신호"),
    dict(metric_type="price", ticker="NVDA", direction="below", abs_value=140.0,
         priority="MED", note="본격 조정, T2 신호 강화"),
    dict(metric_type="price", ticker="AVGO", direction="below", abs_value=370.0,
         priority="MED", note="반도체 코어 약세"),
    dict(metric_type="price", ticker="GOOGL", direction="below", abs_value=340.0,
         priority="MED", note="빅테크 약세 진입"),
    dict(metric_type="price", ticker="VST", direction="below", abs_value=140.0,
         priority="MED", note="전력 인프라 매수 검토"),
    dict(metric_type="price", ticker="META", direction="below", abs_value=580.0,
         priority="MED", note="빅테크 분산 매수"),
    dict(metric_type="price", ticker="TSLA", direction="below", abs_value=320.0,
         priority="LOW", note="비중 조절 검토"),
]

# §3.2 매도 알림 (상향 돌파)
SELL_ALERTS = [
    dict(metric_type="price", ticker="TQQQ", direction="above", abs_value=75.0,
         priority="HIGH", note="1차 매도 가속"),
    dict(metric_type="price", ticker="TQQQ", direction="above", abs_value=85.0,
         priority="HIGH", note="2차 매도 (시장 과열)"),
    dict(metric_type="price", ticker="SOXL", direction="above", abs_value=180.0,
         priority="HIGH", note="1차 매도 가속"),
    dict(metric_type="price", ticker="SOXL", direction="above", abs_value=220.0,
         priority="HIGH", note="2차 매도 (시장 과열)"),
]

# §3.3 변동성 지표 알림
VIX_ALERTS = [
    dict(metric_type="vix", direction="above", abs_value=25.0,
         priority="LOW", note="변동성 경계 진입"),
    dict(metric_type="vix", direction="above", abs_value=30.0,
         priority="MED", note="T1 매수 신호 강화"),
    dict(metric_type="vix", direction="above", abs_value=35.0,
         priority="HIGH", note="본격 매수 구간 진입"),
    dict(metric_type="vix", direction="above", abs_value=50.0,
         priority="HIGH", note="T3 강력 매수 신호 (드물게 발생)"),
]

# CNN Fear & Greed (전략 MD에 명시 없음 — 통상적 임계치)
FG_ALERTS = [
    dict(metric_type="fg_cnn", direction="below", abs_value=25.0,
         priority="MED", note="극심한 공포 — 매수 컨텍스트 강화"),
    dict(metric_type="fg_cnn", direction="above", abs_value=75.0,
         priority="MED", note="극심한 탐욕 — 매도 컨텍스트 강화"),
    dict(metric_type="fg_crypto", direction="below", abs_value=20.0,
         priority="LOW", note="크립토 극심한 공포"),
    dict(metric_type="fg_crypto", direction="above", abs_value=80.0,
         priority="LOW", note="크립토 극심한 탐욕"),
]

ALL_SEEDS = BUY_ALERTS + SELL_ALERTS + VIX_ALERTS + FG_ALERTS


# ──────────────────────────────────────────────
# 중복 판별
# ──────────────────────────────────────────────

def _key(d: dict) -> tuple:
    return (
        d.get("metric_type"),
        d.get("ticker"),
        d.get("direction"),
        d.get("abs_value"),
        d.get("ref_window"),
        d.get("ref_pct"),
    )


def _existing_keys(db) -> set[tuple]:
    rows = db.execute(select(ThresholdAlert)).scalars().all()
    return {(r.metric_type, r.ticker, r.direction, r.abs_value, r.ref_window, r.ref_pct) for r in rows}


def _delete_matching(db, seeds: list[dict]) -> int:
    """--reset 시: 같은 key 의 기존 룰 모두 삭제."""
    keys = {_key(s) for s in seeds}
    rows = db.execute(select(ThresholdAlert)).scalars().all()
    n = 0
    for r in rows:
        rk = (r.metric_type, r.ticker, r.direction, r.abs_value, r.ref_window, r.ref_pct)
        if rk in keys:
            db.delete(r)
            n += 1
    db.commit()
    return n


# ──────────────────────────────────────────────
# main
# ──────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="기존 동일 룰 삭제 후 재시드")
    args = parser.parse_args()

    init_db()
    db = SessionLocal()
    try:
        if args.reset:
            n = _delete_matching(db, ALL_SEEDS)
            log.info(f"기존 동일 룰 {n}건 삭제")

        existing = _existing_keys(db)
        added = 0
        skipped = 0
        for seed in ALL_SEEDS:
            if _key(seed) in existing:
                skipped += 1
                continue
            crud.insert_threshold_alert(db, **seed)
            added += 1

        log.info(f"✅ 시드 완료 — 추가 {added}건, 스킵 {skipped}건 (총 {len(ALL_SEEDS)}건)")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
