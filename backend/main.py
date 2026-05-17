"""
Scanner 진입점 — DB 워치리스트를 읽어 주기적으로 스캔.

API 서버를 띄우려면:
    uvicorn backend.api.app:app --reload

스캐너만 띄우려면 (무한 루프):
    python -m backend.main

1회만 돌려보려면:
    python -m backend.scripts.run_once

가짜 STRONG 시그널로 파이프라인 점검:
    python -m backend.scripts.test_alert
"""
import logging
import os
from dataclasses import dataclass
from dotenv import load_dotenv

from backend.core.datasource import DataProvider
from backend.core.strategy import DipBuyStrategy
from backend.core.alerter import AlertEngine
from backend.core.scanner import Scanner
from backend.core.threshold_alerts import ThresholdAlertEvaluator
from backend.core.positions import PositionEvaluator
from backend.core.market import MarketFetcher
from backend.core.datasource.calendar_fetcher import CalendarFetcher
from backend.core.enrichment import LLMClient, LLMEnricher, Synthesizer
from backend.core.enrichment.analysts import NewsAnalyst, FundamentalsAnalyst, FilingAnalyst
from backend.db import init_db, SessionLocal, crud

load_dotenv(override=True)  # .env가 셸 환경변수보다 우선

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# DB 비어있을 때 사용할 fallback
FALLBACK_TICKERS = ["SOXL", "TQQQ", "ETH-USD"]


@dataclass
class Pipeline:
    """팩토리 결과 묶음 — 스크립트에서 부분만 꺼내 쓰기 편하게."""
    provider: DataProvider
    strategy: DipBuyStrategy
    llm: LLMClient
    enricher: LLMEnricher
    alerter: AlertEngine
    scanner: Scanner


def _persist_llm_call(usage) -> None:
    """LLMClient.on_call → DB(LLMCallLog)에 호출 내역 기록."""
    db = SessionLocal()
    try:
        crud.insert_llm_call(
            db,
            model=usage.model,
            purpose=usage.purpose or "unknown",
            ticker=usage.ticker,
            user_id=usage.user_id,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_creation_tokens=usage.cache_creation_tokens,
            cost_cents=round(usage.cost_usd() * 100, 4),
            latency_ms=usage.latency_ms,
        )
    finally:
        db.close()


def build_pipeline() -> Pipeline:
    """env에서 모든 설정을 읽어 파이프라인 객체들을 조립."""
    init_db()

    provider = DataProvider(
        binance_api_key=os.getenv("BINANCE_API_KEY", ""),
        binance_api_secret=os.getenv("BINANCE_API_SECRET", ""),
    )

    # M1: 이벤트 캘린더 — 환경변수 키로 자동 활성/비활성. 키 없으면 silent skip.
    calendar_fetcher = CalendarFetcher()

    strategy = DipBuyStrategy(
        rsi_threshold=float(os.getenv("RSI_THRESHOLD", "35")),
        bb_std=float(os.getenv("BB_STD", "2.0")),
        calendar_fetcher=calendar_fetcher,
    )

    llm = LLMClient(
        max_daily_usd=float(os.getenv("MAX_DAILY_LLM_USD", "2.0")),
        on_call=_persist_llm_call,
    )
    enricher = LLMEnricher(
        analysts=[NewsAnalyst(), FundamentalsAnalyst(), FilingAnalyst(llm=llm)],
        synthesizer=Synthesizer(
            llm=llm,
            model=os.getenv("LLM_MODEL_ENRICHMENT", "claude-haiku-4-5"),
        ),
        timeout_sec=30.0,
    )

    alerter = AlertEngine(
        telegram_token=os.getenv("TELEGRAM_TOKEN", "YOUR_BOT_TOKEN"),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID"),
        cooldown_min=int(os.getenv("COOLDOWN_MIN", "60")),
        enricher=enricher,
    )

    threshold_eval = ThresholdAlertEvaluator(calendar_fetcher=calendar_fetcher)
    market_fetcher = MarketFetcher()
    position_eval = PositionEvaluator()

    scanner = Scanner(
        data_provider=provider,
        strategy=strategy,
        alert_engine=alerter,
        interval=os.getenv("DATA_INTERVAL", "1h"),
        period=os.getenv("DATA_PERIOD", "60d"),
        fallback_tickers=FALLBACK_TICKERS,
        threshold_evaluator=threshold_eval,
        market_fetcher=market_fetcher,
        position_evaluator=position_eval,
    )

    return Pipeline(provider, strategy, llm, enricher, alerter, scanner)


def main() -> None:
    pipeline = build_pipeline()
    pipeline.scanner.run(check_interval_min=int(os.getenv("CHECK_INTERVAL_MIN", "15")))


if __name__ == "__main__":
    main()
