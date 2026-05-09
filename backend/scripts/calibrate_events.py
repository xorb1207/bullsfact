"""
이벤트별 RSI threshold 캘리브레이션 실행 (M2-A).

사용:
    python -m backend.scripts.calibrate_events
    python -m backend.scripts.calibrate_events --event cpi --ticker SPY
    python -m backend.scripts.calibrate_events --lookback 1095   # 3년

수동 실행 권장 — 주 1회 또는 새 이벤트 시즌 전.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv("backend/.env", override=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("calibrate")

from backend.db import init_db, SessionLocal, crud  # noqa: E402
from backend.core.calibration import (                # noqa: E402
    calibrate_all, calibrate_one,
    DEFAULT_EVENT_TYPES, DEFAULT_TICKERS, LOOKBACK_DAYS,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", type=str, default=None,
                        help="단일 이벤트 (cpi/nfp/fomc). 미지정 시 전체.")
    parser.add_argument("--ticker", type=str, default=None,
                        help="단일 티커. 미지정 시 전체.")
    parser.add_argument("--lookback", type=int, default=LOOKBACK_DAYS,
                        help=f"백테스트 기간 (일, 기본 {LOOKBACK_DAYS}).")
    args = parser.parse_args()

    init_db()
    fred_key = os.getenv("FRED_API_KEY", "")
    if not fred_key:
        log.warning("FRED_API_KEY 미설정 — CPI/NFP 캘리브레이션 건너뜀 (FOMC만 가능)")

    event_types = [args.event] if args.event else DEFAULT_EVENT_TYPES
    tickers = [args.ticker] if args.ticker else DEFAULT_TICKERS

    log.info(f"캘리브레이션 시작: events={event_types} tickers={tickers} lookback={args.lookback}d")
    results = calibrate_all(
        event_types=event_types,
        tickers=tickers,
        fred_key=fred_key,
        lookback_days=args.lookback,
    )

    if not results:
        log.warning("결과 없음 (sample 부족 또는 데이터 fetch 실패)")
        return 1

    db = SessionLocal()
    try:
        for r in results:
            crud.upsert_event_calibration(
                db,
                event_type=r.event_type,
                ticker=r.ticker,
                rsi_threshold=r.rsi_threshold,
                hit_rate=r.hit_rate,
                sample_count=r.sample_count,
                lookback_days=args.lookback,
            )
    finally:
        db.close()

    log.info(f"✅ {len(results)}건 저장 완료")
    print()
    print(f"{'event':6s}  {'ticker':8s}  {'RSI':>6s}  {'hit%':>6s}  {'n':>4s}")
    print("─" * 40)
    for r in sorted(results, key=lambda x: (x.event_type, x.ticker)):
        print(f"{r.event_type:6s}  {r.ticker:8s}  "
              f"{r.rsi_threshold:>6.1f}  {r.hit_rate*100:>5.1f}%  {r.sample_count:>4d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
