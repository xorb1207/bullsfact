"""
스캐너 1사이클만 실행 — 무한 루프 안 돌고 한 번 스캔 후 종료.

워치리스트 비어있으면 fallback 종목(SOXL/TQQQ/ETH-USD) 사용.
실제 yfinance/Binance 데이터로 시그널 계산 → STRONG이면 enrich + Telegram 발송.

사용:
    python -m backend.scripts.run_once
    python -m backend.scripts.run_once --tickers SOXL,TQQQ,ETH/USDT
    python -m backend.scripts.run_once --seed-watchlist   # 워치리스트에 fallback 등록
"""
import argparse
import logging

from backend.db import SessionLocal, crud
from backend.main import build_pipeline, FALLBACK_TICKERS

log = logging.getLogger(__name__)


def _seed_watchlist():
    """fallback 종목들을 watchlist DB에 등록 (이미 있으면 스킵)."""
    db = SessionLocal()
    try:
        existing = {w.ticker for w in crud.list_watchlist(db, active_only=False)}
        added = []
        for t in FALLBACK_TICKERS:
            if t in existing:
                continue
            source = "binance" if "/" in t else "yfinance"
            crud.add_watchlist(db, ticker=t, source=source)
            added.append(t)
        if added:
            print(f"[seed] watchlist 등록: {added}")
        else:
            print("[seed] 모든 fallback 종목 이미 등록됨")
    finally:
        db.close()


def main():
    ap = argparse.ArgumentParser(description="스캐너 1사이클 실행")
    ap.add_argument("--tickers", help="콤마 분리 (워치리스트 무시하고 임시 사용)")
    ap.add_argument("--seed-watchlist", action="store_true", help="워치리스트에 fallback 등록 후 실행")
    args = ap.parse_args()

    if args.seed_watchlist:
        _seed_watchlist()

    pipeline = build_pipeline()

    if args.tickers:
        tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
        print(f"\n>>> 임시 종목 스캔: {tickers}")
        for t in tickers:
            pipeline.scanner.scan_one(t)
    else:
        print("\n>>> 워치리스트 스캔 (1사이클)")
        pipeline.scanner.scan_all()

    print()
    print(f">>> 오늘 LLM 비용: ${pipeline.llm.spent_today_usd():.4f}")


if __name__ == "__main__":
    main()
