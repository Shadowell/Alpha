"""写接口 Pydantic 校验与边界测试（422 而非 500）。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import agent, paper, predict, strategy


class FakePaperTrading:
    def __init__(self) -> None:
        self.settings = {
            "commission_rate": 0.00025,
            "min_commission": 5.0,
            "stamp_tax_rate": 0.0005,
            "slippage_rate": 0.001,
        }

    def update_settings(self, **kwargs):
        self.settings.update({k: v for k, v in kwargs.items() if k in self.settings})

    def get_settings(self):
        return dict(self.settings)


class FakeStateStore:
    def upsert_custom_strategy(self, payload):
        saved = dict(payload)
        saved["id"] = saved.get("id") or "generated-id"
        return saved


class FakeConfigService:
    def __init__(self, config):
        self.config = config

    def get_config(self):
        return dict(self.config)

    def update_config(self, patch):
        self.config.update(patch)
        return dict(self.config)


@pytest.fixture()
def client() -> TestClient:
    container = SimpleNamespace(
        paper_trading=FakePaperTrading(),
        funnel_service=SimpleNamespace(state_store=FakeStateStore()),
        predict_funnel_service=FakeConfigService({"top_k_boards": 10, "feishu_enabled": True}),
        hot_stock_ai_service=FakeConfigService({"top_n": 20, "tradingagents_enabled": True}),
        hermes_memory=SimpleNamespace(),
    )
    app = FastAPI()
    for router in (paper.router, predict.router, strategy.router, agent.router):
        app.include_router(router, prefix="/api")
    app.state.container = container
    return TestClient(app)


def _assert_422(response):
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail  # 可读错误信息


# ── 模拟盘 ──


@pytest.mark.parametrize(
    "payload",
    [
        {"symbol": "600519", "qty": 0},
        {"symbol": "600519", "qty": -100},
        {"symbol": "600519", "qty": "abc"},
        {"symbol": "600519", "qty": 1_000_001},
        {"symbol": "", "qty": 100},
        {"qty": 100},
    ],
)
def test_paper_buy_rejects_invalid_qty(client: TestClient, payload):
    _assert_422(client.post("/api/paper/buy", json=payload))


def test_paper_sell_requires_position_id(client: TestClient):
    _assert_422(client.post("/api/paper/sell", json={"note": "x"}))
    _assert_422(client.post("/api/paper/sell", json={"position_id": ""}))


@pytest.mark.parametrize(
    "payload",
    [
        {"commission_rate": -0.1},
        {"commission_rate": 1.5},
        {"min_commission": -1},
        {"min_commission": 101},
        {"stamp_tax_rate": 2},
        {"slippage_rate": 0.5},
        {"slippage_rate": -0.1},
    ],
)
def test_paper_settings_rejects_out_of_range(client: TestClient, payload):
    _assert_422(client.post("/api/paper/settings", json=payload))


def test_paper_settings_accepts_boundaries(client: TestClient):
    response = client.post(
        "/api/paper/settings",
        json={
            "commission_rate": 0,
            "min_commission": 100,
            "stamp_tax_rate": 1,
            "slippage_rate": 0.1,
        },
    )
    assert response.status_code == 200
    assert response.json()["slippage_rate"] == 0.1


# ── 配置类 ──


@pytest.mark.parametrize(
    "payload",
    [
        {"top_k_boards": 0},
        {"top_k_boards": 51},
        {"top_k_boards": "abc"},
        {"horizon": 11},
        {"lookback": 10},
        {"feishu_enabled": "not-a-bool"},
    ],
)
def test_predict_funnel_config_rejects_invalid(client: TestClient, payload):
    _assert_422(client.post("/api/predict-funnel/config", json=payload))


def test_predict_funnel_config_allows_extra_fields(client: TestClient):
    response = client.post("/api/predict-funnel/config", json={"top_k_boards": 8, "future_flag": True})
    assert response.status_code == 200
    assert response.json()["config"]["top_k_boards"] == 8


@pytest.mark.parametrize(
    "payload",
    [
        {"top_n": 4},
        {"top_n": 31},
        {"lookback": 10},
        {"tradingagents_top_n": 21},
        {"tradingagents_timeout_seconds": 10},
        {"refresh_interval_minutes": 0},
        {"auto_refresh_enabled": "maybe"},
    ],
)
def test_hot_stock_ai_config_rejects_invalid(client: TestClient, payload):
    _assert_422(client.post("/api/strategy/hot-stock-ai/config", json=payload))


# ── 自定义策略规则 ──


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "", "rules": [{"rule_code": "price_range"}]},
        {"name": "   ", "rules": [{"rule_code": "price_range"}]},
        {"name": "x", "rules": []},
        {"name": "x", "rules": [{"rule_code": "unknown_rule"}]},
        {"name": "x", "rules": [{"rule_code": "price_range", "params": {"min": -5}}]},
        {"name": "x", "rules": [{"rule_code": "price_range", "params": {"max": 5000}}]},
        {"name": "x", "rules": [{"rule_code": "volume_spike_today", "params": {"lookback": "abc"}}]},
        {"name": "x", "rules": [{"rule_code": "volume_spike_today", "params": {"nope": 1}}]},
        {"name": "x", "rules": [{"rule_code": "exclude_boards", "params": {"exclude_st": "yes"}}]},
        {"name": "x", "rules": [{"rule_code": "ma_bull_stack", "params": {"periods": "5,x"}}]},
        {"name": "x", "rules": "not-a-list"},
    ],
)
def test_custom_strategy_rejects_invalid_rules(client: TestClient, payload):
    _assert_422(client.post("/api/strategy/custom", json=payload))


def test_custom_strategy_accepts_valid_rule_and_coerces_types(client: TestClient):
    response = client.post(
        "/api/strategy/custom",
        json={
            "name": " 我的策略 ",
            "description": "d",
            "rules": [
                {"rule_code": "price_range", "enabled": True, "params": {"min": 5, "max": 60}},
                {"rule_code": "volume_spike_today", "params": {"lookback": 20.0, "min_ratio": 2}},
            ],
        },
    )
    assert response.status_code == 200, response.text
    strategy = response.json()["strategy"]
    assert strategy["name"] == "我的策略"
    assert strategy["rules"][1]["params"]["lookback"] == 20
    assert isinstance(strategy["rules"][1]["params"]["lookback"], int)


# ── 扫描/回测参数边界 ──


@pytest.mark.parametrize(
    "query",
    [
        "limit=0",
        "limit=10001",
        "hold_days=0",
        "hold_days=21",
        "tp_pct=0.5",
        "tp_pct=51",
        "sl_pct=-50",
        "sl_pct=0",
        "history_days=29",
        "history_days=366",
    ],
)
def test_backtest_query_bounds(client: TestClient, query):
    _assert_422(client.post(f"/api/strategy/custom/s1/backtest?{query}"))


@pytest.mark.parametrize("query", ["limit=0", "limit=10001"])
def test_scan_limit_bounds(client: TestClient, query):
    _assert_422(client.post(f"/api/strategy/custom/s1/scan?{query}"))


@pytest.mark.parametrize(
    "query",
    [
        "lookback_days=4",
        "lookback_days=61",
        "amp_threshold=0.01",
        "vol_cv_threshold=2",
        "vol_spike_ratio=100",
        "limit=10001",
    ],
)
def test_quiet_breakout_scan_bounds(client: TestClient, query):
    _assert_422(client.post(f"/api/strategy/quiet-breakout/scan?{query}"))


# ── Agent / 监控 ──


@pytest.mark.parametrize(
    "payload",
    [
        {"task_type": "rm -rf"},
        {"task_type": "daily_review", "params": "not-a-dict"},
    ],
)
def test_agent_run_rejects_invalid_body(client: TestClient, payload):
    _assert_422(client.post("/api/agent/run", json=payload))


@pytest.mark.parametrize(
    "payload",
    [
        {"interval_minutes": 0},
        {"interval_minutes": 241},
        {"enabled": "maybe"},
    ],
)
def test_monitor_config_rejects_invalid(client: TestClient, payload):
    _assert_422(client.post("/api/agent/monitor/config", json=payload))


def test_malformed_json_returns_422(client: TestClient):
    response = client.post(
        "/api/paper/settings",
        content="{not-json",
        headers={"Content-Type": "application/json"},
    )
    _assert_422(response)


# ── OpenAPI schema ──


def test_openapi_exposes_request_models(client: TestClient):
    schema = client.get("/openapi.json").json()
    components = schema["components"]["schemas"]
    for name in (
        "PaperBuyRequest",
        "PaperSellRequest",
        "PaperSettingsUpdate",
        "PredictFunnelConfigUpdate",
        "HotStockAIConfigUpdate",
        "CustomStrategyUpsert",
        "AgentRunRequest",
        "MonitorConfigUpdate",
    ):
        assert name in components, f"missing schema {name}"
    qty_schema = components["PaperBuyRequest"]["properties"]["qty"]
    assert qty_schema["maximum"] == 1_000_000
    assert qty_schema["exclusiveMinimum"] == 0
    settings = components["PaperSettingsUpdate"]["properties"]

    def _non_null(schema):
        return next((part for part in schema.get("anyOf", [schema]) if part.get("type") != "null"), schema)

    assert _non_null(settings["commission_rate"])["maximum"] == 1
    assert _non_null(settings["slippage_rate"])["maximum"] == 0.1
    for path in ("/api/paper/buy", "/api/strategy/custom", "/api/agent/run"):
        assert "requestBody" in schema["paths"][path]["post"]
