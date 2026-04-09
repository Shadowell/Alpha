#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$ROOT_DIR/.run/funnel.pid"
PORT="${PORT:-18888}"

is_running() {
  local pid="$1"
  if [[ -z "$pid" ]]; then
    return 1
  fi
  kill -0 "$pid" >/dev/null 2>&1
}

stop_pid() {
  local pid="$1"
  if ! is_running "$pid"; then
    return 1
  fi

  kill "$pid" >/dev/null 2>&1 || true
  for _ in {1..15}; do
    if ! is_running "$pid"; then
      return 0
    fi
    sleep 1
  done

  kill -9 "$pid" >/dev/null 2>&1 || true
  sleep 1
  ! is_running "$pid"
}

STOPPED=0
if [[ -f "$PID_FILE" ]]; then
  PID="$(cat "$PID_FILE" 2>/dev/null || true)"
  if stop_pid "$PID"; then
    echo "已停止服务: PID=$PID"
    STOPPED=1
  fi
  rm -f "$PID_FILE"
fi

# 兜底: 按端口查找 uvicorn 进程
if command -v lsof >/dev/null 2>&1; then
  PIDS="$(lsof -ti tcp:"$PORT" || true)"
  if [[ -n "$PIDS" ]]; then
    for pid in $PIDS; do
      if stop_pid "$pid"; then
        echo "已停止端口进程: PID=$pid"
        STOPPED=1
      fi
    done
  fi
fi

if [[ "$STOPPED" -eq 0 ]]; then
  echo "未发现运行中的服务"
else
  echo "停止完成"
fi
