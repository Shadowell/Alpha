from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any

import pandas as pd

from app.services.data_provider import AkshareDataProvider, normalize_symbol
from app.services.kline_store import KlineSQLiteStore
from app.services.kronos_predict_service import KronosPredictService
from app.services.sqlite_store import SQLiteStateStore
from app.services.time_utils import now_cn
from app.services.tradingagents_adapter import (
    DEFAULT_DEEP_MODEL,
    DEFAULT_QUICK_MODEL,
    DEEPSEEK_PROVIDER,
    TradingAgentsAdapter,
)

log = logging.getLogger(__name__)

POOL_CANDIDATE = "candidate"
POOL_FOCUS = "focus"
POOL_BUY = "buy"
STATE_KEY = "hot_stock_ai"
DISCUSSION_STATE_KEY = "hot_stock_ai_tradingagents_discussions"

DEFAULT_CONFIG: dict[str, Any] = {
    "top_n": 20,
    "lookback": 90,
    "horizon": 3,
    "threshold_candidate": 8.0,
    "threshold_focus": 11.5,
    "threshold_buy": 14.5,
    "max_buy_pool_size": 5,
    "auto_refresh_enabled": True,
    "refresh_interval_minutes": 5,
    "use_kronos": True,
    "tradingagents_enabled": True,
    "tradingagents_top_n": 20,
    "tradingagents_timeout_seconds": 240,
    "tradingagents_provider": DEEPSEEK_PROVIDER,
    "tradingagents_quick_model": DEFAULT_QUICK_MODEL,
    "tradingagents_deep_model": DEFAULT_DEEP_MODEL,
}

AUTO_LIGHT_TOP_N = 12


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _safe_pct(numerator: float, denominator: float) -> float:
    if abs(denominator) < 1e-8:
        return 0.0
    return (numerator / denominator) * 100.0


class HotStockAIService:
    def __init__(
        self,
        provider: AkshareDataProvider,
        kline_store: KlineSQLiteStore,
        kronos_service: KronosPredictService,
        state_store: SQLiteStateStore,
        tradingagents_adapter: TradingAgentsAdapter | None = None,
    ) -> None:
        self.provider = provider
        self.kline_store = kline_store
        self.kronos = kronos_service
        self.state_store = state_store
        self.tradingagents = tradingagents_adapter
        self.lock = asyncio.Lock()
        self.running = False
        self.progress: dict[str, Any] = {
            "phase": "idle",
            "current": 0,
            "total": 0,
            "detail": "",
            "started_at": None,
            "finished_at": None,
        }
        self._snapshot: dict[str, Any] = self._load_state() or {
            "trade_date": "",
            "updated_at": "",
            "config": dict(DEFAULT_CONFIG),
            "pools": {POOL_CANDIDATE: [], POOL_FOCUS: [], POOL_BUY: []},
            "entries": [],
            "meta": {},
        }

    def _load_state(self) -> dict[str, Any] | None:
        try:
            raw = self.state_store.get_kv(STATE_KEY)
            if raw:
                return raw
        except Exception as exc:
            log.warning("[hot_stock_ai] load state failed: %s", exc)
        return None

    def _save_state(self) -> None:
        try:
            self.state_store.set_kv(STATE_KEY, self._snapshot)
        except Exception as exc:
            log.warning("[hot_stock_ai] save state failed: %s", exc)

    def get_config(self) -> dict[str, Any]:
        cfg = dict(DEFAULT_CONFIG)
        cfg.update(self._snapshot.get("config", {}))
        return cfg

    def update_config(self, patch: dict[str, Any]) -> dict[str, Any]:
        cfg = self.get_config()
        for key, value in (patch or {}).items():
            if key not in DEFAULT_CONFIG:
                continue
            cfg[key] = value
        cfg["top_n"] = max(5, min(int(cfg["top_n"]), 30))
        cfg["lookback"] = max(30, min(int(cfg["lookback"]), 240))
        cfg["horizon"] = max(1, min(int(cfg["horizon"]), 5))
        cfg["threshold_candidate"] = float(cfg["threshold_candidate"])
        cfg["threshold_focus"] = max(cfg["threshold_candidate"], float(cfg["threshold_focus"]))
        cfg["threshold_buy"] = max(cfg["threshold_focus"], float(cfg["threshold_buy"]))
        cfg["max_buy_pool_size"] = max(1, min(int(cfg["max_buy_pool_size"]), 10))
        cfg["refresh_interval_minutes"] = max(1, min(int(cfg["refresh_interval_minutes"]), 60))
        cfg["auto_refresh_enabled"] = bool(cfg["auto_refresh_enabled"])
        cfg["use_kronos"] = bool(cfg["use_kronos"])
        cfg["tradingagents_enabled"] = bool(cfg["tradingagents_enabled"])
        cfg["tradingagents_top_n"] = max(0, min(int(cfg["tradingagents_top_n"]), 20))
        cfg["tradingagents_timeout_seconds"] = max(30, min(int(cfg["tradingagents_timeout_seconds"]), 900))
        cfg["tradingagents_provider"] = DEEPSEEK_PROVIDER
        cfg["tradingagents_quick_model"] = str(cfg.get("tradingagents_quick_model") or DEFAULT_QUICK_MODEL).strip() or DEFAULT_QUICK_MODEL
        cfg["tradingagents_deep_model"] = str(cfg.get("tradingagents_deep_model") or DEFAULT_DEEP_MODEL).strip() or DEFAULT_DEEP_MODEL
        self._snapshot["config"] = cfg
        self._save_state()
        return cfg

    def get_snapshot(self) -> dict[str, Any]:
        payload = dict(self._snapshot)
        payload["progress"] = dict(self.progress)
        payload["running"] = self.running
        return payload

    def is_stale(self) -> bool:
        cfg = self.get_config()
        updated_at = str(self._snapshot.get("updated_at") or "")
        if not updated_at:
            return True
        try:
            ts = datetime.fromisoformat(updated_at)
        except Exception:
            return True
        age = (now_cn() - ts).total_seconds()
        return age >= int(cfg["refresh_interval_minutes"]) * 60

    async def run(self, trigger: str = "manual") -> dict[str, Any]:
        if self.running:
            return {"ok": False, "error": "已有热门股票智能分析任务在执行", "snapshot": self.get_snapshot()}
        async with self.lock:
            self.running = True
            self.progress = {
                "phase": "init",
                "current": 0,
                "total": 0,
                "detail": "准备热门前 20 个股",
                "started_at": now_cn().isoformat(),
                "finished_at": None,
            }
            try:
                self._snapshot = await self._execute(trigger)
                self._save_state()
            finally:
                self.progress["finished_at"] = now_cn().isoformat()
                self.progress["phase"] = "done" if not self.progress.get("error") else "error"
                self.running = False
        return {"ok": True, "snapshot": self.get_snapshot()}

    async def _execute(self, trigger: str) -> dict[str, Any]:
        cfg = self.get_config()
        runtime_cfg = self._resolve_runtime_config(cfg, trigger=trigger)
        t0 = time.time()
        self.progress.update(phase="fetch", detail=f"拉取热门股票 Top{runtime_cfg['top_n']}")
        hot_df = await self.provider.get_hot_stocks(top_n=int(runtime_cfg["top_n"]), cache_ttl_seconds=300)
        if hot_df is None or hot_df.empty:
            self.progress["error"] = "热门股票接口返回为空"
            return {
                "trade_date": now_cn().date().isoformat(),
                "updated_at": now_cn().isoformat(),
                "config": cfg,
                "pools": {POOL_CANDIDATE: [], POOL_FOCUS: [], POOL_BUY: []},
                "entries": [],
                "meta": {"error": "hot stocks unavailable", "trigger": trigger},
            }

        hot_df = hot_df.head(int(runtime_cfg["top_n"])).copy()
        total = len(hot_df)
        self.progress.update(phase="analyze", current=0, total=total, detail=f"开始逐股分析 {total} 只")

        entries: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        for idx, (_, row) in enumerate(hot_df.iterrows(), start=1):
            symbol = normalize_symbol(row.get("symbol", ""))
            name = str(row.get("name", "") or symbol)
            self.progress.update(current=idx, detail=f"{idx}/{total} {name}")
            try:
                analyzed = await self._analyze_symbol(row.to_dict(), runtime_cfg)
                if analyzed is None:
                    failed.append({"symbol": symbol, "name": name, "reason": "insufficient_history"})
                    continue
                entries.append(analyzed)
            except Exception as exc:
                failed.append({"symbol": symbol, "name": name, "reason": str(exc)})
                log.info("[hot_stock_ai] analyze fail %s: %s", symbol, exc)

        entries.sort(key=lambda item: item.get("score", 0.0), reverse=True)
        discussion_meta = await self._attach_tradingagents_discussions(entries, runtime_cfg, trigger=trigger)
        entries.sort(key=lambda item: item.get("score", 0.0), reverse=True)
        pools = self._build_pools(entries, cfg)
        trade_date = entries[0]["trade_date"] if entries else now_cn().date().isoformat()
        avg_score = round(sum(float(item["score"]) for item in entries) / max(len(entries), 1), 2) if entries else 0.0
        ta_runtime = (
            self.tradingagents.describe_runtime()
            if self.tradingagents is not None and hasattr(self.tradingagents, "describe_runtime")
            else {}
        )
        self.progress.update(phase="done", current=total, total=total, detail=f"完成分析 {len(entries)} 只")
        return {
            "trade_date": trade_date,
            "updated_at": now_cn().isoformat(),
            "config": cfg,
            "pools": pools,
            "entries": entries,
            "meta": {
                "trigger": trigger,
                "entries_count": len(entries),
                "stocks_scanned": total,
                "failed_count": len(failed),
                "failed_symbols": failed[:8],
                "elapsed_sec": round(time.time() - t0, 2),
                "avg_score": avg_score,
                "kronos_enabled": bool(cfg["use_kronos"]),
                "runtime_kronos_enabled": bool(runtime_cfg["use_kronos"]),
                "kronos_loaded": self.kronos.is_loaded(),
                "kronos_device": self.kronos.get_device(),
                "tradingagents_enabled": bool(cfg["tradingagents_enabled"]),
                "runtime_tradingagents_enabled": bool(runtime_cfg["tradingagents_enabled"]),
                "tradingagents_discussed": discussion_meta.get("discussed_count", 0),
                "tradingagents_cache_hits": discussion_meta.get("cache_hits", 0),
                "tradingagents_failures": discussion_meta.get("failed", 0),
                "tradingagents_backend": "TradingAgents + DeepSeek" if self.tradingagents is not None else "disabled",
                "tradingagents_repo_path": ta_runtime.get("repo_path", ""),
                "tradingagents_provider": ta_runtime.get("provider", DEEPSEEK_PROVIDER),
                "tradingagents_backend_url": ta_runtime.get("backend_url", ""),
                "execution_mode": "light_auto" if trigger == "auto" else "full_manual",
                "runtime_top_n": int(runtime_cfg["top_n"]),
                "thresholds": {
                    "candidate": cfg["threshold_candidate"],
                    "focus": cfg["threshold_focus"],
                    "buy": cfg["threshold_buy"],
                },
            },
        }

    @staticmethod
    def _resolve_runtime_config(cfg: dict[str, Any], *, trigger: str) -> dict[str, Any]:
        runtime_cfg = dict(cfg)
        if trigger == "auto":
            runtime_cfg["top_n"] = min(int(runtime_cfg.get("top_n", 20)), AUTO_LIGHT_TOP_N)
            runtime_cfg["use_kronos"] = False
            runtime_cfg["tradingagents_enabled"] = False
        return runtime_cfg

    def _load_discussion_cache(self) -> dict[str, Any]:
        try:
            payload = self.state_store.get_kv(DISCUSSION_STATE_KEY)
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}

    def _save_discussion_cache(self, payload: dict[str, Any]) -> None:
        try:
            self.state_store.set_kv(DISCUSSION_STATE_KEY, payload)
        except Exception:
            pass

    async def _attach_tradingagents_discussions(
        self,
        entries: list[dict[str, Any]],
        cfg: dict[str, Any],
        *,
        trigger: str,
    ) -> dict[str, int]:
        if not entries:
            return {"discussed_count": 0, "cache_hits": 0, "failed": 0}
        if not cfg.get("tradingagents_enabled") or self.tradingagents is None:
            for item in entries:
                item["tradingagents"] = {"status": "disabled"}
            return {"discussed_count": 0, "cache_hits": 0, "failed": 0}

        cache = self._load_discussion_cache()
        trade_date = str(entries[0].get("trade_date") or now_cn().date().isoformat())
        discussed_count = 0
        cache_hits = 0
        failed = 0
        top_n = int(cfg.get("tradingagents_top_n", 0))
        selected_symbols = {item["symbol"] for item in entries[:top_n]}

        for item in entries:
            cache_key = (
                f"{trade_date}:{item['symbol']}:"
                f"{cfg['tradingagents_provider']}:{cfg['tradingagents_quick_model']}:{cfg['tradingagents_deep_model']}"
            )
            cached = cache.get(cache_key)
            if cached:
                cache_hits += 1
                self._apply_tradingagents_result(item, cached, source="cache")
                continue
            if item["symbol"] not in selected_symbols:
                item["tradingagents"] = {"status": "skipped", "reason": "not in discussion top n"}
                continue

            try:
                result = await asyncio.wait_for(
                    asyncio.to_thread(
                        self.tradingagents.analyze,
                        item["symbol"],
                        trade_date,
                        provider=str(cfg["tradingagents_provider"]),
                        quick_model=str(cfg["tradingagents_quick_model"]),
                        deep_model=str(cfg["tradingagents_deep_model"]),
                        selected_analysts=["market", "news", "fundamentals"],
                        output_language="Chinese",
                    ),
                    timeout=float(cfg["tradingagents_timeout_seconds"]),
                )
                discussed_count += 1
                cache[cache_key] = result
                self._apply_tradingagents_result(item, result, source="fresh")
            except Exception as exc:
                failed += 1
                item["tradingagents"] = {"status": "failed", "error": str(exc)}

        self._save_discussion_cache(cache)
        return {"discussed_count": discussed_count, "cache_hits": cache_hits, "failed": failed}

    @staticmethod
    def _apply_tradingagents_result(item: dict[str, Any], result: dict[str, Any], *, source: str) -> None:
        ta_payload = dict(result)
        ta_payload["status"] = "ok"
        ta_payload["source"] = source
        item["tradingagents"] = ta_payload
        item["base_score"] = round(float(item.get("score", 0.0)), 2)
        bonus = float(result.get("score_bonus", 0.0))
        item["tradingagents_bonus"] = round(bonus, 2)
        item["score"] = round(_clamp(float(item["base_score"]) + bonus, 0.0, 20.0), 2)

    async def _analyze_symbol(self, row: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any] | None:
        symbol = normalize_symbol(row.get("symbol", ""))
        if not symbol:
            return None
        name = str(row.get("name", "") or symbol)
        history_rows = self.kline_store.get_kline(symbol, days=max(int(cfg["lookback"]) + 20, 80))
        if len(history_rows) < 35:
            return None
        hist = pd.DataFrame(history_rows)
        for col in ["open", "high", "low", "close", "volume", "amount"]:
            hist[col] = pd.to_numeric(hist[col], errors="coerce").fillna(0.0)

        latest_price = float(row.get("latest_price") or hist["close"].iloc[-1])
        change_pct = float(row.get("change_pct") or 0.0)
        rank = int(float(row.get("rank") or 0))
        current_close = float(hist["close"].iloc[-1])
        ma5 = float(hist["close"].tail(5).mean())
        ma10 = float(hist["close"].tail(10).mean())
        ma20 = float(hist["close"].tail(20).mean())
        high20 = float(hist["high"].tail(20).max())
        low20 = float(hist["low"].tail(20).min())
        avg_amount20 = float(hist["amount"].tail(20).mean())
        today_amount = float(hist["amount"].iloc[-1])
        amount_ratio20 = today_amount / avg_amount20 if avg_amount20 > 0 else 0.0
        ret5 = _safe_pct(latest_price - float(hist["close"].iloc[-6]), float(hist["close"].iloc[-6])) if len(hist) > 6 else 0.0
        dist_ma20 = _safe_pct(latest_price - ma20, ma20)
        dist_high20 = _safe_pct(latest_price - high20, high20)
        pullback_from_high20 = _safe_pct(high20 - latest_price, high20)
        swing20 = _safe_pct(high20 - low20, low20)

        pred_max_high_pct = 0.0
        pred_last_close_pct = 0.0
        pred_avg_close_pct = 0.0
        predicted = []
        if cfg.get("use_kronos", True):
            pred = await self.kronos.predict(symbol, lookback=int(cfg["lookback"]), horizon=int(cfg["horizon"]))
            predicted = pred.get("predicted_kline") or []
            if predicted:
                highs = [float(item.get("high", 0.0)) for item in predicted]
                closes = [float(item.get("close", 0.0)) for item in predicted]
                pred_max_high_pct = _safe_pct(max(highs) - current_close, current_close)
                pred_last_close_pct = _safe_pct(closes[-1] - current_close, current_close)
                pred_avg_close_pct = _safe_pct((sum(closes) / len(closes)) - current_close, current_close)

        popularity_score = _clamp(4.8 - max(rank - 1, 0) * 0.19, 0.6, 4.8)
        momentum_score = _clamp(max(change_pct, 0.0) * 0.22, 0.0, 3.4)
        trend_score = 0.0
        trend_score += 0.8 if latest_price >= ma5 else 0.0
        trend_score += 0.9 if latest_price >= ma10 else 0.0
        trend_score += 1.1 if latest_price >= ma20 else 0.0
        trend_score += 0.7 if dist_high20 >= -3.0 else 0.0
        trend_score += _clamp(ret5 * 0.08, -0.4, 0.8)

        liquidity_score = _clamp((amount_ratio20 - 0.8) * 1.35, 0.0, 2.2)
        if avg_amount20 >= 150_000_000:
            liquidity_score += 0.5

        prediction_score = 0.0
        prediction_score += _clamp(pred_max_high_pct * 0.33, -0.6, 2.4)
        prediction_score += _clamp(pred_last_close_pct * 0.28, -0.8, 1.6)
        prediction_score += _clamp(pred_avg_close_pct * 0.24, -0.7, 1.4)

        risk_penalty = 0.0
        if change_pct >= 9.8:
            risk_penalty += min((change_pct - 9.8) * 0.35, 1.4)
        if dist_ma20 >= 12.0:
            risk_penalty += min((dist_ma20 - 12.0) * 0.18, 1.5)
        if pred_last_close_pct < 0:
            risk_penalty += min(abs(pred_last_close_pct) * 0.25, 1.8)
        if latest_price < ma10:
            risk_penalty += 0.8
        if amount_ratio20 < 0.85:
            risk_penalty += 0.6

        raw_score = popularity_score + momentum_score + trend_score + liquidity_score + prediction_score - risk_penalty
        score = round(_clamp(raw_score, 0.0, 20.0), 2)

        tags: list[str] = []
        if rank <= 3:
            tags.append("热度龙头")
        elif rank <= 8:
            tags.append("热度靠前")
        if latest_price >= ma20 and dist_high20 >= -2.5:
            tags.append("强趋势")
        if amount_ratio20 >= 1.3:
            tags.append("量能放大")
        if pred_max_high_pct >= 4.0:
            tags.append("Kronos看多")
        if pred_last_close_pct < 0:
            tags.append("预测回撤")
        if dist_ma20 >= 10.0:
            tags.append("高位偏离")

        analysis_parts = [
            f"热度排名第{max(rank, 0)}",
            f"当日涨幅{change_pct:+.2f}%",
            f"距20日高{dist_high20:+.2f}%",
            f"量额比20日均值{amount_ratio20:.2f}倍",
        ]
        if cfg.get("use_kronos", True) and predicted:
            analysis_parts.append(f"Kronos 3日高点{pred_max_high_pct:+.2f}%")
            analysis_parts.append(f"末日收盘{pred_last_close_pct:+.2f}%")

        return {
            "symbol": symbol,
            "name": name,
            "trade_date": str(hist["date"].iloc[-1]),
            "rank": rank,
            "score": score,
            "latest_price": round(latest_price, 3),
            "change_pct": round(change_pct, 2),
            "amount_ratio_20d": round(amount_ratio20, 2),
            "avg_amount_20d": round(avg_amount20, 2),
            "dist_ma20_pct": round(dist_ma20, 2),
            "dist_high20_pct": round(dist_high20, 2),
            "pullback_from_high20_pct": round(pullback_from_high20, 2),
            "ret5_pct": round(ret5, 2),
            "swing20_pct": round(swing20, 2),
            "pred_max_high_pct": round(pred_max_high_pct, 2),
            "pred_last_close_pct": round(pred_last_close_pct, 2),
            "pred_avg_close_pct": round(pred_avg_close_pct, 2),
            "predicted_kline": predicted,
            "base_score": score,
            "tradingagents_bonus": 0.0,
            "tradingagents": {"status": "pending"},
            "score_breakdown": {
                "popularity": round(popularity_score, 2),
                "momentum": round(momentum_score, 2),
                "trend": round(trend_score, 2),
                "liquidity": round(liquidity_score, 2),
                "prediction": round(prediction_score, 2),
                "risk_penalty": round(risk_penalty, 2),
            },
            "tags": tags,
            "analysis": " · ".join(analysis_parts),
        }

    @staticmethod
    def _build_pools(entries: list[dict[str, Any]], cfg: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
        pools = {POOL_CANDIDATE: [], POOL_FOCUS: [], POOL_BUY: []}
        max_buy = int(cfg["max_buy_pool_size"])
        for item in entries:
            score = float(item.get("score", 0.0))
            if score >= float(cfg["threshold_buy"]):
                if len(pools[POOL_BUY]) < max_buy:
                    pools[POOL_BUY].append(item)
                else:
                    pools[POOL_FOCUS].append(item)
            elif score >= float(cfg["threshold_focus"]):
                pools[POOL_FOCUS].append(item)
            elif score >= float(cfg["threshold_candidate"]):
                pools[POOL_CANDIDATE].append(item)
        return pools
