"""Kronos-base 三日预测服务 — 惰性加载、缓存、可配置采样、隔离推理。

性能设计（issue #17）：

* TTL + LRU 结果缓存，key 为
  ``symbol + last_trade_date + lookback + horizon + sample_count + model``。
  命中直接返回并标记 ``cached: true``，不获取模型/推理锁。
* ``sample_count`` 支持 ``KRONOS_SAMPLE_COUNT`` 覆盖；CPU 默认 50，
  MPS/CUDA 默认 100，并在状态接口暴露。
* 模型加载与推理都在单 worker 的专用 ``ThreadPoolExecutor`` 上执行，
  不与 asyncio 默认线程池（网络/行情 I/O）争抢线程；推理天然串行。
* 模型加载由 ``asyncio.Lock`` 保护且只执行一次；相同 key 的并发请求
  通过 single-flight 共享同一次推理。
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Hashable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

import pandas as pd

log = logging.getLogger(__name__)

_MODEL_ID = "NeoQuasar/Kronos-base"
_TOKENIZER_ID = "NeoQuasar/Kronos-Tokenizer-base"
_MAX_CONTEXT = 512

_DEFAULT_CACHE_TTL_SECONDS = 900.0
_DEFAULT_CACHE_MAX_ENTRIES = 128
_CPU_SAMPLE_COUNT = 50
_ACCELERATOR_SAMPLE_COUNT = 100
_ACCELERATOR_DEVICES = frozenset({"cuda", "mps"})
_SAMPLE_COUNT_MIN = 1
_SAMPLE_COUNT_MAX = 500


class _TTLCache:
    """Thread-safe TTL + LRU cache used for prediction results."""

    def __init__(
        self,
        max_entries: int,
        ttl_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_entries = max(1, int(max_entries))
        self._ttl_seconds = max(0.0, float(ttl_seconds))
        self._clock = clock
        self._data: OrderedDict[Hashable, tuple[float, dict[str, Any]]] = OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def get(self, key: Hashable) -> dict[str, Any] | None:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                self._misses += 1
                return None
            expires_at, value = entry
            if self._clock() >= expires_at:
                del self._data[key]
                self._misses += 1
                return None
            self._data.move_to_end(key)
            self._hits += 1
            return value

    def set(self, key: Hashable, value: dict[str, Any]) -> None:
        with self._lock:
            expires_at = self._clock() + self._ttl_seconds
            self._data[key] = (expires_at, value)
            self._data.move_to_end(key)
            while len(self._data) > self._max_entries:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            now = self._clock()
            expired = [
                key for key, (expires_at, _) in self._data.items() if now >= expires_at
            ]
            for key in expired:
                del self._data[key]
            total = self._hits + self._misses
            return {
                "entries": len(self._data),
                "capacity": self._max_entries,
                "ttl_seconds": self._ttl_seconds,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(self._hits / total, 4) if total else 0.0,
            }


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        log.warning("[kronos] invalid %s=%r; using default %.1f", name, raw, default)
        return default
    if value < minimum:
        log.warning(
            "[kronos] %s=%s below minimum; using default %.1f", name, raw, default
        )
        return default
    return value


def _env_int(name: str, default: int, *, minimum: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        log.warning("[kronos] invalid %s=%r; using default %d", name, raw, default)
        return default
    if value < minimum:
        log.warning(
            "[kronos] %s=%s below minimum; using default %d", name, raw, default
        )
        return default
    return value


def _clamp_sample_count(value: int) -> int:
    return max(_SAMPLE_COUNT_MIN, min(int(value), _SAMPLE_COUNT_MAX))


class KronosPredictService:
    def __init__(
        self,
        kline_store: Any,
        provider: Any,
        *,
        cache_ttl_seconds: float | None = None,
        cache_max_entries: int | None = None,
        sample_count: int | None = None,
    ) -> None:
        self._kline_store = kline_store
        self._provider = provider
        self._predictor = None
        self._device: str = "cpu"
        self._loading = False

        # Serialises model loading only. Inference is serialised by the
        # dedicated single-worker executor, so cache hits never wait on it.
        self._model_lock = asyncio.Lock()
        self._inflight_lock = asyncio.Lock()
        self._inflight: dict[tuple[Any, ...], asyncio.Future[dict[str, Any]]] = {}
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="kronos-infer"
        )

        ttl = (
            cache_ttl_seconds
            if cache_ttl_seconds is not None
            else _env_float("KRONOS_CACHE_TTL_SECONDS", _DEFAULT_CACHE_TTL_SECONDS)
        )
        capacity = (
            cache_max_entries
            if cache_max_entries is not None
            else _env_int(
                "KRONOS_CACHE_MAX_ENTRIES", _DEFAULT_CACHE_MAX_ENTRIES, minimum=1
            )
        )
        self._cache = _TTLCache(capacity, ttl)
        self._sample_count_override = (
            _clamp_sample_count(sample_count) if sample_count is not None else None
        )

    # ── public ──

    async def predict(self, symbol: str, lookback: int = 180, horizon: int = 3) -> dict:
        lookback = max(10, min(int(lookback), 240))
        horizon = max(1, min(int(horizon), 10))
        sample_count, _ = self._resolve_sample_count()

        history = self._get_history(symbol, lookback)
        if len(history) < lookback:
            raise ValueError(
                f"历史K线不足: 需要 {lookback} 根，实际 {len(history)} 根。"
                f"请先同步 {symbol} 的K线数据。"
            )

        key = (
            symbol,
            str(history[-1]["date"]),
            lookback,
            horizon,
            sample_count,
            _MODEL_ID,
        )
        cached = self._cache.get(key)
        if cached is not None:
            return {**cached, "cached": True}

        future, owner = await self._join_or_start_inflight(key)
        if not owner:
            return await asyncio.shield(future)

        try:
            response = await self._compute(
                symbol, history, lookback, horizon, sample_count, key
            )
        except BaseException as exc:
            if not future.done():
                future.set_exception(exc)
                # Mark retrieved so an unawaited future does not warn at GC.
                future.exception()
            raise
        else:
            if not future.done():
                future.set_result(response)
            return response
        finally:
            async with self._inflight_lock:
                if self._inflight.get(key) is future:
                    del self._inflight[key]

    def is_loaded(self) -> bool:
        return self._predictor is not None

    def status(self) -> dict:
        """模型/缓存状态（供前端徽标与 /api/predict/kronos/status 使用）。"""

        sample_count, sample_source = self._resolve_sample_count()
        cache = self._cache.stats()
        return {
            "loaded": self.is_loaded(),
            "loading": bool(self._loading),
            "device": self._device,
            "model": _MODEL_ID,
            "sample_count": sample_count,
            "sample_count_source": sample_source,
            "cache": cache,
            # 兼容/便捷的扁平字段（与 cache 内数值一致）。
            "cache_entries": cache["entries"],
            "cache_hits": cache["hits"],
            "cache_misses": cache["misses"],
            "cache_hit_rate": cache["hit_rate"],
        }

    def get_device(self) -> str:
        return self._device

    def shutdown(self) -> None:
        """Stop the dedicated inference worker (used by tests/shutdown hooks)."""

        self._executor.shutdown(wait=False, cancel_futures=False)

    # ── sample count ──

    def _resolve_sample_count(self) -> tuple[int, str]:
        if self._sample_count_override is not None:
            return self._sample_count_override, "config"

        raw = os.getenv("KRONOS_SAMPLE_COUNT", "").strip()
        if raw:
            try:
                return _clamp_sample_count(int(raw)), "env"
            except ValueError:
                log.warning(
                    "[kronos] invalid KRONOS_SAMPLE_COUNT=%r; falling back to device default",
                    raw,
                )

        if self._device in _ACCELERATOR_DEVICES:
            return _ACCELERATOR_SAMPLE_COUNT, f"device_default:{self._device}"
        return _CPU_SAMPLE_COUNT, f"device_default:{self._device}"

    # ── single-flight ──

    async def _join_or_start_inflight(
        self, key: tuple[Any, ...]
    ) -> tuple[asyncio.Future[dict[str, Any]], bool]:
        async with self._inflight_lock:
            existing = self._inflight.get(key)
            if existing is not None:
                return existing, False
            loop = asyncio.get_running_loop()
            future: asyncio.Future[dict[str, Any]] = loop.create_future()
            self._inflight[key] = future
            return future, True

    async def _compute(
        self,
        symbol: str,
        history: list[dict],
        lookback: int,
        horizon: int,
        sample_count: int,
        key: tuple[Any, ...],
    ) -> dict:
        async with self._model_lock:
            predictor = await self._ensure_model()

        future_dates = await self._get_future_trade_days(history, horizon)
        loop = asyncio.get_running_loop()
        pred_df = await loop.run_in_executor(
            self._executor,
            self._run_inference,
            predictor,
            history,
            future_dates,
            horizon,
            sample_count,
        )
        response = self._build_response(
            symbol, history, pred_df, future_dates, lookback, sample_count
        )
        response["cached"] = False
        self._cache.set(key, response)
        return response

    # ── model loading ──

    async def _ensure_model(self):
        if self._predictor is not None:
            return self._predictor

        self._loading = True
        try:
            loop = asyncio.get_running_loop()
            predictor, device = await loop.run_in_executor(
                self._executor, self._load_model
            )
            self._predictor = predictor
            self._device = device
            log.info("[kronos] model loaded on %s", device)
            return predictor
        finally:
            self._loading = False

    @staticmethod
    def _load_model():
        from app.services.kronos_model import Kronos, KronosTokenizer, KronosPredictor

        log.info("[kronos] loading tokenizer %s ...", _TOKENIZER_ID)
        tokenizer = KronosTokenizer.from_pretrained(_TOKENIZER_ID)
        log.info("[kronos] loading model %s ...", _MODEL_ID)
        model = Kronos.from_pretrained(_MODEL_ID)
        predictor = KronosPredictor(
            model, tokenizer, device=None, max_context=_MAX_CONTEXT
        )
        return predictor, predictor.device

    # ── history ──

    def _get_history(self, symbol: str, lookback: int) -> list[dict]:
        return self._kline_store.get_kline(symbol, days=lookback)

    # ── future trade days ──

    async def _get_future_trade_days(
        self, history: list[dict], horizon: int
    ) -> list[str]:
        last_date = history[-1]["date"]
        trade_days_df = await self._provider.get_trade_days()
        target = pd.to_datetime(last_date)

        if not trade_days_df.empty and "trade_date" in trade_days_df.columns:
            all_dates = pd.to_datetime(
                trade_days_df["trade_date"], errors="coerce"
            ).dropna()
            future = all_dates[all_dates > target].sort_values()
            if len(future) >= horizon:
                return [d.strftime("%Y-%m-%d") for d in future.iloc[:horizon]]

        result = []
        d = target
        while len(result) < horizon:
            d += pd.Timedelta(days=1)
            if d.weekday() < 5:
                result.append(d.strftime("%Y-%m-%d"))
        return result

    # ── inference ──

    @staticmethod
    def _run_inference(
        predictor: Any,
        history: list[dict],
        future_dates: list[str],
        horizon: int,
        sample_count: int,
    ) -> pd.DataFrame:
        df = pd.DataFrame(history)
        x_df = df[["open", "high", "low", "close", "volume", "amount"]].copy()

        hist_dates = pd.to_datetime(df["date"])
        x_timestamp = pd.Series(hist_dates).reset_index(drop=True)
        y_timestamp = pd.Series(pd.to_datetime(future_dates))

        pred_df = predictor.predict(
            df=x_df.reset_index(drop=True),
            x_timestamp=x_timestamp,
            y_timestamp=y_timestamp,
            pred_len=horizon,
            T=1.0,
            top_k=0,
            top_p=0.9,
            sample_count=sample_count,
            verbose=False,
        )
        return pred_df

    # ── response ──

    def _build_response(
        self,
        symbol: str,
        history: list[dict],
        pred_df: pd.DataFrame,
        future_dates: list[str],
        lookback: int,
        sample_count: int,
    ) -> dict:
        history_kline = []
        for row in history:
            history_kline.append(
                {
                    "date": row["date"],
                    "open": round(row["open"], 4),
                    "high": round(row["high"], 4),
                    "low": round(row["low"], 4),
                    "close": round(row["close"], 4),
                    "volume": row["volume"],
                    "amount": row["amount"],
                    "type": "history",
                }
            )

        predicted_kline = []
        for i, date_str in enumerate(future_dates):
            r = pred_df.iloc[i]
            o, h, low, c = (
                float(r["open"]),
                float(r["high"]),
                float(r["low"]),
                float(r["close"]),
            )
            v = float(r["volume"]) if "volume" in pred_df.columns else 0
            a = float(r["amount"]) if "amount" in pred_df.columns else 0
            h = max(h, o, c)
            low = min(low, o, c)
            predicted_kline.append(
                {
                    "date": date_str,
                    "open": round(o, 4),
                    "high": round(h, 4),
                    "low": round(low, 4),
                    "close": round(c, 4),
                    "volume": round(v, 2),
                    "amount": round(a, 2),
                    "type": "predicted",
                }
            )

        merged_kline = history_kline + predicted_kline

        return {
            "symbol": symbol,
            "model": "Kronos-base",
            "device": self._device,
            "lookback": len(history),
            "requested_lookback": lookback,
            "horizon": len(predicted_kline),
            "sample_count": sample_count,
            "history_kline": history_kline,
            "predicted_kline": predicted_kline,
            "merged_kline": merged_kline,
            "prediction_start_index": len(history_kline),
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        }
