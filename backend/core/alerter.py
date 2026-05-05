"""
Alert Engine — 신호를 받아 Telegram 발송 + DB 로깅.
쿨다운은 메모리에서 관리 (재시작 시 초기화).
"""
import logging
import math
import requests
from datetime import datetime
from typing import Optional

from .strategy.dip_buy import Signal, SignalStrength
from backend.db import SessionLocal, crud

log = logging.getLogger(__name__)


_STRENGTH_EMOJI = {
    SignalStrength.STRONG: "🔴",
    SignalStrength.WEAK:   "🟡",
    SignalStrength.NONE:   "⚪",
}


def _fmt(value: Optional[float], spec: str = ".4f", prefix: str = "") -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "N/A"
    return f"{prefix}{value:{spec}}"


def _format_message(signal: Signal, source: str) -> str:
    emoji = _STRENGTH_EMOJI[signal.strength]
    label = "강한 매수 신호" if signal.strength == SignalStrength.STRONG else "매수 신호"
    reasons_text = "\n".join(f"  • {r}" for r in signal.reasons)
    ind = signal.indicators

    return (
        f"{emoji} <b>{label} — {signal.ticker}</b> [{source}]\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 현재가: <b>${signal.price:.4f}</b>\n"
        f"\n"
        f"📋 충족 조건:\n{reasons_text}\n"
        f"\n"
        f"📊 RSI:     <b>{_fmt(ind.get('rsi'), '.1f')}</b>\n"
        f"📉 BB 하단: <b>{_fmt(ind.get('bb_lower'), '.4f', '$')}</b>\n"
        f"📈 BB 중앙: {_fmt(ind.get('bb_mid'), '.4f', '$')}\n"
        f"\n"
        f"⏰ {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>⚠️ 참고용 알람. 투자 결정은 본인 판단으로.</i>"
    )


class AlertEngine:

    def __init__(
        self,
        telegram_token: str,
        telegram_chat_id: str,
        cooldown_min: int = 60,
        log_to_db: bool = True,
    ):
        self._token = telegram_token
        self._chat_id = telegram_chat_id
        self._cooldown_min = cooldown_min
        self._log_to_db = log_to_db
        self._last_alert: dict[str, datetime] = {}

    def _is_on_cooldown(self, ticker: str) -> bool:
        last = self._last_alert.get(ticker)
        if last is None:
            return False
        elapsed = (datetime.utcnow() - last).total_seconds() / 60
        return elapsed < self._cooldown_min

    def _send_telegram(self, message: str) -> bool:
        if not self._token or "YOUR_" in self._token:
            print("\n" + "=" * 50 + "\n" + message + "\n" + "=" * 50)
            return True
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{self._token}/sendMessage",
                json={"chat_id": self._chat_id, "text": message, "parse_mode": "HTML"},
                timeout=10,
            )
            resp.raise_for_status()
            return True
        except Exception as e:
            log.error(f"Telegram 전송 실패: {e}")
            return False

    def _persist(self, signal: Signal, source: str) -> None:
        if not self._log_to_db:
            return
        ind = signal.indicators
        rsi = ind.get("rsi")
        bb_lower = ind.get("bb_lower")
        if isinstance(rsi, float) and math.isnan(rsi):
            rsi = None
        if isinstance(bb_lower, float) and math.isnan(bb_lower):
            bb_lower = None
        try:
            db = SessionLocal()
            try:
                crud.insert_alert(
                    db,
                    ticker=signal.ticker,
                    strength=signal.strength.value,
                    price=signal.price,
                    rsi=rsi,
                    bb_lower=bb_lower,
                    source=source,
                    reasons=signal.reasons,
                )
            finally:
                db.close()
        except Exception as e:
            log.warning(f"[AlertEngine] DB 로깅 실패: {e}")

    def process(self, signal: Signal, source: str) -> bool:
        if signal.strength == SignalStrength.NONE:
            return False

        if self._is_on_cooldown(signal.ticker):
            log.info(f"[AlertEngine] {signal.ticker} 쿨다운 중 — 스킵")
            return False

        message = _format_message(signal, source)
        sent = self._send_telegram(message)

        if sent:
            self._last_alert[signal.ticker] = datetime.utcnow()
            self._persist(signal, source)
            log.info(f"[AlertEngine] ✅ {signal.ticker} 알람 발송 ({signal.strength.value})")

        return sent
