"""
보유 포지션 + 익절 룰 평가기.

매매전략 MD §4 익절 룰:
  +50%  도달 → 20% 매도 (누적 회수율 20%)
  +100% 도달 → 30% 매도 (누적 50%, 원금 회수)
  +200% 도달 → 25% 매도 (누적 75%)
  +400% 도달 → 15% 매도 (누적 90%)
  +600% 도달 → 재량  (누적 95%+, "공짜 칩")

마일스톤이 한 번 발동하면 Position.highest_milestone 에 기록되고
다음 마일스톤만 감시한다 (재발동 방지).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from backend.db import SessionLocal, crud
from backend.db.models import Position

log = logging.getLogger(__name__)


# (마일스톤, 매도 비율, 누적 회수율, 라벨)
MILESTONES: list[tuple[float, float, float, str]] = [
    (0.5,  0.20, 0.20, "+50% 도달 — 1차 익절"),
    (1.0,  0.30, 0.50, "+100% 도달 — 원금 회수 단계"),
    (2.0,  0.25, 0.75, "+200% 도달 — 2차 익절"),
    (4.0,  0.15, 0.90, "+400% 도달 — 3차 익절"),
    (6.0,  0.0,  0.95, "+600% 도달 — 재량 매도 (공짜 칩 구간)"),
]


@dataclass
class MilestoneTrigger:
    position: Position
    current_price: float
    return_pct: float                  # 0.52 = +52%
    milestone: float                   # 0.5 / 1.0 / ...
    sell_ratio: float                  # 0.20 = 20% 매도 권장
    cumulative_recovery: float         # 0.20 = 누적 20%
    label: str
    suggested_sell_qty: float          # qty * sell_ratio


def _next_milestone(current_pct: float, highest_triggered: float) -> Optional[tuple[float, float, float, str]]:
    """현재 수익률 >= 마일스톤이고, highest_triggered 보다 높은 첫 마일스톤 반환."""
    for ms, sell, cum, label in MILESTONES:
        if ms <= highest_triggered + 1e-9:
            continue
        if current_pct >= ms:
            return (ms, sell, cum, label)
    return None


def highest_passed_milestone(current_pct: float) -> float:
    """포지션 신규 등록 시: 이미 지나간 가장 높은 마일스톤 반환 (알림 폭탄 방지)."""
    passed = 0.0
    for ms, _, _, _ in MILESTONES:
        if current_pct >= ms:
            passed = ms
        else:
            break
    return passed


class PositionEvaluator:
    """
    스캐너 사이클당 ticker별로 호출.
    매 호출마다 DB 세션 새로 열고 닫음.
    """

    def evaluate(
        self, ticker: str, current_price: float, user_id: Optional[int] = None,
    ) -> Optional[MilestoneTrigger]:
        """현재가 기준으로 다음 마일스톤 도달 여부 평가. 트리거 시 DB 갱신."""
        db = SessionLocal()
        try:
            pos = crud.get_position(db, ticker, user_id=user_id)
            if not pos:
                return None
            if pos.avg_cost <= 0:
                log.warning(f"[PositionEval] {ticker} avg_cost={pos.avg_cost} 비정상 — 스킵")
                return None

            ret = (current_price / pos.avg_cost) - 1.0
            nxt = _next_milestone(ret, pos.highest_milestone)
            if nxt is None:
                return None

            ms, sell_ratio, cum, label = nxt
            crud.update_position_milestone(db, ticker, ms, user_id=user_id)

            return MilestoneTrigger(
                position=pos,
                current_price=current_price,
                return_pct=ret,
                milestone=ms,
                sell_ratio=sell_ratio,
                cumulative_recovery=cum,
                label=label,
                suggested_sell_qty=pos.qty * sell_ratio,
            )
        finally:
            db.close()
