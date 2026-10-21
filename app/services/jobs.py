"""后台任务管理器 — 提交、去重、进度、超时、取消/清理。

任务契约（与前端约定）：
- 提交返回 job 记录，API 映射为 202 {job_id, status, poll_url}
- 同 kind + dedupe_key 的运行中任务重复提交 → JobConflict（API 409）
- 状态：queued / running / succeeded / failed / interrupted
- progress: {percent, message}
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from app.services.job_store import JobStore
from app.services.observability import METRICS
from app.services.time_utils import now_cn

log = logging.getLogger("jobs")

JobRunner = Callable[["JobContext"], Awaitable[Any]]


class JobConflict(Exception):
    """同类任务正在运行。"""

    def __init__(self, job: dict[str, Any]) -> None:
        super().__init__("同类任务正在运行")
        self.job = job


@dataclass
class JobContext:
    """传给任务执行体的句柄，用于上报阶段进度。"""

    job_id: str
    kind: str
    manager: "JobManager"

    def progress(self, percent: float, message: str = "") -> None:
        self.manager.update_progress(self.job_id, percent, message)


class JobManager:
    def __init__(self, store: JobStore, *, max_retained: int = 200) -> None:
        self.store = store
        self.max_retained = max(1, int(max_retained))
        self._tasks: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    # ── 生命周期 ──

    def recover(self) -> int:
        """服务启动时调用：上次进程遗留的 queued/running → interrupted。"""
        count = self.store.mark_stale_interrupted("服务重启，任务已中断")
        self.store.prune(self.max_retained)
        if count:
            log.warning("recovered %d interrupted job(s) after restart", count)
        return count

    async def shutdown(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self.store.mark_stale_interrupted("服务关闭，任务已中断")

    # ── 提交/执行 ──

    async def submit(
        self,
        *,
        kind: str,
        runner: JobRunner,
        dedupe_key: str = "",
        params: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
        request_id: str = "",
    ) -> dict[str, Any]:
        async with self._lock:
            existing = self.store.find_active(kind, dedupe_key)
            if existing is not None:
                raise JobConflict(existing)
            job = self.store.create(
                job_id=uuid.uuid4().hex,
                kind=kind,
                dedupe_key=dedupe_key,
                params=params or {},
                request_id=request_id,
            )
            METRICS.inc(f"jobs_submitted.{kind}")
            task = asyncio.create_task(
                self._run(job, runner, timeout_seconds),
                name=f"job:{kind}:{job['id']}",
            )
            self._tasks[job["id"]] = task
            return job

    async def _run(
        self,
        job: dict[str, Any],
        runner: JobRunner,
        timeout_seconds: float | None,
    ) -> None:
        job_id = job["id"]
        kind = job["kind"]
        self.store.update(
            job_id,
            status="running",
            progress={"percent": 5.0, "message": "任务已启动"},
        )
        context = JobContext(job_id=job_id, kind=kind, manager=self)
        try:
            coroutine = runner(context)
            if timeout_seconds is not None:
                result = await asyncio.wait_for(coroutine, timeout=timeout_seconds)
            else:
                result = await coroutine
            self.store.update(
                job_id,
                status="succeeded",
                result=result,
                error=None,
                progress={"percent": 100.0, "message": "已完成"},
                finished_at=now_cn().isoformat(timespec="seconds"),
            )
            log.info("job succeeded id=%s kind=%s", job_id, kind)
        except asyncio.TimeoutError:
            METRICS.inc_job_timeout(kind)
            message = f"任务超时（{timeout_seconds:g}s）"
            self.store.update(
                job_id,
                status="failed",
                error=message,
                progress={"percent": 100.0, "message": message},
                finished_at=now_cn().isoformat(timespec="seconds"),
            )
            log.warning("job timed out id=%s kind=%s timeout=%ss", job_id, kind, timeout_seconds)
        except asyncio.CancelledError:
            self.store.update(
                job_id,
                status="interrupted",
                error="任务已取消",
                progress={"percent": 100.0, "message": "任务已取消"},
                finished_at=now_cn().isoformat(timespec="seconds"),
            )
            log.warning("job interrupted id=%s kind=%s", job_id, kind)
            raise
        except Exception as exc:  # noqa: BLE001 — 任务失败需持久化并暴露可读错误
            METRICS.inc_job_failure(kind)
            message = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            self.store.update(
                job_id,
                status="failed",
                error=message,
                progress={"percent": 100.0, "message": "任务失败"},
                finished_at=now_cn().isoformat(timespec="seconds"),
            )
            log.exception("job failed id=%s kind=%s", job_id, kind)
        finally:
            self._tasks.pop(job_id, None)
            try:
                self.store.prune(self.max_retained)
            except Exception:  # noqa: BLE001 — 清理失败不应影响任务结果
                log.exception("job prune failed")

    def update_progress(self, job_id: str, percent: float, message: str = "") -> None:
        clamped = max(0.0, min(float(percent), 100.0))
        self.store.update(
            job_id,
            progress={"percent": round(clamped, 1), "message": message},
        )

    # ── 查询/取消 ──

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        return self.store.get(job_id)

    def list_jobs(self, *, kind: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        return self.store.list_jobs(kind=kind, limit=limit)

    def stats(self) -> dict[str, int]:
        return self.store.stats()

    def cancel(self, job_id: str) -> bool:
        task = self._tasks.get(job_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    @property
    def active_count(self) -> int:
        return len(self._tasks)
