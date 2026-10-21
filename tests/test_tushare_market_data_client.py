import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd

from app.services.tushare_market_data_client import TushareFirstMarketDataClient


def _run(coro):
    return asyncio.run(coro)


class FakeFallback:
    async def fetch_hist(self, symbol, start_date, end_date, adjust="qfq"):
        return pd.DataFrame(
            [{"日期": "2026-08-07", "开盘": 9, "收盘": 10, "最高": 11, "最低": 8, "成交量": 2, "成交额": 20}]
        )

    async def fetch_spot(self):
        return pd.DataFrame([{"代码": "600000", "名称": "浦发银行", "最新价": 10}])

    async def fetch_trade_days(self, min_days=0):
        return pd.DataFrame({"trade_date": ["2026-08-07"]})


def test_tushare_daily_normalizes_dates_units_and_provenance():
    raw = pd.DataFrame(
        [
            {
                "ts_code": "600000.SH",
                "trade_date": "20260807",
                "open": 9.8,
                "close": 10.2,
                "high": 10.5,
                "low": 9.7,
                "vol": 123.0,
                "amount": 456.7,
            }
        ]
    )
    pro = SimpleNamespace(daily=Mock(return_value=raw))
    tushare = SimpleNamespace(set_token=Mock(), pro_api=Mock(return_value=pro))
    client = TushareFirstMarketDataClient(
        fallback=FakeFallback(), token="test-token", tushare_module=tushare
    )

    frame = _run(client.fetch_daily_by_trade_date("2026-08-07"))

    assert frame.iloc[0]["代码"] == "600000"
    assert frame.iloc[0]["日期"] == "2026-08-07"
    assert frame.iloc[0]["成交量"] == 123.0
    assert frame.iloc[0]["成交额"] == 456700.0
    assert frame.attrs["data_source"] == "tushare"
    pro.daily.assert_called_once_with(trade_date="20260807")


def test_missing_token_falls_back_and_explains_reason():
    client = TushareFirstMarketDataClient(
        fallback=FakeFallback(), token="", tushare_module=SimpleNamespace()
    )

    frame = _run(client.fetch_hist("600000", "20260807", "20260807"))

    assert frame.iloc[0]["收盘"] == 10
    assert frame.attrs["data_source"] == "eastmoney"
    assert frame.attrs["fallback_reason"] == "tushare_not_configured"
    assert client.describe()["ready"] is False


def test_stock_basic_is_normalized_to_alpha_symbol_map():
    pro = SimpleNamespace(
        stock_basic=Mock(
            return_value=pd.DataFrame(
                [
                    {"ts_code": "600000.SH", "symbol": "600000", "name": "浦发银行"},
                    {"ts_code": "000001.SZ", "symbol": "000001", "name": "平安银行"},
                ]
            )
        )
    )
    tushare = SimpleNamespace(set_token=Mock(), pro_api=Mock(return_value=pro))
    client = TushareFirstMarketDataClient(
        fallback=FakeFallback(), token="test-token", tushare_module=tushare
    )

    names = _run(client.fetch_symbol_names())

    assert names == {"600000": "浦发银行", "000001": "平安银行"}
    assert client.describe()["last_operation"]["source"] == "tushare"


def _client_with_pro(pro, tushare=None):
    tushare = tushare or SimpleNamespace(set_token=Mock(), pro_api=Mock(return_value=pro))
    return TushareFirstMarketDataClient(
        fallback=FakeFallback(), token="test-token", tushare_module=tushare
    )


def test_daily_basic_converts_market_value_to_yuan():
    pro = SimpleNamespace(
        daily_basic=Mock(
            return_value=pd.DataFrame(
                [
                    {
                        "ts_code": "600000.SH",
                        "trade_date": "20261021",
                        "turnover_rate": 1.2,
                        "turnover_rate_f": 1.5,
                        "volume_ratio": 0.9,
                        "pe_ttm": 6.1,
                        "pb": 0.7,
                        "ps_ttm": 2.2,
                        "total_mv": 10000.0,
                        "circ_mv": 8000.0,
                    }
                ]
            )
        )
    )
    client = _client_with_pro(pro)

    frame = _run(client.fetch_daily_basic_by_trade_date("2026-10-21"))

    row = frame.iloc[0]
    assert row["symbol"] == "600000"
    assert row["trade_date"] == "2026-10-21"
    assert row["total_mv"] == 100000000.0
    assert row["circ_mv"] == 80000000.0
    assert row["pe_ttm"] == 6.1
    assert frame.attrs["data_source"] == "tushare"


def test_price_limits_and_adj_factors_are_symbol_normalized():
    pro = SimpleNamespace(
        stk_limit=Mock(
            return_value=pd.DataFrame(
                [
                    {
                        "ts_code": "000001.SZ",
                        "trade_date": "20261021",
                        "up_limit": 12.5,
                        "down_limit": 10.3,
                    }
                ]
            )
        ),
        adj_factor=Mock(
            return_value=pd.DataFrame(
                [{"ts_code": "000001.SZ", "trade_date": "20261021", "adj_factor": 1.8}]
            )
        ),
    )
    client = _client_with_pro(pro)

    limits = _run(client.fetch_price_limits_by_trade_date("20261021"))
    adj = _run(client.fetch_adj_factors_by_trade_date("20261021"))

    assert limits.iloc[0]["symbol"] == "000001"
    assert limits.iloc[0]["up_limit"] == 12.5
    assert adj.iloc[0]["adj_factor"] == 1.8


def test_moneyflow_and_hsgt_convert_wan_to_yuan():
    pro = SimpleNamespace(
        moneyflow=Mock(
            return_value=pd.DataFrame(
                [
                    {
                        "ts_code": "600000.SH",
                        "trade_date": "20261021",
                        "buy_sm_amount": 1.0,
                        "sell_sm_amount": 2.0,
                        "buy_md_amount": 3.0,
                        "sell_md_amount": 4.0,
                        "buy_lg_amount": 5.0,
                        "sell_lg_amount": 6.0,
                        "buy_elg_amount": 7.0,
                        "sell_elg_amount": 8.0,
                        "net_mf_amount": 9.0,
                    }
                ]
            )
        ),
        moneyflow_hsgt=Mock(
            return_value=pd.DataFrame(
                [
                    {
                        "trade_date": "20261021",
                        "hgt": 1.0,
                        "sgt": 2.0,
                        "north_money": 3.0,
                        "south_money": 4.0,
                    }
                ]
            )
        ),
    )
    client = _client_with_pro(pro)

    flow = _run(client.fetch_moneyflow_by_trade_date("20261021"))
    hsgt = _run(client.fetch_moneyflow_hsgt("20261021", "20261021"))

    assert flow.iloc[0]["net_mf_amount"] == 90000.0
    assert hsgt.iloc[0]["north_money"] == 30000.0


def test_index_daily_keeps_ts_code_and_unit_conventions():
    pro = SimpleNamespace(
        index_daily=Mock(
            return_value=pd.DataFrame(
                [
                    {
                        "ts_code": "000001.SH",
                        "trade_date": "20261021",
                        "open": 3900.0,
                        "high": 3920.0,
                        "low": 3880.0,
                        "close": 3910.0,
                        "pre_close": 3890.0,
                        "change": 20.0,
                        "pct_chg": 0.5,
                        "vol": 100.0,
                        "amount": 200.0,
                    }
                ]
            )
        )
    )
    client = _client_with_pro(pro)

    frame = _run(client.fetch_index_daily("20261021", ["000001.SH"]))

    row = frame.iloc[0]
    assert row["ts_code"] == "000001.SH"
    assert row["volume"] == 100.0
    assert row["amount"] == 200000.0


def test_rt_min_normalizes_bar_time_and_units():
    pro = SimpleNamespace(
        rt_min=Mock(
            return_value=pd.DataFrame(
                [
                    {
                        "ts_code": "000001.SZ",
                        "freq": "1MIN",
                        "time": "2026-10-21 15:00:00",
                        "open": 11.41,
                        "close": 11.41,
                        "high": 11.41,
                        "low": 11.41,
                        "vol": 751500.0,
                        "amount": 8574615.0,
                    }
                ]
            )
        )
    )
    client = _client_with_pro(pro)

    frame = _run(client.fetch_rt_min("000001"))

    row = frame.iloc[0]
    assert row["symbol"] == "000001"
    assert row["bar_time"] == "2026-10-21 15:00:00"
    assert row["trade_date"] == "2026-10-21"
    assert row["volume"] == 751500.0
    assert row["amount"] == 8574615.0
    assert row["source"] == "tushare.rt_min"


def test_realtime_quote_returns_normalized_snapshot():
    quote = pd.DataFrame(
        [
            {
                "TS_CODE": "000001.SZ",
                "PRICE": 11.41,
                "DATE": "20261021",
                "TIME": "16:30:00",
                "VOLUME": 81354137.0,
                "AMOUNT": 930546787.77,
            }
        ]
    )
    tushare = SimpleNamespace(
        set_token=Mock(),
        pro_api=Mock(return_value=SimpleNamespace()),
        realtime_quote=Mock(return_value=quote),
    )
    client = _client_with_pro(SimpleNamespace(), tushare=tushare)

    snapshot = _run(client.fetch_realtime_quote("000001"))

    assert snapshot["symbol"] == "000001"
    assert snapshot["price"] == 11.41
    assert snapshot["bar_time"] == "2026-10-21 16:30:00"
    assert snapshot["source"] == "tushare.realtime_quote"
def test_tushare_timeout_falls_back_to_eastmoney(monkeypatch):
    import time

    monkeypatch.setenv("ALPHA_EXTERNAL_TIMEOUT_SECONDS", "0.15")

    def _slow_daily(**kwargs):
        time.sleep(0.8)
        return pd.DataFrame()

    pro = SimpleNamespace(daily=Mock(side_effect=_slow_daily))
    tushare = SimpleNamespace(set_token=Mock(), pro_api=Mock(return_value=pro))
    client = TushareFirstMarketDataClient(
        fallback=FakeFallback(), token="test-token", tushare_module=tushare
    )

    started = time.monotonic()
    frame = _run(client.fetch_hist("600000", "20260807", "20260807"))
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert frame.iloc[0]["收盘"] == 10
    assert frame.attrs["data_source"] == "eastmoney"
    assert frame.attrs["fallback_reason"] == "tushare_timeout"
    assert client.describe()["last_operation"]["source"] == "eastmoney"
