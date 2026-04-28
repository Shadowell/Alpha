#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${PORT:-18890}"
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

prepare_app_shutdown() {
  echo "重启准备: 请求应用释放数据库资源..."
  if curl --max-time 5 -fsS -X POST "http://127.0.0.1:${PORT}/api/admin/shutdown-prepare?reason=restart" >/dev/null 2>&1; then
    echo "重启准备: 应用已完成数据库资源释放"
  else
    echo "重启准备: 应用未响应，继续执行脚本级数据库 checkpoint"
  fi
}

checkpoint_sqlite() {
  ROOT_DIR="$ROOT_DIR" "$PYTHON_BIN" - <<'PY'
import os
import sqlite3
from pathlib import Path

root = Path(os.environ["ROOT_DIR"])
for rel in ("data/funnel_state.db", "data/market_kline.db"):
    path = root / rel
    if not path.exists():
        continue
    try:
        conn = sqlite3.connect(str(path), timeout=1.0)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
        print(f"重启准备: checkpoint {rel} 完成")
    except Exception as exc:
        print(f"重启准备: checkpoint {rel} 失败（继续重启）: {exc}")
PY
}

prepare_app_shutdown
checkpoint_sqlite
PORT="$PORT" "$ROOT_DIR/stop.sh"
PORT="$PORT" "$ROOT_DIR/start.sh"
