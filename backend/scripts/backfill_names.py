"""
Watchlist.name 일괄 채우기.

- 한국 종목 (.KS / .KQ): DART corpCode.xml 한글명 우선 (예: 005930.KS → 삼성전자)
- 그 외: yfinance.info.longName / shortName

사용:
    python -m backend.scripts.backfill_names           # name 빈 항목만
    python -m backend.scripts.backfill_names --force   # 한국 종목은 영문→한글 강제 덮어쓰기
"""
from __future__ import annotations

import argparse
import logging
import sys

from dotenv import load_dotenv

load_dotenv("backend/.env", override=True)

from backend.db import SessionLocal, init_db, crud  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("backfill-names")


def _is_kr_ticker(ticker: str) -> bool:
    upper = ticker.upper()
    return upper.endswith(".KS") or upper.endswith(".KQ")


def fetch_korean_name(ticker: str) -> str | None:
    try:
        from backend.core.datasource.krx_names import resolve_korean_name
        return resolve_korean_name(ticker)
    except Exception as e:
        log.warning(f"  {ticker}: KR fetch 실패 ({type(e).__name__})")
        return None


def fetch_yfinance_name(ticker: str) -> str | None:
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info
        return info.get("longName") or info.get("shortName")
    except Exception as e:
        log.warning(f"  {ticker}: yfinance fetch 실패 ({type(e).__name__})")
        return None


def fetch_name(ticker: str, source: str) -> str | None:
    if source != "yfinance":
        return None
    if _is_kr_ticker(ticker):
        kr = fetch_korean_name(ticker)
        if kr:
            return kr
        # fallthrough → yfinance 영문 fallback
    return fetch_yfinance_name(ticker)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true",
                        help="한국 종목은 기존 영문이라도 한글로 강제 갱신")
    args = parser.parse_args()

    init_db()
    db = SessionLocal()
    try:
        items = crud.list_watchlist(db, active_only=False)

        # 갱신 대상 선정
        candidates = []
        for w in items:
            if not w.name:
                candidates.append(w)
            elif args.force and _is_kr_ticker(w.ticker):
                candidates.append(w)

        log.info(f"전체 {len(items)}개 중 갱신 대상 {len(candidates)}개"
                 f" {'(--force)' if args.force else '(미설정만)'}")

        updated = 0
        for w in candidates:
            log.info(f"  fetching {w.ticker} ({w.source})...")
            name = fetch_name(w.ticker, w.source)
            if name and name != w.name:
                crud.update_watchlist_name(db, w.ticker, name)
                log.info(f"    → {name}")
                updated += 1
            elif name == w.name:
                log.info(f"    (변경 없음: {name})")
            else:
                log.info(f"    (가져올 이름 없음)")
        log.info(f"✅ {updated}/{len(candidates)} 갱신")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
