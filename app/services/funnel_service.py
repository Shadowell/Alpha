from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pandas as pd

from app.config import POOL_BUY, POOL_CANDIDATE, POOL_FOCUS, StrategyConfig, VALID_POOLS
from app.models import (
    FunnelResponse,
    HotConceptResponse,
    HotStocksResponse,
    MovePoolResponse,
    StockCard,
    StockDetailResponse,
)
from app.services.concept_engine import (
    build_concept_heat,
    build_hot_concepts_payload,
    build_top_tags,
    map_stock_concepts,
)
from app.services.data_provider import AkshareDataProvider
from app.services.strategy_engine import (
    analyze_adjustment_candidate,
    apply_transition_rules,
    compute_intraday_score,
    get_last_n_trade_window,
    prefilter_universe,
)
from app.services.sqlite_store import SQLiteStateStore
from app.services.time_utils import elapsed_market_ratio, is_after_close, now_cn


class FunnelService:
    def __init__(
        self,
        provider: AkshareDataProvider,
        config: StrategyConfig | None = None,
        kline_cache_service: Any | None = None,
        persist_db_path: str = "data/funnel_state.db",
        legacy_json_path: str = "data/funnel_state.json",
    ) -> None:
        self.provider = provider
        self.config = config or StrategyConfig()
        self.kline_cache_service = kline_cache_service
        self.state_store = SQLiteStateStore(persist_db_path)
        self.legacy_json_file = Path(legacy_json_path)

        self.lock = asyncio.Lock()
        self.trade_date = now_cn().date().isoformat()
        self.entries: dict[str, dict[str, Any]] = {}
        self.hot_concepts: list[dict[str, Any]] = []
        self.hot_stocks: list[dict[str, Any]] = []
        self.updated_at = now_cn().isoformat()
        self.frozen = False

        self.strategy_profile = self._ensure_strategy_profile()
        self._load_state()

    def _reset_state(self, trade_date: str) -> None:
        self.trade_date = trade_date
        self.entries = {}
        self.hot_concepts = []
        self.hot_stocks = []
        self.updated_at = now_cn().isoformat()
        self.frozen = False

    def _load_state(self) -> None:
        payload = self.state_store.load_state()
        if payload is None and self.legacy_json_file.exists():
            try:
                payload = json.loads(self.legacy_json_file.read_text(encoding="utf-8"))
                self.state_store.save_state(payload)
            except Exception:
                payload = None
        if payload is None:
            return
        try:
            if payload.get("trade_date") != self.trade_date:
                return
            self.entries = payload.get("entries", {})
            self.hot_concepts = payload.get("hot_concepts", [])
            self.hot_stocks = payload.get("hot_stocks", [])
            self.updated_at = payload.get("updated_at", self.updated_at)
            self.frozen = bool(payload.get("frozen", False))
        except Exception:
            return

    def _ensure_strategy_profile(self) -> dict[str, Any]:
        profile = self.state_store.get_active_strategy_profile()
        if profile is not None:
            saved_config = profile.get("config", {})
            if saved_config:
                self.config = self.config.merge(saved_config)
            return profile
        config_payload = self.config.to_dict()
        return self.state_store.upsert_single_active_strategy_profile(
            name="alpha_rule_engine",
            config=config_payload,
            updated_at=now_cn().isoformat(),
        )

    def _save_state(self) -> None:
        payload = {
            "trade_date": self.trade_date,
            "entries": self.entries,
            "hot_concepts": self.hot_concepts,
            "hot_stocks": self.hot_stocks,
            "updated_at": self.updated_at,
            "frozen": self.frozen,
        }
        self.state_store.save_state(payload)

    def _record_trigger(self, entry: dict[str, Any], note: str, level: str = "info") -> None:
        logs = entry.setdefault("trigger_log", [])
        logs.append({"time": now_cn().isoformat(), "level": level, "note": note})
        if len(logs) > 120:
            del logs[:-120]

    def _snapshot_index(self, snapshot: pd.DataFrame) -> dict[str, dict[str, Any]]:
        if snapshot.empty:
            return {}
        index: dict[str, dict[str, Any]] = {}
        for _, row in snapshot.iterrows():
            code = str(row.get("代码", "")).strip()
            if code:
                index[code] = row.to_dict()
        return index

    def _build_pool_lists(self) -> dict[str, list[dict[str, Any]]]:
        pools = {POOL_CANDIDATE: [], POOL_FOCUS: [], POOL_BUY: []}
        for entry in self.entries.values():
            pools[entry["pool"]].append(entry)

        for pool_name in pools:
            pools[pool_name].sort(key=lambda x: x.get("score", 0), reverse=True)
        return pools

    async def ensure_trade_date(self, trade_date: str | None = None) -> None:
        date_str = trade_date or now_cn().date().isoformat()
        if date_str != self.trade_date:
            self._reset_state(date_str)
            self._save_state()

    def _build_cache_snapshot(self, trade_date: str) -> pd.DataFrame:
        if self.kline_cache_service is None:
            return pd.DataFrame()
        try:
            return self.kline_cache_service.build_snapshot_for_screen(trade_date)
        except Exception:
            return pd.DataFrame()

    async def _pick_screen_snapshot(self) -> tuple[pd.DataFrame, str]:
        now = now_cn()
        after_close = is_after_close(now)
        if after_close:
            cache_df = self._build_cache_snapshot(self.trade_date)
            if not cache_df.empty:
                return cache_df, "history_cache"
            spot_df = await self.provider.get_snapshot_spot()
            if not spot_df.empty:
                return spot_df, "spot"
            cache_fallback = await self.provider.get_realtime_snapshot(cache_ttl_seconds=24 * 3600)
            if not cache_fallback.empty:
                return cache_fallback, "realtime_cache"
            return pd.DataFrame(), "none"

        realtime_df = await self.provider.get_realtime_snapshot()
        if not realtime_df.empty:
            return realtime_df, "realtime"
        spot_df = await self.provider.get_snapshot_spot()
        if not spot_df.empty:
            return spot_df, "spot"
        cache_fallback = await self.provider.get_realtime_snapshot(cache_ttl_seconds=24 * 3600)
        if not cache_fallback.empty:
            return cache_fallback, "realtime_cache"
        return pd.DataFrame(), "none"

    async def run_eod_screen(self, trade_date: str | None = None) -> dict[str, Any]:
        started = time.time()

        async with self.lock:
            await self.ensure_trade_date(trade_date)
            current_trade_date = self.trade_date
            config = self.config

        snapshot, source_used = await self._pick_screen_snapshot()
        if snapshot.empty:
            raise RuntimeError("实时行情数据获取失败，请稍后重试")

        trade_days = await self.provider.get_trade_days()
        try:
            start_date, end_date = get_last_n_trade_window(trade_days, current_trade_date, config.period_days)
        except ValueError:
            raise RuntimeError("交易日历不可用，无法执行盘后筛选")

        universe = prefilter_universe(snapshot, config)
        universe.sort(key=lambda x: x.get("amount", 0), reverse=True)
        universe = universe[:config.universe_top_n]

        entries: dict[str, dict[str, Any]] = {}
        for stock in universe:
            symbol = stock["symbol"]
            try:
                hist = await self.provider.get_hist(symbol, start_date, end_date)
            except Exception:
                continue

            passed, reasons, metrics = analyze_adjustment_candidate(stock, hist, config)
            if not passed:
                continue

            entries[symbol] = {
                "symbol": symbol,
                "name": stock["name"],
                "pool": POOL_CANDIDATE,
                "recommended_pool": None,
                "score": 0.0,
                "prev_score": 0.0,
                "score_breakdown": {
                    "breakout_strength": 0,
                    "volume_quality": 0,
                    "intraday_structure": 0,
                    "risk_penalty": 0,
                    "total": 0,
                },
                "metrics": {},
                "warnings": [],
                "reasons": reasons,
                "transitions": {
                    "above60_count": 0,
                    "breakout_confirm_count": 0,
                    "below65_count": 0,
                },
                "breakout_level": float(metrics.get("breakout_level", 0)),
                "avg_amount20": float(metrics.get("avg_amount20", 0)),
                "concept_tags": [],
                "concept_candidates": [],
                "trigger_log": [
                    {
                        "time": now_cn().isoformat(),
                        "level": "info",
                        "note": "盘后进入调整期候选池",
                    }
                ],
                "updated_at": now_cn().isoformat(),
            }

        async with self.lock:
            self.entries = entries
            if self.entries:
                await self._refresh_scores_unlocked(symbol=None, force_concept_refresh=True)
            else:
                await self._refresh_market_panels_unlocked(force=True)
            self.updated_at = now_cn().isoformat()
            self.frozen = is_after_close(now_cn())
            self._save_state()

        candidate_count = len(entries)
        elapsed_ms = int((time.time() - started) * 1000)
        return {
            "candidate_count": candidate_count,
            "source_used": source_used,
            "elapsed_ms": elapsed_ms,
            "message": f"筛选完成，候选池{candidate_count}只",
        }

    async def _refresh_scores_unlocked(self, symbol: str | None = None, force_concept_refresh: bool = False) -> None:
        """Refresh scores. Caller MUST hold self.lock."""
        snapshot = await self.provider.get_realtime_snapshot()
        if snapshot.empty or not self.entries:
            return

        snap_idx = self._snapshot_index(snapshot)
        elapsed_ratio = elapsed_market_ratio(now_cn())

        symbols = [symbol] if symbol else list(self.entries.keys())
        for s in symbols:
            entry = self.entries.get(s)
            if not entry:
                continue
            market_row = snap_idx.get(s)
            if not market_row:
                continue

            prev_score = float(entry.get("score", 0.0))
            score, breakdown, metrics, warnings = compute_intraday_score(entry, market_row, elapsed_ratio, self.config)
            entry["prev_score"] = prev_score
            entry["score"] = round(score, 2)
            entry["score_breakdown"] = breakdown
            entry["metrics"] = metrics
            entry["warnings"] = warnings
            entry["updated_at"] = now_cn().isoformat()

            transitions_result = apply_transition_rules(entry, self.config)
            entry["recommended_pool"] = transitions_result["recommended_pool"]

            for note in transitions_result["trigger_notes"]:
                self._record_trigger(entry, note)

            auto_move_to = transitions_result.get("auto_move_to")
            if auto_move_to and entry["pool"] == POOL_BUY:
                entry["pool"] = auto_move_to
                entry["recommended_pool"] = None
                self._record_trigger(entry, "分数低于65连续5分钟，自动降级至重点池", "warn")

        await self._refresh_market_panels_unlocked(force=force_concept_refresh)
        self.updated_at = now_cn().isoformat()
        self._save_state()

    async def refresh_scores(self, symbol: str | None = None, force_concept_refresh: bool = False) -> None:
        async with self.lock:
            await self._refresh_scores_unlocked(symbol, force_concept_refresh)

    async def _refresh_concepts_unlocked(self, force: bool = False) -> None:
        """Refresh concepts. Caller MUST hold self.lock."""
        if self.frozen and is_after_close(now_cn()) and not force:
            return

        concept_heat_df = await build_concept_heat(self.provider, top_n=120)
        if concept_heat_df.empty:
            print("[funnel] concept refresh skipped: empty concept source")
            return

        data_source = str(concept_heat_df.get("数据源", pd.Series(["em"])).iloc[0]).lower()
        symbols = set(self.entries.keys())
        stock_map: dict[str, list[dict[str, Any]]]
        if data_source == "em":
            stock_map = await map_stock_concepts(self.provider, symbols, concept_heat_df)
            for symbol, entry in self.entries.items():
                concepts = stock_map.get(symbol, [])
                entry["concept_candidates"] = concepts
                entry["concept_tags"] = build_top_tags(concepts, top_k=3)
        else:
            stock_map = {s: self.entries.get(s, {}).get("concept_candidates", []) for s in symbols}

        selected_symbols = {s for s, e in self.entries.items() if e.get("pool") in VALID_POOLS}
        self.hot_concepts = build_hot_concepts_payload(concept_heat_df, selected_symbols, stock_map, top_n=10)

    async def _refresh_hot_stocks_unlocked(self, force: bool = False) -> None:
        """Refresh hot stocks. Caller MUST hold self.lock."""
        if self.frozen and is_after_close(now_cn()) and not force:
            return
        hot_df = await self.provider.get_hot_stocks(top_n=30)
        if hot_df.empty:
            return
        self.hot_stocks = []
        for _, row in hot_df.iterrows():
            self.hot_stocks.append(
                {
                    "rank": int(row.get("rank", 0)),
                    "symbol": str(row.get("symbol", "")),
                    "name": str(row.get("name", "")),
                    "latest_price": float(row.get("latest_price", 0.0)),
                    "change_pct": float(row.get("change_pct", 0.0)),
                    "change_amount": float(row.get("change_amount", 0.0)),
                }
            )

    async def _refresh_market_panels_unlocked(self, force: bool = False) -> None:
        """Refresh market panels. Caller MUST hold self.lock."""
        await self._refresh_concepts_unlocked(force=force)
        await self._refresh_hot_stocks_unlocked(force=force)

    async def refresh_market_panels(self, force: bool = False) -> None:
        async with self.lock:
            await self._refresh_market_panels_unlocked(force=force)

    async def move_pool(self, symbol: str, target_pool: str, note: str | None = None) -> MovePoolResponse:
        async with self.lock:
            if target_pool not in VALID_POOLS:
                return MovePoolResponse(success=False, message="非法目标池", symbol=symbol, pool=POOL_CANDIDATE)

            entry = self.entries.get(symbol)
            if not entry:
                return MovePoolResponse(success=False, message="股票不存在于当前选股池", symbol=symbol, pool=POOL_CANDIDATE)

            if target_pool == POOL_BUY and entry["pool"] != POOL_BUY:
                buy_count = sum(1 for e in self.entries.values() if e.get("pool") == POOL_BUY)
                if buy_count >= self.config.buy_pool_max_size:
                    return MovePoolResponse(success=False, message="买入池已满(5只)", symbol=symbol, pool=entry["pool"])

            old_pool = entry["pool"]
            entry["pool"] = target_pool
            entry["recommended_pool"] = None
            entry["updated_at"] = now_cn().isoformat()
            self._record_trigger(entry, note or f"手动迁移: {old_pool} -> {target_pool}")

            await self._refresh_market_panels_unlocked(force=True)
            self.updated_at = now_cn().isoformat()
            self._save_state()
            return MovePoolResponse(success=True, message="迁移成功", symbol=symbol, pool=target_pool)

    async def recompute(self, symbol: str | None = None) -> None:
        async with self.lock:
            force_refresh = not (self.frozen and is_after_close(now_cn()))
            await self._refresh_scores_unlocked(symbol=symbol, force_concept_refresh=force_refresh)

    async def get_funnel(self, trade_date: str | None = None) -> FunnelResponse:
        async with self.lock:
            await self.ensure_trade_date(trade_date)
            pools_raw = self._build_pool_lists()
            pools: dict[str, list[StockCard]] = {POOL_CANDIDATE: [], POOL_FOCUS: [], POOL_BUY: []}

            for pool_name, entries in pools_raw.items():
                pools[pool_name] = [
                    StockCard(
                        symbol=e["symbol"],
                        name=e["name"],
                        pool=e["pool"],
                        score=round(float(e.get("score", 0)), 2),
                        score_delta=round(float(e.get("score", 0)) - float(e.get("prev_score", 0)), 2),
                        recommended_pool=e.get("recommended_pool"),
                        breakout_level=round(float(e.get("breakout_level", 0)), 3),
                        volume_ratio=round(float(e.get("metrics", {}).get("volume_ratio", 0)), 3),
                        pct_change=round(float(e.get("metrics", {}).get("pct_change", 0)), 3),
                        concept_tags=e.get("concept_tags", []),
                        reasons=e.get("reasons", []),
                        warnings=e.get("warnings", []),
                        updated_at=e.get("updated_at", self.updated_at),
                    )
                    for e in entries
                ]

            stats = {
                "candidate": len(pools[POOL_CANDIDATE]),
                "focus": len(pools[POOL_FOCUS]),
                "buy": len(pools[POOL_BUY]),
            }

            return FunnelResponse(
                trade_date=self.trade_date,
                updated_at=self.updated_at,
                pools=pools,
                stats=stats,
            )

    async def get_strategy_profile(self) -> dict[str, Any]:
        async with self.lock:
            self.strategy_profile = self._ensure_strategy_profile()
            return self.strategy_profile

    async def get_hot_concepts(self, trade_date: str | None = None) -> HotConceptResponse:
        async with self.lock:
            await self.ensure_trade_date(trade_date)
            if not self.hot_concepts:
                await self._refresh_concepts_unlocked(force=True)
                self.updated_at = now_cn().isoformat()
                self._save_state()
            elif len(self.hot_concepts) > 10:
                self.hot_concepts = self.hot_concepts[:10]
                self.updated_at = now_cn().isoformat()
                self._save_state()
            return HotConceptResponse(
                trade_date=self.trade_date,
                updated_at=self.updated_at,
                frozen=self.frozen,
                items=self.hot_concepts,
            )

    async def get_hot_stocks(self, trade_date: str | None = None) -> HotStocksResponse:
        async with self.lock:
            await self.ensure_trade_date(trade_date)
            if not self.hot_stocks:
                await self._refresh_hot_stocks_unlocked(force=True)
                self.updated_at = now_cn().isoformat()
                self._save_state()
            return HotStocksResponse(
                trade_date=self.trade_date,
                updated_at=self.updated_at,
                frozen=self.frozen,
                items=self.hot_stocks,
            )

    async def _get_latest_trade_date(self) -> str | None:
        try:
            df = await self.provider.get_trade_days()
            if df.empty:
                return now_cn().date().isoformat()
            today = pd.Timestamp(now_cn().date())
            df["trade_date"] = pd.to_datetime(df["trade_date"])
            valid = df[df["trade_date"] <= today]["trade_date"]
            if valid.empty:
                return now_cn().date().isoformat()
            return valid.iloc[-1].strftime("%Y-%m-%d")
        except Exception:
            return now_cn().date().isoformat()

    async def _build_today_bar(self, symbol: str) -> dict | None:
        import akshare as ak

        trade_date = await self._get_latest_trade_date()
        if not trade_date:
            return None

        bar = None
        try:
            raw_symbol = symbol.replace("sh", "").replace("sz", "").replace("bj", "")
            df = await asyncio.to_thread(ak.stock_bid_ask_em, symbol=raw_symbol)
            if df is not None and not df.empty:
                lookup = dict(zip(df["item"], df["value"]))
                price = float(lookup.get("最新", 0))
                open_ = float(lookup.get("今开", 0))
                if price > 0 and open_ > 0:
                    volume_hands = float(lookup.get("总手", 0))
                    bar = {
                        "date": trade_date,
                        "open": open_,
                        "high": float(lookup.get("最高", price)),
                        "low": float(lookup.get("最低", price)),
                        "close": price,
                        "volume": volume_hands * 100,
                    }
        except Exception:
            pass

        if bar is None:
            try:
                snapshot = await self.provider.get_realtime_snapshot(cache_ttl_seconds=300)
                if not snapshot.empty:
                    row = snapshot[snapshot["代码"] == symbol]
                    if not row.empty:
                        r = row.iloc[0]
                        price = float(r.get("最新价", 0))
                        open_ = float(r.get("今开", 0))
                        if price > 0 and open_ > 0:
                            bar = {
                                "date": trade_date,
                                "open": open_,
                                "high": float(r.get("最高", price)),
                                "low": float(r.get("最低", price)),
                                "close": price,
                                "volume": float(r.get("成交量", 0)),
                            }
            except Exception:
                pass

        return bar

    async def get_stock_detail(
        self, symbol: str, trade_date: str | None = None, kline_days: int = 30
    ) -> StockDetailResponse:
        async with self.lock:
            await self.ensure_trade_date(trade_date)
            entry = self.entries.get(symbol)
            if not entry:
                raise KeyError(symbol)
            entry_copy = dict(entry)
            current_trade_date = self.trade_date

        trade_days = await self.provider.get_trade_days()
        try:
            days = max(10, min(kline_days, 180))
            start_date, end_date = get_last_n_trade_window(trade_days, current_trade_date, days)
            hist = await self.provider.get_hist(symbol, start_date, end_date)
        except ValueError:
            hist = pd.DataFrame()

        kline = []
        if self.kline_cache_service is not None:
            try:
                cached_rows = self.kline_cache_service.get_kline(symbol, days)
            except Exception:
                cached_rows = []
            if cached_rows:
                kline = [
                    {
                        "date": str(r.get("date", "")),
                        "open": float(r.get("open", 0)),
                        "high": float(r.get("high", 0)),
                        "low": float(r.get("low", 0)),
                        "close": float(r.get("close", 0)),
                        "volume": float(r.get("volume", 0)),
                    }
                    for r in cached_rows
                ]

        if not kline and not hist.empty:
            for _, row in hist.tail(days).iterrows():
                kline.append(
                    {
                        "date": str(row.get("日期", "")),
                        "open": float(row.get("开盘", 0)),
                        "high": float(row.get("最高", 0)),
                        "low": float(row.get("最低", 0)),
                        "close": float(row.get("收盘", 0)),
                        "volume": float(row.get("成交量", 0)),
                    }
                )

        today_bar = await self._build_today_bar(symbol)
        if today_bar:
            if kline and kline[-1]["date"] == today_bar["date"]:
                kline[-1] = today_bar
            else:
                kline.append(today_bar)

        return StockDetailResponse(
            symbol=entry_copy["symbol"],
            name=entry_copy["name"],
            pool=entry_copy["pool"],
            score=round(float(entry_copy.get("score", 0)), 2),
            recommended_pool=entry_copy.get("recommended_pool"),
            score_breakdown=entry_copy.get("score_breakdown", {}),
            metrics=entry_copy.get("metrics", {}),
            concept_tags=entry_copy.get("concept_tags", []),
            concept_candidates=entry_copy.get("concept_candidates", []),
            trigger_log=entry_copy.get("trigger_log", []),
            kline=kline,
        )

    async def tick(self) -> bool:
        async with self.lock:
            today = now_cn().date().isoformat()
            if today != self.trade_date:
                self._reset_state(today)
                self._save_state()
            has_entries = bool(self.entries)
            is_frozen = self.frozen

        if not has_entries and is_after_close(now_cn()):
            return False

        if not has_entries:
            async with self.lock:
                await self._refresh_market_panels_unlocked(force=False)
                self.updated_at = now_cn().isoformat()
                self._save_state()
            return False

        if is_after_close(now_cn()):
            if not is_frozen:
                await self.recompute()
                async with self.lock:
                    self.frozen = True
                    self.updated_at = now_cn().isoformat()
                    self._save_state()
                return True
            return False

        await self.recompute()
        return True
