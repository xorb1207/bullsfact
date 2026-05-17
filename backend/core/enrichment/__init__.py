"""
Enrichment 레이어 — STRONG 시그널에 LLM 기반 컨텍스트(뉴스/펀더멘털/3관점 코멘트) 부착.

Day 1: 골격 + stub. 실제 LLM 호출은 Day 2~3.
"""
from .types import EnrichmentContext, AnalystResult, Perspective
from .base import Analyst, Enricher
from .orchestrator import StubEnricher, LLMEnricher
from .llm_client import LLMClient, BudgetExceeded
from .synthesizer import Synthesizer
from .null_result import classify_null_result

__all__ = [
    "EnrichmentContext",
    "AnalystResult",
    "Perspective",
    "Analyst",
    "Enricher",
    "StubEnricher",
    "LLMEnricher",
    "LLMClient",
    "BudgetExceeded",
    "Synthesizer",
    "classify_null_result",
]
