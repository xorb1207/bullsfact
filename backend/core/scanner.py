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
from .threshold_alerts import ThresholdAlertEvaluator
from .positions import PositionEvaluator
from .market import MarketFetcher
from backend.db import SessionLocal, crud
from backend.db.models import User

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
        threshold_evaluator: Optional[ThresholdAlertEvaluator] = None,
        market_fetcher: Optional[MarketFetcher] = None,
        position_evaluator: Optional[PositionEvaluator] = None,
    ):
        self.provider = data_provider
        self.strategy = strategy
        self.alerter = alert_engine
        self.interval = interval
        self.period = period
        self._fallback = list(fallback_tickers or [])
        self.threshold_evaluator = threshold_evaluator
        self.market_fetcher = market_fetcher
        self.position_evaluator = position_evaluator

    def _gather_users_and_watchlists(self) -> tuple[list[User], dict[int, set[str]]]:
        """active 사용자 + 사용자별 watchlist ticker set."""
        db = SessionLocal()
        try:
            users = list(crud.list_users(db, active_only=True))
            user_wl: dict[int, set[str]] = {}
            for u in users:
                items = crud.list_watchlist(db, active_only=True, user_id=u.id)
                user_wl[u.id] = {i.ticker for i in items}
        finally:
            db.close()
        return users, user_wl

    def scan_one_for_users(
        self, ticker: str, users: list[User], user_wl: dict[int, set[str]],
    ) -> None:
        """ticker 1번 fetch → 모든 사용자별 평가/발송."""
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

            for user in users:
                interested = ticker in user_wl.get(user.id, set())

                # DipBuy: watchlist 에 들어있는 사용자에게만
                if interested:
                    try:
                        self.alerter.process(
                            signal, source,
                            target_chat_id=user.telegram_chat_id, user_id=user.id,
                        )
                    except Exception as e:
                        log.error(f"[Scanner] {ticker} dip user={user.id} 실패: {e}")

                # ThresholdAlert: 사용자별 룰 (watchlist 무관 — 본인이 등록한 것만)
                if self.threshold_evaluator is not None:
                    try:
                        evals = self.threshold_evaluator.evaluate_for_ticker(
                            ticker, df, signal.price, user_id=user.id,
                        )
                        for ev in evals:
                            self.alerter.process_threshold(
                                ev, target_chat_id=user.telegram_chat_id,
                            )
                    except Exception as e:
                        log.error(f"[Scanner] {ticker} threshold user={user.id} 실패: {e}")

                # Position 마일스톤: 사용자별 평단
                if self.position_evaluator is not None:
                    try:
                        trig = self.position_evaluator.evaluate(
                            ticker, signal.price, user_id=user.id,
                        )
                        if trig:
                            self.alerter.process_milestone(
                                trig, target_chat_id=user.telegram_chat_id,
                            )
                    except Exception as e:
                        log.error(f"[Scanner] {ticker} milestone user={user.id} 실패: {e}")

        except Exception as e:
            log.error(f"[Scanner] {ticker} 오류: {e}")

    def scan_all(self) -> None:
        users, user_wl = self._gather_users_and_watchlists()
        all_tickers = sorted({t for s in user_wl.values() for t in s})

        if not all_tickers and self._fallback:
            log.info(f"[Scanner] 사용자 watchlist 비어있음 — fallback: {self._fallback}")
            all_tickers = list(self._fallback)
            # fallback 의 경우 OWNER 만 알림 (있다면)
            owners = [u for u in users if u.tier == "OWNER"]
            users = owners or users
            user_wl = {u.id: set(all_tickers) for u in users}

        log.info(
            f"── 전체 스캔 시작 — 사용자 {len(users)}명, "
            f"unique ticker {len(all_tickers)}개 ──"
        )

        # 시간 경과한 알림 자동 재무장
        if self.threshold_evaluator is not None:
            try:
                db = SessionLocal()
                try:
                    n = crud.re_arm_due_alerts(db)
                    if n:
                        log.info(f"[Scanner] {n}개 알림 자동 재활성화")
                finally:
                    db.close()
            except Exception as e:
                log.warning(f"[Scanner] 재무장 실패: {e}")

        for ticker in all_tickers:
            self.scan_one_for_users(ticker, users, user_wl)

        # 시장 게이지 알림 (VIX / F&G) — 사이클당 1회 fetch, 사용자별 룰 평가
        if self.threshold_evaluator is not None and self.market_fetcher is not None:
            try:
                snap = self.market_fetcher.fetch()
                for user in users:
                    evals = self.threshold_evaluator.evaluate_market_gauges(
                        snap, user_id=user.id,
                    )
                    for ev in evals:
                        self.alerter.process_threshold(
                            ev, target_chat_id=user.telegram_chat_id,
                        )
            except Exception as e:
                log.error(f"[Scanner] 게이지 평가 실패: {e}")

    def run(self, check_interval_min: int = 15) -> None:
        log.info(f"🚀 Scanner 시작 | 주기: {check_interval_min}분")
        self.scan_all()
        schedule.every(check_interval_min).minutes.do(self.scan_all)
        while True:
            schedule.run_pending()
            time.sleep(30)
