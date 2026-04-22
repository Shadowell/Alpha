import asyncio
from datetime import timedelta

import pandas as pd

from app.services.funnel_service import FunnelService
from app.services.time_utils import now_cn


class EmptyConceptProvider:
    async def get_all_concepts(self, cache_seconds=30):
        return pd.DataFrame()

    async def get_concept_constituents(self, concept_name):
        return pd.DataFrame()

    async def get_hot_stocks(self, top_n=10, **kwargs):
        return pd.DataFrame(columns=["rank", "symbol", "name", "latest_price", "change_amount", "change_pct"])

    async def get_realtime_snapshot(self, **kwargs):
        return pd.DataFrame()


class CountingHotStockProvider(EmptyConceptProvider):
    def __init__(self):
        self.calls = 0

    async def get_hot_stocks(self, top_n=10, **kwargs):
        self.calls += 1
        return pd.DataFrame(
            [
                {
                    "rank": 1,
                    "symbol": "600111",
                    "name": "北方稀土",
                    "latest_price": 24.1,
                    "change_amount": 0.12,
                    "change_pct": 0.5,
                }
            ]
        )


def test_refresh_concepts_keeps_previous_hot_concepts_when_source_empty(tmp_path):
    provider = EmptyConceptProvider()
    service = FunnelService(provider=provider, persist_db_path=str(tmp_path / "state.db"))
    service.hot_concepts = [{"name": "旧概念", "heat": 0.88, "change_pct": 2.1}]

    async def _run():
        async with service.lock:
            await service._refresh_concepts_unlocked(force=True)

    asyncio.run(_run())

    assert service.hot_concepts == [{"name": "旧概念", "heat": 0.88, "change_pct": 2.1}]


def test_get_hot_concepts_clips_legacy_snapshot_to_top10(tmp_path):
    provider = EmptyConceptProvider()
    service = FunnelService(provider=provider, persist_db_path=str(tmp_path / "state.db"))
    service.hot_concepts = [
        {
            "name": f"概念{i}",
            "heat": 1.0 - i * 0.01,
            "change_pct": 1.2,
            "limit_up_count": 2,
            "up_count": 20,
            "down_count": 10,
            "leader": f"龙头{i}",
            "selected_count": i,
        }
        for i in range(15)
    ]

    payload = asyncio.run(service.get_hot_concepts())

    assert len(payload.items) == 10
    assert payload.items[0].name == "概念0"
    assert payload.items[-1].name == "概念9"


def test_get_hot_stocks_refreshes_stale_snapshot_even_when_frozen(tmp_path):
    provider = CountingHotStockProvider()
    service = FunnelService(provider=provider, persist_db_path=str(tmp_path / "state.db"))
    service.frozen = True
    service.hot_stocks = [
        {
            "rank": 1,
            "symbol": "000001",
            "name": "旧热门",
            "latest_price": 10.0,
            "change_pct": 1.0,
            "change_amount": 0.1,
        }
    ]
    service.hot_stocks_updated_at = (now_cn() - timedelta(minutes=6)).isoformat()

    payload = asyncio.run(service.get_hot_stocks())

    assert provider.calls == 1
    assert payload.items[0].symbol == "600111"
    assert payload.updated_at == service.hot_stocks_updated_at


def test_get_hot_stocks_uses_recent_snapshot_without_refetch(tmp_path):
    provider = CountingHotStockProvider()
    service = FunnelService(provider=provider, persist_db_path=str(tmp_path / "state.db"))
    service.hot_stocks = [
        {
            "rank": 1,
            "symbol": "600111",
            "name": "北方稀土",
            "latest_price": 24.1,
            "change_pct": 0.5,
            "change_amount": 0.12,
        }
    ]
    service.hot_stocks_updated_at = now_cn().isoformat()

    payload = asyncio.run(service.get_hot_stocks())

    assert provider.calls == 0
    assert payload.items[0].symbol == "600111"
