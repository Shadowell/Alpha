"""#46：历史 trade_date 读取不得修改当前三池状态。"""

from __future__ import annotations

import asyncio

import pandas as pd

from app.services.funnel_service import FunnelService
from app.services.sqlite_store import SQLiteStateStore
from app.services.time_utils import now_cn


class ReadOnlyProvider:
    kline_store = None

    async def get_all_concepts(self, cache_seconds=30):
        return pd.DataFrame()

    async def get_concept_constituents(self, concept_name):
        return pd.DataFrame()

    async def get_hot_stocks(self, top_n=10, **kwargs):
        return pd.DataFrame(columns=["rank", "symbol", "name", "latest_price", "change_amount", "change_pct"])

    async def get_realtime_snapshot(self, **kwargs):
        return pd.DataFrame()

    async def get_symbol_name_map(self, cache_ttl_seconds=3600):
        return {}


def _service_with_pool(tmp_path):
    db_path = str(tmp_path / "funnel_state.db")
    service = FunnelService(provider=ReadOnlyProvider(), persist_db_path=db_path)
    service.trade_date = "2026-10-22"
    service.updated_at = "2026-10-22T10:00:00+08:00"
    service.hot_stocks_updated_at = service.updated_at
    service.entries = {
        "600000": {
            "symbol": "600000",
            "name": "浦发银行",
            "pool": "candidate",
            "score": 80,
            "prev_score": 70,
            "updated_at": service.updated_at,
            "metrics": {},
            "concept_tags": [],
            "reasons": [],
            "warnings": [],
            "trigger_log": [],
        },
        "600001": {
            "symbol": "600001",
            "name": "测试股",
            "pool": "focus",
            "score": 90,
            "prev_score": 80,
            "updated_at": service.updated_at,
            "metrics": {},
            "concept_tags": [],
            "reasons": [],
            "warnings": [],
            "trigger_log": [],
        },
    }
    service._save_state()
    return service


def test_historical_funnel_read_does_not_reset_current_state(tmp_path):
    service = _service_with_pool(tmp_path)
    before = SQLiteStateStore(service.state_store.db_path).load_state()

    result = asyncio.run(service.get_funnel("2020-01-02"))

    assert result.trade_date == "2020-01-02"
    assert result.stats == {"candidate": 0, "focus": 0, "buy": 0}
    after = SQLiteStateStore(service.state_store.db_path).load_state()
    assert after == before
    assert set(service.entries) == {"600000", "600001"}
    assert service.trade_date == "2026-10-22"


def test_current_funnel_read_still_returns_live_pools(tmp_path):
    service = _service_with_pool(tmp_path)

    result = asyncio.run(service.get_funnel())

    assert result.trade_date == "2026-10-22"
    assert result.stats == {"candidate": 1, "focus": 1, "buy": 0}


def test_historical_market_panel_reads_do_not_refresh_or_reset(tmp_path):
    service = _service_with_pool(tmp_path)
    before = SQLiteStateStore(service.state_store.db_path).load_state()

    concepts = asyncio.run(service.get_hot_concepts("2020-01-02"))
    stocks = asyncio.run(service.get_hot_stocks("2020-01-02"))

    assert concepts.items == []
    assert stocks.items == []
    assert SQLiteStateStore(service.state_store.db_path).load_state() == before


def test_historical_stock_detail_returns_not_found_without_state_change(tmp_path):
    service = _service_with_pool(tmp_path)
    before = SQLiteStateStore(service.state_store.db_path).load_state()

    try:
        asyncio.run(service.get_stock_detail("600000", trade_date="2020-01-02"))
    except KeyError:
        pass
    else:
        raise AssertionError("historical detail should not expose current state")

    assert SQLiteStateStore(service.state_store.db_path).load_state() == before


def test_roll_to_current_trade_date_only_advances_to_current_cn_date(tmp_path):
    service = _service_with_pool(tmp_path)
    service.trade_date = "2020-01-02"
    asyncio.run(service.roll_to_current_trade_date())
    today = now_cn().date().isoformat()
    assert service.trade_date == today
    assert service.entries == {}