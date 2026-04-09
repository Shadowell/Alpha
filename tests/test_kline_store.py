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

