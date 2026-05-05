"""
Synthesizer — Analyst 결과 + Signal을 받아 단일 LLM 호출로 EnrichmentContext 생성.

핵심: LLM은 정리수/번역가 역할. 새 사실 만들지 않고 주어진 데이터만 정리.
출력은 strict JSON.
"""
import json
import logging
import re

from .llm_client import LLMClient
from .types import AnalystResult, EnrichmentContext, Perspective
from ..strategy.dip_buy import Signal

log = logging.getLogger(__name__)


SYSTEM_PROMPT = """당신은 트레이딩 알람의 컨텍스트 정리수입니다.
주어진 시그널 + 뉴스/펀더멘털을 한국어로 정리해 트레이더에게 전달합니다.

엄격한 규칙:
1. 주어진 데이터 외의 사실(뉴스, 수치, 일정)은 절대 추가하지 마세요. 데이터에 없으면 빈 값으로.
2. "사세요/파세요"라고 단정하지 마세요. "관점 제시" 형태로만.
3. 각 관점(단타/스윙/장투)은 2~3문장. 진입가/손절/목표가는 시그널 데이터(가격, BB)만 근거로.
4. 출력은 JSON 한 개. 다른 텍스트 X.

출력 스키마:
{
  "headline": "한 줄 요약. 뉴스에서 핵심만. 헤드라인 없으면 빈 문자열",
  "risk_flags": ["earnings_in_3d", "fomc_tomorrow", ...]    // 데이터에서 발견한 임박 리스크만
  "perspectives": {
    "scalp": "단타(분~당일) 관점. 2~3문장.",
    "swing": "스윙(3~10일) 관점. 진입/손절/목표 포함. 2~3문장.",
    "long":  "장투(1개월+) 관점. DCA/누적 등. 2~3문장."
  }
}"""


def _build_user_prompt(signal: Signal, source: str, results: list[AnalystResult]) -> str:
    ind = signal.indicators
    rsi = ind.get("rsi")
    bbl = ind.get("bb_lower")
    bbm = ind.get("bb_mid")

    parts = [
        "[시그널]",
        f"종목: {signal.ticker} ({source})",
        f"현재가: ${signal.price:.4f}",
        f"신호 강도: {signal.strength.value}",
        f"충족 조건: {'; '.join(signal.reasons)}",
        f"RSI: {rsi:.1f}" if isinstance(rsi, float) else "RSI: N/A",
        f"BB하단: ${bbl:.4f}" if isinstance(bbl, float) else "BB하단: N/A",
        f"BB중앙: ${bbm:.4f}" if isinstance(bbm, float) else "BB중앙: N/A",
        "",
    ]
    for r in results:
        if r.error:
            parts.append(f"[{r.name}] (수집 실패: {r.error})")
        elif r.summary:
            parts.append(f"[{r.name}]\n{r.summary}")
        else:
            parts.append(f"[{r.name}] (데이터 없음)")
        parts.append("")
    parts.append("위 데이터만 근거로 JSON을 생성하세요.")
    return "\n".join(parts)


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(text: str) -> dict:
    """LLM이 ```json ... ``` 같이 감싸도 추출."""
    m = _JSON_RE.search(text)
    if not m:
        raise ValueError(f"JSON 추출 실패: {text[:200]}")
    return json.loads(m.group(0))


class Synthesizer:
    def __init__(
        self,
        llm: LLMClient,
        model: str = "claude-sonnet-4-6",
        max_tokens: int = 1024,
    ):
        self.llm = llm
        self.model = model
        self.max_tokens = max_tokens

    def synthesize(
        self,
        signal: Signal,
        source: str,
        results: list[AnalystResult],
    ) -> EnrichmentContext:
        user = _build_user_prompt(signal, source, results)
        text, usage = self.llm.call(
            model=self.model,
            system=SYSTEM_PROMPT,
            user=user,
            max_tokens=self.max_tokens,
            cache_system=True,
            purpose="synthesizer",
            ticker=signal.ticker,
        )
        try:
            data = _extract_json(text)
        except Exception as e:
            log.warning(f"[Synthesizer] JSON 파싱 실패: {e}")
            data = {}

        perspectives_raw = data.get("perspectives") or {}
        perspectives: dict[Perspective, str] = {}
        for p in Perspective:
            v = perspectives_raw.get(p.value)
            if isinstance(v, str) and v.strip():
                perspectives[p] = v.strip()

        citations: list[str] = []
        for r in results:
            citations.extend(r.citations)

        cost_cents = round(usage.cost_usd() * 100, 2)
        log.info(
            f"[Synthesizer] {signal.ticker} cost=¢{cost_cents:.2f} "
            f"(in={usage.input_tokens} out={usage.output_tokens} "
            f"cache_r={usage.cache_read_tokens} cache_w={usage.cache_creation_tokens})"
        )

        return EnrichmentContext(
            headline=str(data.get("headline") or "").strip(),
            citations=citations,
            perspectives=perspectives,
            risk_flags=[str(x) for x in (data.get("risk_flags") or []) if x],
            cost_cents=cost_cents,
        )
