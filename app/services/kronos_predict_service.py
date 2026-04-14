"""Kronos-base 三日预测服务 — 惰性加载、串行推理、交易日推算。"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

_MODEL_ID = "NeoQuasar/Kronos-base"
_TOKENIZER_ID = "NeoQuasar/Kronos-Tokenizer-base"
_MAX_CONTEXT = 512


class KronosPredictService:

    def __init__(self, kline_store, provider):
        self._kline_store = kline_store
        self._provider = provider
        self._predictor = None
        self._device: str = "cpu"
        self._lock = asyncio.Lock()
        self._loading = False

    # ── public ──

    async def predict(self, symbol: str, lookback: int = 30, horizon: int = 3) -> dict:
        lookback = max(10, min(lookback, 200))
        horizon = max(1, min(horizon, 10))

        async with self._lock:
            predictor = await self._ensure_model()
            history = self._get_history(symbol, lookback)
            if len(history) < lookback:
                raise ValueError(
                    f"历史K线不足: 需要 {lookback} 根，实际 {len(history)} 根。"
                    f"请先同步 {symbol} 的K线数据。"
                )
            future_dates = await self._get_future_trade_days(history, horizon)
            pred_df = await asyncio.to_thread(
                self._run_inference, predictor, history, future_dates, horizon
            )
            return self._build_response(symbol, history, pred_df, future_dates)

    def is_loaded(self) -> bool:
        return self._predictor is not None

    def get_device(self) -> str:
        return self._device

    # ── model loading ──

    async def _ensure_model(self):
        if self._predictor is not None:
            return self._predictor

        self._loading = True
        try:
            predictor, device = await asyncio.to_thread(self._load_model)
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
        predictor = KronosPredictor(model, tokenizer, device=None, max_context=_MAX_CONTEXT)
        return predictor, predictor.device

    # ── history ──

    def _get_history(self, symbol: str, lookback: int) -> list[dict]:
        return self._kline_store.get_kline(symbol, days=lookback)

    # ── future trade days ──

    async def _get_future_trade_days(self, history: list[dict], horizon: int) -> list[str]:
        last_date = history[-1]["date"]
        trade_days_df = await self._provider.get_trade_days()
        if trade_days_df.empty or "trade_date" not in trade_days_df.columns:
            raise RuntimeError("交易日历不可用，无法推算未来交易日。")

        all_dates = pd.to_datetime(trade_days_df["trade_date"], errors="coerce").dropna()
        target = pd.to_datetime(last_date)
        future = all_dates[all_dates > target].sort_values()

        if len(future) < horizon:
            raise RuntimeError(
                f"交易日历中找不到 {last_date} 之后的 {horizon} 个交易日。"
                "日历可能尚未更新。"
            )
        return [d.strftime("%Y-%m-%d") for d in future.iloc[:horizon]]

    # ── inference ──

    @staticmethod
    def _run_inference(predictor, history: list[dict], future_dates: list[str], horizon: int) -> pd.DataFrame:
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
            sample_count=1,
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
    ) -> dict:
        history_kline = []
        for row in history:
            history_kline.append({
                "date": row["date"],
                "open": round(row["open"], 4),
                "high": round(row["high"], 4),
                "low": round(row["low"], 4),
                "close": round(row["close"], 4),
                "volume": row["volume"],
                "amount": row["amount"],
                "type": "history",
            })

        predicted_kline = []
        for i, date_str in enumerate(future_dates):
            r = pred_df.iloc[i]
            o, h, l, c = float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])
            h = max(h, o, c)
            l = min(l, o, c)
            predicted_kline.append({
                "date": date_str,
                "open": round(o, 4),
                "high": round(h, 4),
                "low": round(l, 4),
                "close": round(c, 4),
                "volume": 0,
                "amount": 0,
                "type": "predicted",
            })

        merged_kline = history_kline + predicted_kline

        return {
            "symbol": symbol,
            "model": "Kronos-base",
            "device": self._device,
            "lookback": len(history),
            "horizon": len(predicted_kline),
            "history_kline": history_kline,
            "predicted_kline": predicted_kline,
            "merged_kline": merged_kline,
            "prediction_start_index": len(history_kline),
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        }
