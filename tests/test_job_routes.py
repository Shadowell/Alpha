"""统一任务 API 契约测试：202 / 轮询 / 409 / 失败 / 列表 / 同步兼容。"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import agent, jobs, strategy
from app.services.job_store import JobStore
from app.services.jobs import JobManager


class FakeRuntime:
    def __init__(self, *, success: bool = True, gate: threading.Event | None = None):
        self.success = success
        self.gate = gate

    async def run_task(self, task_type, trigger="manual", params=None):
        if self.gate is not None:
            import asyncio

            await asyncio.to_thread(self.gate.wait)
        if not self.success:
            return {"success": False, "message": "LLM 不可用"}
        return {"success": True, "task_id": 7, "summary": {"task": task_type}}


class FakeStateStore:
    def __init__(self):
        self.data = {
            "id": "s1",
            "name": "测试策略",
            "description": "",
            "rules": [{"rule_code": "price_range", "enabled": True, "params": {"min": 5, "max": 60}}],
            "is_builtin": False,
            "is_default": True,
        }

    def get_custom_strategy(self, strategy_id):
        return dict(self.data) if strategy_id == "s1" else None


class FakeScanner:
    def __init__(self, gate: threading.Event | None = None):
        self.gate = gate

    def is_running(self, strategy_id):
        return False

    async def scan(self, strategy, limit=None):
        if self.gate is not None:
            import asyncio

            await asyncio.to_thread(self.gate.wait)
        return {"strategy_id": strategy.id, "total_hits": 2, "hits": [], "total_scanned": 10}


class FakeBacktestLab:
    async def run_custom_strategy(self, strategy, **kwargs):
        return {"strategy": f"custom:{strategy.id}", "total_signals": 4, "params": kwargs}


def _container(tmp_path: Path, *, runtime=None, scanner=None):
    store = JobStore(tmp_path / "jobs.db")
    manager = JobManager(store)
    container = SimpleNamespace(
        job_manager=manager,
        job_store=store,
        hermes_runtime=runtime or FakeRuntime(),
        hermes_memory=None,
        funnel_service=SimpleNamespace(state_store=FakeStateStore()),
        custom_strategy_scanner=scanner or FakeScanner(),
        backtest_lab=FakeBacktestLab(),
    )
    return container


def _client(container) -> TestClient:
    app = FastAPI()
    for router in (agent.router, strategy.router, jobs.router):
        app.include_router(router, prefix="/api")
    app.state.container = container
    return TestClient(app)


def _wait_job(client: TestClient, job_id: str, timeout: float = 3.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = client.get(f"/api/jobs/{job_id}")
        assert response.status_code == 200
        payload = response.json()
        if payload["status"] in ("succeeded", "failed", "interrupted"):
            return payload
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish in {timeout}s")


def test_agent_run_returns_202_and_poll_succeeds(tmp_path: Path):
    container = _container(tmp_path)
    with _client(container) as client:
        response = client.post("/api/agent/run", json={"task_type": "daily_review"})
        assert response.status_code == 202
        accepted = response.json()
        assert accepted["status"] == "queued"
        assert accepted["poll_url"] == f"/api/jobs/{accepted['job_id']}"
        finished = _wait_job(client, accepted["job_id"])
        assert finished["status"] == "succeeded"
        assert finished["result"]["success"] is True
        assert finished["progress"]["percent"] == 100
        assert finished["kind"] == "agent_run"


def test_duplicate_agent_run_returns_409_with_existing_job_id(tmp_path: Path):
    gate = threading.Event()
    container = _container(tmp_path, runtime=FakeRuntime(gate=gate))
    with _client(container) as client:
        first = client.post("/api/agent/run", json={"task_type": "daily_review"})
        assert first.status_code == 202
        second = client.post("/api/agent/run", json={"task_type": "daily_review"})
        assert second.status_code == 409
        payload = second.json()
        assert payload["job_id"] == first.json()["job_id"]
        assert payload["poll_url"].endswith(payload["job_id"])
        gate.set()
        assert _wait_job(client, payload["job_id"])["status"] == "succeeded"


def test_agent_failure_records_error(tmp_path: Path):
    container = _container(tmp_path, runtime=FakeRuntime(success=False))
    with _client(container) as client:
        accepted = client.post("/api/agent/run", json={"task_type": "notice_review"}).json()
        finished = _wait_job(client, accepted["job_id"])
        assert finished["status"] == "failed"
        assert "LLM 不可用" in finished["error"]


def test_agent_sync_compat_returns_legacy_payload(tmp_path: Path):
    container = _container(tmp_path)
    with _client(container) as client:
        response = client.post("/api/agent/run?sync=true", json={"task_type": "daily_review"})
        assert response.status_code == 200
        assert response.json()["success"] is True
        # 同步模式不创建任务
        assert client.get("/api/jobs").json()["items"] == []


def test_custom_scan_and_backtest_jobs(tmp_path: Path):
    container = _container(tmp_path)
    with _client(container) as client:
        scan = client.post("/api/strategy/custom/s1/scan?limit=100")
        assert scan.status_code == 202
        scan_job = _wait_job(client, scan.json()["job_id"])
        assert scan_job["status"] == "succeeded"
        assert scan_job["result"]["total_hits"] == 2
        assert scan_job["kind"] == "strategy_scan"

        backtest = client.post("/api/strategy/custom/s1/backtest?history_days=60&hold_days=2")
        assert backtest.status_code == 202
        bt_job = _wait_job(client, backtest.json()["job_id"])
        assert bt_job["status"] == "succeeded"
        assert bt_job["result"]["total_signals"] == 4
        assert bt_job["kind"] == "strategy_backtest"


def test_scan_duplicate_returns_409(tmp_path: Path):
    gate = threading.Event()
    container = _container(tmp_path, scanner=FakeScanner(gate=gate))
    with _client(container) as client:
        first = client.post("/api/strategy/custom/s1/scan")
        assert first.status_code == 202
        second = client.post("/api/strategy/custom/s1/scan")
        assert second.status_code == 409
        assert second.json()["job_id"] == first.json()["job_id"]
        gate.set()
        _wait_job(client, first.json()["job_id"])


def test_scan_sync_compat_returns_legacy_payload(tmp_path: Path):
    container = _container(tmp_path)
    with _client(container) as client:
        response = client.post("/api/strategy/custom/s1/scan?sync=true&limit=50")
        assert response.status_code == 200
        assert response.json()["total_hits"] == 2


def test_job_list_filter_and_unknown_job(tmp_path: Path):
    container = _container(tmp_path)
    with _client(container) as client:
        accepted = client.post("/api/agent/run", json={"task_type": "daily_review"}).json()
        _wait_job(client, accepted["job_id"])
        listing = client.get("/api/jobs?kind=agent_run&limit=10").json()
        assert listing["total"] == 1
        assert listing["items"][0]["id"] == accepted["job_id"]
        assert client.get("/api/jobs?kind=other").json()["items"] == []
        assert client.get("/api/jobs/does-not-exist").status_code == 404
