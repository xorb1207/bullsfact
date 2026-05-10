"""
Anthropic 클라이언트 래퍼 — 일일 비용 캡, 프롬프트 캐싱, 사용량 추적.

Day 1: 클라이언트 셋업 + 비용 트래킹 골격만. 실제 호출은 Day 2~3에서 사용.
"""
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Optional

log = logging.getLogger(__name__)


# 모델별 단가 (USD per 1M tokens) — 2026 기준 가격표 변경 시 업데이트
_PRICING: dict[str, tuple[float, float]] = {
    # model: (input_per_1m, output_per_1m)
    "claude-haiku-4-5":  (1.00, 5.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-4-7":   (15.00, 75.00),
}


@dataclass
class CallUsage:
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    purpose: str = ""              # "synthesizer" | "analyst:news" | ...
    ticker: Optional[str] = None
    user_id: Optional[int] = None  # 멀티유저 비용 추적
    latency_ms: Optional[int] = None

    def cost_usd(self) -> float:
        in_rate, out_rate = _PRICING.get(self.model, (0.0, 0.0))
        # 캐시 read는 input의 10%, cache write는 input의 125% (Anthropic 공식)
        regular_in = self.input_tokens / 1_000_000 * in_rate
        cache_in   = self.cache_read_tokens / 1_000_000 * in_rate * 0.10
        cache_out  = self.cache_creation_tokens / 1_000_000 * in_rate * 1.25
        out        = self.output_tokens / 1_000_000 * out_rate
        return regular_in + cache_in + cache_out + out


class BudgetExceeded(RuntimeError):
    pass


class LLMClient:
    """
    얇은 Anthropic 래퍼. 일일 비용 캡 초과 시 BudgetExceeded raise.
    호출자(Enricher)는 이걸 잡아서 raw 알람으로 폴백.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        max_daily_usd: float = 2.0,
        on_call: Optional[Callable[[CallUsage], None]] = None,
        user_cap_resolver: Optional[Callable[[int], float]] = None,
        user_spent_resolver: Optional[Callable[[int], float]] = None,
    ):
        """
        user_cap_resolver(user_id) → 해당 사용자 일일 캡 (USD).
        user_spent_resolver(user_id) → 해당 사용자 오늘 누적 (USD).
        둘 다 미지정 시 글로벌 max_daily_usd 만 적용.
        """
        self._api_key = api_key or os.getenv("ANTHROPIC_API_KEY", "")
        self._max_daily_usd = max_daily_usd
        self._on_call = on_call
        self._user_cap_resolver = user_cap_resolver
        self._user_spent_resolver = user_spent_resolver
        self._lock = threading.Lock()
        self._spent_today_usd: float = 0.0
        self._spent_date: date = date.today()
        self._client = None  # lazy init

    def _ensure_client(self):
        if self._client is None:
            if not self._api_key:
                raise RuntimeError("ANTHROPIC_API_KEY 미설정")
            from anthropic import Anthropic
            self._client = Anthropic(api_key=self._api_key)
        return self._client

    def _check_and_reset_budget(self, user_id: Optional[int] = None) -> None:
        with self._lock:
            today = date.today()
            if today != self._spent_date:
                self._spent_date = today
                self._spent_today_usd = 0.0
            if self._spent_today_usd >= self._max_daily_usd:
                raise BudgetExceeded(
                    f"일일 LLM 비용 캡(global) ${self._max_daily_usd:.2f} 초과 "
                    f"(오늘 누적 ${self._spent_today_usd:.4f})"
                )

        # 사용자별 캡 (있을 때만)
        if user_id is not None and self._user_cap_resolver and self._user_spent_resolver:
            try:
                cap = self._user_cap_resolver(user_id)
                spent = self._user_spent_resolver(user_id)
            except Exception as e:
                log.warning(f"[LLM] user cap resolver 실패 (무시): {e}")
                return
            if spent >= cap:
                raise BudgetExceeded(
                    f"사용자(#{user_id}) 일일 캡 ${cap:.2f} 초과 (오늘 ${spent:.4f})"
                )

    def _record_spend(self, usage: CallUsage) -> None:
        with self._lock:
            self._spent_today_usd += usage.cost_usd()
            log.debug(
                f"[LLM] {usage.model} cost=${usage.cost_usd():.5f} "
                f"누적=${self._spent_today_usd:.4f}"
            )

    def spent_today_usd(self) -> float:
        with self._lock:
            return self._spent_today_usd

    def call(
        self,
        model: str,
        system: str,
        user: str,
        max_tokens: int = 1024,
        cache_system: bool = True,
        purpose: str = "",
        ticker: Optional[str] = None,
        user_id: Optional[int] = None,
    ) -> tuple[str, CallUsage]:
        """단일 메시지 호출. (text, usage) 반환."""
        self._check_and_reset_budget(user_id=user_id)
        client = self._ensure_client()

        system_blocks = [{"type": "text", "text": system}]
        if cache_system:
            system_blocks[0]["cache_control"] = {"type": "ephemeral"}

        t0 = time.monotonic()
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_blocks,
            messages=[{"role": "user", "content": user}],
        )
        latency_ms = int((time.monotonic() - t0) * 1000)

        text = "".join(b.text for b in resp.content if b.type == "text")
        usage = CallUsage(
            model=model,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            cache_read_tokens=getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
            cache_creation_tokens=getattr(resp.usage, "cache_creation_input_tokens", 0) or 0,
            purpose=purpose,
            ticker=ticker,
            user_id=user_id,
            latency_ms=latency_ms,
        )
        self._record_spend(usage)
        if self._on_call:
            try:
                self._on_call(usage)
            except Exception as e:
                log.warning(f"[LLM] on_call 콜백 실패 (무시): {e}")
        return text, usage

    def call_with_web_search(
        self,
        model: str,
        system: str,
        user: str,
        *,
        max_tokens: int = 2048,
        max_searches: int = 5,
        purpose: str = "",
        ticker: Optional[str] = None,
        user_id: Optional[int] = None,
    ) -> tuple[str, list[str], CallUsage]:
        """
        web_search 서버 툴을 켠 호출. Anthropic이 자동으로 검색 실행, 결과를 본문에 통합.
        Returns: (text, citation_urls, usage)

        주의: web_search는 토큰 외 검색 비용 별도 ($10/1k searches @ 2026-05).
              여기서는 일반 토큰 비용만 추적 — 검색 비용은 캡 외에 발생함을 인지할 것.
        """
        self._check_and_reset_budget(user_id=user_id)
        client = self._ensure_client()

        t0 = time.monotonic()
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            tools=[{
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": max_searches,
            }],
            messages=[{"role": "user", "content": user}],
        )
        latency_ms = int((time.monotonic() - t0) * 1000)

        text_parts: list[str] = []
        citations: list[str] = []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
                for cit in (getattr(block, "citations", None) or []):
                    url = getattr(cit, "url", None)
                    if url and url not in citations:
                        citations.append(url)
        text = "".join(text_parts)

        usage = CallUsage(
            model=model,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            cache_read_tokens=getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
            cache_creation_tokens=getattr(resp.usage, "cache_creation_input_tokens", 0) or 0,
            purpose=purpose,
            ticker=ticker,
            user_id=user_id,
            latency_ms=latency_ms,
        )
        self._record_spend(usage)
        if self._on_call:
            try:
                self._on_call(usage)
            except Exception as e:
                log.warning(f"[LLM] on_call 콜백 실패 (무시): {e}")
        return text, citations, usage
