"""#53：策略扫描快照为命中附带概念标签（与系统候选同源），取不到时优雅降级。

覆盖：命中携带 concept_tags（name/color/heat/change_pct/limit_up_count 结构）、
快照持久化后仍可读出、tagger 异常仅降级为空标签且不泄漏上游错误、
未注入 tagger（旧构造方式）时保持空标签。
"""

from __future__ import annotations

import asyncio
import datetime as dt

from app.services.custom_strategy import (
    CustomStrategy,
    CustomStrategyScanner,
    StrategyRuleRef,
)
from app.services.sqlite_store import SQLiteStateStore


class FakeKlineStore:
    def __init__(self, rows_by_symbol: dict[str, list[dict]]):
        self._rows = rows_by_symbol

    def get_stats(self):
        return {"max_date": "2026-10-21"}

    def get_all_symbols(self):
        return list(self._rows)

    def get_kline(self, symbol: str, days: int):
        return self._rows.get(symbol, [])[-days:]


def _flat_kline(
    days: int = 30, close: float = 10.0, start: str = "2026-01-01"
) -> list[dict]:
    d = dt.date.fromisoformat(start)
    rows = []
    for _ in range(days):
        rows.append(
            {
                "date": d.isoformat(),
                "open": close,
                "high": close * 1.01,
                "low": close * 0.99,
                "close": close,
                "volume": 1_000_000.0,
            }
        )
        d = d + dt.timedelta(days=1)
    return rows


def _strategy() -> CustomStrategy:
    return CustomStrategy(
        id="tag-test",
        name="标签策略",
        description="",
        rules=[StrategyRuleRef("price_range", True, {"min": 5.0, "max": 30.0})],
    )


# 与 concept_engine.build_top_tags 输出同构的标签样例
_MOTOR_TAG = {
    "name": "电机",
    "rank": 1,
    "color": "#ef4444",
    "heat": 0.9123,
    "change_pct": 3.21,
    "limit_up_count": 5,
    "up_count": 10,
    "down_count": 2,
    "leader": "600001",
}


def _scanner(store, state_store, concept_tagger=None) -> CustomStrategyScanner:
    return CustomStrategyScanner(
        FakeKlineStore({"600000": _flat_kline()}),
        state_store=state_store,
        concept_tagger=concept_tagger,
    )


def test_hits_carry_concept_tags_and_persist_snapshot(tmp_path):
    state_store = SQLiteStateStore(str(tmp_path / "state.db"))
    seen: list[set[str]] = []

    async def tagger(symbols: set[str]):
        seen.append(symbols)
        return {"600000": [dict(_MOTOR_TAG)]}

    scanner = _scanner(None, state_store, tagger)
    payload = asyncio.run(scanner.scan(_strategy()))

    assert seen and seen[0] == {"600000"}
    hit = payload["hits"][0]
    assert hit["symbol"] == "600000"
    assert hit["concept_tags"] == [_MOTOR_TAG]
    # 前端筛选读取的字段齐备
    tag = hit["concept_tags"][0]
    assert {"name", "color", "heat", "change_pct", "limit_up_count"} <= set(tag)

    stored = state_store.get_kv("custom_strategy_scan:tag-test")
    assert stored is not None
    assert stored["hits"][0]["concept_tags"][0]["name"] == "电机"
    # 重启恢复路径（get_last_snapshot）同样带标签
    restored = _scanner(None, state_store, tagger).get_last_snapshot("tag-test")
    assert restored["hits"][0]["concept_tags"][0]["name"] == "电机"


def test_tagger_failure_degrades_to_empty_tags_without_leaking(tmp_path):
    state_store = SQLiteStateStore(str(tmp_path / "state.db"))

    async def boom(symbols: set[str]):
        raise RuntimeError("akshare upstream leaked 500")

    scanner = _scanner(None, state_store, boom)
    payload = asyncio.run(scanner.scan(_strategy()))

    assert payload["total_hits"] == 1
    assert payload["hits"][0]["concept_tags"] == []
    # 响应中不得出现上游异常文本
    assert "leaked" not in repr(payload)


def test_tagger_returning_garbage_is_ignored(tmp_path):
    state_store = SQLiteStateStore(str(tmp_path / "state.db"))

    async def garbage(symbols: set[str]):
        return {"600000": ["不是字典", {"name": "电机"}, 42]}

    scanner = _scanner(None, state_store, garbage)
    payload = asyncio.run(scanner.scan(_strategy()))
    assert payload["hits"][0]["concept_tags"] == [{"name": "电机"}]


def test_scanner_without_tagger_keeps_empty_concept_tags(tmp_path):
    state_store = SQLiteStateStore(str(tmp_path / "state.db"))
    scanner = _scanner(None, state_store, None)
    payload = asyncio.run(scanner.scan(_strategy()))
    assert payload["hits"][0]["concept_tags"] == []
