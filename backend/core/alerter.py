"""
Alert Engine — 신호를 받아 Telegram 발송 + DB 로깅.
쿨다운은 메모리에서 관리 (재시작 시 초기화).
"""
import html
import logging
import math
import requests
from datetime import datetime
from typing import Optional

from .strategy.dip_buy import Signal, SignalStrength
from .enrichment import Enricher, EnrichmentContext, Perspective
from .threshold_alerts import (
    AlertEvaluation,
    METRIC_PRICE, METRIC_VIX, METRIC_FG_CNN, METRIC_FG_CRYPTO,
    REF_HIGH_252, REF_LOW_252, REF_EMA_50,
)
from .positions import MilestoneTrigger
from .money import format_money, format_money_signed, currency_for
from backend.db import SessionLocal, crud


def _esc(s: str) -> str:
    """Telegram HTML parse_mode에 안전한 텍스트로 이스케이프."""
    return html.escape(s, quote=False)

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


_PERSPECTIVE_LABEL = {
    Perspective.SCALP: "⚡ 단타",
    Perspective.SWING: "🎯 스윙",
    Perspective.LONG:  "🌱 장투",
}


def _format_enrichment(ctx: EnrichmentContext) -> str:
    parts = ["", "━━━ 컨텍스트 ━━━"]
    if ctx.headline:
        parts.append(f"📰 {_esc(ctx.headline)}")
    if ctx.risk_flags:
        parts.append(f"⚠️ {_esc(', '.join(ctx.risk_flags))}")
    for persp, label in _PERSPECTIVE_LABEL.items():
        text = ctx.perspectives.get(persp)
        if text:
            parts.append(f"\n{label}\n{_esc(text)}")
    if ctx.citations:
        # URL 자체는 escape하면 안 됨 (HTML attribute로 들어가니 quote만 escape)
        links = " ".join(
            f'<a href="{html.escape(u, quote=True)}">[{i+1}]</a>'
            for i, u in enumerate(ctx.citations)
        )
        parts.append(f"\n📎 {links}")
    return "\n".join(parts)


def _format_message(signal: Signal, source: str, enrichment: Optional[EnrichmentContext] = None) -> str:
    emoji = _STRENGTH_EMOJI[signal.strength]
    label = "강한 매수 신호" if signal.strength == SignalStrength.STRONG else "매수 신호"
    reasons_text = "\n".join(f"  • {_esc(r)}" for r in signal.reasons)
    ind = signal.indicators

    price_str = format_money(signal.price, signal.ticker)
    bb_lower = ind.get("bb_lower")
    bb_mid = ind.get("bb_mid")
    bb_lower_str = format_money(bb_lower, signal.ticker) if isinstance(bb_lower, float) and not math.isnan(bb_lower) else "N/A"
    bb_mid_str = format_money(bb_mid, signal.ticker) if isinstance(bb_mid, float) and not math.isnan(bb_mid) else "N/A"

    base = (
        f"{emoji} <b>{label} — {_esc(signal.ticker)}</b> [{_esc(source)}]\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 현재가: <b>{price_str}</b>\n"
        f"\n"
        f"📋 충족 조건:\n{reasons_text}\n"
        f"\n"
        f"📊 RSI:     <b>{_fmt(ind.get('rsi'), '.1f')}</b>\n"
        f"📉 BB 하단: <b>{bb_lower_str}</b>\n"
        f"📈 BB 중앙: {bb_mid_str}"
    )

    enrichment_text = _format_enrichment(enrichment) if enrichment else ""

    footer = (
        f"\n\n⏰ {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>⚠️ 참고용 알람. 투자 결정은 본인 판단으로.</i>"
    )

    return base + enrichment_text + footer


_PRIORITY_EMOJI = {"HIGH": "🔴", "MED": "🟡", "LOW": "🟢"}

_REF_LABEL = {
    REF_HIGH_252: "52주 고점",
    REF_LOW_252: "52주 저점",
    REF_EMA_50: "50일 EMA",
}

_METRIC_HEADER = {
    METRIC_PRICE:     "🎯 가격 레벨 도달",
    METRIC_VIX:       "⚠️ VIX 임계치 돌파",
    METRIC_FG_CNN:    "😱 CNN F&G 임계치 돌파",
    METRIC_FG_CRYPTO: "🪙 Crypto F&G 임계치 돌파",
}


def _fmt_price_compact(v: float, ticker: str = "") -> str:
    """티커가 주어지면 통화 자동 추정 (KRW/JPY/HKD/USD). 없으면 USD."""
    return format_money(v, ticker)


def _format_threshold_message(ev: AlertEvaluation) -> str:
    a = ev.alert
    arrow = "↓" if a.direction == "below" else "↑"
    pri = _PRIORITY_EMOJI.get(a.priority, "🟡")
    header = _METRIC_HEADER.get(a.metric_type, "🔔 알림")

    # 헤더 라인: "🎯 SOXL T1 도달 🔴 [HIGH]"
    title_parts = [header]
    if a.metric_type == METRIC_PRICE and a.ticker:
        title_parts = [f"🎯 <b>{_esc(a.ticker)}</b>"]
        if a.tier:
            title_parts.append(f"<b>{_esc(a.tier)}</b> 도달")
        else:
            title_parts.append("레벨 도달")
    title = " ".join(title_parts) + f"  {pri} [{a.priority}]"

    # 값 라인 (가격 알림은 ticker 통화로, 게이지는 무단위)
    ticker_for_fmt = a.ticker if a.metric_type == METRIC_PRICE else ""
    if a.metric_type == METRIC_PRICE:
        cur_str = _fmt_price_compact(ev.current_value, ticker_for_fmt)
        thr_str = _fmt_price_compact(ev.threshold, ticker_for_fmt)
    else:
        cur_str = f"{ev.current_value:.2f}"
        thr_str = f"{ev.threshold:.2f}"

    value_line = f"💰 {cur_str} {arrow} {thr_str}"

    # 상대값이면 기준점도 표시
    if a.ref_window and a.ref_pct is not None and ev.ref_value is not None:
        ref_label = _REF_LABEL.get(a.ref_window, a.ref_window)
        ref_str = _fmt_price_compact(ev.ref_value, ticker_for_fmt) if a.metric_type == METRIC_PRICE else f"{ev.ref_value:.2f}"
        pct_str = f"{a.ref_pct * 100:+.1f}%"
        value_line += f"\n└ {_esc(ref_label)} {ref_str} 대비 {pct_str}"
    elif a.abs_value is not None:
        value_line += "  <i>(절대값)</i>"

    # 노트
    note_line = f"\n\n📌 {_esc(a.note)}" if a.note else ""

    # M1: 이벤트 캘린더 컨텍스트 (있을 때만)
    cal_lines = ""
    if ev.calendar_contexts:
        cal_lines = "\n\n" + "\n".join(_esc(c) for c in ev.calendar_contexts)

    footer = (
        f"\n\n⏰ {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>알림 #{a.id} · 자동 비활성화됨</i>"
    )

    return (
        f"{title}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{value_line}"
        f"{note_line}"
        f"{cal_lines}"
        f"{footer}"
    )


def _format_milestone_message(t: MilestoneTrigger) -> str:
    p = t.position
    pnl = (t.current_price - p.avg_cost) * p.qty
    sell_qty_str = f"{t.suggested_sell_qty:.2f}".rstrip("0").rstrip(".")
    qty_str = f"{p.qty:.4f}".rstrip("0").rstrip(".")

    sell_line = (
        f"보유: {qty_str}주 → 매도 권장: ~{sell_qty_str}주 ({t.sell_ratio*100:.0f}%)"
        if t.sell_ratio > 0
        else f"보유: {qty_str}주 → 재량 매도 (공짜 칩 구간)"
    )

    return (
        f"💰 <b>익절 룰 트리거 — {_esc(p.ticker)}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📈 <b>{t.return_pct*100:+.1f}%</b>  "
        f"(현재 {_fmt_price_compact(t.current_price, p.ticker)} / 평단 {_fmt_price_compact(p.avg_cost, p.ticker)})\n"
        f"💵 평가손익: {format_money_signed(pnl, p.ticker)}\n\n"
        f"📌 {_esc(t.label)}\n"
        f"{sell_line}\n"
        f"누적 회수율: <b>{t.cumulative_recovery*100:.0f}%</b>\n\n"
        f"⏰ {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>마일스톤 자동 기록됨 · 다음 마일스톤만 감시</i>"
    )


class AlertEngine:

    def __init__(
        self,
        telegram_token: str,
        telegram_chat_id: str,
        cooldown_min: int = 60,
        log_to_db: bool = True,
        enricher: Optional[Enricher] = None,
    ):
        self._token = telegram_token
        self._chat_id = telegram_chat_id
        self._cooldown_min = cooldown_min
        self._log_to_db = log_to_db
        self._enricher = enricher
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
            if not resp.ok:
                # 본문만 로깅 (URL에 토큰이 박혀있어 e/__str__ 사용 금지)
                log.error(
                    f"Telegram 전송 실패 status={resp.status_code} body={resp.text[:300]}"
                )
                return False
            return True
        except requests.exceptions.RequestException as e:
            # 네트워크 오류 등 — 메시지에서 URL/토큰 마스킹
            log.error(f"Telegram 전송 네트워크 오류: {type(e).__name__}")
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

    def _maybe_enrich(self, signal: Signal, source: str) -> Optional[EnrichmentContext]:
        """STRONG일 때만 enrich. 실패/None은 호출자가 raw로 폴백."""
        if self._enricher is None:
            return None
        if signal.strength != SignalStrength.STRONG:
            return None
        try:
            return self._enricher.enrich(signal, source)
        except Exception as e:
            log.warning(f"[AlertEngine] enrich 실패 — raw 알람으로 폴백: {e}")
            return None

    def process(self, signal: Signal, source: str) -> bool:
        if signal.strength == SignalStrength.NONE:
            return False

        if self._is_on_cooldown(signal.ticker):
            log.info(f"[AlertEngine] {signal.ticker} 쿨다운 중 — 스킵")
            return False

        enrichment = self._maybe_enrich(signal, source)
        message = _format_message(signal, source, enrichment)
        sent = self._send_telegram(message)

        if sent:
            self._last_alert[signal.ticker] = datetime.utcnow()
            self._persist(signal, source)
            log.info(f"[AlertEngine] ✅ {signal.ticker} 알람 발송 ({signal.strength.value})")

        return sent

    def process_milestone(self, trigger: MilestoneTrigger) -> bool:
        """
        익절 마일스톤 발동 알림. PositionEvaluator가 이미 DB의 highest_milestone을
        갱신한 상태로 trigger를 반환했음 — 여기서는 발송만.
        """
        msg = _format_milestone_message(trigger)
        sent = self._send_telegram(msg)
        if sent:
            log.info(
                f"[AlertEngine] 💰 milestone {trigger.position.ticker} "
                f"+{trigger.return_pct*100:.1f}% (M={trigger.milestone}) 발송"
            )
        return sent

    def process_threshold(self, ev: AlertEvaluation) -> bool:
        """
        ThresholdAlert 평가 결과 처리. 트리거되면 발송 + DB에 비활성화 마킹.
        쿨다운 없음 — 알림 자체가 1회성 (재무장은 re_arm_after_h 또는 수동).
        """
        if not ev.triggered:
            return False
        msg = _format_threshold_message(ev)
        sent = self._send_telegram(msg)
        if sent:
            try:
                db = SessionLocal()
                try:
                    crud.mark_threshold_triggered(
                        db, ev.alert.id, last_value=ev.current_value
                    )
                finally:
                    db.close()
            except Exception as e:
                log.warning(f"[AlertEngine] threshold #{ev.alert.id} 마킹 실패: {e}")
            log.info(
                f"[AlertEngine] 🎯 threshold #{ev.alert.id} 발동 "
                f"({ev.alert.metric_type} {ev.alert.ticker or ''} "
                f"{ev.current_value:.4g} {ev.alert.direction} {ev.threshold:.4g})"
            )
        return sent
