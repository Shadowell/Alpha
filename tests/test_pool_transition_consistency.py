"""Issue #56：策略池 /api/pool/move 与公告池 /api/notice/pool/move 迁池层级一致性回归。

覆盖：合法逐级晋升、非法跨级（一致 4xx 文案）、降级、同级幂等、买入池容量。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import market as market_router
from app.routers import notice as notice_router
from app.services.funnel_service import FunnelService
from app.services.notice_service import NoticeService
from app.services.pool_transition import (
    INVALID_TARGET_MESSAGE,
    MISSING_ENTRY_MESSAGE,
    buy_pool_full_message,
    validate_pool_transition,
)

SYMBOL = "600000"


# ── validator 纯函数 ──


def test_validator_promote_is_level_by_level():
    ok_cf = validate_pool_transition("candidate", "focus")
    ok_fb = validate_pool_transition("focus", "buy")
    assert ok_cf.allowed and ok_cf.code == "ok_promote"
    assert ok_fb.allowed and ok_fb.code == "ok_promote"
    assert ok_cf.reason == "手动迁移: candidate -> focus"


def test_validator_rejects_skip_level_promotion():
    bad = validate_pool_transition("candidate", "buy")
    assert not bad.allowed
    assert bad.code == "skip_level"
    assert "禁止跨级晋升" in bad.message
    assert "候选→重点→买入" in bad.message


def test_validator_allows_demote_and_idempotent():
    assert validate_pool_transition("buy", "candidate").allowed
    assert validate_pool_transition("buy", "focus").allowed
    same = validate_pool_transition("focus", "focus")
    assert same.allowed and same.code == "ok_idempotent"


def test_validator_invalid_pool_names():
    assert validate_pool_transition("candidate", "watch").message == INVALID_TARGET_MESSAGE
    assert validate_pool_transition(None, "focus").code == "invalid_current"
    assert validate_pool_transition("candidate", "").message == INVALID_TARGET_MESSAGE


# ── 服务级：策略池与公告池同一语义 ──


class FakeProvider:
    async def get_realtime_snapshot(self, **kwargs):
        return pd.DataFrame()

    async def get_symbol_name_map(self, cache_ttl_seconds=3600):
        return {}


def _funnel(tmp_path, pool="candidate"):
    service = FunnelService(provider=FakeProvider(), persist_db_path=str(tmp_path / "funnel_state.db"))
    service.entries = {
        SYMBOL: {
            "symbol": SYMBOL,
            "name": "测试股",
            "pool": pool,
            "score": 80.0,
            "metrics": {},
            "concept_tags": [],
            "reasons": [],
            "warnings": [],
            "trigger_log": [],
        }
    }
    service._refresh_market_panels_unlocked = lambda force=False: asyncio.sleep(0)
    return service


def _notice(tmp_path, pool="candidate"):
    service = NoticeService(state_store=SimpleNamespace(load_notice_state=lambda: None, save_notice_state=lambda payload: None))
    service.entries = {
        SYMBOL: {
            "symbol": SYMBOL,
            "name": "测试公告股",
            "pool": pool,
            "score": 90.0,
            "title": "业绩预增公告",
            "notice_type": "业绩预告",
            "notice_date": "2026-10-22",
            "url": "http://x/1",
            "reason": "业绩预增",
            "risk": "",
        }
    }
    return service


@pytest.mark.parametrize("factory", [_funnel, _notice], ids=["funnel", "notice"])
def test_cross_level_promotion_rejected_identically(tmp_path, factory):
    funnel_msg = ""
    service = factory(tmp_path)

    async def _move():
        if isinstance(service, FunnelService):
            resp = await service.move_pool(SYMBOL, "buy")
            return resp.message
        payload = await service.move_pool(SYMBOL, "buy")
        return payload["message"]

    msg = asyncio.run(_move())
    assert "禁止跨级晋升" in msg
    # 两侧文案逐字一致
    other = _notice(tmp_path) if isinstance(service, FunnelService) else _funnel(tmp_path)

    async def _other():
        if isinstance(other, FunnelService):
            return (await other.move_pool(SYMBOL, "buy")).message
        return (await other.move_pool(SYMBOL, "buy"))["message"]

    assert asyncio.run(_other()) == msg
    assert service.entries[SYMBOL]["pool"] == "candidate"


@pytest.mark.parametrize("factory", [_funnel, _notice], ids=["funnel", "notice"])
def test_stepwise_promote_demote_and_idempotent(tmp_path, factory):
    service = factory(tmp_path)

    async def _move(target):
        if isinstance(service, FunnelService):
            return await service.move_pool(SYMBOL, target)
        return await service.move_pool(SYMBOL, target)

    res = asyncio.run(_move("focus"))
    assert (res.success if isinstance(res, object) and hasattr(res, "success") else res["success"]) is True
    assert service.entries[SYMBOL]["pool"] == "focus"

    res = asyncio.run(_move("focus"))  # 同级幂等
    ok = res.success if hasattr(res, "success") else res["success"]
    assert ok is True

    res = asyncio.run(_move("candidate"))  # 降级允许
    ok = res.success if hasattr(res, "success") else res["success"]
    assert ok is True

    logs = service.entries[SYMBOL].get("trigger_log", [])
    if isinstance(service, FunnelService):
        assert any("手动迁移: focus -> candidate" in log["note"] for log in logs)
    else:
        assert any("手动迁移: focus -> candidate" in log["note"] for log in logs)


@pytest.mark.parametrize("factory", [_funnel, _notice], ids=["funnel", "notice"])
def test_buy_pool_capacity_shared_rule(tmp_path, factory):
    service = factory(tmp_path, pool="focus")
    max_size = service.config.buy_pool_max_size if isinstance(service, FunnelService) else None
    from app.config import StrategyConfig

    limit = max_size if max_size is not None else StrategyConfig().buy_pool_max_size
    for i in range(limit):
        other = f"60001{i}"
        service.entries[other] = {
            "symbol": other,
            "name": f"占位{i}",
            "pool": "buy",
            "score": 80.0,
            "metrics": {},
            "concept_tags": [],
            "reasons": [],
            "warnings": [],
            "trigger_log": [],
            "title": "",
            "notice_type": "",
            "notice_date": "",
            "url": "",
            "reason": "",
            "risk": "",
        }

    async def _move():
        if isinstance(service, FunnelService):
            return await service.move_pool(SYMBOL, "buy")
        return await service.move_pool(SYMBOL, "buy")

    res = asyncio.run(_move())
    msg = res.message if hasattr(res, "message") else res["message"]
    assert msg == buy_pool_full_message(limit)
    assert service.entries[SYMBOL]["pool"] == "focus"


@pytest.mark.parametrize("factory", [_funnel, _notice], ids=["funnel", "notice"])
def test_missing_symbol_same_message(tmp_path, factory):
    service = factory(tmp_path)

    async def _move():
        if isinstance(service, FunnelService):
            return await service.move_pool("999999", "focus")
        return await service.move_pool("999999", "focus")

    res = asyncio.run(_move())
    msg = res.message if hasattr(res, "message") else res["message"]
    assert msg == MISSING_ENTRY_MESSAGE


# ── API 级：两个路由的非法迁移行为一致（同源 4xx） ──


@pytest.fixture()
def client(tmp_path):
    funnel = _funnel(tmp_path)
    notice = _notice(tmp_path)
    container = SimpleNamespace(
        funnel_service=funnel,
        notice_service=notice,
        broadcast_snapshot=lambda: asyncio.sleep(0),
    )
    app = FastAPI()
    app.include_router(market_router.router, prefix="/api")
    app.include_router(notice_router.router, prefix="/api")
    app.state.container = container
    return TestClient(app)


def test_api_pool_move_illegal_cross_level_400(client):
    resp = client.post("/api/pool/move", json={"symbol": SYMBOL, "target_pool": "buy"})
    assert resp.status_code == 400
    assert "禁止跨级晋升" in resp.json()["detail"]


def test_api_notice_pool_move_illegal_cross_level_400_same_detail(client):
    resp_strategy = client.post("/api/pool/move", json={"symbol": SYMBOL, "target_pool": "buy"})
    resp_notice = client.post("/api/notice/pool/move", json={"symbol": SYMBOL, "target_pool": "buy"})
    assert resp_notice.status_code == 400
    assert resp_notice.json()["detail"] == resp_strategy.json()["detail"]


def test_api_pool_move_legal_and_idempotent(client):
    resp = client.post("/api/pool/move", json={"symbol": SYMBOL, "target_pool": "focus"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True and body["pool"] == "focus"

    resp_same = client.post("/api/pool/move", json={"symbol": SYMBOL, "target_pool": "focus"})
    assert resp_same.status_code == 200

    resp_notice = client.post("/api/notice/pool/move", json={"symbol": SYMBOL, "target_pool": "focus"})
    assert resp_notice.status_code == 200
    assert resp_notice.json()["pool"] == "focus"


def test_api_pool_move_invalid_target_400(client):
    resp = client.post("/api/pool/move", json={"symbol": SYMBOL, "target_pool": "hold"})
    assert resp.status_code == 422  # Pydantic PoolName 先在请求模型层拦截
    resp_notice = client.post("/api/notice/pool/move", json={"symbol": SYMBOL, "target_pool": "hold"})
    assert resp_notice.status_code == 422
