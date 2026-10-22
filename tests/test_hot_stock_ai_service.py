from __future__ import annotations

import asyncio
from pathlib import Path

import pandas as pd

from app.services.hot_stock_ai_service import HotStockAIService
from app.services.sqlite_store import SQLiteStateStore

# 固定「决策交易日」：与 FakeKlineStore 的 K 线末日对齐，保证 lag==0。
FRESH_TRADE_DATE = "2026-04-24"


def _make_rows(base_close: float, drift: float, amount_base: float, periods: int = 80, end: str | None = None) -> list[dict]:
    if end is None:
        dates = pd.bdate_range("2026-01-05", periods=periods)
    else:
        dates = pd.bdate_range(end=end, periods=periods)
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


def _flat_rows(base_close: float, amount_base: float, end: str, periods: int = 60) -> list[dict]:
    """平稳收盘序列：最后一天收盘价恰为 base_close（drift=0），便于精确断言追加 bar 后的均线。"""
    rows = _make_rows(base_close, 0.0, amount_base, periods=periods, end=end)
    return rows


class FakeProvider:
    def __init__(self, rows: list[dict] | None = None):
        self._rows = rows

    async def get_hot_stocks(self, top_n=20, **kwargs):
        if self._rows is not None:
            return pd.DataFrame(self._rows).head(top_n).copy()
        frame = pd.DataFrame(
            [
                {"rank": 1, "symbol": "600001", "name": "强趋势", "latest_price": 18.6, "change_amount": 1.2, "change_pct": 8.4},
                {"rank": 6, "symbol": "600002", "name": "中趋势", "latest_price": 13.5, "change_amount": 0.4, "change_pct": 3.6},
                {"rank": 12, "symbol": "600003", "name": "弱趋势", "latest_price": 10.8, "change_amount": 0.1, "change_pct": 1.8},
            ]
        )
        return frame.head(top_n).copy()


class FakeKlineStore:
    def __init__(self, rows: dict[str, list[dict]] | None = None):
        self.rows = rows or {
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
        realtime_trade_date=FRESH_TRADE_DATE,
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


def test_hot_stock_ai_move_candidate_to_focus_pool(tmp_path: Path):
    state_path = tmp_path / "state.db"
    service = HotStockAIService(
        provider=FakeProvider(),
        realtime_trade_date=FRESH_TRADE_DATE,
        kline_store=FakeKlineStore(),
        kronos_service=FakeKronos(),
        state_store=SQLiteStateStore(str(state_path)),
    )

    asyncio.run(service.run(trigger="manual"))
    moved = service.move_pool("600003", "focus")
    snap = moved["snapshot"]

    assert moved["success"] is True
    assert all(item["symbol"] != "600003" for item in snap["pools"]["candidate"])
    assert any(item["symbol"] == "600003" for item in snap["pools"]["focus"])
    assert next(item for item in snap["entries"] if item["symbol"] == "600003")["manual_pool"] == "focus"

    reloaded = HotStockAIService(
        provider=FakeProvider(),
        realtime_trade_date=FRESH_TRADE_DATE,
        kline_store=FakeKlineStore(),
        kronos_service=FakeKronos(),
        state_store=SQLiteStateStore(str(state_path)),
    )
    assert any(item["symbol"] == "600003" for item in reloaded.get_snapshot()["pools"]["focus"])


def test_hot_stock_ai_applies_tradingagents_bonus_and_cache(tmp_path: Path):
    adapter = FakeTradingAgentsAdapter()
    service = HotStockAIService(
        provider=FakeProvider(),
        realtime_trade_date=FRESH_TRADE_DATE,
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
        realtime_trade_date=FRESH_TRADE_DATE,
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
        realtime_trade_date=FRESH_TRADE_DATE,
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
        realtime_trade_date=FRESH_TRADE_DATE,
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
        realtime_trade_date=FRESH_TRADE_DATE,
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


class EmptyKronos:
    def is_loaded(self) -> bool:
        return False

    def get_device(self) -> str:
        return "cpu"

    async def predict(self, symbol: str, lookback: int = 90, horizon: int = 3) -> dict:
        return {"predicted_kline": []}


def test_hot_stock_ai_skips_stale_kline_symbols(tmp_path: Path):
    """Issue #51：缓存严重落后（约一个月）时，实时价不得与陈旧窗口混算——整只跳过并计入 skipped_stale。"""
    stale_rows = {
        "600001": _make_rows(12.0, 0.0042, 220_000_000, end="2026-03-02"),
        "600002": _make_rows(11.0, 0.0016, 150_000_000, end="2026-03-02"),
        "600003": _make_rows(10.0, 0.0008, 105_000_000, end="2026-03-02"),
    }
    service = HotStockAIService(
        provider=FakeProvider(),
        realtime_trade_date=FRESH_TRADE_DATE,
        kline_store=FakeKlineStore(stale_rows),
        kronos_service=FakeKronos(),
        state_store=SQLiteStateStore(str(tmp_path / "state.db")),
    )

    asyncio.run(service.run(trigger="manual"))
    snap = service.get_snapshot()

    assert snap["entries"] == []
    assert snap["trade_date"] == FRESH_TRADE_DATE
    meta = snap["meta"]
    assert meta["stale_kline_count"] == 3
    assert meta["realtime_trade_date"] == FRESH_TRADE_DATE
    assert meta["kline_max_date"] == "2026-03-02"
    assert {item["symbol"] for item in meta["skipped_stale"]} == {"600001", "600002", "600003"}
    assert all(item["reason"] == "beyond_tolerance" for item in meta["skipped_stale"])
    assert all(item["stale_kline"] is True for item in meta["skipped_stale"])
    # 关键回归：陈旧 K 线 + 实时价不再产出跨期混算的 +77% 类指标
    assert not any(abs(float(item["dist_ma20_pct"])) > 30 for item in snap["entries"])


def test_hot_stock_ai_appends_only_verifiable_realtime_bar(tmp_path: Path):
    """Issue #51：缓存恰好缺当日时，只有「实时价昨收口径可与缓存收盘对账」的标的才追加当日 bar 统一计算。"""
    kline_rows = {
        # 平稳 10.0 收盘到周五 2026-04-03；实时 10.8 (+8%) → 推导昨收恰为 10.0，可验证 → 追加
        "600001": _flat_rows(10.0, 200_000_000, end="2026-04-03"),
        # 平稳 5.0 收盘；实时 9.0 (+10%) → 推导昨收 8.18 与缓存 5.0 严重不符 → 不可信，跳过
        "600002": _flat_rows(5.0, 160_000_000, end="2026-04-03"),
    }
    provider = FakeProvider(
        rows=[
            {"rank": 1, "symbol": "600001", "name": "补bars", "latest_price": 10.8, "change_amount": 0.8, "change_pct": 8.0},
            {"rank": 2, "symbol": "600002", "name": "错位股", "latest_price": 9.0, "change_amount": 0.82, "change_pct": 10.0},
        ]
    )
    service = HotStockAIService(
        provider=provider,
        realtime_trade_date="2026-04-06",
        kline_store=FakeKlineStore(kline_rows),
        kronos_service=FakeKronos(),
        state_store=SQLiteStateStore(str(tmp_path / "state.db")),
    )

    asyncio.run(service.run(trigger="manual"))
    snap = service.get_snapshot()
    meta = snap["meta"]

    assert snap["trade_date"] == "2026-04-06"
    assert meta["realtime_trade_date"] == "2026-04-06"
    assert meta["realtime_bar_appended_count"] == 1
    assert meta["stale_kline_count"] == 1
    assert meta["skipped_stale"][0]["symbol"] == "600002"
    assert meta["skipped_stale"][0]["reason"] == "realtime_prev_close_mismatch"

    entry = snap["entries"][0]
    assert entry["symbol"] == "600001"
    assert entry["trade_date"] == "2026-04-06"
    assert entry["kline_max_date"] == "2026-04-03"
    assert entry["stale_kline"] is True
    assert entry["realtime_bar_appended"] is True
    assert entry["amount_base_date"] == "2026-04-03"
    # 追加当日 bar 后，均线基期与卡片价格一致：ma20 = (19*10.0 + 10.8)/20 = 10.04 → 距MA20 ≈ +7.57%
    expected_ma20 = (19 * 10.0 + 10.8) / 20
    assert entry["dist_ma20_pct"] == round((10.8 - expected_ma20) / expected_ma20 * 100.0, 2)
    assert abs(entry["dist_ma20_pct"]) < 10.0


def test_hot_stock_ai_quantile_tiering_when_prediction_missing(tmp_path: Path):
    """Issue #52：Kronos 无预测结果时，改用分位数降级分层，重点/买入池不再静默恒空。"""
    service = HotStockAIService(
        provider=FakeProvider(),
        realtime_trade_date=FRESH_TRADE_DATE,
        kline_store=FakeKlineStore(),
        kronos_service=EmptyKronos(),
        state_store=SQLiteStateStore(str(tmp_path / "state.db")),
    )

    asyncio.run(service.run(trigger="manual"))
    snap = service.get_snapshot()
    meta = snap["meta"]

    assert meta["prediction_enabled"] is True
    assert meta["prediction_available"] is False
    assert meta["pred_missing_count"] == len(snap["entries"])
    assert meta["pool_tier_mode"] == "quantile_degraded"
    assert "prediction_unavailable" in meta["pool_tier_reason"]
    # 降级分层保证有区分度（旧绝对阈值下 focus/buy 恒空）
    assert snap["pools"]["buy"], "无预测时买入池不应静默恒空"
    assert snap["pools"]["focus"], "无预测时重点池不应静默恒空"
    assert len(snap["pools"]["buy"]) == 1
    assert snap["pools"]["buy"][0]["symbol"] == "600001"


def test_hot_stock_ai_absolute_tiering_kept_when_prediction_available(tmp_path: Path):
    """Issue #52：有 Kronos 预测时保持原绝对阈值语义（向后兼容）。"""
    service = HotStockAIService(
        provider=FakeProvider(),
        realtime_trade_date=FRESH_TRADE_DATE,
        kline_store=FakeKlineStore(),
        kronos_service=FakeKronos(),
        state_store=SQLiteStateStore(str(tmp_path / "state.db")),
    )

    asyncio.run(service.run(trigger="manual"))
    snap = service.get_snapshot()
    meta = snap["meta"]

    assert meta["prediction_available"] is True
    assert meta["pred_missing_count"] == 0
    assert meta["pool_tier_mode"] == "absolute"
    assert snap["pools"]["buy"][0]["symbol"] == "600001"
    assert snap["pools"]["focus"][0]["symbol"] == "600002"


def test_hot_stock_ai_auto_light_run_reports_prediction_disabled(tmp_path: Path):
    """Issue #52：自动轻量模式（未启用 Kronos）同样暴露降级状态而非静默。"""
    service = HotStockAIService(
        provider=FakeProvider(),
        realtime_trade_date=FRESH_TRADE_DATE,
        kline_store=FakeKlineStore(),
        kronos_service=FakeKronos(),
        state_store=SQLiteStateStore(str(tmp_path / "state.db")),
    )

    asyncio.run(service.run(trigger="auto"))
    snap = service.get_snapshot()
    meta = snap["meta"]

    assert meta["prediction_enabled"] is False
    assert meta["prediction_available"] is False
    assert meta["pool_tier_mode"] == "quantile_degraded"
