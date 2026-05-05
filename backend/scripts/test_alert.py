"""
가짜 STRONG 시그널 1발을 파이프라인에 흘려서 Telegram 알람 도착까지 점검.

사용:
    python -m backend.scripts.test_alert            # 기본 (ETH/USDT)
    python -m backend.scripts.test_alert SOXL       # 다른 종목
    python -m backend.scripts.test_alert SOXL --no-llm   # LLM 끄고 raw만
"""
import argparse
import sys

from backend.core.strategy.dip_buy import Signal, SignalStrength
from backend.main import build_pipeline


def _fake_signal(ticker: str) -> tuple[Signal, str]:
    """티커 종류로 source 추정 + 그럴듯한 가짜 가격/지표 만들기."""
    if "/" in ticker:
        source = "binance"
        price, bb_lower, bb_mid, bb_upper = 3200.0, 3250.0, 3400.0, 3550.0
    else:
        source = "yfinance"
        price, bb_lower, bb_mid, bb_upper = 18.42, 18.65, 19.50, 20.35

    return Signal(
        ticker=ticker,
        strength=SignalStrength.STRONG,
        price=price,
        reasons=[f"RSI=31.2 < 35", f"가격 ${price:.2f} < BB하단 ${bb_lower:.2f}"],
        indicators={"rsi": 31.2, "bb_lower": bb_lower, "bb_mid": bb_mid, "bb_upper": bb_upper},
    ), source


def main():
    ap = argparse.ArgumentParser(description="가짜 STRONG 시그널로 알람 파이프라인 점검")
    ap.add_argument("ticker", nargs="?", default="ETH/USDT", help="기본: ETH/USDT")
    ap.add_argument("--no-llm", action="store_true", help="enrich 끄고 raw 알람만")
    args = ap.parse_args()

    pipeline = build_pipeline()

    if args.no_llm:
        pipeline.alerter._enricher = None

    signal, source = _fake_signal(args.ticker)
    print(f"\n>>> 가짜 STRONG 시그널: {signal.ticker} (source={source})\n")

    sent = pipeline.alerter.process(signal, source)

    print()
    print(f">>> 발송 결과:    {'OK' if sent else 'FAIL (raw 폴백 시도됨)'}")
    print(f">>> 오늘 LLM 비용: ${pipeline.llm.spent_today_usd():.4f}")

    sys.exit(0 if sent else 1)


if __name__ == "__main__":
    main()
