"""#50：POST /api/pool/enroll 入池接口回归。

覆盖：入池成功、重复入池幂等、逐级晋升到目标池、跨级非法（文案与 /api/pool/move
逐字一致）、买入池容量满、非法 symbol/source/name 超长由 Pydantic 422、审计字段落盘。
不触碰仓库 data/*.db（一律 tmp_path SQLite）。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import market as market_router
from app.services.funnel_service import FunnelService
from app.services.pool_transition import MISSING_ENTRY_MESSAGE, buy_pool_full_message

SYMBOL = "000065"


class FakeProvider:
    kline_store = None

    def __init__(self, name_map: dict[str, str] | None = None):
        self.name_map = name_map or {}

    async def get_realtime_snapshot(self, **kwargs):
        return pd.DataFrame()

    async def get_symbol_name_map(self, cache_ttl_seconds=3600):
        return dict(self.name_map)

    async def get_trade_days(self):
        return pd.DataFrame({"trade_date": ["2026-10-22"]})


def _entry(symbol: str, pool: str, name: str = "占位") -> dict:
    return {
        "symbol": symbol,
        "name": name,
        "pool": pool,
        "score": 80.0,
        "prev_score": 80.0,
        "metrics": {},
        "concept_tags": [],
        "reasons": [],
        "warnings": [],
        "trigger_log": [],
    }


def _funnel(
    tmp_path, entries: dict | None = None, name_map: dict | None = None
) -> FunnelService:
    service = FunnelService(
        provider=FakeProvider(name_map),
        persist_db_path=str(tmp_path / "funnel_state.db"),
    )
    service.trade_date = "2026-10-22"
    service.entries = dict(entries) if entries is not None else {}
    service._refresh_market_panels_unlocked = lambda force=False: asyncio.sleep(0)
    return service


def _app_client(service: FunnelService) -> TestClient:
    container = SimpleNamespace(
        funnel_service=service,
        broadcast_snapshot=lambda: asyncio.sleep(0),
    )
    app = FastAPI()
    app.include_router(market_router.router, prefix="/api")
    app.state.container = container
    return TestClient(app)


# ── 服务级 ──


def test_enroll_creates_candidate_entry_with_audit(tmp_path):
    service = _funnel(tmp_path)
    resp = asyncio.run(service.enroll_pool(SYMBOL, "北方国际", "strategy_scan"))
    assert resp.success is True
    assert resp.pool == "candidate" and resp.created is True and resp.moved is False

    entry = service.entries[SYMBOL]
    assert entry["name"] == "北方国际"
    assert entry["enroll_source"] == "strategy_scan"
    assert any(log["note"] == "入池: strategy_scan" for log in entry["trigger_log"])
    assert entry["concept_tags"] == [] and entry["transitions"]["above60_count"] == 0


def test_enroll_falls_back_to_provider_name_map(tmp_path):
    service = _funnel(tmp_path, name_map={SYMBOL: "北方国际"})
    resp = asyncio.run(service.enroll_pool(SYMBOL, None, "strategy_scan"))
    assert resp.success is True
    assert service.entries[SYMBOL]["name"] == "北方国际"


def test_enroll_is_idempotent_for_duplicate_symbol(tmp_path):
    service = _funnel(tmp_path)

    async def _run():
        first = await service.enroll_pool(SYMBOL, "北方国际", "strategy_scan")
        second = await service.enroll_pool(SYMBOL, "北方国际", "strategy_scan")
        return first, second

    first, second = asyncio.run(_run())
    assert first.success and second.success
    assert second.created is False
    assert second.message == "已在选股池（幂等入池）"
    assert list(service.entries) == [SYMBOL]
    # 幂等入池不得重复堆积审计日志
    assert (
        sum(
            1
            for log in service.entries[SYMBOL]["trigger_log"]
            if log["note"].startswith("入池:")
        )
        == 1
    )


def test_enroll_with_target_focus_promotes_stepwise(tmp_path):
    service = _funnel(tmp_path)
    resp = asyncio.run(
        service.enroll_pool("600508", "上海能源", "strategy_scan", target_pool="focus")
    )
    assert resp.success and resp.pool == "focus" and resp.created and resp.moved
    logs = service.entries["600508"]["trigger_log"]
    assert any(
        "手动迁移: candidate -> focus · source=strategy_scan" in log["note"]
        for log in logs
    )


def test_enroll_new_symbol_to_buy_is_cross_level_illegal_message(tmp_path):
    service = _funnel(tmp_path)
    resp = asyncio.run(
        service.enroll_pool(SYMBOL, "北方国际", "strategy_scan", target_pool="buy")
    )
    assert resp.success is False
    assert "禁止跨级晋升" in resp.message
    # 跨级预检失败时不应创建半成品条目
    assert SYMBOL not in service.entries


def test_enroll_missing_entry_move_equivalence_still_holds(tmp_path):
    # 基线核对：move 对不在池个股仍返回既有文案，enroll 不改变该语义。
    service = _funnel(tmp_path)
    moved = asyncio.run(service.move_pool("999999", "focus"))
    assert moved.success is False and moved.message == MISSING_ENTRY_MESSAGE


def test_enroll_into_full_buy_pool_returns_capacity_message(tmp_path):
    service = _funnel(tmp_path)
    limit = service.config.buy_pool_max_size
    service.entries = {
        "600508": _entry("600508", "focus", "上海能源"),
        **{f"60001{i}": _entry(f"60001{i}", "buy") for i in range(limit)},
    }
    resp = asyncio.run(
        service.enroll_pool("600508", "上海能源", "strategy_scan", target_pool="buy")
    )
    assert resp.success is False
    assert resp.message == buy_pool_full_message(limit)
    assert service.entries["600508"]["pool"] == "focus"


# ── API 级（前端 funnel.js 固定契约：{symbol, name, source}）──


def test_api_enroll_success_and_pool_field(tmp_path):
    client = _app_client(_funnel(tmp_path))
    resp = client.post(
        "/api/pool/enroll",
        json={"symbol": SYMBOL, "name": "北方国际", "source": "strategy_scan"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["pool"] == "candidate"
    assert body["symbol"] == SYMBOL


def test_api_enroll_cross_level_400_same_detail_as_move(tmp_path):
    service = _funnel(tmp_path, entries={SYMBOL: _entry(SYMBOL, "candidate")})
    client = _app_client(service)
    move_resp = client.post(
        "/api/pool/move", json={"symbol": SYMBOL, "target_pool": "buy"}
    )
    enroll_resp = client.post(
        "/api/pool/enroll",
        json={
            "symbol": SYMBOL,
            "name": "北方国际",
            "source": "strategy_scan",
            "target_pool": "buy",
        },
    )
    assert move_resp.status_code == 400 and enroll_resp.status_code == 400
    assert enroll_resp.json()["detail"] == move_resp.json()["detail"]


def test_api_enroll_capacity_full_400_message_matches_move(tmp_path):
    service = _funnel(tmp_path)
    limit = service.config.buy_pool_max_size
    service.entries = {
        "600508": _entry("600508", "focus"),
        **{f"60001{i}": _entry(f"60001{i}", "buy") for i in range(limit)},
    }
    client = _app_client(service)
    move_resp = client.post(
        "/api/pool/move", json={"symbol": "600508", "target_pool": "buy"}
    )
    enroll_resp = client.post(
        "/api/pool/enroll",
        json={
            "symbol": "600508",
            "name": "上海能源",
            "source": "strategy_scan",
            "target_pool": "buy",
        },
    )
    assert move_resp.status_code == 400 and enroll_resp.status_code == 400
    assert (
        move_resp.json()["detail"]
        == enroll_resp.json()["detail"]
        == buy_pool_full_message(limit)
    )


@pytest.mark.parametrize("bad_symbol", ["abc123", "12345", "1234567", ""])
def test_api_enroll_invalid_symbol_422(tmp_path, bad_symbol):
    client = _app_client(_funnel(tmp_path))
    resp = client.post(
        "/api/pool/enroll", json={"symbol": bad_symbol, "source": "strategy_scan"}
    )
    assert resp.status_code == 422


def test_api_enroll_symbol_prefix_normalized(tmp_path):
    client = _app_client(_funnel(tmp_path))
    resp = client.post(
        "/api/pool/enroll", json={"symbol": "sz000065", "source": "strategy_scan"}
    )
    assert resp.status_code == 200
    assert resp.json()["symbol"] == SYMBOL


def test_api_enroll_invalid_source_and_target_422(tmp_path):
    client = _app_client(_funnel(tmp_path))
    bad_source = client.post(
        "/api/pool/enroll", json={"symbol": SYMBOL, "source": "hot_tip"}
    )
    bad_target = client.post(
        "/api/pool/enroll", json={"symbol": SYMBOL, "target_pool": "watch"}
    )
    assert bad_source.status_code == 422
    assert bad_target.status_code == 422


def test_api_enroll_name_too_long_422(tmp_path):
    client = _app_client(_funnel(tmp_path))
    resp = client.post(
        "/api/pool/enroll",
        json={"symbol": SYMBOL, "name": "名" * 65, "source": "strategy_scan"},
    )
    assert resp.status_code == 422
