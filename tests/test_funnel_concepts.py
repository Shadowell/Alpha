import asyncio

import pandas as pd

from app.services.funnel_service import FunnelService


class EmptyConceptProvider:
    def get_all_concepts(self, cache_seconds=30):
        return pd.DataFrame()

    def get_concept_constituents(self, concept_name):
        return pd.DataFrame()


def test_refresh_concepts_keeps_previous_hot_concepts_when_source_empty(tmp_path):
    provider = EmptyConceptProvider()
    service = FunnelService(provider=provider, persist_db_path=str(tmp_path / "state.db"))
    service.hot_concepts = [{"name": "旧概念", "heat": 0.88, "change_pct": 2.1}]

    asyncio.run(service._refresh_concepts(force=True))

    assert service.hot_concepts == [{"name": "旧概念", "heat": 0.88, "change_pct": 2.1}]
