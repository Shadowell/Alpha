"""项目数据/日志绝对路径统一入口。

所有需要落盘或 checkpoint 的模块都应从这里取路径，禁止再写
``"data/xxx.db"`` 这类相对路径——否则从其他 cwd 启动时 DB 会落到别处，
WAL checkpoint 也会静默跳过（参见 issue #21）。

可通过环境变量覆盖：

- ``ALPHA_DATA_DIR``：数据目录，默认 ``<project>/data``
- ``ALPHA_LOG_DIR``：日志目录，默认 ``<project>/logs``
"""

from __future__ import annotations

import os
from pathlib import Path

# app/services/paths.py -> app/services -> app -> <project>
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _resolve_dir(env_name: str, default: Path) -> Path:
    raw = os.environ.get(env_name, "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return default


DATA_DIR = _resolve_dir("ALPHA_DATA_DIR", PROJECT_ROOT / "data")
LOGS_DIR = _resolve_dir("ALPHA_LOG_DIR", PROJECT_ROOT / "logs")


def data_path(name: str | Path) -> Path:
    """返回数据文件的绝对路径；已是绝对路径时原样返回。"""
    path = Path(name).expanduser()
    if path.is_absolute():
        return path
    return DATA_DIR / path


FUNNEL_DB_PATH = data_path("funnel_state.db")
KLINE_DB_PATH = data_path("market_kline.db")
FUNNEL_STATE_JSON_PATH = data_path("funnel_state.json")
JOBS_DB_PATH = data_path("jobs.db")


def checkpoint_paths() -> tuple[Path, ...]:
    """lifespan shutdown / 手动 checkpoint 需要处理的 SQLite 绝对路径。"""
    return (FUNNEL_DB_PATH, KLINE_DB_PATH)
