"""外部阻塞调用超时 / 熔断 / 降级的回归测试（issue #9）。"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.services.blocking_io import (
    ExternalCircuitOpenError,
    ExternalTimeoutError,
    external_io_stats,
    reset_external_io_state,
    run_blocking,
)


@pytest.fixture(autouse=True)
def _clean_external_state(monkeypatch):
    monkeypatch.setenv("ALPHA_EXTERNAL_BREAKER_ENABLED", "true")
    monkeypatch.setenv("ALPHA_EXTERNAL_BREAKER_THRESHOLD", "3")
    monkeypatch.setenv("ALPHA_EXTERNAL_BREAKER_COOLDOWN_SECONDS", "30")
    reset_external_io_state()
    yield
    reset_external_io_state()


def test_run_blocking_returns_result_and_counts_success():
    result = asyncio.run(run_blocking(lambda x: x * 2, 21, name="unit.success"))
    assert result == 42
    assert external_io_stats()["unit.success"]["ok"] == 1


def test_run_blocking_raises_identifiable_timeout(monkeypatch):
    monkeypatch.setenv("ALPHA_EXTERNAL_TIMEOUT_SECONDS", "0.15")

    def slow():
        time.sleep(0.7)
        return "late"

    started = time.monotonic()
    with pytest.raises(ExternalTimeoutError):
        asyncio.run(run_blocking(slow, name="unit.timeout"))
    elapsed = time.monotonic() - started
    assert elapsed < 0.6
    assert external_io_stats()["unit.timeout"]["timeouts"] == 1


def test_run_blocking_propagates_non_timeout_errors():
    def boom():
        raise ValueError("upstream said no")

    with pytest.raises(ValueError, match="upstream said no"):
        asyncio.run(run_blocking(boom, name="unit.error"))
    assert external_io_stats()["unit.error"]["errors"] == 1


def test_circuit_breaker_skips_calls_after_consecutive_timeouts(monkeypatch):
    monkeypatch.setenv("ALPHA_EXTERNAL_TIMEOUT_SECONDS", "0.1")
    monkeypatch.setenv("ALPHA_EXTERNAL_BREAKER_THRESHOLD", "2")
    monkeypatch.setenv("ALPHA_EXTERNAL_BREAKER_COOLDOWN_SECONDS", "5")
    calls: list[int] = []

    def slow():
        calls.append(1)
        time.sleep(0.6)
        return "late"

    for _ in range(2):
        with pytest.raises(ExternalTimeoutError):
            asyncio.run(run_blocking(slow, name="unit.breaker"))

    started = time.monotonic()
    with pytest.raises(ExternalCircuitOpenError):
        asyncio.run(run_blocking(slow, name="unit.breaker"))
    elapsed = time.monotonic() - started
    assert elapsed < 0.2
    assert len(calls) == 2, "熔断期间不应再次提交到外部线程池"
    stats = external_io_stats()["unit.breaker"]
    assert stats["timeouts"] == 2
    assert stats["breaker_trips"] == 1
    assert stats["circuit_open"] == 1
