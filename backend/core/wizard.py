"""
대화형 입력 wizard 상태 관리 (chat_id → 진행 중인 대화 흐름).

가족 사용 편의 — 텍스트 명령 외우기 부담을 단계별 prompt 로 해소.
인메모리 dict (재시작 시 진행 중 wizard 손실 — 사용자가 다시 시작하면 끝).

흐름 종류 (flow):
  - portfolio_add    : 종목 추가 (ticker → qty → avg_cost)
  - portfolio_update : 평단 갱신 (ticker → qty → avg_cost)
  - feedback         : 의견 입력 (text 한 줄)
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

log = logging.getLogger(__name__)

# 5분 무응답 → 자동 만료
WIZARD_TIMEOUT_SEC = 5 * 60


@dataclass
class WizardState:
    flow: str                                # "portfolio_add" | "portfolio_update" | ...
    step: str                                # 현재 단계 (flow별 정의)
    data: dict = field(default_factory=dict)  # 누적 입력 데이터
    started_at: datetime = field(default_factory=datetime.utcnow)
    user_id: Optional[int] = None            # 진행 사용자 (DB User.id)

    def is_expired(self, timeout_sec: int = WIZARD_TIMEOUT_SEC) -> bool:
        return (datetime.utcnow() - self.started_at) > timedelta(seconds=timeout_sec)


# 인메모리 store — chat_id 기준
_store: dict[str, WizardState] = {}
_lock = threading.Lock()


def start(chat_id: str, flow: str, step: str, user_id: Optional[int] = None) -> WizardState:
    """기존 wizard 가 있으면 덮어씀 (취소 후 새로 시작)."""
    state = WizardState(flow=flow, step=step, user_id=user_id)
    with _lock:
        _store[str(chat_id)] = state
    return state


def get(chat_id: str) -> Optional[WizardState]:
    """진행 중 wizard. 만료된 건 자동 cleanup."""
    with _lock:
        state = _store.get(str(chat_id))
        if state is None:
            return None
        if state.is_expired():
            _store.pop(str(chat_id), None)
            return None
        return state


def advance(chat_id: str, next_step: str, **data_updates) -> Optional[WizardState]:
    """현재 상태를 다음 step 으로 + data 업데이트."""
    with _lock:
        state = _store.get(str(chat_id))
        if state is None:
            return None
        state.step = next_step
        state.data.update(data_updates)
        return state


def cancel(chat_id: str) -> bool:
    with _lock:
        existed = str(chat_id) in _store
        _store.pop(str(chat_id), None)
        return existed


def cleanup_expired() -> int:
    """주기적으로 호출 가능 (안 해도 get() 시점에 정리됨)."""
    with _lock:
        expired = [cid for cid, s in _store.items() if s.is_expired()]
        for cid in expired:
            _store.pop(cid, None)
        return len(expired)
