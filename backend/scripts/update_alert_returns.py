"""
알림 후속 추적 데이터 수동 갱신 (M3 부가).

봇이 매일 06:00 KST 자동 호출하지만, 즉시 갱신 필요 시 수동 실행:

    python -m backend.scripts.update_alert_returns
"""
from __future__ import annotations

import logging
import sys

from dotenv import load_dotenv

load_dotenv("backend/.env", override=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("update-alert-returns")

from backend.db import init_db                          # noqa: E402
from backend.core.post_mortem import (                  # noqa: E402
    update_returns, compute_statistics, format_stats_section,
)


def main() -> int:
    init_db()
    n = update_returns()
    log.info(f"갱신: {n}건")

    stats = compute_statistics(lookback_days=90)
    print(format_stats_section(stats))
    return 0


if __name__ == "__main__":
    sys.exit(main())
