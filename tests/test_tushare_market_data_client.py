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
