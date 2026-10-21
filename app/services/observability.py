"""可观测性基础设施 — 内存指标、后台任务注册表、健康检查。

设计原则：
- 指标只保存在进程内存（无外部依赖），通过 /api/metrics 与 /api/health 暴露。
- 后台任务统一注册，避免 create_task 引用丢失，并在 shutdown 时可控取消。
- 健康检查为纯函数 `collect_health()`，依赖以参数注入，便于单元测试。
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.parse import quote

from app.services.time_utils import now_cn

log = logging.getLogger("observability")


# ─────────────────────────────── 指标 ───────────────────────────────


class Metrics:
    """线程安全的内存计数器。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: Counter[str] = Counter()
        self._started_at = now_cn().isoformat(timespec="seconds")

    def inc(self, name: str, amount: float = 1) -> None:
        with self._lock:
            self._counters[name] += amount

    # 语义化快捷方法
    def inc_timeout(self, component: str) -> None:
        self.inc(f"timeouts.{component}")

    def inc_degraded(self, component: str) -> None:
        self.inc(f"degraded.{component}")

    def inc_job_failure(self, kind: str) -> None:
        self.inc(f"job_failures.{kind}")

    def inc_job_timeout(self, kind: str) -> None:
        self.inc(f"job_timeouts.{kind}")

    def record_request(self, *, method: str, path: str, status_code: int, duration_ms: float) -> None:
        self.inc("requests_total")
        self.inc("request_duration_ms_total", round(duration_ms, 3))
        if status_code >= 500:
            self.inc("request_errors_total")
        if status_code >= 400:
            self.inc("request_client_errors_total")
        self.inc(f"requests.{method.upper()}")

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            counters = dict(self._counters)
        return {
            "started_at": self._started_at,
            "counters": counters,
        }


METRICS = Metrics()


# ─────────────────────────── 后台散任务注册表 ───────────────────────────


class BackgroundTaskRegistry:
    """持引用 + 统一取消，防止 asyncio.create_task 被 GC 且 shutdown 可等待。"""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task] = set()

    def spawn(self, coro, *, name: str | None = None) -> asyncio.Task:
        task = asyncio.create_task(coro, name=name)

        def _done(t: asyncio.Task) -> None:
            self._tasks.discard(t)
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                log.error("background task failed: %s", exc, exc_info=exc)

        self._tasks.add(task)
        task.add_done_callback(_done)
        return task

    @property
    def size(self) -> int:
        return len(self._tasks)

    async def shutdown(self, timeout: float = 20.0) -> None:
        tasks = list(self._tasks)
        if not tasks:
            return
        for task in tasks:
            task.cancel()
        await asyncio.wait(tasks, timeout=timeout)
        self._tasks.clear()


# ─────────────────────────────── 健康检查 ───────────────────────────────

_TASK_LABELS = {
    "ticker_task": "行情 ticker",
    "kline_cache_task": "K 线缓存调度",
    "hermes_task": "Hermes 调度",
    "monitor_task": "智能监控",
    "predict_funnel_task": "预测选股调度",
    "hot_stock_ai_task": "热门股票 AI 调度",
}


def check_sqlite_readable(path: str | Path, timeout_seconds: float = 1.0) -> dict[str, Any]:
    """以只读 URI + busy_timeout 执行 SELECT 1，防止健康检查阻塞写路径。"""
    resolved = str(Path(path).resolve())
    started = time.perf_counter()
    error = ""
    try:
        uri = f"file:{quote(resolved)}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=timeout_seconds)
        try:
            conn.execute(f"PRAGMA busy_timeout = {int(timeout_seconds * 1000)}")
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()
    except Exception as exc:  # sqlite3.OperationalError / file missing
        error = f"{type(exc).__name__}: {exc}"
    return {
        "path": resolved,
        "ok": not error,
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        "error": error,
    }


def read_kline_latest_date(path: str | Path, timeout_seconds: float = 1.0) -> tuple[str, str]:
    """读取 K 线同步状态中的最近成功交易日（避免全表 MAX 扫描大库）。"""
    resolved = str(Path(path).resolve())
    try:
        uri = f"file:{quote(resolved)}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=timeout_seconds)
        try:
            conn.execute(f"PRAGMA busy_timeout = {int(timeout_seconds * 1000)}")
            row = conn.execute(
                "SELECT last_success_trade_date FROM kline_sync_state WHERE id = 1"
            ).fetchone()
        finally:
            conn.close()
        return (str(row[0]) if row and row[0] else ""), ""
    except Exception as exc:
        return "", f"{type(exc).__name__}: {exc}"


def _kline_check(path: str | Path, now: datetime, max_stale_days: int = 10) -> dict[str, Any]:
    latest, error = read_kline_latest_date(path)
    if error:
        return {"status": "degraded", "latest_date": "", "stale_days": None, "error": error}
    if not latest:
        return {
            "status": "degraded",
            "latest_date": "",
            "stale_days": None,
            "error": "尚无成功同步记录",
        }
    try:
        stale_days = (now.date() - date.fromisoformat(latest[:10])).days
    except ValueError:
        return {"status": "degraded", "latest_date": latest, "stale_days": None, "error": "日期格式异常"}
    status = "ok" if stale_days <= max_stale_days else "degraded"
    return {"status": status, "latest_date": latest, "stale_days": stale_days, "error": ""}


def collect_health(
    *,
    db_paths: Sequence[str | Path],
    kline_db_path: str | Path | None,
    kronos_status: dict[str, Any],
    job_stats: Callable[[], dict[str, int]],
    task_states: dict[str, bool | None],
    now: datetime | None = None,
) -> tuple[dict[str, Any], int]:
    """聚合健康状态。返回 (payload, http_status)。

    - 任一核心 SQLite 不可读 → status=error, HTTP 503
    - 后台任务退出 / K 线过旧 / 任务统计失败 → status=degraded, HTTP 200
    """
    current = now or now_cn()
    checks: dict[str, Any] = {}

    db_items = [check_sqlite_readable(p) for p in db_paths]
    db_ok = all(item["ok"] for item in db_items)
    checks["database"] = {
        "status": "ok" if db_ok else "error",
        "items": db_items,
    }

    task_items: dict[str, str] = {}
    not_running: list[str] = []
    for key, label in _TASK_LABELS.items():
        done = task_states.get(key)
        if done is None:
            task_items[label] = "missing"
            not_running.append(label)
        elif done:
            task_items[label] = "done"
            not_running.append(label)
        else:
            task_items[label] = "running"
    checks["background_tasks"] = {
        "status": "ok" if not not_running else "degraded",
        "tasks": task_items,
        "not_running": not_running,
    }

    loaded = bool(kronos_status.get("loaded"))
    loading = bool(kronos_status.get("loading"))
    checks["kronos"] = {
        "status": "ok" if loaded else ("loading" if loading else "idle"),
        "loaded": loaded,
        "loading": loading,
    }

    if kline_db_path is None:
        checks["kline_data"] = {"status": "degraded", "latest_date": "", "error": "未配置"}
    else:
        checks["kline_data"] = _kline_check(kline_db_path, current)

    try:
        stats = job_stats()
        checks["jobs"] = {"status": "ok", **stats}
    except Exception as exc:
        log.warning("job stats unavailable: %s", exc)
        checks["jobs"] = {"status": "degraded", "error": f"{type(exc).__name__}: {exc}"}

    statuses = [checks[key]["status"] for key in checks]
    if "error" in statuses:
        overall = "error"
        http_status = 503
    elif "degraded" in statuses:
        overall = "degraded"
        http_status = 200
    else:
        overall = "ok"
        http_status = 200
    if overall != "ok":
        METRICS.inc_degraded(f"health.{overall}")
    return {
        "status": overall,
        "timestamp": current.isoformat(timespec="seconds"),
        "checks": checks,
        "metrics": METRICS.snapshot(),
    }, http_status
