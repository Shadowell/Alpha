from __future__ import annotations

import asyncio
import threading
import time

import pandas as pd

from app.services.kronos_predict_service import KronosPredictService, _TTLCache

SAMPLE_HISTORY = [
    {
        "date": f"2026-01-{day:02d}",
        "open": 10.0 + day / 100,
        "high": 10.5 + day / 100,
        "low": 9.8 + day / 100,
        "close": 10.2 + day / 100,
        "volume": 1000 + day,
        "amount": 10000 + day,
    }
    for day in range(1, 31)
]


class FakeKlineStore:
    def get_kline(self, symbol: str, days: int = 30):
        rows = [
            {**row, "symbol": symbol, "close": row["close"] + (hash(symbol) % 7) / 100}
            for row in SAMPLE_HISTORY
        ]
        return rows[-days:]


class FakeProvider:
    async def get_trade_days(self):
        return pd.DataFrame()


class FakePredictor:
    def __init__(self, delay: float = 0.0) -> None:
        self.calls: list[dict] = []
        self.delay = delay
        self._lock = threading.Lock()

    def predict(self, **kwargs):
        with self._lock:
            self.calls.append(kwargs)
        if self.delay:
            time.sleep(self.delay)
        pred_len = int(kwargs["pred_len"])
        return pd.DataFrame(
            {
                "open": [10.0 + i * 0.1 for i in range(pred_len)],
                "high": [10.5 + i * 0.1 for i in range(pred_len)],
                "low": [9.5 + i * 0.1 for i in range(pred_len)],
                "close": [10.2 + i * 0.1 for i in range(pred_len)],
                "volume": [1000.0 + i for i in range(pred_len)],
                "amount": [10000.0 + i for i in range(pred_len)],
            }
        )


def _service(**kwargs) -> tuple[KronosPredictService, FakePredictor]:
    predictor = FakePredictor()
    service = KronosPredictService(FakeKlineStore(), FakeProvider(), **kwargs)
    service._predictor = predictor
    service._device = "cpu"
    return service, predictor


def _run(coro):
    return asyncio.run(coro)


def test_same_input_second_call_is_served_from_cache():
    service, predictor = _service()

    async def scenario():
        first = await service.predict("000001", lookback=10, horizon=3)
        second = await service.predict("000001", lookback=10, horizon=3)
        return first, second

    first, second = _run(scenario())

    assert len(predictor.calls) == 1
    assert first["cached"] is False
    assert second["cached"] is True
    assert second["sample_count"] == 50
    assert predictor.calls[0]["sample_count"] == 50
    status = service.status()
    assert status["cache_hits"] == 1
    assert status["cache_misses"] == 1
    assert status["cache_entries"] == 1
    assert status["cache_hit_rate"] == 0.5


def test_sample_count_is_configurable_and_part_of_cache_key(monkeypatch):
    service, predictor = _service()
    monkeypatch.setenv("KRONOS_SAMPLE_COUNT", "20")
    first = _run(service.predict("000001", lookback=10, horizon=3))

    monkeypatch.setenv("KRONOS_SAMPLE_COUNT", "50")
    second = _run(service.predict("000001", lookback=10, horizon=3))

    assert first["cached"] is False
    assert second["cached"] is False
    assert [call["sample_count"] for call in predictor.calls] == [20, 50]
    assert service.status()["sample_count"] == 50


def test_invalid_sample_count_env_falls_back_to_device_default(monkeypatch):
    service, _ = _service()
    monkeypatch.setenv("KRONOS_SAMPLE_COUNT", "not-a-number")

    response = _run(service.predict("000001", lookback=10, horizon=2))

    assert response["sample_count"] == 50
    status = service.status()
    assert status["sample_count"] == 50
    assert status["sample_count_source"] == "device_default:cpu"


def test_accelerator_devices_default_to_100_samples(monkeypatch):
    monkeypatch.delenv("KRONOS_SAMPLE_COUNT", raising=False)

    for device in ("cpu", "cuda", "mps"):
        service, _ = _service()
        service._device = device
        expected = 100 if device in {"cuda", "mps"} else 50
        assert service.status()["sample_count"] == expected


def test_ttl_expiry_triggers_new_inference():
    service, predictor = _service(cache_ttl_seconds=0.0)

    _run(service.predict("000001", lookback=10, horizon=3))
    second = _run(service.predict("000001", lookback=10, horizon=3))

    assert len(predictor.calls) == 2
    assert second["cached"] is False
    # The entry is expired as soon as TTL=0, so status prunes it.
    assert service.status()["cache_entries"] == 0


def test_lru_capacity_evicts_oldest_entry():
    service, predictor = _service(cache_max_entries=1)

    async def scenario():
        await service.predict("000001", lookback=10, horizon=3)
        await service.predict("000002", lookback=10, horizon=3)
        third = await service.predict("000001", lookback=10, horizon=3)
        return third

    third = _run(scenario())

    assert len(predictor.calls) == 3
    assert third["cached"] is False
    status = service.status()
    assert status["cache_entries"] == 1
    assert status["cache"]["capacity"] == 1


def test_cache_environment_config_is_exposed_in_status(monkeypatch):
    monkeypatch.setenv("KRONOS_CACHE_TTL_SECONDS", "12.5")
    monkeypatch.setenv("KRONOS_CACHE_MAX_ENTRIES", "9")
    service, _ = _service()

    cache = service.status()["cache"]
    assert cache["ttl_seconds"] == 12.5
    assert cache["capacity"] == 9


def test_concurrent_requests_for_same_key_share_one_inference():
    predictor = FakePredictor(delay=0.05)
    service = KronosPredictService(FakeKlineStore(), FakeProvider())
    service._predictor = predictor
    service._device = "cpu"

    async def scenario():
        return await asyncio.gather(
            *[service.predict("000001", lookback=10, horizon=3) for _ in range(4)]
        )

    responses = _run(scenario())

    assert len(predictor.calls) == 1
    assert all(response["symbol"] == "000001" for response in responses)
    # Every caller performs its own (missed) cache lookup before single-flight.
    assert service.status()["cache_misses"] == 4


def test_different_symbols_are_both_computed():
    predictor = FakePredictor(delay=0.01)
    service = KronosPredictService(FakeKlineStore(), FakeProvider())
    service._predictor = predictor
    service._device = "cpu"

    async def scenario():
        return await asyncio.gather(
            service.predict("000001", lookback=10, horizon=3),
            service.predict("000002", lookback=10, horizon=3),
        )

    responses = _run(scenario())

    assert len(predictor.calls) == 2
    assert {response["symbol"] for response in responses} == {"000001", "000002"}
    assert service.status()["cache_entries"] == 2


def test_ttl_cache_lru_and_stats_with_fake_clock():
    now = [0.0]
    cache = _TTLCache(2, 10.0, clock=lambda: now[0])

    cache.set("a", {"v": 1})
    cache.set("b", {"v": 2})
    assert cache.get("a") == {"v": 1}
    cache.set("c", {"v": 3})

    assert cache.get("b") is None  # least recently used evicted
    assert cache.get("a") == {"v": 1}
    assert cache.get("c") == {"v": 3}

    now[0] = 20.0
    assert cache.get("a") is None  # expired
    stats = cache.stats()
    assert stats["hits"] == 3
    assert stats["misses"] == 2
    assert stats["hit_rate"] == 0.6
