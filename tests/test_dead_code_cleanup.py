"""#20：死代码清理与 legacy 归属回归测试。"""

from __future__ import annotations

import importlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_unused_full_table_snapshot_helpers_are_removed():
    """未调用的全表扫描快照入口不得再对外暴露。"""
    from app.services.funnel_service import FunnelService
    from app.services.kline_cache_service import KlineCacheService

    assert not hasattr(FunnelService, "_pick_screen_snapshot")
    assert not hasattr(FunnelService, "_build_cache_snapshot")
    assert not hasattr(KlineCacheService, "build_snapshot_for_screen")

    for rel in ("app/services/funnel_service.py", "app/services/kline_cache_service.py"):
        source = (ROOT / rel).read_text(encoding="utf-8")
        assert "build_snapshot_for_screen" not in source
        assert "_pick_screen_snapshot" not in source
        assert "_build_cache_snapshot" not in source


def test_daban_scripts_live_under_legacy_only():
    """旧版打板脚本迁移到 legacy/daban，原路径不再存在。"""
    legacy_dir = ROOT / "legacy" / "daban"
    assert legacy_dir.is_dir()
    assert sorted(p.name for p in legacy_dir.glob("*.py")) == [
        "daban.py",
        "daban_2.py",
        "daban_3.py",
    ]
    assert not (ROOT / "strategy" / "daban").exists()
    # 归属说明：目录入口文档保留迁移原因。
    assert (legacy_dir / "README.md").is_file()

    for name in ("daban.py", "daban_2.py", "daban_3.py"):
        source = (legacy_dir / name).read_text(encoding="utf-8")
        assert source.strip(), f"{name} 迁移后不应为空"


def test_quiet_breakout_scanner_remains_importable():
    """quiet_breakout_scanner 是兼容保留路径，清理后仍可导入。"""
    module = importlib.import_module("app.services.quiet_breakout_scanner")
    assert hasattr(module, "QuietBreakoutConfig")
    assert hasattr(module, "QuietBreakoutScanner")

    source = (ROOT / "app/services/quiet_breakout_scanner.py").read_text(encoding="utf-8")
    assert "兼容保留路径" in source
