"""
On-demand 매크로/티커 해설 — Telegram /why 명령용.

매크로 일일 브리핑(macro_briefing.py)이 "어제 왜 움직였나" 라면,
이 모듈은 사용자 트리거(/why TICKER, /why) 시점의 "지금 뭐가 일어나고 있나" 해설.

LLMClient.call_with_web_search 재사용. 비용은 일일 cap 안에서 자동 제한됨.
"""
from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from typing import Optional

import pandas as pd

from .enrichment.llm_client import LLMClient, BudgetExceeded
from .market import MarketSnapshot
from backend.db import SessionLocal, crud

log = logging.getLogger(__name__)


@dataclass
class ResearchResult:
    title: str                 # "SOXL — 왜 움직이는가" / "현재 시장 상황"
    text: str                  # Telegram HTML body
    citations: list[str]
    cost_usd: float


# ──────────────────────────────────────────────
# 시스템 프롬프트
# ──────────────────────────────────────────────

_SYSTEM_TICKER = """You are a macro/equity analyst answering an individual Korean retail investor's question
about why a specific ticker is moving recently.

Output rules:
- Korean, concise. Total under 700 characters.
- Telegram HTML safe: <b></b> only. No <p>/<ul>/etc.
- Structure exactly:
  • <b>최근 동향</b> — 1-2줄, 가격/변동폭 요약
  • <b>주요 원인 (가능성)</b> — 2-3 bullet, 각 1줄
  • <b>지금 주시할 포인트</b> — 1-2줄
- web_search 적극 활용. 최근 1-2주 뉴스/실적/매크로 이벤트 우선.
- 출처는 자동으로 붙으니 본문에 URL 적지 말 것.
- "투자 권유" 절대 금지. 사실/맥락 설명만.
- 종목이 마이너/생소하면 "충분한 정보 부족" 솔직히 말할 것."""

_SYSTEM_MACRO_NOW = """You are a macro market analyst giving a Korean retail investor a real-time read on
current market conditions.

Output rules:
- Korean, concise. Total under 700 characters.
- Telegram HTML safe: <b></b> only.
- Structure exactly:
  • <b>현재 분위기</b> — 1-2줄 (위험선호 vs 회피, 주도 섹터)
  • <b>핵심 매크로 상황</b> — 2-3 bullet
  • <b>가까운 변곡점</b> — 1-2줄 (다음 주요 이벤트/데이터)
- 일일 브리핑("어제")과 달리, **지금 이 시각** 의 현황과 임박 이벤트에 집중
- web_search로 최신 데이터 (24-48시간) 확인 우선
- AI/반도체, VIX, 국채금리, 빅테크 capex, Fed 정책 포커스
- 투자 권유 금지. 묘사+해설만."""


# ──────────────────────────────────────────────
# 컨텍스트 빌더
# ──────────────────────────────────────────────

def _ticker_context(
    ticker: str,
    df: pd.DataFrame,
    position_info: Optional[str] = None,
    snap: Optional[MarketSnapshot] = None,
) -> str:
    """LLM에 줄 컨텍스트 — 가격 흐름 + 보유 정보 + 매크로 스냅샷."""
    parts: list[str] = [f"종목: {ticker}"]

    if df is not None and not df.empty and "close" in df.columns:
        close = df["close"].dropna()
        if len(close) >= 2:
            cur = float(close.iloc[-1])
            prev = float(close.iloc[-2])
            week_ago = float(close.iloc[-min(5, len(close))])
            month_ago = float(close.iloc[-min(21, len(close))])
            ytd = float(close.iloc[0])
            high_252 = float(close.tail(252).max())
            low_252 = float(close.tail(252).min())

            parts.append(
                f"가격 데이터:\n"
                f"  현재: ${cur:.2f}\n"
                f"  1일: {(cur/prev - 1)*100:+.2f}%\n"
                f"  1주: {(cur/week_ago - 1)*100:+.2f}%\n"
                f"  1개월: {(cur/month_ago - 1)*100:+.2f}%\n"
                f"  데이터 시작 대비: {(cur/ytd - 1)*100:+.2f}%\n"
                f"  52주 고/저: ${high_252:.2f} / ${low_252:.2f}\n"
                f"  52주 고점 대비: {(cur/high_252 - 1)*100:+.2f}%"
            )

    if position_info:
        parts.append(f"사용자 보유 정보:\n  {position_info}")

    if snap:
        # VIX + F&G만 짧게
        vix = next((q.price for q in snap.indices if q.label == "VIX" and not q.error), None)
        fg_cnn = next((fg.score for fg in snap.sentiment if fg.source == "cnn"), None)
        macro = []
        if vix is not None:
            macro.append(f"VIX: {vix:.1f}")
        if fg_cnn is not None:
            macro.append(f"CNN F&G: {fg_cnn:.0f}")
        if macro:
            parts.append("매크로:\n  " + " | ".join(macro))

    return "\n\n".join(parts)


def _macro_context(snap: MarketSnapshot) -> str:
    """현재 시장 상황 컨텍스트 (macro_briefing._format_snapshot_context와 유사하나 압축)."""
    parts: list[str] = []

    idx = []
    for q in snap.indices:
        if not q.error:
            idx.append(f"{q.label}: {q.price:.2f} ({q.change_pct:+.2f}%)")
    if idx:
        parts.append("지수: " + " | ".join(idx))

    bonds = []
    for q in snap.bonds:
        if not q.error:
            bonds.append(f"{q.label}: {q.price:.3f}")
    if bonds:
        parts.append("국채: " + " | ".join(bonds))

    if snap.sentiment:
        s = " | ".join(f"{fg.source}:{fg.score:.0f}({fg.rating})" for fg in snap.sentiment)
        parts.append("F&G: " + s)

    if snap.commodities:
        com = []
        for q in snap.commodities:
            if not q.error:
                com.append(f"{q.label}:{q.price:.2f} ({q.change_pct:+.2f}%)")
        if com:
            parts.append("원자재: " + " | ".join(com))

    return "\n".join(parts)


# ──────────────────────────────────────────────
# LLM 호출
# ──────────────────────────────────────────────

def _position_summary(ticker: str) -> Optional[str]:
    """DB에서 해당 ticker 포지션이 있으면 요약 문자열 반환."""
    try:
        db = SessionLocal()
        try:
            p = crud.get_position(db, ticker)
            if not p:
                return None
            return (
                f"보유 {p.qty}주, 평단 ${p.avg_cost:.2f}, "
                f"마지막 발동 마일스톤 +{p.highest_milestone*100:.0f}%"
            )
        finally:
            db.close()
    except Exception:
        return None


def research_ticker(
    llm: LLMClient,
    ticker: str,
    df: Optional[pd.DataFrame],
    snap: Optional[MarketSnapshot] = None,
    *,
    model: str = "claude-sonnet-4-6",
    max_searches: int = 5,
) -> Optional[ResearchResult]:
    """
    /why TICKER — 특정 종목이 왜 움직이는지 해설.
    df: 일봉 OHLCV (최근 1-3개월). None 가능 (가격 컨텍스트 없이도 동작).
    """
    pos_info = _position_summary(ticker)
    ctx = _ticker_context(ticker, df, pos_info, snap)

    user = (
        f"{ticker} 종목이 최근 왜 움직이고 있는지 해설해주세요.\n\n"
        f"보조 컨텍스트:\n{ctx}\n\n"
        "최신 뉴스/실적/매크로 이벤트를 web_search로 확인하세요. "
        "1-2주 시계 기준."
    )

    try:
        text, citations, usage = llm.call_with_web_search(
            model=model,
            system=_SYSTEM_TICKER,
            user=user,
            max_tokens=1200,
            max_searches=max_searches,
            purpose="why_ticker",
            ticker=ticker,
        )
    except BudgetExceeded as e:
        log.warning(f"[OnDemand] 예산 초과: {e}")
        return None
    except Exception as e:
        log.error(f"[OnDemand] /why {ticker} LLM 실패: {type(e).__name__}: {e}")
        return None

    text = text.strip()
    if not text:
        return None
    return ResearchResult(
        title=f"{ticker} — 왜 움직이는가",
        text=text,
        citations=citations,
        cost_usd=usage.cost_usd(),
    )


def research_macro_now(
    llm: LLMClient,
    snap: MarketSnapshot,
    *,
    model: str = "claude-sonnet-4-6",
    max_searches: int = 5,
) -> Optional[ResearchResult]:
    """/why (인자 없음) — 현재 시장 상황 해설."""
    ctx = _macro_context(snap)
    user = (
        "지금 시장 상황을 해설해주세요. 다음 24-48시간 시계.\n\n"
        f"현재 스냅샷:\n{ctx}\n\n"
        "최신 뉴스/매크로 이벤트를 web_search로 확인하세요. "
        "임박한 데이터/이벤트 우선."
    )

    try:
        text, citations, usage = llm.call_with_web_search(
            model=model,
            system=_SYSTEM_MACRO_NOW,
            user=user,
            max_tokens=1200,
            max_searches=max_searches,
            purpose="why_macro",
        )
    except BudgetExceeded as e:
        log.warning(f"[OnDemand] 예산 초과: {e}")
        return None
    except Exception as e:
        log.error(f"[OnDemand] /why (macro) LLM 실패: {type(e).__name__}: {e}")
        return None

    text = text.strip()
    if not text:
        return None
    return ResearchResult(
        title="현재 시장 상황",
        text=text,
        citations=citations,
        cost_usd=usage.cost_usd(),
    )


# ──────────────────────────────────────────────
# Telegram 포맷
# ──────────────────────────────────────────────

def format_for_telegram(r: ResearchResult, max_citations: int = 5) -> str:
    parts = [
        f"🔍 <b>{html.escape(r.title)}</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        r.text,
    ]
    if r.citations:
        urls = r.citations[:max_citations]
        links = " ".join(
            f'<a href="{html.escape(u, quote=True)}">[{i+1}]</a>'
            for i, u in enumerate(urls)
        )
        parts.append(f"\n📎 출처: {links}")
    parts.append(f"\n<i>비용 ${r.cost_usd:.4f}</i>")
    return "\n".join(parts)
