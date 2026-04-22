from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any


def _normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


class TradingAgentsAdapter:
    def __init__(
        self,
        repo_path: str | Path = "/Users/jie.feng/work/github/TradingAgents",
        runtime_root: str | Path = "data/tradingagents_runtime",
    ) -> None:
        self.repo_path = Path(repo_path)
        self.runtime_root = Path(runtime_root)
        self.runtime_root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def to_vendor_symbol(symbol: str) -> str:
        raw = str(symbol or "").strip()
        if raw.startswith(("60", "68")):
            return f"{raw}.SS"
        if raw.startswith(("00", "30")):
            return f"{raw}.SZ"
        if raw.startswith(("43", "83", "87", "92")):
            return f"{raw}.BJ"
        return raw

    def _load_classes(self):
        if not self.repo_path.exists():
            raise FileNotFoundError(f"TradingAgents repo not found: {self.repo_path}")
        repo_str = str(self.repo_path)
        if repo_str not in sys.path:
            sys.path.insert(0, repo_str)
        from tradingagents.graph.trading_graph import TradingAgentsGraph
        from tradingagents.default_config import DEFAULT_CONFIG

        return TradingAgentsGraph, DEFAULT_CONFIG

    @staticmethod
    def _decision_bonus(decision: str) -> float:
        mapping = {
            "BUY": 2.0,
            "OVERWEIGHT": 1.0,
            "HOLD": 0.0,
            "UNDERWEIGHT": -1.0,
            "SELL": -2.0,
        }
        return mapping.get(str(decision or "").strip().upper(), 0.0)

    def analyze(
        self,
        symbol: str,
        trade_date: str,
        *,
        provider: str = "deepseek",
        quick_model: str = "deepseek-chat",
        deep_model: str = "deepseek-chat",
        selected_analysts: list[str] | None = None,
        output_language: str = "Chinese",
    ) -> dict[str, Any]:
        TradingAgentsGraph, DEFAULT_CONFIG = self._load_classes()
        vendor_symbol = self.to_vendor_symbol(symbol)

        config = DEFAULT_CONFIG.copy()
        config["llm_provider"] = provider
        config["quick_think_llm"] = quick_model
        config["deep_think_llm"] = deep_model
        config["output_language"] = output_language
        config["max_debate_rounds"] = 1
        config["max_risk_discuss_rounds"] = 1
        config["results_dir"] = str(self.runtime_root / "logs")
        config["data_cache_dir"] = str(self.runtime_root / "cache")

        graph = TradingAgentsGraph(
            debug=False,
            config=config,
            selected_analysts=selected_analysts or ["market", "news", "fundamentals"],
        )
        final_state, decision = graph.propagate(vendor_symbol, trade_date)
        decision_text = _normalize_whitespace(final_state.get("final_trade_decision", ""))
        investment_plan = _normalize_whitespace(final_state.get("investment_plan", ""))
        trader_plan = _normalize_whitespace(final_state.get("trader_investment_plan", ""))
        market_report = _normalize_whitespace(final_state.get("market_report", ""))
        news_report = _normalize_whitespace(final_state.get("news_report", ""))
        fundamentals_report = _normalize_whitespace(final_state.get("fundamentals_report", ""))
        summary_source = decision_text or trader_plan or investment_plan or market_report
        summary = summary_source[:360]

        return {
            "ok": True,
            "symbol": symbol,
            "vendor_symbol": vendor_symbol,
            "trade_date": trade_date,
            "decision": str(decision or "").strip().upper(),
            "score_bonus": self._decision_bonus(decision),
            "summary": summary,
            "discussion": decision_text[:4000],
            "reports": {
                "market": market_report[:1200],
                "news": news_report[:1200],
                "fundamentals": fundamentals_report[:1200],
                "investment_plan": investment_plan[:1200],
                "trader_plan": trader_plan[:1200],
            },
        }
