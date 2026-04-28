#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_DIR="$ROOT_DIR/.run"
LOG_DIR="$ROOT_DIR/logs"

# 自动加载 .env（类 dotenv：行内注释用 # 开头；KEY=VALUE；支持引号；跳过空行）
if [[ -f "$ROOT_DIR/.env" ]]; then
  set -o allexport
  # shellcheck disable=SC1090
  source <(grep -Ev '^\s*(#|$)' "$ROOT_DIR/.env")
  set +o allexport
  echo "已加载 .env"
fi

PORT="${PORT:-18890}"
HOST="${HOST:-0.0.0.0}"
RELOAD="${RELOAD:-0}"
if [[ -z "${PYTHON_BIN:-}" ]]; then
  if [[ -x "$HOME/arm-python/python/bin/python3.11" ]]; then
    PYTHON_BIN="$HOME/arm-python/python/bin/python3.11"
  elif [[ -x "/opt/homebrew/bin/python3.11" ]]; then
    PYTHON_BIN="/opt/homebrew/bin/python3.11"
  elif [[ -x "/opt/homebrew/bin/python3" ]]; then
    PYTHON_BIN="/opt/homebrew/bin/python3"
  else
    PYTHON_BIN="python3"
  fi
fi
LOG_FILE="${LOG_FILE:-$LOG_DIR/server-${PORT}.log}"

mkdir -p "$PID_DIR" "$LOG_DIR"

PY_MACHINE="$("$PYTHON_BIN" - <<'PY'
import platform
print(platform.machine())
PY
)"
if [[ "$PY_MACHINE" != "arm64" ]]; then
  echo "启动失败: $PYTHON_BIN 是 $PY_MACHINE，不是 arm64。"
  echo "数据中心补数曾因 Rosetta/x86_64 Python + 阻塞网络调用进入 UEs。"
  echo "请安装/指定 arm64 Python 3.11+，例如: PYTHON_BIN=/Users/jie.feng/arm-python/python/bin/python3.11 ./start.sh"
  exit 1
fi

is_running() {
  local pid="$1"
  if [[ -z "$pid" ]]; then
    return 1
  fi
  kill -0 "$pid" >/dev/null 2>&1
}

is_service_pid() {
  local pid="$1"
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
    lsof -nP -a -p "$pid" -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1
  else
    return 1
  fi
}

listening_pid() {
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | head -n 1 || true
  fi
}

if command -v lsof >/dev/null 2>&1; then
  CURRENT_LISTENER="$(listening_pid)"
  if [[ -n "$CURRENT_LISTENER" ]] && ! is_service_pid "$CURRENT_LISTENER"; then
    echo "启动失败: 端口 $PORT 被不可用进程占用 PID=$CURRENT_LISTENER"
    echo "请先释放 $PORT；若进程处于 UEs 状态，需要重启 Mac 后再执行 ./start.sh"
    exit 1
  fi
fi

PID_FILE="$PID_DIR/funnel-${PORT}.pid"

if [[ -f "$PID_FILE" ]]; then
  OLD_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
  if is_service_pid "$OLD_PID"; then
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

UVICORN_ARGS_STR="$(printf '%s\x1f' "${UVICORN_ARGS[@]}")"

BOOT_PID="$(
ROOT_DIR="$ROOT_DIR" LOG_FILE="$LOG_FILE" UVICORN_ARGS_STR="$UVICORN_ARGS_STR" PYTHON_BIN="$PYTHON_BIN" "$PYTHON_BIN" - <<'PY'
import os
import subprocess

root_dir = os.environ["ROOT_DIR"]
log_file = os.environ["LOG_FILE"]
uvicorn_args = [arg for arg in os.environ.get("UVICORN_ARGS_STR", "").split("\x1f") if arg]
args = [os.environ.get("PYTHON_BIN", "python3"), "-m", "uvicorn", *uvicorn_args]

with open(log_file, "ab", buffering=0) as fh:
    proc = subprocess.Popen(
        args,
        cwd=root_dir,
        stdin=subprocess.DEVNULL,
        stdout=fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
        env=os.environ.copy(),
    )
print(proc.pid)
PY
)"

NEW_PID="$BOOT_PID"
for _ in {1..20}; do
  PORT_PID="$(listening_pid)"
  if [[ -n "$PORT_PID" ]]; then
    if is_service_pid "$PORT_PID"; then
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
  for _ in {1..10}; do
    if curl --max-time 2 -fsS "http://127.0.0.1:$PORT/" >/dev/null 2>&1; then
      break
    fi
    sleep 0.5
  done
else
  echo "启动失败，请检查日志: $LOG_FILE"
  rm -f "$PID_FILE"
  exit 1
fi
