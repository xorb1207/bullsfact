"""
CalendarFetcher 동작 확인 (M1 smoke test).

사용:
    python -m backend.scripts.test_calendar

테스트 항목:
  1. 모든 API 키 비어있는 상태 → 빈 리스트 반환 (graceful degradation)
  2. 환경변수 키가 있으면 실제 호출 결과 출력
  3. SOXL (미국 ETF), 005930.KS (한국 주식) 두 종목 비교
"""
from __future__ import annotations

import logging
import os
import sys

from dotenv import load_dotenv

# .env 로드 (worktree에서도 backend/.env 자동 인식)
load_dotenv("backend/.env", override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

from backend.core.datasource.calendar_fetcher import CalendarFetcher  # noqa: E402


def _show_keys() -> None:
    keys = {
        "FINNHUB_API_KEY": os.getenv("FINNHUB_API_KEY", ""),
        "FRED_API_KEY":    os.getenv("FRED_API_KEY", ""),
        "DART_API_KEY":    os.getenv("DART_API_KEY", ""),
    }
    for name, val in keys.items():
        status = "✅ 설정" if val else "⚪ 미설정 (silent skip)"
        print(f"  {name}: {status}")


def _run(label: str, fetcher: CalendarFetcher, ticker: str) -> None:
    print(f"\n── {label}: {ticker} ─────────────────────────")
    events = fetcher.get_events(ticker)
    contexts = fetcher.get_context_strings(ticker)

    print(f"  raw events: {len(events)}건")
    for ev in events[:10]:
        print(f"    [{ev.source:11s}] D{ev.days_until:+3d}  {ev.event_date}  {ev.description}")

    print(f"\n  context strings (Signal.reasons에 들어갈 형태):")
    if not contexts:
        print(f"    (빈 리스트 — 이벤트 없음 또는 키 미설정)")
    for s in contexts:
        print(f"    {s}")


def main() -> int:
    print("CalendarFetcher 동작 확인")
    print("=" * 60)

    print("\n[1] 환경변수 키 상태:")
    _show_keys()

    print("\n[2] 키 강제 비움 — graceful degradation 확인")
    empty_fetcher = CalendarFetcher(finnhub_key="", fred_key="", dart_key="")
    _run("(빈 키) SOXL", empty_fetcher, "SOXL")
    _run("(빈 키) 005930.KS", empty_fetcher, "005930.KS")

    print("\n[3] 환경변수 키 그대로 사용")
    real_fetcher = CalendarFetcher()
    _run("(환경변수) SOXL", real_fetcher, "SOXL")
    _run("(환경변수) 005930.KS", real_fetcher, "005930.KS")

    print("\n[4] 부가 검증 — 크립토/매크로(없는 ticker)도 graceful")
    _run("(환경변수) ETH/USDT", real_fetcher, "ETH/USDT")  # 어닝/DART 안 가져옴, 매크로만

    print("\n" + "=" * 60)
    print("✅ 완료 — 어떤 케이스에서도 예외 전파 없음")
    return 0


if __name__ == "__main__":
    sys.exit(main())
