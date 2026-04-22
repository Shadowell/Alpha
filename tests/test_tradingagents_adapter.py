from __future__ import annotations

import json
import subprocess

from app.services.tradingagents_adapter import (
    DEEPSEEK_BACKEND_URL,
    DEEPSEEK_PROVIDER,
    TradingAgentsAdapter,
)

def test_tradingagents_adapter_forces_deepseek(monkeypatch, tmp_path):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")

    adapter = TradingAgentsAdapter(repo_path=tmp_path / "TradingAgents", runtime_root=tmp_path / "runtime")
    adapter.repo_path.mkdir(parents=True, exist_ok=True)
    captured = {}

    def _fake_run(cmd, cwd, capture_output, text, check, timeout, env):
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        output_file = cmd[cmd.index("--output-file") + 1]
        payload = {
            "decision": "BUY",
            "final_trade_decision": "BUY with strong conviction",
            "investment_plan": "Accumulate on strength.",
            "trader_investment_plan": "Enter in two tranches.",
            "market_report": "Market report",
            "news_report": "News report",
            "fundamentals_report": "Fundamentals report",
        }
        with open(output_file, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    result = adapter.analyze("600001", "2026-04-22")

    assert captured["cwd"] == str(adapter.repo_path)
    assert captured["cmd"][:5] == ["uv", "run", "python", "-m", "cli.main"]
    assert "--provider" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--provider") + 1] == DEEPSEEK_PROVIDER
    assert captured["cmd"][captured["cmd"].index("--ticker") + 1] == "600001.SS"
    assert captured["cmd"][captured["cmd"].index("--deep-model") + 1] == "deepseek-reasoner"
    assert result["provider"] == DEEPSEEK_PROVIDER
    assert result["backend_url"] == DEEPSEEK_BACKEND_URL
    assert result["decision"] == "BUY"
    assert result["decision_action"] == "buy"
    assert result["decision_action_text"] == "买入"
