"""健康检查与指标测试：ok / degraded / error 分支 + 全应用装配冒烟。"""
from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import system
from app.services.observability import collect_health
from app.services.time_utils import now_cn


def _make_db(path: Path, *, with_kline_state: bool = False, latest: str | None = None) -> Path:
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS dummy (id INTEGER)")
        if with_kline_state:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS kline_sync_state "
                "(id INTEGER PRIMARY KEY, last_success_trade_date TEXT)"
            )
            conn.execute(
                "INSERT OR REPLACE INTO kline_sync_state (id, last_success_trade_date) VALUES (1, ?)",
                (latest,),
            )
        conn.commit()
    finally:
        conn.close()
    return path


def _db_paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    funnel = _make_db(tmp_path / "funnel_state.db")
    kline = _make_db(tmp_path / "market_kline.db", with_kline_state=True, latest=now_cn().date().isoformat())
    jobs = _make_db(tmp_path / "jobs.db")
    return funnel, kline, jobs


def _running_tasks(**overrides):
    keys = (
        "ticker_task",
        "kline_cache_task",
        "hermes_task",
        "monitor_task",
        "predict_funnel_task",
        "hot_stock_ai_task",
    )
    states = {key: False for key in keys}
    states.update(overrides)
    return states


def test_health_ok(tmp_path: Path):
    funnel, kline, jobs = _db_paths(tmp_path)
    payload, status_code = collect_health(
        db_paths=[funnel, kline, jobs],
        kline_db_path=kline,
        kronos_status={"loaded": True, "loading": False},
        job_stats=lambda: {"queued": 0, "running": 1, "succeeded": 2, "failed": 0, "interrupted": 0},
        task_states=_running_tasks(),
    )
    assert status_code == 200
    assert payload["status"] == "ok"
    assert payload["checks"]["database"]["status"] == "ok"
    assert payload["checks"]["background_tasks"]["status"] == "ok"
    assert payload["checks"]["kline_data"]["status"] == "ok"
    assert payload["checks"]["kronos"]["loaded"] is True


def test_health_degraded_for_dead_task(tmp_path: Path):
    funnel, kline, jobs = _db_paths(tmp_path)
    payload, status_code = collect_health(
        db_paths=[funnel, kline, jobs],
        kline_db_path=kline,
        kronos_status={"loaded": False, "loading": False},
        job_stats=lambda: {"queued": 0, "running": 0, "succeeded": 0, "failed": 0, "interrupted": 0},
        task_states=_running_tasks(ticker_task=True),
    )
    assert status_code == 200
    assert payload["status"] == "degraded"
    assert payload["checks"]["background_tasks"]["status"] == "degraded"
    assert payload["checks"]["background_tasks"]["not_running"]


def test_health_degraded_for_stale_kline(tmp_path: Path):
    funnel = _make_db(tmp_path / "funnel_state.db")
    stale = (now_cn() - timedelta(days=30)).date().isoformat()
    kline = _make_db(tmp_path / "market_kline.db", with_kline_state=True, latest=stale)
    jobs = _make_db(tmp_path / "jobs.db")
    payload, status_code = collect_health(
        db_paths=[funnel, kline, jobs],
        kline_db_path=kline,
        kronos_status={"loaded": False, "loading": False},
        job_stats=lambda: {"queued": 0, "running": 0, "succeeded": 0, "failed": 0, "interrupted": 0},
        task_states=_running_tasks(),
    )
    assert status_code == 200
    assert payload["status"] == "degraded"
    assert payload["checks"]["kline_data"]["stale_days"] >= 30


def test_health_error_when_core_db_unavailable(tmp_path: Path):
    missing = tmp_path / "missing.db"
    funnel, kline, jobs = _db_paths(tmp_path)
    payload, status_code = collect_health(
        db_paths=[missing, kline, jobs],
        kline_db_path=kline,
        kronos_status={"loaded": False, "loading": False},
        job_stats=lambda: {"queued": 0, "running": 0, "succeeded": 0, "failed": 0, "interrupted": 0},
        task_states=_running_tasks(),
    )
    assert status_code == 503
    assert payload["status"] == "error"
    assert payload["checks"]["database"]["status"] == "error"


def test_health_degraded_when_job_stats_fail(tmp_path: Path):
    funnel, kline, jobs = _db_paths(tmp_path)

    def broken_stats():
        raise RuntimeError("jobs db locked")

    payload, status_code = collect_health(
        db_paths=[funnel, kline, jobs],
        kline_db_path=kline,
        kronos_status={"loaded": False, "loading": False},
        job_stats=broken_stats,
        task_states=_running_tasks(),
    )
    assert status_code == 200
    assert payload["checks"]["jobs"]["status"] == "degraded"


class _FakeTask:
    def __init__(self, done: bool) -> None:
        self._done = done

    def done(self) -> bool:
        return self._done


def _health_app(tmp_path: Path, *, db_missing: bool = False) -> TestClient:
    funnel, kline, jobs = _db_paths(tmp_path)
    db_paths = [funnel if not db_missing else tmp_path / "nope.db", kline, jobs]
    container = SimpleNamespace(
        db_paths=db_paths,
        kline_db_path=kline,
        kronos_service=SimpleNamespace(status=lambda: {"loaded": False, "loading": False}),
        job_manager=SimpleNamespace(stats=lambda: {"queued": 1, "running": 0, "succeeded": 0, "failed": 0, "interrupted": 0}),
    )
    app = FastAPI()
    app.include_router(system.router, prefix="/api")
    app.state.container = container
    for key in (
        "ticker_task",
        "kline_cache_task",
        "hermes_task",
        "monitor_task",
        "predict_funnel_task",
        "hot_stock_ai_task",
    ):
        setattr(app.state, key, _FakeTask(key == "ticker_task"))
    return TestClient(app)


def test_health_route_returns_200_when_degraded(tmp_path: Path):
    with _health_app(tmp_path) as client:
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json()["status"] == "degraded"  # kline 无同步记录 + 任务缺失判定为运行中


def test_health_route_returns_503_on_db_error(tmp_path: Path):
    with _health_app(tmp_path, db_missing=True) as client:
        response = client.get("/api/health")
        assert response.status_code == 503
        assert response.json()["status"] == "error"


def test_metrics_route_exposes_counters(tmp_path: Path):
    with _health_app(tmp_path) as client:
        response = client.get("/api/metrics")
        assert response.status_code == 200
        payload = response.json()
        assert "metrics" in payload and "counters" in payload["metrics"]
        assert payload["jobs"]["queued"] == 1


def test_metrics_records_timeout_degraded_and_job_failure():
    from app.services.observability import METRICS

    METRICS.inc_timeout("unit")
    METRICS.inc_degraded("unit")
    METRICS.inc_job_failure("unit")
    counters = METRICS.snapshot()["counters"]
    assert counters["timeouts.unit"] >= 1
    assert counters["degraded.unit"] >= 1
    assert counters["job_failures.unit"] >= 1


def test_full_app_health_and_request_id(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("ALPHA_DATA_DIR", str(tmp_path))
    import app.main as main

    with TestClient(main.app) as client:
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.headers.get("X-Request-ID")
        payload = response.json()
        assert payload["status"] in ("ok", "degraded")
        assert payload["checks"]["database"]["status"] == "ok"
        assert set(payload["checks"]) >= {
            "database",
            "background_tasks",
            "kronos",
            "kline_data",
            "jobs",
        }
        custom_header = client.get("/api/health", headers={"X-Request-ID": "trace-123"})
        assert custom_header.headers.get("X-Request-ID") == "trace-123"
