import asyncio

import pandas as pd

from app.services.kline_cache_service import KlineCacheService
from app.services.kline_store import KlineSQLiteStore


class FakeProvider:
    symbol_name_cache = None

    async def get_trade_days(self, min_days=0):
        dates = pd.bdate_range("2026-02-20", "2026-04-10")
        return pd.DataFrame({"trade_date": dates.date.astype(str)})

    async def get_realtime_snapshot(self, **kwargs):
        return pd.DataFrame(
            {
                "代码": ["000001", "600111", "300001"],
                "名称": ["平安银行", "北方稀土", "创业测试"],
            }
        )

    async def get_symbol_name_map(self, cache_ttl_seconds=3600):
        return {"000001": "平安银行", "600111": "北方稀土", "300001": "创业测试"}

    async def get_snapshot_spot(self, **kwargs):
        return pd.DataFrame()

    async def get_hist(self, symbol, start_date, end_date, adjust="qfq", force_remote=False):
        dates = pd.bdate_range("2026-03-01", "2026-04-10").date.astype(str)
        return pd.DataFrame(
            {
                "日期": dates,
                "开盘": [10.0] * len(dates),
                "最高": [10.5] * len(dates),
                "最低": [9.8] * len(dates),
                "收盘": [10.2] * len(dates),
                "成交量": [1000000] * len(dates),
                "成交额": [10000000] * len(dates),
            }
        )


def test_kline_cache_service_sync_trade_date(tmp_path):
    store = KlineSQLiteStore(str(tmp_path / "market_kline.db"))
    service = KlineCacheService(provider=FakeProvider(), store=store, window_days=30)
    result = asyncio.run(service.sync_trade_date(trade_date="2026-04-09", force=True))
    assert result["success"] is True
    assert result["symbol_count"] == 3

    rows = service.get_kline("000001", 30)
    assert len(rows) == 30
    assert rows[-1]["date"] >= rows[0]["date"]

    status = service.get_sync_state()
    assert status["status"] == "success"
    assert status["last_success_trade_date"] == "2026-04-09"
    assert status["total_symbols"] >= 2
    assert status["synced_symbols"] >= 2
    assert status["task_id"] is not None

    logs = service.list_sync_logs(page=1, page_size=10)
    assert logs["total"] >= 1
    task_id = logs["items"][0]["task_id"]
    detail = service.get_sync_log_detail(task_id)
    assert detail is not None
    assert detail["task"]["task_id"] == task_id


def test_kline_cache_service_marks_interrupted_tasks_failed(tmp_path):
    store = KlineSQLiteStore(str(tmp_path / "market_kline.db"))
    service = KlineCacheService(provider=FakeProvider(), store=store, window_days=30)
    store.start_sync_task(
        task_id="stale-task",
        trigger_mode="manual",
        trade_date="2026-04-09",
        total_symbols=3,
        started_at="2026-04-09T15:00:00+08:00",
    )
    store.set_sync_state(
        attempt_trade_date="2026-04-09",
        success_trade_date=None,
        status="running",
        symbol_count=1,
        total_symbols=3,
        synced_symbols=1,
        success_symbols=1,
        failed_symbols=0,
        task_id="stale-task",
        trigger_mode="manual",
        updated_at="2026-04-09T15:00:01+08:00",
        message="补缺同步 1/3",
    )

    status = service.get_sync_state()
    assert status["status"] == "failed"
    assert status["message"] == "上次同步中断，请重新提交同步任务"

    detail = service.get_sync_log_detail("stale-task")
    assert detail is not None
    assert detail["task"]["status"] == "failed"
    assert detail["task"]["finished_at"]
