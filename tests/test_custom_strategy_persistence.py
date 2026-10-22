"""#49：自定义策略扫描快照持久化与重启恢复。"""

from __future__ import annotations

import asyncio

from app.services.custom_strategy import CustomStrategy, CustomStrategyScanner
from app.services.sqlite_store import SQLiteStateStore


class FakeKlineStore:
    def __init__(self, max_date="2026-10-21"):
        self.max_date = max_date

    def get_stats(self):
        return {"max_date": self.max_date}

    def get_all_symbols(self):
        return []


def _strategy():
    return CustomStrategy(id="test-strategy", name="测试策略", description="", rules=[])


def test_empty_scan_persists_snapshot_and_job_metadata(tmp_path):
    store = SQLiteStateStore(str(tmp_path / "state.db"))
    scanner = CustomStrategyScanner(FakeKlineStore(), state_store=store)

    payload = asyncio.run(scanner.scan(_strategy(), job_id="job-49"))

    assert payload["generated_at"]
    assert payload["job_id"] == "job-49"
    assert payload["data_date"] == "2026-10-21"
    assert store.get_kv("custom_strategy_scan:test-strategy")["job_id"] == "job-49"


def test_restarts_load_last_snapshot(tmp_path):
    db = str(tmp_path / "state.db")
    first = CustomStrategyScanner(FakeKlineStore(), state_store=SQLiteStateStore(db))
    generated_at = asyncio.run(first.scan(_strategy()))["generated_at"]

    second = CustomStrategyScanner(FakeKlineStore(), state_store=SQLiteStateStore(db))
    restored = second.get_last_snapshot("test-strategy")

    assert restored is not None
    assert restored["generated_at"] == generated_at
    assert restored["stale"] is False


def test_snapshot_becomes_stale_when_newer_kline_date_exists(tmp_path):
    store = SQLiteStateStore(str(tmp_path / "state.db"))
    scanner = CustomStrategyScanner(FakeKlineStore(), state_store=store)
    asyncio.run(scanner.scan(_strategy()))
    scanner.store = FakeKlineStore(max_date="2026-10-22")

    snapshot = scanner.get_last_snapshot("test-strategy")

    assert snapshot is not None
    assert snapshot["data_date"] == "2026-10-21"
    assert snapshot["latest_data_date"] == "2026-10-22"
    assert snapshot["stale"] is True
