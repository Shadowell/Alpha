from __future__ import annotations

import asyncio
from pathlib import Path

from app.services.paper_trading import PaperTradingService


def test_paper_positions_return_db_records_when_realtime_times_out(monkeypatch, tmp_path: Path):
    import app.main as main

    paper = PaperTradingService(tmp_path / "paper.db")
    paper.open_position("600001", "测试持仓", 10.0, qty=100)

    async def hanging_snapshot(*args, **kwargs):
        await asyncio.sleep(1)

    monkeypatch.setattr(main, "paper_trading", paper)
    monkeypatch.setattr(main.provider, "get_realtime_snapshot", hanging_snapshot)
    monkeypatch.setattr(main, "_is_a_market_open", lambda: True)
    monkeypatch.setattr(main, "PAPER_SNAPSHOT_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(main, "PAPER_DB_FALLBACK_TIMEOUT_SECONDS", 0.01)

    payload = asyncio.run(main.paper_positions())

    assert len(payload["positions"]) == 1
    assert payload["positions"][0]["symbol"] == "600001"
    assert "timeout" in payload["price_source"]


def test_paper_summary_returns_db_records_when_realtime_times_out(monkeypatch, tmp_path: Path):
    import app.main as main

    paper = PaperTradingService(tmp_path / "paper.db")
    paper.open_position("600002", "测试摘要", 8.0, qty=100)

    async def hanging_snapshot(*args, **kwargs):
        await asyncio.sleep(1)

    monkeypatch.setattr(main, "paper_trading", paper)
    monkeypatch.setattr(main.provider, "get_realtime_snapshot", hanging_snapshot)
    monkeypatch.setattr(main, "_is_a_market_open", lambda: True)
    monkeypatch.setattr(main, "PAPER_SNAPSHOT_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(main, "PAPER_DB_FALLBACK_TIMEOUT_SECONDS", 0.01)

    payload = asyncio.run(main.paper_summary())

    assert payload["open_count"] == 1
    assert "timeout" in payload["price_source"]
