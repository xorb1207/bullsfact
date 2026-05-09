"""
매매전략 §1.2 양도세 분할 매도 캘린더 시드.

사용:
    python -m backend.scripts.seed_sell_reminders          # 미존재만 추가
    python -m backend.scripts.seed_sell_reminders --reset  # 시드 항목 모두 삭제 후 재시드

대략 일정 (정확한 매도일은 본인이 시장 상황 보고 조정):
  - 1차    2026-06-15  TQQQ 15 + SOXL 5 + RGTI 1 + SOXS 10  (차익 ~$1,390)
  - 2차    2026-09-15  TQQQ 15 + SOXL 5 + IONQ 5 + TEM 15   (차익 ~$1,260)
  - 3차    2026-12-15  TQQQ 15 + SOXL 5                      (차익 ~$1,451)
  - 신규   2027-02-15  TQQQ 25 + SOXL 10                     (차익 ~$2,649, 신규 250만원)
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime

from sqlalchemy import select

from backend.db import SessionLocal, init_db, crud
from backend.db.models import SellReminder

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("seed-reminders")


SEEDS = [
    {
        "title": "1차 매도 — TQQQ 15 + SOXL 5 + RGTI 1 + SOXS 10",
        "target_date": datetime(2026, 6, 15),
        "notes": "차익 ~$1,390 (KRW ~195만원, 250만원 공제 내)",
    },
    {
        "title": "2차 매도 — TQQQ 15 + SOXL 5 + IONQ 5 + TEM 15",
        "target_date": datetime(2026, 9, 15),
        "notes": "차익 ~$1,260 (KRW ~176만원)",
    },
    {
        "title": "3차 매도 — TQQQ 15 + SOXL 5",
        "target_date": datetime(2026, 12, 15),
        "notes": "차익 ~$1,451 (KRW ~203만원), 연 합계 +574만원",
    },
    {
        "title": "2027 신규 250만원 — TQQQ 25 + SOXL 10",
        "target_date": datetime(2027, 2, 15),
        "notes": "차익 ~$2,649 (KRW ~371만원), 신규 공제 활용",
    },
]


def _key(s: dict) -> tuple:
    """중복 판별 — 같은 title + target_date 면 동일 시드."""
    return (s["title"], s["target_date"].date())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()

    init_db()
    db = SessionLocal()
    try:
        if args.reset:
            keys = {_key(s) for s in SEEDS}
            rows = db.execute(select(SellReminder)).scalars().all()
            removed = 0
            for r in rows:
                rk = (r.title, r.target_date.date() if hasattr(r.target_date, "date") else r.target_date)
                if rk in keys:
                    db.delete(r)
                    removed += 1
            db.commit()
            log.info(f"기존 시드 {removed}건 삭제")

        existing = db.execute(select(SellReminder)).scalars().all()
        existing_keys = {(r.title, r.target_date.date() if hasattr(r.target_date, "date") else r.target_date)
                         for r in existing}

        added = 0
        skipped = 0
        for s in SEEDS:
            if _key(s) in existing_keys:
                skipped += 1
                continue
            crud.insert_reminder(
                db,
                title=s["title"],
                target_date=s["target_date"],
                notes=s.get("notes"),
            )
            added += 1
        log.info(f"✅ 시드 완료 — 추가 {added}건, 스킵 {skipped}건 (총 {len(SEEDS)}건)")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
