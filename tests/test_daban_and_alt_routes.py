"""#22/#23/#24/#25/#26/#33/#36 容器装配、迁移与打板/另类情绪 API 路由集成测试。"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import alt_sentiment, daban, limit_board
from app.services.alt_sentiment import AltSentimentService, AltSentimentStore
from app.services.sqlite_migrations import (
    MARKET_KLINE_DB_MIGRATIONS,
    apply_migrations,
)


def _build_client(tmp_path) -> TestClient:
    kline_db = tmp_path / "market_kline.db"
    with sqlite3.connect(str(kline_db)) as conn:
        apply_migrations(conn, MARKET_KLINE_DB_MIGRATIONS)
        conn.commit()

    alt_store = AltSentimentStore(db_path=str(tmp_path / "alt_sentiment.db"))
    alt_service = AltSentimentService(store=alt_store)

    class _FakeLimitBoardService:
        is_enabled = True
        capability_snapshot = {"apis": [], "enabled": True}

        async def sync_trade_date(
            self, trade_date=None, *, datasets=None, trigger_mode="manual", force=False
        ):
            return {
                "trade_date": trade_date or "2026-10-22",
                "trigger_mode": trigger_mode,
                "force": force,
                "datasets": {},
            }

    container = SimpleNamespace(
        kline_db_path=kline_db,
        limit_board_service=_FakeLimitBoardService(),
        alt_sentiment_store=alt_store,
        alt_sentiment_service=alt_service,
    )
    app = FastAPI()
    app.include_router(limit_board.router, prefix="/api")
    app.include_router(alt_sentiment.router, prefix="/api")
    app.include_router(daban.router, prefix="/api")
    app.state.container = container
    return TestClient(app)


def test_limit_board_manual_sync_route(tmp_path):
    client = _build_client(tmp_path)
    resp = client.post("/api/data/limit-board/sync?trade_date=2026-10-22&force=true")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["result"]["trigger_mode"] == "manual"
    assert body["result"]["force"] is True


def test_market_sentiment_routes_and_migration(tmp_path):
    client = _build_client(tmp_path)
    comp = client.post(
        "/api/market/sentiment/compute",
        json={"start_date": "2026-10-20", "end_date": "2026-10-22"},
    )
    assert comp.status_code == 200
    assert comp.json()["status"] == "ok"

    latest = client.get("/api/market/sentiment/latest")
    assert latest.status_code == 200
    assert "available" in latest.json()

    hist = client.get("/api/market/sentiment/history?limit=10")
    assert hist.status_code == 200
    assert "items" in hist.json()


def test_alt_sentiment_routes_gated_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("ALPHA_ALT_SENTIMENT_ENABLED", raising=False)
    client = _build_client(tmp_path)

    src = client.get("/api/alt-sentiment/sources")
    assert src.status_code == 200
    assert src.json()["enabled"] is False

    sync_denied = client.post("/api/alt-sentiment/sync", json={"symbols": ["000001"]})
    assert sync_denied.status_code == 403

    cov = client.get("/api/alt-sentiment/coverage")
    assert cov.status_code == 200
    assert cov.json()["daily_rows"] == 0

    fac = client.get("/api/alt-sentiment/factors/000001")
    assert fac.status_code == 200
    assert fac.json()["symbol"] == "000001"

    rep = client.get("/api/alt-sentiment/offline-report")
    assert rep.status_code == 200
    assert "markdown" in rep.json()


def test_daban_research_backtest_and_validation_routes(tmp_path):
    client = _build_client(tmp_path)

    bt = client.post(
        "/api/daban/backtest",
        json={"signals": [], "mode": "strict", "initial_cash": 500000},
    )
    assert bt.status_code == 200
    assert bt.json()["engine"] == "limit_up_backtest/v1"

    mq = client.get("/api/daban/microstructure/quality?as_of_date=2026-10-22")
    assert mq.status_code == 200

    lb = client.post("/api/daban/labels/build", json={"mode": "all_limit_up"})
    assert lb.status_code == 200
    assert "row_count" in lb.json()

    val = client.post(
        "/api/daban/validation/run",
        json={"scenario": "informative_sequence", "n_dates": 60},
    )
    assert val.status_code == 200
    assert "overall_decision" in val.json()
