from __future__ import annotations

from app.services.tradingagents_adapter import (
    DEEPSEEK_BACKEND_URL,
    DEEPSEEK_PROVIDER,
    TradingAgentsAdapter,
)


class _FakeGraph:
    last_kwargs = None
    last_propagate = None

    def __init__(self, *, debug, config, selected_analysts):
        _FakeGraph.last_kwargs = {
            "debug": debug,
            "config": config,
            "selected_analysts": selected_analysts,
        }

    def propagate(self, symbol: str, trade_date: str):
        _FakeGraph.last_propagate = {"symbol": symbol, "trade_date": trade_date}
        return (
            {
                "final_trade_decision": "BUY with strong conviction",
                "investment_plan": "Accumulate on strength.",
                "trader_investment_plan": "Enter in two tranches.",
                "market_report": "Market report",
                "news_report": "News report",
                "fundamentals_report": "Fundamentals report",
            },
            "BUY",
        )


def test_tradingagents_adapter_forces_deepseek(monkeypatch, tmp_path):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")

    adapter = TradingAgentsAdapter(repo_path=tmp_path / "TradingAgents", runtime_root=tmp_path / "runtime")
    adapter.repo_path.mkdir(parents=True, exist_ok=True)

    def _fake_load_classes():
        return _FakeGraph, {"llm_provider": "openai", "backend_url": "https://api.openai.com/v1"}

    monkeypatch.setattr(adapter, "_load_classes", _fake_load_classes)

    result = adapter.analyze("600001", "2026-04-22")

    assert _FakeGraph.last_kwargs is not None
    assert _FakeGraph.last_kwargs["config"]["llm_provider"] == DEEPSEEK_PROVIDER
    assert _FakeGraph.last_kwargs["config"]["backend_url"] == DEEPSEEK_BACKEND_URL
    assert _FakeGraph.last_propagate == {"symbol": "600001.SS", "trade_date": "2026-04-22"}
    assert result["provider"] == DEEPSEEK_PROVIDER
    assert result["backend_url"] == DEEPSEEK_BACKEND_URL
    assert result["decision"] == "BUY"
