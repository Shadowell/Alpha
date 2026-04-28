from app.services.kline_store import KlineSQLiteStore


def test_kline_store_roundtrip(tmp_path):
    store = KlineSQLiteStore(str(tmp_path / "market_kline.db"))
    rows = [
        {
            "trade_date": "2026-04-07",
            "open": 10.1,
            "high": 10.8,
            "low": 9.9,
            "close": 10.5,
            "volume": 1200000,
            "amount": 12800000,
        },
        {
            "trade_date": "2026-04-08",
            "open": 10.6,
            "high": 11.0,
            "low": 10.3,
            "close": 10.9,
            "volume": 1500000,
            "amount": 15800000,
        },
    ]
    n = store.upsert_symbol_klines("000001", rows, "2026-04-09T16:00:00+08:00")
    assert n == 2

    out = store.get_kline("000001", days=30)
    assert len(out) == 2
    assert out[-1]["date"] == "2026-04-08"
    assert out[-1]["close"] == 10.9


def test_kline_store_records_sync_batch(tmp_path):
    store = KlineSQLiteStore(str(tmp_path / "market_kline.db"))
    store.start_sync_task(
        task_id="task-1",
        trigger_mode="manual",
        trade_date="2026-04-09",
        total_symbols=2,
        started_at="2026-04-09T15:00:00+08:00",
    )
    written = store.record_sync_batch(
        kline_items=[
            (
                "000001",
                {
                    "trade_date": "2026-04-09",
                    "open": 10.0,
                    "high": 11.0,
                    "low": 9.8,
                    "close": 10.5,
                    "volume": 1000,
                    "amount": 10000,
                },
            )
        ],
        detail_rows=[
            {
                "task_id": "task-1",
                "symbol": "000001",
                "status": "success",
                "elapsed_ms": 12,
                "error_message": "",
                "created_at": "2026-04-09T15:00:01+08:00",
            },
            {
                "task_id": "task-1",
                "symbol": "000002",
                "status": "failed",
                "elapsed_ms": 20,
                "error_message": "数据为空",
                "created_at": "2026-04-09T15:00:01+08:00",
            },
        ],
        updated_at="2026-04-09T15:00:01+08:00",
        task_id="task-1",
        synced_symbols=2,
        success_symbols=1,
        failed_symbols=1,
        attempt_trade_date="2026-04-09",
        success_trade_date=None,
        status="running",
        symbol_count=1,
        total_symbols=2,
        trigger_mode="manual",
        message="补缺同步 2/2",
    )

    assert written == 1
    assert store.get_kline("000001", days=1)[0]["close"] == 10.5

    state = store.get_sync_state()
    assert state["status"] == "running"
    assert state["synced_symbols"] == 2
    assert state["success_symbols"] == 1
    assert state["failed_symbols"] == 1

    detail = store.get_sync_task_detail("task-1")
    assert detail is not None
    assert detail["task"]["synced_symbols"] == 2
    assert len(detail["items"]) == 2
