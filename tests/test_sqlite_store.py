from app.services.sqlite_store import SQLiteStateStore


def test_sqlite_state_store_roundtrip(tmp_path):
    db_path = tmp_path / "funnel_state.db"
    store = SQLiteStateStore(str(db_path))

    payload = {
        "trade_date": "2026-04-09",
        "entries": {"000001": {"symbol": "000001", "score": 66.5}},
        "hot_concepts": [{"name": "算力", "heat": 0.91}],
        "hot_stocks": [{"rank": 1, "symbol": "000001", "name": "平安银行"}],
        "updated_at": "2026-04-09T18:50:00+08:00",
        "frozen": True,
    }

    store.save_state(payload)
    loaded = store.load_state()

    assert loaded is not None
    assert loaded["trade_date"] == payload["trade_date"]
    assert loaded["entries"]["000001"]["score"] == 66.5
    assert loaded["hot_concepts"][0]["name"] == "算力"
    assert loaded["hot_stocks"][0]["symbol"] == "000001"
    assert loaded["frozen"] is True
