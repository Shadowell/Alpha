from app.services.kline_store import KlineSQLiteStore


def _store(tmp_path):
    return KlineSQLiteStore(str(tmp_path / "market_kline.db"))


def test_daily_extras_partial_upserts_preserve_existing_fields(tmp_path):
    store = _store(tmp_path)
    store.upsert_daily_extras(
        [
            {
                "symbol": "600000",
                "trade_date": "2026-10-21",
                "pe_ttm": 6.1,
                "pb": 0.7,
                "turnover_rate": 1.2,
                "total_mv": 100000000.0,
            }
        ],
        "2026-10-21T16:00:00+08:00",
    )
    store.upsert_daily_extras(
        [
            {
                "symbol": "600000",
                "trade_date": "2026-10-21",
                "up_limit": 12.5,
                "down_limit": 10.3,
                "adj_factor": 1.8,
            }
        ],
        "2026-10-21T16:01:00+08:00",
    )

    rows = store.get_daily_extras("600000", days=5)

    assert len(rows) == 1
    row = rows[0]
    assert row["pe_ttm"] == 6.1
    assert row["turnover_rate"] == 1.2
    assert row["up_limit"] == 12.5
    assert row["adj_factor"] == 1.8


def test_moneyflow_hsgt_index_and_minute_roundtrip(tmp_path):
    store = _store(tmp_path)
    store.upsert_moneyflow(
        [
            {
                "symbol": "600000",
                "trade_date": "2026-10-21",
                "buy_lg_amount": 1000.0,
                "net_mf_amount": -200.0,
            }
        ],
        "2026-10-21T16:00:00+08:00",
    )
    store.upsert_market_capital_flow(
        [{"trade_date": "2026-10-21", "north_money": 3000000.0}],
        "2026-10-21T16:00:00+08:00",
    )
    store.upsert_index_daily(
        [
            {
                "ts_code": "000001.SH",
                "trade_date": "2026-10-21",
                "close": 3910.0,
                "pct_chg": 0.5,
            }
        ],
        "2026-10-21T16:00:00+08:00",
    )
    store.upsert_minute_bars(
        [
            {
                "symbol": "600000",
                "bar_time": "2026-10-21 15:00:00",
                "trade_date": "2026-10-21",
                "open": 10.1,
                "high": 10.1,
                "low": 10.1,
                "close": 10.1,
                "volume": 1000.0,
                "amount": 10100.0,
                "source": "tushare.rt_min",
            }
        ],
        "2026-10-21T15:00:30+08:00",
    )

    flow = store.get_moneyflow("600000")
    hsgt = store.get_market_capital_flow()
    index_rows = store.get_index_daily("000001.SH")
    bars = store.get_minute_bars("600000", "2026-10-21")

    assert flow[0]["net_mf_amount"] == -200.0
    assert hsgt[0]["north_money"] == 3000000.0
    assert index_rows[0]["close"] == 3910.0
    assert bars[0]["bar_time"] == "2026-10-21 15:00:00"
    assert bars[0]["source"] == "tushare.rt_min"


def test_enrichment_overview_combines_state_and_table_stats(tmp_path):
    store = _store(tmp_path)
    store.upsert_daily_extras(
        [{"symbol": "600000", "trade_date": "2026-10-21", "pe_ttm": 6.1}],
        "2026-10-21T16:00:00+08:00",
    )
    store.upsert_minute_bars(
        [
            {
                "symbol": "600000",
                "bar_time": "2026-10-21 15:00:00",
                "trade_date": "2026-10-21",
                "close": 10.1,
            }
        ],
        "2026-10-21T15:00:30+08:00",
    )
    store.set_enrichment_state(
        "daily_extras",
        trade_date="2026-10-21",
        status="success",
        row_count=2,
        message="ok",
        updated_at="2026-10-21T16:00:00+08:00",
    )
    store.set_enrichment_state(
        "minute",
        trade_date="2026-10-21",
        status="success",
        row_count=1,
        message="ok",
        updated_at="2026-10-21T15:00:30+08:00",
    )

    overview = {item["dataset"]: item for item in store.get_enrichment_overview()}

    assert overview["daily_extras"]["status"] == "success"
    assert overview["daily_extras"]["latest_trade_date"] == "2026-10-21"
    assert overview["daily_extras"]["total_rows"] == 1
    assert overview["minute"]["latest_bar_time"] == "2026-10-21 15:00:00"
    assert overview["moneyflow"]["status"] == "none"
