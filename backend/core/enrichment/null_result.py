"""
Null-result classifier (M3) — STRONG 시그널에서 모든 애널리스트가 빈 결과를 반환했을 때,
"이유 없음" 단정 대신 "점검 범위 + 가능성" 한 줄을 LLM으로 분류해 발급.

핵심 의도:
  - False Negative 방지: 데이터 없음 ≠ 원인 없음. 단정하지 않고 가능성으로만.
  - 점검 범위 명시: 어떤 소스를 봤는지 알람에 노출 → 사용자가 추가 조사 여부 판단 가능.
  - 비용 가드: Haiku + plain call (web_search X), llm_cache로 ticker 단위 dedup.
              LLMClient.on_call 가 llm_call_log 에 자동 기록.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from .llm_client import LLMClient, BudgetExceeded
from .types import AnalystResult
from ..strategy.dip_buy import Signal

log = logging.getLogger(__name__)


PURPOSE = "null_result_classifier"
_CACHE_TTL_SEC = 6 * 3600       # 6h — 같은 종목 STRONG 반복 발동 시 재호출 방지
_DEFAULT_MODEL = "claude-haiku-4-5"
_MAX_TOKENS = 80


# 애널리스트 식별자 → 사용자 친화 라벨. 신규 애널리스트 추가 시 여기에 라벨 추가.
_ANALYST_LABEL: dict[str, str] = {
    "news":         "Finnhub/Yahoo 뉴스",
    "fundamentals": "yfinance 펀더멘털/실적 캘린더",
    "sec_filings":  "SEC 8-K",
    "dart":         "DART 공시",
}


SYSTEM_PROMPT = """당신은 트레이딩 알람의 "원인 분류 보조자"입니다.
STRONG 매수 시그널 (RSI 과매도 + 볼린저 밴드 하단)이 발생했는데,
자동 수집된 뉴스/공시/펀더멘털 데이터가 모두 비어 있는 케이스입니다.

당신의 임무: "이유 없음"이라고 단정하지 말고, 가능한 분류 한 가지만 짧게 제시.

엄격한 규칙:
1. 출력은 한국어 한 줄 (10~25자). 마침표/따옴표/이모지 없음.
2. 다음 중 한 가지 톤만 허용 (그대로 또는 약간 변형):
   - "특이사항 없음 / 원인 불명 수급 가능성"
   - "광범위 매크로 영향 추정"
   - "데이터 수집 부족 — 추가 점검 권장"
3. "사세요/파세요" 등 권유 금지.
4. 분류 문자열 한 줄만 출력. 설명/서두/이유 X."""


def _cache_key(ticker: str) -> str:
    """6시간 윈도 버킷 — 같은 ticker × 같은 6h 윈도면 캐시 히트."""
    now = datetime.utcnow()
    bucket_hour = (now.hour // 6) * 6
    return f"{ticker.upper()}|{now.strftime('%Y%m%d')}-{bucket_hour:02d}"


def _checked_label(results: list[AnalystResult]) -> str:
    """애널리스트 결과들 → 점검 소스 라벨 문자열."""
    if not results:
        return "(점검 소스 없음)"
    seen: list[str] = []
    for r in results:
        lbl = _ANALYST_LABEL.get(r.name, r.name)
        if lbl not in seen:
            seen.append(lbl)
    return ", ".join(seen)


def _build_user_prompt(signal: Signal, results: list[AnalystResult]) -> str:
    ind = signal.indicators
    rsi = ind.get("rsi")
    bbl = ind.get("bb_lower")
    parts = [
        f"종목: {signal.ticker}",
        f"신호 강도: {signal.strength.value}",
        f"현재가: {signal.price}",
        f"RSI: {rsi}",
        f"BB 하단: {bbl}",
        "",
        "수집 결과 (모두 비어있거나 실패):",
    ]
    for r in results:
        status = f"실패({r.error})" if r.error else "데이터 없음"
        parts.append(f"  - {r.name}: {status}")
    parts.append("")
    parts.append("위 정보만 바탕으로 분류 한 줄을 출력하세요.")
    return "\n".join(parts)


def _try_cache_get(key: str) -> Optional[str]:
    try:
        from backend.db import SessionLocal, crud
        db = SessionLocal()
        try:
            row = crud.get_llm_cache(db, purpose=PURPOSE, cache_key=key)
            if not row:
                return None
            data = row.result_text or {}
            return data.get("classification") or None
        finally:
            db.close()
    except Exception as e:
        log.debug(f"[NullResult] cache get 실패 (무시): {type(e).__name__}: {e}")
        return None


def _cache_put(key: str, classification: str, cost_usd: float) -> None:
    try:
        from backend.db import SessionLocal, crud
        db = SessionLocal()
        try:
            crud.put_llm_cache(
                db,
                purpose=PURPOSE,
                cache_key=key,
                result_text={"classification": classification},
                cost_usd=cost_usd,
                ttl_seconds=_CACHE_TTL_SEC,
            )
        finally:
            db.close()
    except Exception as e:
        log.debug(f"[NullResult] cache put 실패 (무시): {type(e).__name__}: {e}")


def _sanitize(text: str) -> str:
    """LLM 출력 → 안전한 한 줄. 첫 줄만, 따옴표/마침표 제거, 60자 캡."""
    text = (text or "").strip()
    if not text:
        return ""
    text = text.split("\n", 1)[0].strip()
    text = text.strip(' "\'`.。')
    if len(text) > 60:
        text = text[:60].rstrip()
    return text


def classify_null_result(
    llm: LLMClient,
    signal: Signal,
    results: list[AnalystResult],
    *,
    model: str = _DEFAULT_MODEL,
    use_cache: bool = True,
    user_id: Optional[int] = None,
) -> Optional[str]:
    """
    "📋 점검 완료: {checked} → {classification}" 한 줄 반환.

    None 반환 조건: 예산 초과 / LLM 실패 / 분류 결과 빈 문자열.
    호출자는 None 이면 raw 알람으로 폴백.
    """
    checked = _checked_label(results)
    key = _cache_key(signal.ticker)

    if use_cache:
        cached = _try_cache_get(key)
        if cached:
            log.info(f"[NullResult] cache hit: {key}")
            return f"📋 점검 완료: {checked} → {cached}"

    user = _build_user_prompt(signal, results)
    try:
        text, usage = llm.call(
            model=model,
            system=SYSTEM_PROMPT,
            user=user,
            max_tokens=_MAX_TOKENS,
            cache_system=True,
            purpose=PURPOSE,
            ticker=signal.ticker,
            user_id=user_id,
        )
    except BudgetExceeded as e:
        log.warning(f"[NullResult] 예산 초과 → 폴백: {e}")
        return None
    except Exception as e:
        log.error(f"[NullResult] LLM 호출 실패: {type(e).__name__}: {e}")
        return None

    classification = _sanitize(text)
    if not classification:
        log.warning("[NullResult] LLM 응답 비어있음 → 폴백")
        return None

    if use_cache:
        _cache_put(key, classification, usage.cost_usd())

    log.info(
        f"[NullResult] {signal.ticker} → {classification!r} "
        f"(cost=${usage.cost_usd():.5f})"
    )
    return f"📋 점검 완료: {checked} → {classification}"
