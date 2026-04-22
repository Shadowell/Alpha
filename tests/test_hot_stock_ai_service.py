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
    def is_loaded(self) -> bool:
        return True

    def get_device(self) -> str:
        return "cpu"

    async def predict(self, symbol: str, lookback: int = 90, horizon: int = 3) -> dict:
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
        }
    )

    assert cfg["top_n"] == 30
    assert cfg["lookback"] == 30
    assert cfg["horizon"] == 5
    assert cfg["threshold_focus"] >= cfg["threshold_candidate"]
    assert cfg["threshold_buy"] >= cfg["threshold_focus"]
    assert cfg["refresh_interval_minutes"] == 1
    assert cfg["max_buy_pool_size"] == 10
