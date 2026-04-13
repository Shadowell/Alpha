import asyncio

import pandas as pd

from app.services.concept_engine import build_concept_heat, build_top_tags, map_stock_concepts


class FakeProvider:
    def __init__(self):
        self.constituents = {
            "算力": pd.DataFrame({"代码": ["000001", "000002"], "涨跌幅": [10.0, 5.0]}),
            "机器人": pd.DataFrame({"代码": ["000001"], "涨跌幅": [9.9]}),
            "汽车": pd.DataFrame({"代码": ["000001", "000003"], "涨跌幅": [3.0, 2.0]}),
        }

    async def get_all_concepts(self, cache_seconds=30):
        return pd.DataFrame(
            {
                "板块名称": ["算力", "机器人", "汽车"],
                "涨跌幅": [5.0, 4.0, 2.0],
                "上涨家数": [10, 8, 5],
                "下跌家数": [1, 2, 6],
                "领涨股票": ["A", "B", "C"],
            }
        )

    async def get_concept_constituents(self, concept_name):
        return self.constituents.get(concept_name, pd.DataFrame())


def test_concept_heat_and_tags_order():
    provider = FakeProvider()
    heat_df = asyncio.run(build_concept_heat(provider, top_n=3))
    assert not heat_df.empty
    assert heat_df.iloc[0]["板块名称"] == "算力"

    mapping = asyncio.run(map_stock_concepts(provider, {"000001"}, heat_df))
    tags = build_top_tags(mapping["000001"], top_k=3)
    assert len(tags) == 3
    assert tags[0]["name"] == "算力"
    assert tags[0]["color"] == "#ef4444"
    assert tags[1]["color"] == "#f97316"
    assert tags[2]["color"] == "#3b82f6"


def test_concept_heat_uses_precomputed_limit_up_count():
    provider = FakeProvider()
    concepts_df = pd.DataFrame(
        {
            "板块名称": ["A", "B", "C"],
            "涨跌幅": [1.0, 2.0, 3.0],
            "上涨家数": [5, 5, 5],
            "下跌家数": [5, 5, 5],
            "涨停家数": [8, 1, 0],
            "领涨股票": ["a", "b", "c"],
        }
    )
    out = asyncio.run(build_concept_heat(provider, top_n=3, concepts_df=concepts_df))
    assert "涨停家数" in out.columns
    mapped = {row["板块名称"]: int(row["涨停家数"]) for _, row in out.iterrows()}
    assert mapped["A"] == 8
