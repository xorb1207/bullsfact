"""
포트폴리오 자유 형식 입력 LLM 파싱.

가족이 자기 증권사 화면 보면서 자유롭게 입력하면 → 구조화된 (ticker, qty, avg_cost) 추출.

예시 입력:
    삼성전자 10주 평단 70000
    SOXL 23주 21.47
    TQQQ 155 19.87
    NVDA 50주 평균 $10.38

LLM 한 번 호출 (~$0.005). Haiku 사용 (단순 추출 작업).
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

from .enrichment.llm_client import LLMClient, BudgetExceeded

log = logging.getLogger(__name__)


_SYSTEM = """You are a portfolio import parser for a Korean retail investor.
Extract holdings from free-form text and output ONLY valid JSON.

Schema:
{"positions": [{"ticker": "...", "qty": <number>, "avg_cost": <number>, "note": "..."}], "warnings": ["..."]}

Rules:
- ticker: Use proper market suffix.
  - Korean stocks: append .KS for KOSPI (e.g., 삼성전자 → 005930.KS, SK하이닉스 → 000660.KS, 카카오 → 035720.KS, NAVER → 035420.KS).
  - Korean stocks KOSDAQ: append .KQ if known.
  - US stocks/ETFs: ticker as-is (e.g., NVDA, SOXL, TQQQ, QQQ).
  - Crypto: BTC/USDT format if Binance, BTC-USD if Yahoo.
- qty: numeric. "10주" → 10.
- avg_cost: numeric in native currency. "70,000원" → 70000. "$21.47" → 21.47.
- If a number could be qty or price, prefer the smaller one as qty for stocks (1-10000 range typically qty).
- If you're unsure about ticker mapping, add a warning string and skip that line.

CRITICAL: Output ONLY the JSON object. No markdown code fences, no preamble, no commentary."""


@dataclass
class ParsedPosition:
    ticker: str
    qty: float
    avg_cost: float
    note: Optional[str] = None


@dataclass
class ParseResult:
    positions: list[ParsedPosition]
    warnings: list[str]
    cost_usd: float


def parse_free_form(llm: LLMClient, text: str, user_id: Optional[int] = None) -> Optional[ParseResult]:
    """
    자유 형식 텍스트 → ParsedPosition 리스트.
    실패는 None (호출자가 에러 메시지 제공).
    """
    if not text or not text.strip():
        return None
    if not llm:
        return None

    model = os.getenv("LLM_MODEL_FILING", "claude-haiku-4-5")  # Haiku 적합
    try:
        out, usage = llm.call(
            model=model,
            system=_SYSTEM,
            user=text.strip(),
            max_tokens=1500,
            purpose="portfolio_import",
            user_id=user_id,
        )
    except BudgetExceeded as e:
        log.warning(f"[PortfolioParser] 예산 초과: {e}")
        return None
    except Exception as e:
        log.error(f"[PortfolioParser] LLM 실패: {type(e).__name__}: {e}")
        return None

    # JSON 추출 (LLM이 코드 펜스 둘렀어도 복구)
    cleaned = out.strip()
    m = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if not m:
        log.warning(f"[PortfolioParser] JSON 미발견: {cleaned[:200]}")
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        log.warning(f"[PortfolioParser] JSON 파싱 실패: {e}; raw={cleaned[:200]}")
        return None

    positions: list[ParsedPosition] = []
    for row in data.get("positions", []) or []:
        try:
            ticker = str(row.get("ticker", "")).strip().upper()
            qty = float(row.get("qty", 0))
            avg = float(row.get("avg_cost", 0))
            if not ticker or qty <= 0 or avg <= 0:
                continue
            positions.append(ParsedPosition(
                ticker=ticker, qty=qty, avg_cost=avg,
                note=row.get("note"),
            ))
        except (ValueError, TypeError) as e:
            log.debug(f"[PortfolioParser] row 파싱 실패: {row}: {e}")
            continue

    warnings = [str(w) for w in (data.get("warnings") or [])]
    return ParseResult(
        positions=positions,
        warnings=warnings,
        cost_usd=usage.cost_usd(),
    )
