"""
M3 null_result_classifier dry-run / mock 테스트.

실제 Anthropic API 호출 없이 LLMClient.call 을 monkeypatch 해서
파이프라인 경로(빈 애널리스트 → 분류 한 줄 → EnrichmentContext.null_result_note →
alerter 포맷)를 끝까지 검증.

사용:
    python -m backend.scripts.test_null_result
"""
from __future__ import annotations

import logging
import sys
import threading
from datetime import date

from backend.core.alerter import _format_enrichment, _format_message
from backend.core.enrichment import (
    AnalystResult,
    Analyst,
    LLMClient,
    LLMEnricher,
    Synthesizer,
)
from backend.core.enrichment.llm_client import CallUsage
from backend.core.enrichment.null_result import PURPOSE
from backend.core.strategy.dip_buy import Signal, SignalStrength

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("test_null_result")


# ── 헬퍼 ───────────────────────────────────────────

class _EmptyAnalyst(Analyst):
    def __init__(self, name: str, fail: bool = False):
        self.name = name
        self._fail = fail

    def analyze(self, signal, source):
        if self._fail:
            return AnalystResult(name=self.name, summary="", error="timeout")
        return AnalystResult(name=self.name, summary="", citations=[])


class _DataAnalyst(Analyst):
    def __init__(self, name: str, summary: str):
        self.name = name
        self._summary = summary

    def analyze(self, signal, source):
        return AnalystResult(name=self.name, summary=self._summary, citations=[])


def _make_mock_llm(response_text: str) -> tuple[LLMClient, list[dict]]:
    """LLMClient 인스턴스를 만들고 .call 만 monkeypatch (API 호출 차단)."""
    llm = LLMClient(api_key="fake-key", max_daily_usd=10.0)
    seen: list[dict] = []

    def fake_call(*, model, system, user, max_tokens, cache_system, purpose,
                  ticker=None, user_id=None):
        seen.append({"model": model, "purpose": purpose, "ticker": ticker})
        usage = CallUsage(
            model=model,
            input_tokens=120,
            output_tokens=12,
            purpose=purpose,
            ticker=ticker,
            user_id=user_id,
            latency_ms=42,
        )
        return response_text, usage

    # bound method 자리에 callable 주입 (positional/keyword 둘 다 지원해야 안전)
    def _adapter(model, system, user, max_tokens=1024, cache_system=True,
                 purpose="", ticker=None, user_id=None):
        return fake_call(
            model=model, system=system, user=user, max_tokens=max_tokens,
            cache_system=cache_system, purpose=purpose, ticker=ticker, user_id=user_id,
        )

    llm.call = _adapter  # type: ignore[assignment]
    return llm, seen


def _strong_signal(ticker: str = "SOXL") -> Signal:
    return Signal(
        ticker=ticker,
        strength=SignalStrength.STRONG,
        price=10.50,
        reasons=["RSI=28.5 < 35", "가격 $10.50 < BB하단 $11.00"],
        indicators={"rsi": 28.5, "bb_lower": 11.0, "bb_mid": 12.0, "bb_upper": 13.0},
    )


def _weak_signal(ticker: str = "SOXL") -> Signal:
    return Signal(
        ticker=ticker,
        strength=SignalStrength.WEAK,
        price=10.50,
        reasons=["RSI=28.5 < 35"],
        indicators={"rsi": 28.5, "bb_lower": 11.0, "bb_mid": 12.0, "bb_upper": 13.0},
    )


# ── 케이스 ──────────────────────────────────────────

def case_strong_all_empty() -> None:
    log.info("=== Case 1: STRONG + 모든 애널리스트 empty → null_result_note 발급 ===")
    llm, seen = _make_mock_llm("특이사항 없음 / 원인 불명 수급 가능성")
    enricher = LLMEnricher(
        analysts=[_EmptyAnalyst("news"), _EmptyAnalyst("fundamentals", fail=True)],
        synthesizer=Synthesizer(llm=llm, model="claude-haiku-4-5"),
        timeout_sec=5.0,
    )
    sig = _strong_signal()
    ctx = enricher.enrich(sig, "yfinance")

    assert ctx is not None, "STRONG + null_result 분류 성공 시 EnrichmentContext 기대"
    assert ctx.null_result_note, f"null_result_note 비어 있음: {ctx!r}"
    assert "📋 점검 완료" in ctx.null_result_note
    assert "원인 불명 수급 가능성" in ctx.null_result_note
    assert ctx.perspectives == {}, "null_result 케이스에서 perspectives 비어야 함"
    assert ctx.headline == ""

    assert any(c["purpose"] == PURPOSE for c in seen), (
        f"null_result_classifier 호출 안 됨 (seen={seen!r})"
    )
    log.info(f"  → note: {ctx.null_result_note}")
    log.info("  ✓ 통과")


def case_weak_all_empty_skips_llm() -> None:
    log.info("=== Case 2: WEAK + 모든 애널리스트 empty → LLM 호출 X, None 반환 ===")
    llm, seen = _make_mock_llm("이러면 호출 안 돼야 함")
    enricher = LLMEnricher(
        analysts=[_EmptyAnalyst("news"), _EmptyAnalyst("fundamentals")],
        synthesizer=Synthesizer(llm=llm, model="claude-haiku-4-5"),
    )
    ctx = enricher.enrich(_weak_signal(), "yfinance")

    assert ctx is None, f"WEAK + 빈 데이터면 None 기대, got {ctx!r}"
    assert seen == [], f"WEAK 에서 LLM 호출 발생: {seen!r}"
    log.info("  ✓ 통과 (LLM 미호출)")


def case_strong_has_data_goes_synth() -> None:
    log.info("=== Case 3: STRONG + 데이터 존재 → Synthesizer 정상 경로 (null_result 미발급) ===")
    # Synthesizer 가 부르는 LLM이 JSON 을 돌려주도록 mock
    llm, seen = _make_mock_llm(
        '{"headline":"실적 깜짝","risk_flags":[],"perspectives":{"scalp":"a","swing":"b","long":"c"}}'
    )
    enricher = LLMEnricher(
        analysts=[_DataAnalyst("news", "SOXL: 헤드라인 A; 헤드라인 B")],
        synthesizer=Synthesizer(llm=llm, model="claude-haiku-4-5"),
    )
    ctx = enricher.enrich(_strong_signal(), "yfinance")

    assert ctx is not None
    assert ctx.null_result_note is None, (
        f"데이터 있을 때 null_result_note 발급되면 안 됨: {ctx!r}"
    )
    assert any(c["purpose"] == "synthesizer" for c in seen), seen
    assert not any(c["purpose"] == PURPOSE for c in seen), (
        f"데이터 있을 때 null_result_classifier 호출됨: {seen!r}"
    )
    log.info(f"  → headline={ctx.headline!r}, persp={list(ctx.perspectives.keys())}")
    log.info("  ✓ 통과")


def case_alerter_format_renders_note() -> None:
    log.info("=== Case 4: alerter._format_message 가 null_result_note 라인 포함 ===")
    from backend.core.enrichment import EnrichmentContext
    ctx = EnrichmentContext(
        headline="",
        citations=[],
        perspectives={},
        risk_flags=[],
        cost_cents=0.0,
        latency_ms=10,
        null_result_note="📋 점검 완료: Finnhub/Yahoo 뉴스, yfinance 펀더멘털/실적 캘린더 → 원인 불명 수급 가능성",
    )
    msg = _format_message(_strong_signal(), "yfinance", ctx)
    assert "━━━ 컨텍스트 ━━━" in msg
    assert "📋 점검 완료" in msg, f"알림에 점검 라인 누락:\n{msg}"
    assert "원인 불명 수급 가능성" in msg
    # 기존 포맷 보존 확인
    assert "강한 매수 신호" in msg
    assert "📊 RSI" in msg
    assert "📉 BB 하단" in msg
    log.info("  ✓ 통과 (기존 포맷 보존 + 새 라인 삽입)")
    print("\n──── 샘플 알림 메시지 ────\n" + msg + "\n──────────────────────────\n")


def case_llm_empty_response_falls_back() -> None:
    log.info("=== Case 5: LLM 응답 empty → None 폴백 ===")
    llm, _ = _make_mock_llm("")
    enricher = LLMEnricher(
        analysts=[_EmptyAnalyst("news")],
        synthesizer=Synthesizer(llm=llm, model="claude-haiku-4-5"),
    )
    # cache 우회를 위해 ticker 변경 (case 1 과 다른 ticker)
    sig = _strong_signal(ticker="TQQQ")
    ctx = enricher.enrich(sig, "yfinance")
    assert ctx is None, f"빈 응답이면 None 기대, got {ctx!r}"
    log.info("  ✓ 통과")


def main() -> int:
    try:
        case_strong_all_empty()
        case_weak_all_empty_skips_llm()
        case_strong_has_data_goes_synth()
        case_alerter_format_renders_note()
        case_llm_empty_response_falls_back()
    except AssertionError as e:
        log.error(f"❌ 테스트 실패: {e}")
        return 1
    except Exception as e:
        log.exception(f"❌ 예외: {type(e).__name__}: {e}")
        return 1
    log.info("✅ 모든 케이스 통과")
    return 0


if __name__ == "__main__":
    sys.exit(main())
