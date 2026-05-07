"""
Watchlist.name 빈 항목을 yfinance.info 로 일괄 채우기.

사용:
    python -m backend.scripts.backfill_names
"""
from __future__ import annotations

import logging
import sys

from backend.db import SessionLocal, init_db, crud

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("backfill-names")


def fetch_name(ticker: str, source: str) -> str | None:
    if source != "yfinance":
        return None
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info
        return info.get("longName") or info.get("shortName")
    except Exception as e:
        log.warning(f"  {ticker}: fetch 실패 ({type(e).__name__})")
        return None


def main() -> int:
    init_db()
    db = SessionLocal()
    try:
        items = crud.list_watchlist(db, active_only=False)
        empty = [i for i in items if not i.name]
        log.info(f"전체 {len(items)}개 중 name 미설정 {len(empty)}개")

        updated = 0
        for w in empty:
            log.info(f"  fetching {w.ticker} ({w.source})...")
            name = fetch_name(w.ticker, w.source)
            if name:
                crud.update_watchlist_name(db, w.ticker, name)
                log.info(f"    → {name}")
                updated += 1
        log.info(f"✅ {updated}/{len(empty)} 갱신")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
