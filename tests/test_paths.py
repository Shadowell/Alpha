"""#21：项目数据路径与 cwd 无关回归测试。"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _reload_paths():
    import app.services.paths as paths

    return importlib.reload(paths)


def test_project_root_is_absolute_repo_root(monkeypatch):
    monkeypatch.delenv("ALPHA_DATA_DIR", raising=False)
    paths = _reload_paths()

    assert paths.PROJECT_ROOT.is_absolute()
    assert paths.PROJECT_ROOT == ROOT
    assert paths.DATA_DIR == ROOT / "data"
    assert paths.data_path("funnel_state.db") == ROOT / "data" / "funnel_state.db"
    assert paths.FUNNEL_DB_PATH == ROOT / "data" / "funnel_state.db"
    assert paths.KLINE_DB_PATH == ROOT / "data" / "market_kline.db"
    assert paths.FUNNEL_DB_PATH.is_absolute()
    assert paths.KLINE_DB_PATH.is_absolute()


def test_data_path_keeps_absolute_paths_and_supports_override(monkeypatch, tmp_path):
    override = tmp_path / "alt-data"
    monkeypatch.setenv("ALPHA_DATA_DIR", str(override))
    paths = _reload_paths()

    assert paths.DATA_DIR == override.resolve()
    assert paths.data_path("jobs.db") == override.resolve() / "jobs.db"
    assert paths.FUNNEL_DB_PATH == override.resolve() / "funnel_state.db"

    absolute = tmp_path / "elsewhere" / "custom.db"
    assert paths.data_path(absolute) == absolute

    # 恢复默认常量，避免污染同进程内其它测试。
    monkeypatch.delenv("ALPHA_DATA_DIR", raising=False)
    _reload_paths()


def test_paths_resolve_from_other_cwd(tmp_path):
    """从任意 cwd 启动时仍解析到项目绝对路径。"""
    script = (
        "import json\n"
        "from app.services.paths import PROJECT_ROOT, DATA_DIR, FUNNEL_DB_PATH, KLINE_DB_PATH\n"
        "print(json.dumps({\n"
        "  'root': str(PROJECT_ROOT),\n"
        "  'data': str(DATA_DIR),\n"
        "  'funnel': str(FUNNEL_DB_PATH),\n"
        "  'kline': str(KLINE_DB_PATH),\n"
        "}))\n"
    )
    env = dict(os.environ)
    env.pop("ALPHA_DATA_DIR", None)
    env["PYTHONPATH"] = str(ROOT)
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    payload = json.loads(proc.stdout.strip().splitlines()[-1])

    assert Path(payload["root"]) == ROOT
    assert Path(payload["data"]) == ROOT / "data"
    assert Path(payload["funnel"]) == ROOT / "data" / "funnel_state.db"
    assert Path(payload["kline"]) == ROOT / "data" / "market_kline.db"
