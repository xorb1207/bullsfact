"""
매크로 해설 생성기 — 어제 시장 움직임을 LLM + web_search로 해설.

일일 브리핑(06:00 KST)에 통합. 사용자의 가장 큰 needs인 "왜 움직였는가"에 답하기 위함.
실패는 조용히 None 반환 (브리핑 자체는 살려야 함).
"""
from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from typing import Optional

from datetime import datetime, timezone, timedelta

from .enrichment.llm_client import LLMClient, BudgetExceeded
from .market import MarketSnapshot
from backend.db import SessionLocal, crud


_CACHE_TTL_BRIEFING_SEC = 24 * 60 * 60     # 24h


def _briefing_cache_key() -> str:
    """KST 기준 날짜 단위 — 같은 날 N명 사용자에게 같은 본문."""
    kst = datetime.now(timezone.utc) + timedelta(hours=9)
    return kst.strftime("%Y-%m-%d")

log = logging.getLogger(__name__)


@dataclass
class MacroBriefing:
    text: str                  # Telegram HTML 본문 (제목 제외)
    citations: list[str]       # web_search 출처 URL
    cost_usd: float            # 이번 호출 비용 (모니터링용)


_SYSTEM = """You are a macro market analyst writing for a Korean retail investor focused on AI/semiconductor stocks.

CRITICAL: Output ONLY the final answer. No meta-commentary, no thinking aloud.
Start IMMEDIATELY with "<b>핵심 움직임</b>" — no preamble.

Output rules:
- Korean, very concise. Total under 800 characters of body text.
- Telegram HTML safe: use <b></b> for emphasis only. No <p>, <ul>, etc.
- Structure exactly:
  • <b>핵심 움직임</b> — 3개 bullet (각 1줄, "• " prefix)
  • <b>가능한 원인</b> — 1-2 bullet
  • <b>주시 포인트</b> — 1줄
- Cite via web_search for any claim about news/data. Do NOT inline URLs in prose — citations are attached automatically.
- Focus: AI/반도체 (NVDA, AMD, AVGO, SOXL, TQQQ), VIX, 채권금리, 빅테크 capex, 매크로 데이터(CPI/실업률/Fed).
- If no notable moves, say so honestly in 1-2 lines instead of fabricating.
- No financial advice, no price predictions. Describe what happened and why."""


def _format_snapshot_context(snap: MarketSnapshot) -> str:
    """LLM에 줄 컨텍스트 — 현재 스냅샷 핵심만 텍스트로."""
    parts: list[str] = []

    # 지수 + VIX
    idx_lines = []
    for q in snap.indices:
        if q.error:
            continue
        idx_lines.append(f"  {q.label}: {q.price:.2f} ({q.change_pct:+.2f}%)")
    if idx_lines:
        parts.append("주요 지수:\n" + "\n".join(idx_lines))

    # 채권/수익률
    if snap.bonds:
        bond_lines = [f"  {q.label}: {q.price:.3f}" for q in snap.bonds if not q.error]
        if bond_lines:
            parts.append("국채금리:\n" + "\n".join(bond_lines))
    if snap.yield_curve_2y10y is not None:
        parts.append(f"수익률 곡선 (10Y-2Y): {snap.yield_curve_2y10y:+.3f}%p")

    # 원자재 / DXY
    if snap.commodities:
        com_lines = [f"  {q.label}: {q.price:.2f} ({q.change_pct:+.2f}%)"
                     for q in snap.commodities if not q.error]
        if com_lines:
            parts.append("원자재/DXY:\n" + "\n".join(com_lines))

    # 크립토
    if snap.crypto:
        cr_lines = [f"  {q.label}: ${q.price:,.0f} ({q.change_pct:+.2f}%)"
                    for q in snap.crypto if not q.error]
        if cr_lines:
            parts.append("크립토:\n" + "\n".join(cr_lines))

    # 심리지수
    if snap.sentiment:
        s_lines = [f"  {fg.source}: {fg.score:.0f} ({fg.rating})" for fg in snap.sentiment]
        parts.append("Fear & Greed:\n" + "\n".join(s_lines))

    return "\n\n".join(parts)


def _default_briefing_model() -> str:
    import os
    return os.getenv("LLM_MODEL_BRIEFING", "claude-sonnet-4-6")


def generate_daily_recap(
    llm: LLMClient,
    snap: MarketSnapshot,
    *,
    model: Optional[str] = None,
    max_searches: int = 5,
    user_id: Optional[int] = None,
    use_cache: bool = True,
) -> Optional[MacroBriefing]:
    """
    일일 매크로 해설 생성. 실패는 None.

    실제 검색 비용 (~$0.05/call @ 5 searches) + LLM 토큰 비용 발생.
    하루 1번 호출 → 캐시 24h. 멀티유저 시 같은 날 N명에게 같은 본문 발송 → 비용 ÷N.
    """
    if not llm:
        return None

    cache_key = _briefing_cache_key()
    if use_cache:
        try:
            db = SessionLocal()
            try:
                row = crud.get_llm_cache(db, purpose="macro_briefing", cache_key=cache_key)
                if row:
                    log.info(f"[MacroBriefing] cache hit: {cache_key}")
                    data = row.result_text or {}
                    return MacroBriefing(
                        text=data.get("text", ""),
                        citations=data.get("citations", []) or [],
                        cost_usd=0.0,
                    )
            finally:
                db.close()
        except Exception as e:
            log.debug(f"[MacroBriefing] cache get 실패 (무시): {type(e).__name__}: {e}")

    snapshot_text = _format_snapshot_context(snap)
    effective_model = model or _default_briefing_model()
    user = (
        "어제~오늘 미국 증시 주요 움직임을 해설해주세요.\n"
        "현재 시장 스냅샷 (보조 컨텍스트):\n"
        f"{snapshot_text}\n\n"
        "최신 뉴스/데이터를 web_search로 확인해서 답변하세요. "
        "스냅샷의 숫자는 참고용이며, 정확한 일자/원인은 검색으로 확인 우선."
    )

    try:
        text, citations, usage = llm.call_with_web_search(
            model=effective_model,
            system=_SYSTEM,
            user=user,
            max_tokens=1500,
            max_searches=max_searches,
            purpose="macro_briefing",
            user_id=user_id,
        )
    except BudgetExceeded as e:
        log.warning(f"[MacroBriefing] 예산 초과 — 스킵: {e}")
        return None
    except Exception as e:
        log.error(f"[MacroBriefing] LLM 호출 실패: {type(e).__name__}: {e}")
        return None

    text = text.strip()
    if not text:
        log.warning("[MacroBriefing] 빈 응답")
        return None

    result = MacroBriefing(text=text, citations=citations, cost_usd=usage.cost_usd())

    if use_cache:
        try:
            db = SessionLocal()
            try:
                crud.put_llm_cache(
                    db, purpose="macro_briefing", cache_key=cache_key,
                    result_text={"text": text, "citations": citations},
                    cost_usd=usage.cost_usd(),
                    ttl_seconds=_CACHE_TTL_BRIEFING_SEC,
                )
            finally:
                db.close()
        except Exception as e:
            log.debug(f"[MacroBriefing] cache put 실패 (무시): {type(e).__name__}: {e}")

    return result


def format_for_telegram(briefing: MacroBriefing, max_citations: int = 5) -> str:
    """Telegram HTML 포맷. 본문 + 출처 링크."""
    parts = [
        "📰 <b>어제 시장 해설</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        briefing.text,
    ]
    if briefing.citations:
        urls = briefing.citations[:max_citations]
        links = " ".join(
            f'<a href="{html.escape(u, quote=True)}">[{i+1}]</a>'
            for i, u in enumerate(urls)
        )
        parts.append(f"\n📎 출처: {links}")
    return "\n".join(parts)
