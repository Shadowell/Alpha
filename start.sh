#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_DIR="$ROOT_DIR/.run"
LOG_DIR="$ROOT_DIR/logs"
PID_FILE="$PID_DIR/funnel.pid"
LOG_FILE="$LOG_DIR/server.log"
PORT="${PORT:-18888}"
HOST="${HOST:-0.0.0.0}"
RELOAD="${RELOAD:-0}"

mkdir -p "$PID_DIR" "$LOG_DIR"

is_running() {
  local pid="$1"
  if [[ -z "$pid" ]]; then
    return 1
  fi
  kill -0 "$pid" >/dev/null 2>&1
}

if [[ -f "$PID_FILE" ]]; then
  OLD_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
  if is_running "$OLD_PID"; then
    echo "服务已在运行: PID=$OLD_PID, PORT=$PORT"
    echo "日志: $LOG_FILE"
    exit 0
  else
    rm -f "$PID_FILE"
  fi
fi

cd "$ROOT_DIR"
UVICORN_ARGS=(app.main:app --host "$HOST" --port "$PORT")
if [[ "$RELOAD" == "1" ]]; then
  UVICORN_ARGS+=(
    --reload
    --reload-dir "$ROOT_DIR"
    --reload-include "*.py"
    --reload-include "*.html"
    --reload-include "*.css"
    --reload-include "*.js"
    --reload-include "*.json"
  )
fi

if [[ "${BASH_VERSION:-}" ]]; then
  set +m
fi

BOOT_PID=""
if command -v setsid >/dev/null 2>&1; then
  setsid python3 -m uvicorn "${UVICORN_ARGS[@]}" >"$LOG_FILE" 2>&1 < /dev/null &
  BOOT_PID=$!
else
  nohup python3 -m uvicorn "${UVICORN_ARGS[@]}" >"$LOG_FILE" 2>&1 < /dev/null &
  BOOT_PID=$!
fi

NEW_PID="$BOOT_PID"
for _ in {1..20}; do
  if command -v lsof >/dev/null 2>&1; then
    PORT_PID="$(lsof -ti tcp:"$PORT" 2>/dev/null | head -n 1 || true)"
    if [[ -n "$PORT_PID" ]] && is_running "$PORT_PID"; then
      NEW_PID="$PORT_PID"
      break
    fi
  fi
  if is_running "$BOOT_PID"; then
    NEW_PID="$BOOT_PID"
  fi
  sleep 0.5
done

disown "$BOOT_PID" >/dev/null 2>&1 || true
echo "$NEW_PID" > "$PID_FILE"

if is_running "$NEW_PID"; then
  echo "启动成功: PID=$NEW_PID"
  echo "访问地址: http://127.0.0.1:$PORT"
  echo "自动重启: $([[ "$RELOAD" == "1" ]] && echo 开启 || echo 关闭)"
  echo "日志文件: $LOG_FILE"
else
  echo "启动失败，请检查日志: $LOG_FILE"
  rm -f "$PID_FILE"
  exit 1
fi
