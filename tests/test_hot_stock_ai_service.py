from __future__ import annotations

import asyncio
from pathlib import Path

import pandas as pd

from app.services.hot_stock_ai_service import HotStockAIService
from app.services.sqlite_store import SQLiteStateStore


def _make_rows(base_close: float, drift: float, amount_base: float) -> list[dict]:
    dates = pd.bdate_range("2026-01-05", periods=80)
    rows: list[dict] = []
    close = base_close
    for idx, ts in enumerate(dates, start=1):
        prev = close
        close = round(prev * (1.0 + drift), 4)
        open_p = round(prev * (1.0 + drift / 3), 4)
        high = round(max(open_p, close) * 1.01, 4)
        low = round(min(open_p, close) * 0.99, 4)
        amount = amount_base * (1.0 + idx * 0.01)
        rows.append(
            {
                "date": ts.date().isoformat(),
                "open": open_p,
                "high": high,
                "low": low,
                "close": close,
                "volume": 1_000_000 + idx * 10_000,
                "amount": amount,
            }
        )
    return rows


class FakeProvider:
    async def get_hot_stocks(self, top_n=20, **kwargs):
        frame = pd.DataFrame(
            [
                {"rank": 1, "symbol": "600001", "name": "强趋势", "latest_price": 18.6, "change_amount": 1.2, "change_pct": 8.4},
                {"rank": 6, "symbol": "600002", "name": "中趋势", "latest_price": 13.5, "change_amount": 0.4, "change_pct": 3.6},
                {"rank": 12, "symbol": "600003", "name": "弱趋势", "latest_price": 10.8, "change_amount": 0.1, "change_pct": 1.8},
            ]
        )
        return frame.head(top_n).copy()


class FakeKlineStore:
    def __init__(self):
        self.rows = {
            "600001": _make_rows(12.0, 0.0042, 220_000_000),
            "600002": _make_rows(11.0, 0.0016, 150_000_000),
            "600003": _make_rows(10.0, 0.0008, 105_000_000),
        }

    def get_kline(self, symbol: str, days: int = 30):
        return self.rows.get(symbol, [])[-days:]


class FakeKronos:
    def __init__(self):
        self.calls: list[tuple[str, int, int]] = []

    def is_loaded(self) -> bool:
        return True

    def get_device(self) -> str:
        return "cpu"

    async def predict(self, symbol: str, lookback: int = 90, horizon: int = 3) -> dict:
        self.calls.append((symbol, lookback, horizon))
        bases = {
            "600001": [19.0, 19.6, 20.1],
            "600002": [12.7, 12.8, 12.85],
            "600003": [10.82, 10.9, 10.96],
        }
        closes = bases[symbol]
        predicted = []
        for idx, close in enumerate(closes, start=1):
            predicted.append(
                {
                    "date": f"2026-04-{20+idx}",
                    "open": close * 0.99,
                    "high": close * 1.02,
                    "low": close * 0.98,
                    "close": close,
                    "volume": 0,
                    "amount": 0,
                    "type": "predicted",
                }
            )
        return {"predicted_kline": predicted}


class FakeTradingAgentsAdapter:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def describe_runtime(self) -> dict[str, str]:
        return {
            "repo_path": "/Users/jie.feng/work/github/TradingAgents",
            "provider": "deepseek",
            "backend_url": "https://api.deepseek.com",
        }

    def analyze(self, symbol: str, trade_date: str, **kwargs) -> dict:
        self.calls.append((symbol, trade_date))
        mapping = {
            "600001": ("BUY", 2.0, "龙头股量价共振，讨论结论偏进攻。"),
            "600002": ("OVERWEIGHT", 1.0, "趋势保持但爆发性略弱，建议增配观察。"),
        }
        decision, bonus, summary = mapping.get(symbol, ("HOLD", 0.0, "讨论中性。"))
        action = "buy" if decision in {"BUY", "OVERWEIGHT"} else "watch"
        action_text = "买入" if action == "buy" else "观望"
        return {
            "ok": True,
            "symbol": symbol,
            "trade_date": trade_date,
            "decision": decision,
            "decision_action": action,
            "decision_action_text": action_text,
            "score_bonus": bonus,
            "summary": summary,
            "discussion": summary,
            "reports": {},
        }


def test_hot_stock_ai_builds_three_pools(tmp_path: Path):
    service = HotStockAIService(
        provider=FakeProvider(),
        kline_store=FakeKlineStore(),
        kronos_service=FakeKronos(),
        state_store=SQLiteStateStore(str(tmp_path / "state.db")),
    )

    asyncio.run(service.run(trigger="manual"))
    snap = service.get_snapshot()

    assert snap["meta"]["entries_count"] == 3
    assert snap["meta"]["failed_count"] == 0
    assert snap["pools"]["buy"][0]["symbol"] == "600001"
    assert snap["pools"]["focus"][0]["symbol"] == "600002"
    assert snap["pools"]["candidate"][0]["symbol"] == "600003"
    assert "analysis" in snap["entries"][0]
    assert "score_breakdown" in snap["entries"][0]


def test_hot_stock_ai_applies_tradingagents_bonus_and_cache(tmp_path: Path):
    adapter = FakeTradingAgentsAdapter()
    service = HotStockAIService(
        provider=FakeProvider(),
        kline_store=FakeKlineStore(),
        kronos_service=FakeKronos(),
        state_store=SQLiteStateStore(str(tmp_path / "state.db")),
        tradingagents_adapter=adapter,
    )
    service.update_config({"tradingagents_top_n": 2, "tradingagents_enabled": True})

    asyncio.run(service.run(trigger="manual"))
    snap1 = service.get_snapshot()
    entry1 = next(item for item in snap1["entries"] if item["symbol"] == "600001")
    entry2 = next(item for item in snap1["entries"] if item["symbol"] == "600002")
    entry3 = next(item for item in snap1["entries"] if item["symbol"] == "600003")

    assert len(adapter.calls) == 2
    assert snap1["meta"]["tradingagents_discussed"] == 2
    assert entry1["tradingagents"]["decision"] == "BUY"
    assert entry1["tradingagents"]["source"] == "fresh"
    assert entry1["evaluation_text"] == "买入"
    assert entry1["score"] > entry1["base_score"]
    assert entry2["tradingagents_bonus"] == 1.0
    assert entry3["tradingagents"]["status"] == "skipped"

    asyncio.run(service.run(trigger="manual"))
    snap2 = service.get_snapshot()
    entry1_cached = next(item for item in snap2["entries"] if item["symbol"] == "600001")

    assert len(adapter.calls) == 2
    assert snap2["meta"]["tradingagents_cache_hits"] == 2
    assert entry1_cached["tradingagents"]["source"] == "cache"


def test_hot_stock_ai_config_is_clamped(tmp_path: Path):
    service = HotStockAIService(
        provider=FakeProvider(),
        kline_store=FakeKlineStore(),
        kronos_service=FakeKronos(),
        state_store=SQLiteStateStore(str(tmp_path / "state.db")),
    )

    cfg = service.update_config(
        {
            "top_n": 99,
            "lookback": 10,
            "horizon": 9,
            "threshold_candidate": 7.0,
            "threshold_focus": 6.0,
            "threshold_buy": 5.0,
            "refresh_interval_minutes": 0,
            "max_buy_pool_size": 99,
            "tradingagents_top_n": 99,
            "tradingagents_timeout_seconds": 5,
        }
    )

    assert cfg["top_n"] == 30
    assert cfg["lookback"] == 30
    assert cfg["horizon"] == 5
    assert cfg["threshold_focus"] >= cfg["threshold_candidate"]
    assert cfg["threshold_buy"] >= cfg["threshold_focus"]
    assert cfg["refresh_interval_minutes"] == 1
    assert cfg["max_buy_pool_size"] == 10
    assert cfg["tradingagents_top_n"] == 20
    assert cfg["tradingagents_timeout_seconds"] == 30
    assert cfg["tradingagents_provider"] == "deepseek"


def test_hot_stock_ai_auto_run_uses_light_mode(tmp_path: Path):
    kronos = FakeKronos()
    adapter = FakeTradingAgentsAdapter()
    service = HotStockAIService(
        provider=FakeProvider(),
        kline_store=FakeKlineStore(),
        kronos_service=kronos,
        state_store=SQLiteStateStore(str(tmp_path / "state.db")),
        tradingagents_adapter=adapter,
    )
    service.update_config({"use_kronos": True, "tradingagents_enabled": True, "top_n": 20})

    asyncio.run(service.run(trigger="auto"))
    snap = service.get_snapshot()

    assert snap["meta"]["execution_mode"] == "light_auto"
    assert snap["meta"]["runtime_top_n"] == 12
    assert snap["meta"]["runtime_kronos_enabled"] is False
    assert snap["meta"]["runtime_tradingagents_enabled"] is False
    assert snap["meta"]["tradingagents_discussed"] == 0
    assert kronos.calls == []
    assert adapter.calls == []


def test_hot_stock_ai_meta_exposes_tradingagents_backend(tmp_path: Path):
    adapter = FakeTradingAgentsAdapter()
    service = HotStockAIService(
        provider=FakeProvider(),
        kline_store=FakeKlineStore(),
        kronos_service=FakeKronos(),
        state_store=SQLiteStateStore(str(tmp_path / "state.db")),
        tradingagents_adapter=adapter,
    )

    asyncio.run(service.run(trigger="manual"))
    snap = service.get_snapshot()

    assert snap["meta"]["tradingagents_backend"] == "TradingAgents + DeepSeek"
    assert snap["meta"]["tradingagents_provider"] == "deepseek"


def test_hot_stock_ai_keeps_discussed_result_in_candidate_pool(tmp_path: Path):
    adapter = FakeTradingAgentsAdapter()
    service = HotStockAIService(
        provider=FakeProvider(),
        kline_store=FakeKlineStore(),
        kronos_service=FakeKronos(),
        state_store=SQLiteStateStore(str(tmp_path / "state.db")),
        tradingagents_adapter=adapter,
    )
    service.update_config({
        "threshold_candidate": 30.0,
        "threshold_focus": 31.0,
        "threshold_buy": 32.0,
        "tradingagents_top_n": 1,
    })

    asyncio.run(service.run(trigger="manual"))
    snap = service.get_snapshot()

    assert snap["pools"]["candidate"]
    assert snap["pools"]["candidate"][0]["symbol"] == "600001"
    assert snap["pools"]["candidate"][0]["evaluation_text"] == "买入"
