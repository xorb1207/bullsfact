"""
매도 캘린더 리마인더 평가기.

매매전략 §1.2: 양도세 250만원 분할 매도 (5~6월/8~9월/12월/익년 1~3월).
일일 06:00 KST 브리핑 끝에 임박 항목 자동 첨부.

규칙:
- target_date - days_before <= 오늘 <= target_date  → 활성 (브리핑에 노출)
- target_date < 오늘 - 1d                            → 자동 비활성 (auto_expire_past_reminders)
- 사용자가 명시적 /reminder done                     → done_at 기록 + 비활성

이 모듈은 표시용 데이터만 만들고, 실제 DB 변경은 crud 호출자가 함.
"""
from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from backend.db import SessionLocal, crud

log = logging.getLogger(__name__)


@dataclass
class DueReminder:
    id: int
    title: str
    target_date: datetime
    days_until: int          # 0 = 오늘, 1 = 내일, -1 = 어제 (이미 지났지만 cleanup 전)
    notes: Optional[str]


def get_due_reminders() -> list[DueReminder]:
    """
    현재 활성 + 임박 (target_date - days_before <= today <= target_date) 리마인더 반환.
    가까운 순(days_until 오름차순)으로 정렬.
    """
    today = datetime.utcnow().date()
    out: list[DueReminder] = []
    db = SessionLocal()
    try:
        # 자동 expire 먼저 (D+1 지난 항목 정리)
        try:
            crud.auto_expire_past_reminders(db)
        except Exception as e:
            log.warning(f"[Reminders] auto_expire 실패: {e}")

        rows = crud.list_reminders(db, active_only=True)
        for r in rows:
            target = r.target_date.date() if hasattr(r.target_date, "date") else r.target_date
            window_start = target - timedelta(days=r.days_before)
            if window_start <= today <= target:
                out.append(DueReminder(
                    id=r.id,
                    title=r.title,
                    target_date=r.target_date,
                    days_until=(target - today).days,
                    notes=r.notes,
                ))
    finally:
        db.close()
    out.sort(key=lambda r: r.days_until)
    return out


def format_section(due: list[DueReminder]) -> str:
    """일일 브리핑에 첨부할 HTML 섹션. 빈 리스트면 빈 문자열."""
    if not due:
        return ""
    lines = ["", "📅 <b>매도 캘린더</b>"]
    for r in due:
        if r.days_until == 0:
            tag = "⚠️ 오늘"
        elif r.days_until == 1:
            tag = "⚠️ D-1"
        else:
            tag = f"D-{r.days_until}"
        target_str = r.target_date.strftime("%Y-%m-%d") if hasattr(r.target_date, "strftime") else str(r.target_date)
        lines.append(f"  <b>{html.escape(r.title)}</b> — {tag} ({target_str})")
        if r.notes:
            lines.append(f"     <i>{html.escape(r.notes)}</i>")
        lines.append(f"     <code>/reminder done {r.id}</code>")
    return "\n".join(lines)
