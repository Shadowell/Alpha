"""打板数据底座只读 API 契约测试（issue #22）。

用假 Tushare 客户端 + 真 SQLite 存储验证五个端点的形状、参数校验、
空数据（返回空 items 而非 500）与错误脱敏。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import limit_board
from app.services.kline_store import KlineSQLiteStore
from app.services.limit_board_service import LimitBoardService
from app.services.tushare_market_data_client import (
    CAPABILITY_DENIED,
    LIMIT_BOARD_APIS,
    LimitApiResult,
)

TRADE_DATE = "2026-10-21"


class FakeClient:
    """最小假客户端：可控返回数据、空数据或抛异常。"""

    def __init__(self, *, empty: bool = False, broken: bool = False) -> None:
        self.is_ready = True
        self.empty = empty
        self.broken = broken

    async def call_limit_api(
        self, api: str, *, trade_date: str = "", ts_code: str = "", timeout=None
    ) -> LimitApiResult:
        if self.broken:
            raise RuntimeError("上游炸了")
        if self.empty:
            return LimitApiResult(pd.DataFrame(), "empty", "empty_response")
        if api == "limit_list_d":
            return LimitApiResult(
                pd.DataFrame(
                    [
                        {
                            "trade_date": TRADE_DATE.replace("-", ""),
                            "ts_code": "000001.SZ",
                            "industry": "银行",
                            "name": "测试银",
                            "close": 11.0,
                            "pct_chg": 10.0,
                            "amount": 9.0e8,
                            "limit_amount": 8.0e7,
                            "float_mv": 4.0e9,
                            "total_mv": 5.0e9,
                            "turnover_ratio": 9.5,
                            "fd_amount": 5.0e7,
                            "first_time": "92500",
                            "last_time": "145433",
                            "open_times": 1,
                            "up_stat": "1/1",
                            "limit_times": 2,
                            "limit": "U",
                        },
                        {
                            "trade_date": TRADE_DATE.replace("-", ""),
                            "ts_code": "000002.SZ",
                            "name": "测试软",
                            "close": 9.0,
                            "pct_chg": -10.0,
                            "amount": 2.0e8,
                            "float_mv": 2.0e9,
                            "fd_amount": 3.0e7,
                            "first_time": "145000",
                            "last_time": "145000",
                            "open_times": 0,
                            "up_stat": "0/0",
                            "limit_times": 1,
                            "limit": "D",
                        },
                    ]
                ),
                "ok",
                "",
            )
        if api == "kpl_list":
            return LimitApiResult(
                pd.DataFrame(
                    [
                        {
                            "ts_code": "000001.SZ",
                            "trade_date": TRADE_DATE.replace("-", ""),
                            "status": "2连板",
                            "theme": "AI应用",
                            "lu_desc": "数据要素",
                        }
                    ]
                ),
                "ok",
                "",
            )
        if api == "top_list":
            return LimitApiResult(
                pd.DataFrame(
                    [
                        {
                            "trade_date": TRADE_DATE.replace("-", ""),
                            "ts_code": "000001.SZ",
                            "name": "测试银",
                            "close": 11.0,
                            "pct_change": 10.0,
                            "turnover_rate": 9.5,
                            "amount": 9.0e8,
                            "l_buy": 1.0e8,
                            "l_sell": 9.0e7,
                            "l_amount": 1.9e8,
                            "net_amount": 1.0e7,
                            "net_rate": 1.1,
                            "amount_rate": 21.0,
                            "float_values": 4.0e9,
                            "reason": "日涨幅偏离值达到7%的前5只证券",
                        }
                    ]
                ),
                "ok",
                "",
            )
        if api == "top_inst":
            return LimitApiResult(
                pd.DataFrame(
                    [
                        {
                            "trade_date": TRADE_DATE.replace("-", ""),
                            "ts_code": "000001.SZ",
                            "exalter": "测试营业部",
                            "buy": 1.0e7,
                            "buy_rate": 1.1,
                            "sell": None,
                            "sell_rate": None,
                            "net_buy": 1.0e7,
                            "side": "0",
                            "reason": "日涨幅偏离值达到7%的前5只证券",
                        }
                    ]
                ),
                "ok",
                "",
            )
        if api == "hm_list":
            return LimitApiResult(
                pd.DataFrame(
                    [{"name": "测试游资", "desc": "测试简介", "orgs": '["测试营业部"]'}]
                ),
                "ok",
                "",
            )
        if api == "hsgt_top10":
            return LimitApiResult(
                pd.DataFrame(
                    [
                        {
                            "trade_date": TRADE_DATE.replace("-", ""),
                            "ts_code": "600519.SH",
                            "name": "测试酒",
                            "close": 1600.0,
                            "change": 3.0,
                            "rank": 1,
                            "market_type": "1",
                            "amount": 1.6e10,
                            "net_amount": None,
                            "buy": None,
                            "sell": None,
                        }
                    ]
                ),
                "ok",
                "",
            )
        return LimitApiResult(pd.DataFrame(), CAPABILITY_DENIED, "permission_denied")

    async def probe_limit_capabilities(self, trade_date: str, apis=None) -> list[dict]:
        out = []
        for api in apis or LIMIT_BOARD_APIS:
            result = await self.call_limit_api(api, trade_date=trade_date)
            capability = (
                "available" if result.status in {"ok", "empty"} else result.status
            )
            out.append(
                {
                    "api": api,
                    "capability": capability,
                    "reason": "ok" if capability == "available" else result.reason,
                    "sample_rows": len(result.frame),
                }
            )
        return out


def _build(tmp_path: Path, client: FakeClient):
    store = KlineSQLiteStore(str(tmp_path / "market_kline.db"))
    service = LimitBoardService(store=store, market_data_client=client)
    app = FastAPI()
    app.include_router(limit_board.router, prefix="/api")
    app.state.container = SimpleNamespace(limit_board_service=service)
    return TestClient(app), service, store


def _sync(service: LimitBoardService) -> dict:
    return asyncio.run(service.sync_trade_date(TRADE_DATE, force=True))


# ── 能力矩阵 ─────────────────────────────────────────────


def test_capabilities_endpoint_returns_matrix(tmp_path):
    api_client, _service, _store = _build(tmp_path, FakeClient())
    with api_client as api:
        cached = api.get("/api/data/tushare-capabilities")
        assert cached.status_code == 200
        body = cached.json()
        assert body["enabled"] is True
        # 未探测时明确告知，而不是伪装成空权限
        assert body["probed"] is False
        assert body["apis"] == []
        assert "note" in body

        refreshed = api.get("/api/data/tushare-capabilities?refresh=true")
        assert refreshed.status_code == 200
        matrix = refreshed.json()
        assert matrix["probed"] is True
        statuses = {item["api"]: item["capability"] for item in matrix["apis"]}
        assert statuses["limit_list_d"] == "available"
        assert statuses["limit_step"] == "denied"
        assert set(statuses) == set(LIMIT_BOARD_APIS)
        assert "token" not in refreshed.text.lower()


def test_capabilities_endpoint_degrades_on_probe_failure(tmp_path):
    class Broken:
        is_ready = True

        async def probe_limit_capabilities(self, trade_date, apis=None):
            raise RuntimeError("内部报文不应外泄 secret-text")

    with _client(tmp_path, Broken()) as api:
        response = api.get("/api/data/tushare-capabilities?refresh=true")

    assert response.status_code == 503
    assert "secret-text" not in response.text
    assert response.json()["error_id"].startswith("err_")


# ── 状态 ────────────────────────────────────────────────


def test_status_endpoint_lists_datasets_and_coverage(tmp_path):
    client = FakeClient()
    api_client, service, _store = _build(tmp_path, client)
    _sync(service)
    with api_client as api:
        response = api.get("/api/data/limit-board/status")

    assert response.status_code == 200
    body = response.json()
    assert body["latest_trade_date"] == TRADE_DATE
    assert body["coverage"]["total_rows"] == 2
    assert body["coverage"]["up_count"] == 1
    assert {item["dataset"] for item in body["datasets"]} == {
        "limit_board",
        "limit_board_theme",
        "limit_ladder",
        "dragon_tiger",
        "dragon_tiger_seat",
        "hot_money_seat",
        "hsgt_top10",
    }
    assert body["degradations"]["limit_board"] == "self_calc:kline_daily+stk_limit"


# ── 涨停板 + 天梯 ───────────────────────────────────────


def test_limit_board_by_date_returns_items_and_ladder(tmp_path):
    client = FakeClient()
    api_client, service, _store = _build(tmp_path, client)
    _sync(service)
    with api_client as api:
        compact = api.get(f"/api/data/limit-board/{TRADE_DATE.replace('-', '')}")
        iso = api.get(f"/api/data/limit-board/{TRADE_DATE}")
        ups = api.get(f"/api/data/limit-board/{TRADE_DATE}?board=U")
        filtered = api.get(
            f"/api/data/limit-board/{TRADE_DATE}?min_limit_times=2&limit=1"
        )

    assert compact.status_code == iso.status_code == 200
    body = iso.json()
    assert compact.json()["trade_date"] == TRADE_DATE
    assert body["count"] == 2
    assert {item["limit_flag"] for item in body["items"]} == {"U", "D"}
    assert body["items"][0]["fd_amount"] == 5.0e7
    assert body["items"][0]["first_time"] == "09:25:00"
    assert body["items"][0]["theme"] == "AI应用"
    assert body["ladder"][0]["ladder_level"] == 2
    assert ups.json()["count"] == 1
    assert filtered.json()["count"] == 1


def test_limit_board_empty_date_returns_empty_items(tmp_path):
    with _client(tmp_path, FakeClient()) as api:
        response = api.get("/api/data/limit-board/2020-01-01")

    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["ladder"] == []
    assert body["coverage"]["total_rows"] == 0
    assert body["coverage"]["non_null_ratio"]["fd_amount"] is None


def test_limit_board_invalid_params_return_400(tmp_path):
    with _client(tmp_path, FakeClient()) as api:
        bad_date = api.get("/api/data/limit-board/not-a-date")
        bad_month = api.get("/api/data/limit-board/2026-13-45")
        bad_board = api.get(f"/api/data/limit-board/{TRADE_DATE}?board=X")
        bad_limit = api.get(f"/api/data/limit-board/{TRADE_DATE}?limit=0")
        bad_times = api.get(f"/api/data/limit-board/{TRADE_DATE}?min_limit_times=99")

    assert [r.status_code for r in (bad_date, bad_month, bad_board)] == [400, 400, 400]
    assert bad_limit.status_code == 422
    assert bad_times.status_code == 422
    assert "需为" in bad_date.json()["detail"]


# ── 龙虎榜 / 游资 ───────────────────────────────────────


def test_dragon_tiger_by_date_returns_seats(tmp_path):
    client = FakeClient()
    api_client, service, _store = _build(tmp_path, client)
    _sync(service)
    with api_client as api:
        response = api.get(f"/api/data/dragon-tiger/{TRADE_DATE}")
        filtered = api.get(f"/api/data/dragon-tiger/{TRADE_DATE}?symbol=000001")
        missing = api.get("/api/data/dragon-tiger/2020-01-01")
        bad_symbol = api.get(f"/api/data/dragon-tiger/{TRADE_DATE}?symbol=6000006")

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["items"][0]["amount"] == 9.0e8
    assert body["seats"][0]["seat_name"] == "测试营业部"
    assert body["seats"][0]["side"] == "buy"
    assert filtered.json()["count"] == 1
    assert missing.status_code == 200
    assert missing.json()["items"] == []
    assert missing.json()["seats"] == []
    assert bad_symbol.status_code == 400


def test_hot_money_seats_endpoint(tmp_path):
    client = FakeClient()
    api_client, service, _store = _build(tmp_path, client)
    _sync(service)
    with api_client as api:
        response = api.get("/api/data/hot-money/seats")
        matched = api.get("/api/data/hot-money/seats?keyword=测试游资")
        empty = api.get("/api/data/hot-money/seats?keyword=不存在的席位")

    assert response.status_code == 200
    assert response.json()["items"][0]["seat_name"] == "测试游资"
    assert response.json()["items"][0]["orgs"] == ["测试营业部"]
    assert matched.json()["count"] == 1
    assert empty.json()["items"] == []


def test_queries_degrade_to_500_without_leaking(tmp_path, monkeypatch):
    client = FakeClient()
    api_client, service, _store = _build(tmp_path, client)
    _sync(service)

    def boom(*args, **kwargs):
        raise RuntimeError("database secret-token-abc")

    monkeypatch.setattr(service.store, "get_limit_board_daily", boom)
    with api_client as api:
        response = api.get(f"/api/data/limit-board/{TRADE_DATE}")

    assert response.status_code == 500
    assert "secret-token-abc" not in response.text
    assert response.json()["detail"] == "涨停板数据暂时不可用"


def _client(tmp_path: Path, client):
    """按测试习惯构造只挂载打板路由的 TestClient 上下文。"""
    return _build(tmp_path, client)[0]
