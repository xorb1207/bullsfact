"""Analyst / Enricher 추상 기반."""
from abc import ABC, abstractmethod
from typing import Optional

from ..strategy.dip_buy import Signal
from .types import AnalystResult, EnrichmentContext


class Analyst(ABC):
    """단일 애널리스트 (뉴스, 펀더멘털 등). LLM 1회 호출 단위."""
    name: str

    @abstractmethod
    def analyze(self, signal: Signal, source: str) -> AnalystResult:
        ...


class Enricher(ABC):
    """
    여러 Analyst를 조율 + Synthesizer로 묶어 EnrichmentContext 반환.
    실패/타임아웃 시 None 반환 (호출자가 raw 알람으로 폴백).
    """

    @abstractmethod
    def enrich(self, signal: Signal, source: str) -> Optional[EnrichmentContext]:
        ...
