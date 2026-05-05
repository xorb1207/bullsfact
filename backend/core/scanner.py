"""
Scanner — DB 워치리스트 기반 주기 스캐너.
tickers를 직접 받지 않고 DB에서 동적으로 읽어옴.
"""
import logging
import math
import schedule
import time
from typing import Optional, Sequence

from .datasource.provider import DataProvider
from .strategy.dip_buy import DipBuyStrategy
from .alerter import AlertEngine
from backend.db import SessionLocal, crud

log = logging.getLogger(__name__)


class Scanner:

    def __init__(
        self,
        data_provider: DataProvider,
        strategy: DipBuyStrategy,
        alert_engine: AlertEngine,
        interval: str = "1h",
        period: str = "60d",
        fallback_tickers: Optional[Sequence[str]] = None,
    ):
        self.provider = data_provider
        self.strategy = strategy
        self.alerter = alert_engine
        self.interval = interval
        self.period = period
        self._fallback = list(fallback_tickers or [])

    def _current_tickers(self) -> list[str]:
        db = SessionLocal()
        try:
            items = crud.list_watchlist(db, active_only=True)
            tickers = [i.ticker for i in items]
        finally:
            db.close()
        if not tickers and self._fallback:
            log.info(f"[Scanner] DB 워치리스트 비어있음 — fallback 사용: {self._fallback}")
            return list(self._fallback)
        return tickers

    def scan_one(self, ticker: str) -> None:
        source = self.provider.source_of(ticker)
        try:
            if not self.provider.is_market_open(ticker):
                log.debug(f"[Scanner] {ticker} 장 마감 — 스킵")
                return

            df = self.provider.get_ohlcv(ticker, self.interval, self.period)
            signal = self.strategy.generate_signal(df, ticker)

            rsi = signal.indicators.get("rsi")
            rsi_str = f"{rsi:.1f}" if isinstance(rsi, float) and not math.isnan(rsi) else "N/A"
            log.info(
                f"[{source}] {ticker:12s} | "
                f"${signal.price:.4f} | "
                f"RSI={rsi_str} | "
                f"신호={signal.strength.value}"
            )
            self.alerter.process(signal, source)

        except Exception as e:
            log.error(f"[Scanner] {ticker} 오류: {e}")

    def scan_all(self) -> None:
        tickers = self._current_tickers()
        log.info(f"── 전체 스캔 시작 ({len(tickers)}개 종목) ──")
        for ticker in tickers:
            self.scan_one(ticker)

    def run(self, check_interval_min: int = 15) -> None:
        log.info(f"🚀 Scanner 시작 | 주기: {check_interval_min}분")
        self.scan_all()
        schedule.every(check_interval_min).minutes.do(self.scan_all)
        while True:
            schedule.run_pending()
            time.sleep(30)
