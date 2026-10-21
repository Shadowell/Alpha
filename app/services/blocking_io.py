"""统一的外部阻塞调用入口（超时 / 计数 / 熔断）。

AkShare / Tushare 都是同步库，只能在子线程里跑。如果直接使用
``asyncio.to_thread``，它们会占用事件循环的默认线程池；上游无响应时线程
会被长期占满，Kronos 推理和所有本地 ``to_thread`` 调用都会排队（见 issue #9）。

这里把外部调用隔离到独立的有界线程池：

* 超时可配置（``ALPHA_EXTERNAL_TIMEOUT_SECONDS``，默认 12 秒）；
* 超时抛出可识别的 :class:`ExternalTimeoutError`（``TimeoutError`` 子类），
  调用方可以继续走既有降级路径；
* 连续超时后按数据源熔断冷却（``ALPHA_EXTERNAL_BREAKER_THRESHOLD`` /
  ``ALPHA_EXTERNAL_BREAKER_COOLDOWN_SECONDS``）；
* 每次调用记录结构化日志与进程内计数，便于 ``/api/health`` 或日志排查。

由于超时只取消 asyncio future，工作线程本身仍会继续执行到返回为止；
因此线程池必须与默认执行器隔离，且熔断要阻止后续请求继续堆积。
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, TypeVar

logger = logging.getLogger("alpha.external_io")

DEFAULT_TIMEOUT_SECONDS = 12.0
DEFAULT_MAX_WORKERS = 8
DEFAULT_BREAKER_THRESHOLD = 3
DEFAULT_BREAKER_COOLDOWN_SECONDS = 60.0

T = TypeVar("T")


class ExternalTimeoutError(TimeoutError):
    """外部数据源在约定时间内没有返回。"""


class ExternalCircuitOpenError(ExternalTimeoutError):
    """熔断器打开期间直接拒绝调用，避免继续占用外部线程。"""


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(str(raw).strip())
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(float(str(raw).strip()))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in {"0", "false", "no", "off", ""}


def external_timeout_seconds() -> float:
    """外部调用默认超时（秒）。每次调用时读取，便于测试与运行时调整。"""
    return max(0.1, min(_env_float("ALPHA_EXTERNAL_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS), 120.0))


def breaker_enabled() -> bool:
    return _env_bool("ALPHA_EXTERNAL_BREAKER_ENABLED", True)


def breaker_threshold() -> int:
    return max(1, _env_int("ALPHA_EXTERNAL_BREAKER_THRESHOLD", DEFAULT_BREAKER_THRESHOLD))


def breaker_cooldown_seconds() -> float:
    return max(1.0, _env_float("ALPHA_EXTERNAL_BREAKER_COOLDOWN_SECONDS", DEFAULT_BREAKER_COOLDOWN_SECONDS))


_executor_lock = threading.Lock()
_executor: ThreadPoolExecutor | None = None


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        with _executor_lock:
            if _executor is None:
                workers = max(1, min(_env_int("ALPHA_EXTERNAL_MAX_WORKERS", DEFAULT_MAX_WORKERS), 64))
                _executor = ThreadPoolExecutor(
                    max_workers=workers,
                    thread_name_prefix="alpha-external",
                )
    return _executor


_stats_lock = threading.Lock()
_stats: Counter[str] = Counter()
# name -> (连续超时次数, 熔断截止 monotonic 时间)
_breaker_state: dict[str, tuple[int, float]] = {}


def reset_external_io_state() -> None:
    """清空计数与熔断状态（测试用）。不会重建正在工作的线程池。"""
    with _stats_lock:
        _stats.clear()
        _breaker_state.clear()


def external_io_stats() -> dict[str, dict[str, int]]:
    """返回按数据源聚合的调用统计快照。"""
    with _stats_lock:
        snapshot: dict[str, dict[str, int]] = {}
        for key, value in _stats.items():
            name, _, metric = key.partition("|")
            snapshot.setdefault(name or "unknown", {})[metric] = value
    return snapshot


def _bump(name: str, metric: str, amount: int = 1) -> None:
    with _stats_lock:
        _stats[f"{name}|{metric}"] += amount


def _check_breaker(name: str) -> None:
    if not breaker_enabled():
        return
    now = time.monotonic()
    with _stats_lock:
        failures, open_until = _breaker_state.get(name, (0, 0.0))
        if open_until > now:
            _stats[f"{name}|circuit_open"] += 1
            remaining = open_until - now
    if failures and open_until > now:
        logger.warning(
            "external_circuit_open name=%s remaining_s=%.1f consecutive_timeouts=%d",
            name,
            remaining,
            failures,
        )
        raise ExternalCircuitOpenError(f"{name} circuit open for {remaining:.1f}s")


def _record_timeout(name: str, timeout: float, elapsed: float) -> None:
    now = time.monotonic()
    with _stats_lock:
        _stats[f"{name}|timeouts"] += 1
        failures, open_until = _breaker_state.get(name, (0, 0.0))
        failures += 1
        if breaker_enabled() and failures >= breaker_threshold():
            open_until = now + breaker_cooldown_seconds()
            _stats[f"{name}|breaker_trips"] += 1
        _breaker_state[name] = (failures, open_until)
    logger.warning(
        "external_timeout name=%s timeout_s=%.2f elapsed_s=%.2f consecutive_timeouts=%d",
        name,
        timeout,
        elapsed,
        failures,
    )


def _record_success(name: str) -> None:
    with _stats_lock:
        _stats[f"{name}|ok"] += 1
        if name in _breaker_state:
            _breaker_state[name] = (0, 0.0)


def _record_error(name: str, exc: BaseException) -> None:
    with _stats_lock:
        _stats[f"{name}|errors"] += 1
        # 非超时错误说明数据源有响应，重置连续超时计数。
        if name in _breaker_state:
            _breaker_state[name] = (0, 0.0)
    logger.warning(
        "external_error name=%s error=%s message=%s",
        name,
        type(exc).__name__,
        str(exc)[:200],
    )


async def run_blocking(
    fn: Callable[..., T],
    *args: Any,
    timeout: float | None = None,
    name: str = "external",
    **kwargs: Any,
) -> T:
    """在线程池中执行同步函数，并施加超时。

    与 ``asyncio.to_thread`` 的区别：使用独立的有界线程池，坏掉的数据源不会
    占满默认执行器（Kronos / 本地 SQLite 仍可用）。超时抛出
    :class:`ExternalTimeoutError`，调用方捕获后走降级路径。
    """
    _check_breaker(name)
    budget = external_timeout_seconds() if timeout is None else max(0.05, float(timeout))
    started = time.monotonic()
    loop = asyncio.get_running_loop()
    call = functools.partial(fn, *args, **kwargs)
    future = loop.run_in_executor(_get_executor(), call)
    try:
        result = await asyncio.wait_for(future, budget)
    except asyncio.CancelledError:
        # 外层 wait_for（如模拟盘 2.5s 保护）取消时，线程仍在运行；
        # 计入该数据源的连续失败，避免重复请求继续堆积。
        _record_timeout(name, budget, time.monotonic() - started)
        logger.warning("external_cancelled name=%s elapsed_s=%.2f", name, time.monotonic() - started)
        raise
    except ExternalTimeoutError:
        raise
    except (asyncio.TimeoutError, TimeoutError) as exc:  # noqa: UP041 - 3.10 兼容
        _record_timeout(name, budget, time.monotonic() - started)
        raise ExternalTimeoutError(f"{name} timed out after {budget:.2f}s") from exc
    except Exception as exc:
        _record_error(name, exc)
        raise
    else:
        _record_success(name)
        return result
