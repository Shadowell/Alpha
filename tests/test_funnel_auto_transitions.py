"""#48：三池自动升/降池服务级回归。"""

from __future__ import annotations

import asyncio

import pandas as pd

from app.services import funnel_service as funnel_module
from app.services.funnel_service import FunnelService


class FakeProvider:
    kline_store = None

    def __init__(self, symbol="600000", price=10.5):
        self.symbol = symbol
        self.price = price

    async def get_realtime_snapshot(self, **kwargs):
        return pd.DataFrame(
            [
                {
                    "代码": self.symbol,
                    "名称": "测试股",
                    "最新价": self.price,
                    "涨跌幅": 5.0,
                }
            ]
        )

    async def get_symbol_name_map(self, cache_ttl_seconds=3600):
        return {self.symbol: "测试股"}


async def _noop_panels(self, force: bool = False) -> None:
    return None


def _service(tmp_path, symbol="600000", pool="candidate", score=60.0):
    service = FunnelService(
        provider=FakeProvider(symbol=symbol),
        persist_db_path=str(tmp_path / "funnel_state.db"),
    )
    service.trade_date = "2026-10-22"
    service.entries = {
        symbol: {
            "symbol": symbol,
            "name": "测试股",
            "pool": pool,
            "score": score,
            "prev_score": score,
            "updated_at": "2026-10-22T10:00:00+08:00",
            "metrics": {},
            "concept_tags": [],
            "reasons": [],
            "warnings": [],
            "trigger_log": [],
            "transitions": {
                "above60_count": 0,
                "breakout_confirm_count": 0,
                "below65_count": 0,
            },
        }
    }
    service._refresh_market_panels_unlocked = lambda force=False: asyncio.sleep(0)
    return service


def _patch_score(monkeypatch, score: float, *, breakout_confirm: bool = False):
    metrics = {
        "price": 10.5,
        "breakout_level": 10.0,
        "volume_ratio": 2.0 if breakout_confirm else 0.5,
        "above_vwap": breakout_confirm,
    }

    def fake_compute(entry, market_row, elapsed_ratio, config):
        return score, {}, metrics, []

    monkeypatch.setattr(funnel_module, "compute_intraday_score", fake_compute)


def test_candidate_high_score_autopromotes_to_focus(tmp_path, monkeypatch):
    _patch_score(monkeypatch, 61.0)
    service = _service(tmp_path)

    async def _run():
        for _ in range(3):
            await service.refresh_scores()

    asyncio.run(_run())

    assert service.entries["600000"]["pool"] == "focus"
    assert any("自动迁池" in log["note"] for log in service.entries["600000"]["trigger_log"])


def test_focus_breakout_autopromotes_to_buy(tmp_path, monkeypatch):
    _patch_score(monkeypatch, 80.0, breakout_confirm=True)
    service = _service(tmp_path, pool="focus")

    async def _run():
        for _ in range(2):
            await service.refresh_scores()

    asyncio.run(_run())

    assert service.entries["600000"]["pool"] == "buy"


def test_buy_pool_capacity_blocks_auto_promotion(tmp_path, monkeypatch):
    _patch_score(monkeypatch, 80.0, breakout_confirm=True)
    service = _service(tmp_path, symbol="600002", pool="focus")
    for i in range(service.config.buy_pool_max_size):
        symbol = f"60001{i}"
        service.entries[symbol] = {
            "symbol": symbol,
            "name": f"已满标的{i}",
            "pool": "buy",
            "score": 80,
            "prev_score": 80,
            "updated_at": service.entries["600002"]["updated_at"],
            "metrics": {},
            "concept_tags": [],
            "reasons": [],
            "warnings": [],
            "trigger_log": [],
        }

    async def _run():
        for _ in range(2):
            await service.refresh_scores()

    asyncio.run(_run())

    assert service.entries["600002"]["pool"] == "focus"
    assert service.entries["600002"]["recommended_pool"] == "buy"
    assert any("买入池已满" in log["note"] for log in service.entries["600002"]["trigger_log"])


def test_buy_low_score_autodemotes_to_focus(tmp_path, monkeypatch):
    _patch_score(monkeypatch, 64.0)
    service = _service(tmp_path, pool="buy")

    async def _run():
        for _ in range(5):
            await service.refresh_scores()

    asyncio.run(_run())

    assert service.entries["600000"]["pool"] == "focus"
