#!/usr/bin/env bash
# bullsfact 봇/스캐너 관리 스크립트.
#
# 사용:
#   ./run.sh start     봇 + 스캐너 띄우기
#   ./run.sh stop      둘 다 종료
#   ./run.sh restart   stop + start
#   ./run.sh status    프로세스 상태
#   ./run.sh logs bot|scanner   로그 tail (-f)
#   ./run.sh attach bot|scanner tmux 세션 붙기 (Ctrl+B D 로 빠져나오기)
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

SCANNER_CMD="caffeinate -i python -m backend.main 2>&1 | tee -a scanner.log"
BOT_CMD="python -m backend.scripts.bot 2>&1 | tee -a bot.log"

cmd="${1:-}"

case "$cmd" in
  start)
    if tmux has-session -t scanner 2>/dev/null; then
      echo "scanner 이미 떠있음 (tmux session)"
    else
      tmux new-session -d -s scanner -c "$ROOT_DIR" "$SCANNER_CMD"
      echo "scanner 시작"
    fi
    if tmux has-session -t bot 2>/dev/null; then
      echo "bot 이미 떠있음 (tmux session)"
    else
      tmux new-session -d -s bot -c "$ROOT_DIR" "$BOT_CMD"
      echo "bot 시작"
    fi
    sleep 1
    "$0" status
    ;;

  stop)
    tmux kill-session -t bot 2>/dev/null && echo "bot 종료" || echo "bot 없음"
    tmux kill-session -t scanner 2>/dev/null && echo "scanner 종료" || echo "scanner 없음"
    # 잔여 caffeinate / python 프로세스 정리
    pkill -f "python -m backend.main" 2>/dev/null || true
    pkill -f "python -m backend.scripts.bot" 2>/dev/null || true
    pkill -f "caffeinate -i python -m backend.main" 2>/dev/null || true
    ;;

  restart)
    "$0" stop
    sleep 1
    "$0" start
    ;;

  status)
    echo "── tmux ──"
    tmux ls 2>/dev/null || echo "(no sessions)"
    echo ""
    echo "── processes ──"
    ps aux | grep -E "backend\.(main|scripts\.bot)" | grep -v grep || echo "(no backend processes)"
    ;;

  logs)
    target="${2:-}"
    case "$target" in
      bot)     tail -f "$ROOT_DIR/bot.log" ;;
      scanner) tail -f "$ROOT_DIR/scanner.log" ;;
      *)       echo "사용: $0 logs bot|scanner"; exit 1 ;;
    esac
    ;;

  attach)
    target="${2:-}"
    case "$target" in
      bot|scanner) tmux attach -t "$target" ;;
      *) echo "사용: $0 attach bot|scanner"; exit 1 ;;
    esac
    ;;

  ""|help|-h|--help)
    sed -n '2,12p' "$0"
    ;;

  *)
    echo "알 수 없는 명령: $cmd"
    sed -n '2,12p' "$0"
    exit 1
    ;;
esac
