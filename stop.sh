#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_DIR="$ROOT_DIR/.run"

is_running() {
  local pid="$1"
  [[ -n "$pid" ]] && kill -0 "$pid" >/dev/null 2>&1
}

pid_port() {
  local pid_file="$1"
  local base
  base="$(basename "$pid_file")"
  if [[ "$base" =~ ^funnel-([0-9]+)\.pid$ ]]; then
    echo "${BASH_REMATCH[1]}"
  else
    echo "18890"
  fi
}

is_service_pid() {
  local pid="$1"
  local port="$2"
  if ! is_running "$pid"; then
    return 1
  fi

  local stat
  stat="$(ps -p "$pid" -o stat= 2>/dev/null || true)"
  if [[ "$stat" == *U* ]]; then
    return 1
  fi

  local cmd
  cmd="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  if [[ "$cmd" == *"uvicorn app.main:app"* ]]; then
    return 0
  fi

  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -a -p "$pid" -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1
  else
    return 1
  fi
}

stop_pid() {
  local pid="$1"
  is_running "$pid" || return 1
  kill "$pid" >/dev/null 2>&1 || true
  for _ in {1..8}; do
    is_running "$pid" || return 0
    sleep 1
  done
  kill -9 "$pid" >/dev/null 2>&1 || true
  sleep 1
  ! is_running "$pid"
}

STOPPED=0

# 1) 清理所有 .run/*.pid 文件
for pid_file in "$PID_DIR"/*.pid; do
  [[ -f "$pid_file" ]] || continue
  PID="$(cat "$pid_file" 2>/dev/null || true)"
  PORT="$(pid_port "$pid_file")"
  if is_service_pid "$PID" "$PORT" && stop_pid "$PID"; then
    echo "已停止: PID=$PID (来自 $(basename "$pid_file"))"
    STOPPED=1
  elif [[ -n "$PID" ]]; then
    echo "忽略非服务 PID: PID=$PID (来自 $(basename "$pid_file"))"
  fi
  rm -f "$pid_file"
done

# 2) 兜底：按进程名找所有 app.main:app 实例（覆盖 UEs 之外的残留）
if command -v pgrep >/dev/null 2>&1; then
  PIDS="$(pgrep -f "uvicorn app.main:app" 2>/dev/null || true)"
  for pid in $PIDS; do
    stat="$(ps -p "$pid" -o stat= 2>/dev/null || true)"
    if [[ "$stat" == *U* ]]; then
      echo "跳过不可中断状态进程: PID=$pid STAT=${stat}（需重启 Mac 清理）"
      continue
    fi
    if stop_pid "$pid"; then
      echo "已停止残留进程: PID=$pid"
      STOPPED=1
    fi
  done
fi

if [[ "$STOPPED" -eq 0 ]]; then
  echo "未发现可停止的服务（UEs 内核卡死进程需重启 Mac 清理）"
else
  echo "停止完成"
fi
