"""
일일 비용 리포트 — DB(LLMCallLog, AlertLog) 집계 + (옵션) Telegram 발송.

사용:
    python -m backend.scripts.cost_report                # 어제 + 오늘 출력만
    python -m backend.scripts.cost_report --send         # Telegram 발송도
    python -m backend.scripts.cost_report --days 7       # 최근 7일
"""
import argparse
import os
from datetime import datetime, timedelta, timezone

import requests

from backend.db import SessionLocal, crud, init_db, LLMCallLog, AlertLog
from sqlalchemy import select, func


def _alerts_summary(db, since: datetime) -> dict:
    rows = db.execute(
        select(AlertLog.strength, func.count(AlertLog.id))
        .where(AlertLog.sent_at >= since)
        .group_by(AlertLog.strength)
    ).all()
    counts = {s: 0 for s in ("strong", "weak")}
    for strength, n in rows:
        counts[strength] = n
    return counts


def _fmt_report(days: int) -> str:
    init_db()
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        out = ["📊 <b>bullsfact 비용/활동 리포트</b>", ""]
        for d in range(days):
            day_start = (now - timedelta(days=d)).replace(hour=0, minute=0, second=0, microsecond=0)
            day_end   = day_start + timedelta(days=1)
            llm = crud.llm_cost_summary(db, since=day_start, until=day_end)
            alerts = _alerts_summary_range(db, since=day_start, until=day_end)
            label = "오늘" if d == 0 else ("어제" if d == 1 else f"D-{d}")
            out.append(
                f"• <b>{label}</b> ({day_start:%m-%d}): "
                f"LLM ${llm['cost_usd']:.4f} / 호출 {llm['calls']}회 "
                f"| 알람 STRONG {alerts['strong']} WEAK {alerts['weak']}"
            )
        out.append("")
        # 7일 누적
        week_start = (now - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
        wk = crud.llm_cost_summary(db, since=week_start)
        out.append(
            f"📈 최근 7일 누적: LLM ${wk['cost_usd']:.4f} ({wk['calls']}회, "
            f"입력 {wk['input_tokens']:,} / 출력 {wk['output_tokens']:,} 토큰)"
        )
        return "\n".join(out)
    finally:
        db.close()


def _alerts_summary_range(db, since: datetime, until: datetime) -> dict:
    rows = db.execute(
        select(AlertLog.strength, func.count(AlertLog.id))
        .where(AlertLog.sent_at >= since, AlertLog.sent_at < until)
        .group_by(AlertLog.strength)
    ).all()
    counts = {s: 0 for s in ("strong", "weak")}
    for strength, n in rows:
        counts[strength] = n
    return counts


def _send_telegram(text: str) -> bool:
    token = os.getenv("TELEGRAM_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or "YOUR_" in token:
        print("(TELEGRAM_TOKEN 미설정 — 콘솔 출력으로만 표시)")
        print(text)
        return False
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
        timeout=10,
    )
    if not resp.ok:
        print(f"Telegram 전송 실패 status={resp.status_code} body={resp.text[:200]}")
        return False
    return True


def main():
    from dotenv import load_dotenv
    load_dotenv("backend/.env", override=True)

    ap = argparse.ArgumentParser(description="bullsfact 일일 비용/활동 리포트")
    ap.add_argument("--days", type=int, default=2, help="집계할 일수 (기본 2)")
    ap.add_argument("--send", action="store_true", help="Telegram 발송")
    args = ap.parse_args()

    text = _fmt_report(args.days)
    print(text.replace("<b>", "").replace("</b>", ""))
    if args.send:
        ok = _send_telegram(text)
        print(f"\n>>> Telegram 전송: {'OK' if ok else 'FAIL'}")


if __name__ == "__main__":
    main()
