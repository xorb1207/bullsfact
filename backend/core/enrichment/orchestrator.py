"""
Orchestrator — Analyst들을 병렬 실행하고 Synthesizer로 묶어 EnrichmentContext 반환.

Day 1 구현 (StubEnricher):
  - 실제 LLM 호출 없이 stub Analyst만 실행
  - EnrichmentContext에 더미 perspectives 채워서 알람 포맷 검증용
  - 타임아웃/폴백/스레드풀 골격은 진짜처럼 갖춤 (Day 2~3에서 실제 LLM으로 교체만)
"""
import logging
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Optional

from .base import Analyst, Enricher
from .llm_client import BudgetExceeded
from .null_result import classify_null_result
from .synthesizer import Synthesizer
from .types import AnalystResult, EnrichmentContext, Perspective
from ..strategy.dip_buy import Signal, SignalStrength

log = logging.getLogger(__name__)


class StubEnricher(Enricher):
    """Day 1 stub — 실제 LLM 호출 없이 더미 컨텍스트 생성."""

    def __init__(
        self,
        analysts: list[Analyst],
        timeout_sec: float = 10.0,
        max_workers: int = 3,
    ):
        self.analysts = analysts
        self.timeout_sec = timeout_sec
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="enrich")

    def _run_analysts(self, signal: Signal, source: str) -> list[AnalystResult]:
        """모든 애널리스트 병렬 실행. 개별 실패는 잡고 빈 결과로."""
        futures = {
            self._executor.submit(a.analyze, signal, source): a.name
            for a in self.analysts
        }
        results: list[AnalystResult] = []
        for fut, name in futures.items():
            try:
                results.append(fut.result(timeout=self.timeout_sec))
            except FuturesTimeoutError:
                log.warning(f"[Orchestrator] {name} 타임아웃")
                results.append(AnalystResult(name=name, summary="", error="timeout"))
            except Exception as e:
                log.warning(f"[Orchestrator] {name} 실패: {e}")
                results.append(AnalystResult(name=name, summary="", error=str(e)))
        return results

    def _stub_perspectives(self, signal: Signal) -> dict[Perspective, str]:
        # Day 1 더미 — Day 3 Synthesizer가 LLM으로 진짜 코멘트 채움
        return {
            Perspective.SCALP: f"(stub) {signal.ticker} 단타 관점 코멘트 자리.",
            Perspective.SWING: f"(stub) {signal.ticker} 스윙 관점 코멘트 자리.",
            Perspective.LONG:  f"(stub) {signal.ticker} 장투 관점 코멘트 자리.",
        }

    def enrich(self, signal: Signal, source: str) -> Optional[EnrichmentContext]:
        t0 = time.monotonic()
        try:
            analyst_results = self._run_analysts(signal, source)
            elapsed_ms = int((time.monotonic() - t0) * 1000)

            citations: list[str] = []
            for r in analyst_results:
                citations.extend(r.citations)

            # 적어도 하나는 성공해야 의미 있음. 전부 실패면 None → 폴백
            if all(r.error for r in analyst_results):
                log.warning("[Orchestrator] 모든 애널리스트 실패 — None 반환")
                return None

            return EnrichmentContext(
                headline="(stub) 컨텍스트 헤드라인 자리",
                citations=citations,
                perspectives=self._stub_perspectives(signal),
                risk_flags=[],
                cost_cents=0.0,
                latency_ms=elapsed_ms,
            )
        except Exception as e:
            log.error(f"[Orchestrator] enrich 실패: {e}")
            return None


class LLMEnricher(Enricher):
    """
    실제 LLM 사용:
      1) Analyst들 병렬로 데이터 수집 (LLM 호출 X)
      2) Synthesizer가 단일 LLM 호출로 EnrichmentContext 생성
      3) 일일 비용 캡 초과 / LLM 실패 / JSON 파싱 실패 → None 반환 (raw 폴백)
    """

    def __init__(
        self,
        analysts: list[Analyst],
        synthesizer: Synthesizer,
        timeout_sec: float = 15.0,
        max_workers: int = 3,
    ):
        self.analysts = analysts
        self.synthesizer = synthesizer
        self.timeout_sec = timeout_sec
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="enrich")

    def _run_analysts(self, signal: Signal, source: str) -> list[AnalystResult]:
        futures = {
            self._executor.submit(a.analyze, signal, source): a.name
            for a in self.analysts
        }
        results: list[AnalystResult] = []
        for fut, name in futures.items():
            try:
                results.append(fut.result(timeout=self.timeout_sec))
            except FuturesTimeoutError:
                log.warning(f"[LLMEnricher] {name} 타임아웃")
                results.append(AnalystResult(name=name, summary="", error="timeout"))
            except Exception as e:
                log.warning(f"[LLMEnricher] {name} 실패: {e}")
                results.append(AnalystResult(name=name, summary="", error=str(e)))
        return results

    def enrich(self, signal: Signal, source: str) -> Optional[EnrichmentContext]:
        t0 = time.monotonic()
        try:
            results = self._run_analysts(signal, source)

            # 모든 애널리스트 데이터가 비어있을 때:
            #   - WEAK/threshold (= STRONG 아님) → 폴백 None (비용 가드)
            #   - STRONG → M3 null_result_classifier 호출, 한 줄 분류 컨텍스트로
            has_any = any((r.summary or r.citations) and not r.error for r in results)
            if not has_any:
                if signal.strength != SignalStrength.STRONG:
                    log.info("[LLMEnricher] 수집 데이터 없음 + non-STRONG → 호출 생략")
                    return None
                note = classify_null_result(self.synthesizer.llm, signal, results)
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                if not note:
                    log.info("[LLMEnricher] null_result 분류 실패 → 폴백")
                    return None
                log.info(f"[LLMEnricher] {signal.ticker} null_result 분류 발급 ({elapsed_ms}ms)")
                return EnrichmentContext(
                    headline="",
                    citations=[],
                    perspectives={},
                    risk_flags=[],
                    cost_cents=0.0,
                    latency_ms=elapsed_ms,
                    null_result_note=note,
                )

            ctx = self.synthesizer.synthesize(signal, source, results)
            ctx.latency_ms = int((time.monotonic() - t0) * 1000)
            log.info(
                f"[LLMEnricher] {signal.ticker} 완료 "
                f"({ctx.latency_ms}ms, ¢{ctx.cost_cents:.2f})"
            )
            return ctx
        except BudgetExceeded as e:
            log.warning(f"[LLMEnricher] 비용 캡 — 폴백: {e}")
            return None
        except Exception as e:
            log.error(f"[LLMEnricher] enrich 실패: {e}", exc_info=True)
            return None
