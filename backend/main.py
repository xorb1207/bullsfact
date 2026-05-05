"""
Scanner 진입점 — DB 워치리스트를 읽어 주기적으로 스캔.

API 서버를 띄우려면:
    uvicorn backend.api.app:app --reload

스캐너만 띄우려면:
    python -m backend.main
"""
import logging
import os
from dotenv import load_dotenv

from backend.core.datasource import DataProvider
from backend.core.strategy import DipBuyStrategy
from backend.core.alerter import AlertEngine
from backend.core.scanner import Scanner
from backend.db import init_db

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# DB 비어있을 때 사용할 fallback (개인 사용 편의)
FALLBACK_TICKERS = ["SOXL", "TQQQ", "ETH-USD"]


def main() -> None:
    init_db()

    provider = DataProvider(
        binance_api_key=os.getenv("BINANCE_API_KEY", ""),
        binance_api_secret=os.getenv("BINANCE_API_SECRET", ""),
    )

    strategy = DipBuyStrategy(
        rsi_threshold=float(os.getenv("RSI_THRESHOLD", "35")),
        bb_std=float(os.getenv("BB_STD", "2.0")),
    )

    alerter = AlertEngine(
        telegram_token=os.getenv("TELEGRAM_TOKEN", "YOUR_BOT_TOKEN"),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID"),
        cooldown_min=int(os.getenv("COOLDOWN_MIN", "60")),
    )

    scanner = Scanner(
        data_provider=provider,
        strategy=strategy,
        alert_engine=alerter,
        interval=os.getenv("DATA_INTERVAL", "1h"),
        period=os.getenv("DATA_PERIOD", "60d"),
        fallback_tickers=FALLBACK_TICKERS,
    )

    scanner.run(check_interval_min=int(os.getenv("CHECK_INTERVAL_MIN", "15")))


if __name__ == "__main__":
    main()
