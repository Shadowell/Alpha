"""JobManager / JobStore 生命周期测试（不依赖网络/LLM）。"""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from app.services.job_store import JobStore
from app.services.jobs import JobConflict, JobManager


def _make(tmp_path: Path, *, max_retained: int = 200) -> tuple[JobStore, JobManager]:
    store = JobStore(tmp_path / "jobs.db")
    return store, JobManager(store, max_retained=max_retained)


def test_submit_poll_success_and_progress(tmp_path: Path):
    store, manager = _make(tmp_path)

    async def scenario():
        gate = threading.Event()

        async def runner(ctx):
            ctx.progress(50, "扫描中")
            await asyncio.to_thread(gate.wait)
            return {"hits": 3}

        job = await manager.submit(kind="strategy_scan", runner=runner, dedupe_key="s1")
        assert job["status"] == "queued"
        await asyncio.sleep(0.05)
        running = manager.get_job(job["id"])
        assert running["status"] == "running"
        assert running["progress"]["percent"] == 50
        gate.set()
        for _ in range(50):
            current = manager.get_job(job["id"])
            if current["status"] == "succeeded":
                break
            await asyncio.sleep(0.02)
        assert current["status"] == "succeeded"
        assert current["result"] == {"hits": 3}
        assert current["progress"]["percent"] == 100
        assert current["error"] is None
        assert current["finished_at"]

    asyncio.run(scenario())


def test_duplicate_active_job_raises_conflict(tmp_path: Path):
    _, manager = _make(tmp_path)

    async def scenario():
        gate = threading.Event()

        async def runner(ctx):
            await asyncio.to_thread(gate.wait)

        first = await manager.submit(kind="agent_run", runner=runner, dedupe_key="daily_review")
        with_exc = None
        try:
            await manager.submit(kind="agent_run", runner=runner, dedupe_key="daily_review")
        except JobConflict as exc:
            with_exc = exc
        assert with_exc is not None
        assert with_exc.job["id"] == first["id"]
        # 不同 dedupe_key 可并行
        second = await manager.submit(kind="agent_run", runner=runner, dedupe_key="notice_review")
        assert second["id"] != first["id"]
        gate.set()
        await asyncio.gather(*manager._tasks.values(), return_exceptions=True)

    asyncio.run(scenario())


def test_failed_runner_records_error(tmp_path: Path):
    _, manager = _make(tmp_path)

    async def scenario():
        async def runner(ctx):
            raise ValueError("bad input")

        job = await manager.submit(kind="agent_run", runner=runner)
        await asyncio.sleep(0.05)
        current = manager.get_job(job["id"])
        assert current["status"] == "failed"
        assert "bad input" in current["error"]
        assert "ValueError" in current["error"]

    asyncio.run(scenario())


def test_timeout_marks_failed(tmp_path: Path):
    _, manager = _make(tmp_path)

    async def scenario():
        async def runner(ctx):
            await asyncio.sleep(1)

        job = await manager.submit(kind="agent_run", runner=runner, timeout_seconds=0.05)
        await asyncio.sleep(0.15)
        current = manager.get_job(job["id"])
        assert current["status"] == "failed"
        assert "超时" in current["error"]

    asyncio.run(scenario())


def test_cancel_marks_interrupted(tmp_path: Path):
    _, manager = _make(tmp_path)

    async def scenario():
        gate = threading.Event()

        async def runner(ctx):
            await asyncio.to_thread(gate.wait)

        job = await manager.submit(kind="strategy_backtest", runner=runner)
        await asyncio.sleep(0.02)
        assert manager.cancel(job["id"]) is True
        await asyncio.sleep(0.05)
        current = manager.get_job(job["id"])
        assert current["status"] == "interrupted"
        gate.set()

    asyncio.run(scenario())


def test_restart_marks_active_jobs_interrupted(tmp_path: Path):
    store, _ = _make(tmp_path)
    store.create(job_id="running1", kind="agent_run", dedupe_key="daily_review")
    store.update("running1", status="running")
    store.create(job_id="queued1", kind="strategy_scan", dedupe_key="s1")

    recovered = JobManager(store)
    assert recovered.recover() == 2
    assert store.get("running1")["status"] == "interrupted"
    assert store.get("queued1")["status"] == "interrupted"
    assert store.get("running1")["error"]
    # 历史可查询
    assert {j["id"] for j in recovered.list_jobs()} == {"running1", "queued1"}


def test_list_jobs_filter_and_limit(tmp_path: Path):
    store, manager = _make(tmp_path)
    for index in range(5):
        store.create(job_id=f"a{index}", kind="agent_run")
        store.update(f"a{index}", status="succeeded")
    store.create(job_id="b1", kind="strategy_scan")
    store.update("b1", status="failed")

    agent_jobs = manager.list_jobs(kind="agent_run", limit=3)
    assert len(agent_jobs) == 3
    assert all(job["kind"] == "agent_run" for job in agent_jobs)
    assert [job["id"] for job in manager.list_jobs(kind="strategy_scan")] == ["b1"]


def test_prune_keeps_recent_finished_jobs(tmp_path: Path):
    store, manager = _make(tmp_path, max_retained=3)
    for index in range(6):
        store.create(job_id=f"j{index}", kind="agent_run")
        store.update(f"j{index}", status="succeeded", finished_at=f"2026-01-01T00:00:0{index}")
    removed = store.prune(3)
    assert removed == 3
    assert len(manager.list_jobs(limit=10)) == 3


def test_stats_counts_statuses(tmp_path: Path):
    store, manager = _make(tmp_path)
    store.create(job_id="q", kind="agent_run")
    store.create(job_id="r", kind="agent_run")
    store.update("r", status="running")
    store.create(job_id="s", kind="agent_run")
    store.update("s", status="succeeded")
    stats = manager.stats()
    assert stats["queued"] == 1
    assert stats["running"] == 1
    assert stats["succeeded"] == 1
