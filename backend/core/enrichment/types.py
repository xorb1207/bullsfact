"""Enrichment 도메인 타입."""
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Perspective(Enum):
    SCALP = "scalp"     # 분~시간, 당일 청산
    SWING = "swing"     # 3~10일
    LONG  = "long"      # 1개월+


@dataclass
class AnalystResult:
    """애널리스트 1개의 출력 (뉴스 헤드라인, 펀더멘털 요약 등)."""
    name: str                          # "news", "fundamentals"
    summary: str                       # 한국어 요약 텍스트
    citations: list[str] = field(default_factory=list)  # 출처 URL
    error: Optional[str] = None        # 호출 실패 시 사유


@dataclass
class EnrichmentContext:
    """Synthesizer가 만든 최종 알람 컨텍스트. Telegram 메시지 본문에 들어감."""
    headline: str                                        # 한 줄 요약 (뉴스 핵심)
    citations: list[str] = field(default_factory=list)   # 출처 모음
    perspectives: dict[Perspective, str] = field(default_factory=dict)
    risk_flags: list[str] = field(default_factory=list)  # ["earnings_d3", "fomc_tomorrow"]
    cost_cents: float = 0.0
    latency_ms: int = 0
