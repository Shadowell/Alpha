from __future__ import annotations

import datetime as dt

import pandas as pd

from app.services.kline_cache_service import KlineCacheService
from app.services.kline_store import KlineSQLiteStore


TRADE_DATE = (dt.date.today() - dt.timedelta(days=1)).isoformat()


class FakeEnrichmentClient:
    is_ready = True

    def __init__(self, fail_moneyflow: bool = False) -> None:
        self.fail_moneyflow = fail_moneyflow

    async def fetch_daily_basic_by_trade_date(self, trade_date: str) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "symbol": "600000",
                    "trade_date": trade_date,
                    "pe_ttm": 6.1,
                    "turnover_rate": 1.2,
                    "total_mv": 100000000.0,
                }
            ]
        )

    async def fetch_price_limits_by_trade_date(self, trade_date: str) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "symbol": "600000",
                    "trade_date": trade_date,
                    "up_limit": 12.5,
                    "down_limit": 10.3,
                }
            ]
        )

    async def fetch_adj_factors_by_trade_date(self, trade_date: str) -> pd.DataFrame:
        return pd.DataFrame(
            [{"symbol": "600000", "trade_date": trade_date, "adj_factor": 1.8}]
        )

    async def fetch_moneyflow_by_trade_date(self, trade_date: str) -> pd.DataFrame:
        if self.fail_moneyflow:
            raise RuntimeError("no permission")
        return pd.DataFrame(
            [
                {
                    "symbol": "600000",
                    "trade_date": trade_date,
                    "buy_lg_amount": 1000.0,
                    "net_mf_amount": -200.0,
                }
            ]
        )

    async def fetch_moneyflow_hsgt(self, start_date: str, end_date: str) -> pd.DataFrame:
        return pd.DataFrame(
            [{"trade_date": end_date, "north_money": 3000000.0}]
        )

    async def fetch_index_daily(
        self, trade_date: str, ts_codes: list[str] | None = None
    ) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "ts_code": "000001.SH",
                    "trade_date": trade_date,
                    "close": 3910.0,
                    "pct_chg": 0.5,
                }
            ]
        )

    async def fetch_rt_min(self, symbol: str, freq: str = "1MIN") -> pd.DataFrame:
        if symbol == "600000":
            return pd.DataFrame(
                [
                    {
                        "symbol": symbol,
                        "bar_time": f"{TRADE_DATE} 15:00:00",
                        "trade_date": TRADE_DATE,
                        "open": 10.1,
                        "high": 10.1,
                        "low": 10.1,
                        "close": 10.1,
                        "volume": 1000.0,
                        "amount": 10100.0,
                        "source": "tushare.rt_min",
                    }
                ]
            )
        return pd.DataFrame()

    async def fetch_realtime_quote(self, symbol: str) -> dict | None:
        return {
            "symbol": symbol,
            "price": 20.2,
            "bar_time": f"{TRADE_DATE} 14:59:00",
            "volume": 0.0,
            "amount": 0.0,
            "source": "tushare.realtime_quote",
        }


class DummyProvider:
    pass


def _service(tmp_path, client):
    store = KlineSQLiteStore(str(tmp_path / "market_kline.db"))
    return KlineCacheService(
        provider=DummyProvider(),
        store=store,
        market_data_client=client,
    ), store


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def test_sync_enrichment_writes_datasets_and_isolates_failure(tmp_path):
    service, store = _service(tmp_path, FakeEnrichmentClient(fail_moneyflow=True))

    result = _run(service.sync_enrichment(TRADE_DATE))

    assert result["success"] is False
    by_dataset = {item["dataset"]: item for item in result["datasets"]}
    assert by_dataset["daily_extras"]["status"] == "success"
    assert by_dataset["moneyflow"]["status"] == "failed"
    assert by_dataset["hsgt"]["status"] == "success"
    assert by_dataset["index_daily"]["status"] == "success"

    extras = store.get_daily_extras("600000")
    assert extras[0]["pe_ttm"] == 6.1
    assert extras[0]["up_limit"] == 12.5
    assert extras[0]["adj_factor"] == 1.8
    assert store.get_index_daily("000001.SH")[0]["close"] == 3910.0
    assert store.get_market_capital_flow()[0]["north_money"] == 3000000.0


def test_sync_buy_pool_minutes_uses_quote_fallback(tmp_path):
    service, store = _service(tmp_path, FakeEnrichmentClient())

    result = _run(
        service.sync_buy_pool_minutes(["600000", "000001"], force=True)
    )

    assert result["row_count"] == 2
    bars_600000 = store.get_minute_bars("600000", TRADE_DATE)
    bars_000001 = store.get_minute_bars("000001", TRADE_DATE)
    assert bars_600000[0]["source"] == "tushare.rt_min"
    assert bars_000001[0]["source"] == "tushare.realtime_quote"
    assert bars_000001[0]["close"] == 20.2


def test_sync_buy_pool_minutes_skips_off_hours(tmp_path, monkeypatch):
    service, store = _service(tmp_path, FakeEnrichmentClient())
    monkeypatch.setattr(
        KlineCacheService, "_is_trading_session", staticmethod(lambda now=None: False)
    )

    result = _run(service.sync_buy_pool_minutes(["600000"]))

    assert result["skipped_off_hours"] is True
    assert store.get_minute_bars("600000") == []
