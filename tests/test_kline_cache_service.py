import asyncio

import pandas as pd

from app.services.kline_cache_service import KlineCacheService
from app.services.kline_store import KlineSQLiteStore


class FakeProvider:
    def get_trade_days(self):
        dates = pd.bdate_range("2026-02-20", "2026-04-10")
        return pd.DataFrame({"trade_date": dates.date.astype(str)})

    def get_realtime_snapshot(self, **kwargs):
        return pd.DataFrame(
            {
                "代码": ["000001", "600111", "300001"],
                "名称": ["平安银行", "北方稀土", "创业测试"],
            }
        )

    def get_hist(self, symbol, start_date, end_date, adjust="qfq"):
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
    assert result["symbol_count"] == 2

    rows = service.get_kline("000001", 30)
    assert len(rows) == 30
    assert rows[-1]["date"] >= rows[0]["date"]

    status = service.get_sync_state()
    assert status["status"] == "success"
    assert status["last_success_trade_date"] == "2026-04-09"

