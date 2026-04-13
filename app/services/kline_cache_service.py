from __future__ import annotations

import asyncio
from datetime import time
import uuid
import time as pytime
from typing import Any

import pandas as pd

from app.services.data_provider import AkshareDataProvider, to_float
from app.services.kline_store import KlineSQLiteStore
from app.services.time_utils import now_cn


_CONCURRENCY = 8


class KlineCacheService:
    def __init__(
        self,
        provider: AkshareDataProvider,
        store: KlineSQLiteStore | None = None,
        schedule_after: time = time(15, 20),
        window_days: int = 30,
    ) -> None:
        self.provider = provider
        self.store = store or KlineSQLiteStore()
        self.schedule_after = schedule_after
        self.window_days = max(10, min(window_days, 180))
        self._syncing = False

    async def run_if_due(self) -> bool:
        now = now_cn()
        cur_time = now.timetz().replace(tzinfo=None)
        if cur_time < self.schedule_after:
            return False

        trade_date = await self._resolve_latest_trade_date(now.date().isoformat())
        if not trade_date:
            return False

        state = self.store.get_sync_state()
        if state.get("last_success_trade_date") == trade_date:
            return False

        await self.sync_trade_date(trade_date=trade_date, force=True, trigger_mode="auto")
        return True

    async def sync_trade_date(
        self,
        trade_date: str | None = None,
        force: bool = False,
        trigger_mode: str = "manual",
    ) -> dict[str, Any]:
        if self._syncing:
            return {"success": False, "message": "同步任务正在执行中", "trade_date": trade_date or "", "symbol_count": 0}
        self._syncing = True
        try:
            return await self._do_sync(trade_date=trade_date, force=force, trigger_mode=trigger_mode)
        finally:
            self._syncing = False

    async def _do_sync(
        self,
        trade_date: str | None = None,
        force: bool = False,
        trigger_mode: str = "manual",
    ) -> dict[str, Any]:
        target_trade_date = trade_date or await self._resolve_latest_trade_date(now_cn().date().isoformat())
        if not target_trade_date:
            return {"success": False, "message": "无法确定交易日", "trade_date": "", "symbol_count": 0}

        state = self.store.get_sync_state()
        if not force and state.get("last_success_trade_date") == target_trade_date:
            return {
                "success": True,
                "message": "当日已完成缓存",
                "trade_date": target_trade_date,
                "symbol_count": int(state.get("symbol_count", 0)),
            }

        self.store.set_sync_state(
            attempt_trade_date=target_trade_date,
            success_trade_date=state.get("last_success_trade_date"),
            status="running",
            symbol_count=0,
            total_symbols=0,
            synced_symbols=0,
            success_symbols=0,
            failed_symbols=0,
            task_id=None,
            trigger_mode=trigger_mode,
            updated_at=now_cn().isoformat(),
            message="开始同步",
        )

        symbols = await self._load_symbol_list()
        if not symbols:
            self.store.set_sync_state(
                attempt_trade_date=target_trade_date,
                success_trade_date=state.get("last_success_trade_date"),
                status="failed",
                symbol_count=0,
                total_symbols=0,
                synced_symbols=0,
                success_symbols=0,
                failed_symbols=0,
                task_id=None,
                trigger_mode=trigger_mode,
                updated_at=now_cn().isoformat(),
                message="股票列表为空",
            )
            return {"success": False, "message": "股票列表为空", "trade_date": target_trade_date, "symbol_count": 0}

        start_date, end_date = await self._resolve_window(target_trade_date, self.window_days)
        if not start_date or not end_date:
            self.store.set_sync_state(
                attempt_trade_date=target_trade_date,
                success_trade_date=state.get("last_success_trade_date"),
                status="failed",
                symbol_count=0,
                total_symbols=0,
                synced_symbols=0,
                success_symbols=0,
                failed_symbols=0,
                task_id=None,
                trigger_mode=trigger_mode,
                updated_at=now_cn().isoformat(),
                message="交易窗口计算失败",
            )
            return {"success": False, "message": "交易窗口计算失败", "trade_date": target_trade_date, "symbol_count": 0}

        task_id = uuid.uuid4().hex
        started_at = now_cn().isoformat()
        total = len(symbols)
        self.store.start_sync_task(
            task_id=task_id,
            trigger_mode=trigger_mode,
            trade_date=target_trade_date,
            total_symbols=total,
            started_at=started_at,
        )
        result = await self._concurrent_fetch(
            symbols=symbols,
            start_date=start_date,
            end_date=end_date,
            task_id=task_id,
            target_trade_date=target_trade_date,
            state=state,
            trigger_mode=trigger_mode,
            total=total,
            label="同步",
        )
        completed, success_count, failed_count = result

        final_status = "success" if completed > 0 else "failed"
        final_msg = "同步完成" if completed > 0 else "同步失败"
        self.store.finish_sync_task(
            task_id=task_id,
            status=final_status,
            finished_at=now_cn().isoformat(),
            message=final_msg,
        )
        self.store.set_sync_state(
            attempt_trade_date=target_trade_date,
            success_trade_date=target_trade_date if completed > 0 else state.get("last_success_trade_date"),
            status=final_status,
            symbol_count=completed,
            total_symbols=total,
            synced_symbols=success_count + failed_count,
            success_symbols=success_count,
            failed_symbols=failed_count,
            task_id=task_id,
            trigger_mode=trigger_mode,
            updated_at=now_cn().isoformat(),
            message=final_msg,
        )
        return {
            "success": completed > 0,
            "message": final_msg,
            "trade_date": target_trade_date,
            "symbol_count": completed,
            "task_id": task_id,
            "total_symbols": total,
            "synced_symbols": success_count + failed_count,
        }

    async def incremental_sync(
        self,
        trade_date: str | None = None,
        trigger_mode: str = "manual",
    ) -> dict[str, Any]:
        """增量同步：只同步指定交易日（默认当天）的 K 线数据。"""
        if self._syncing:
            return {"success": False, "message": "同步任务正在执行中", "trade_date": trade_date or "", "symbol_count": 0}
        self._syncing = True
        try:
            return await self._do_incremental_sync(trade_date=trade_date, trigger_mode=trigger_mode)
        finally:
            self._syncing = False

    async def _do_incremental_sync(
        self,
        trade_date: str | None = None,
        trigger_mode: str = "manual",
    ) -> dict[str, Any]:
        target_trade_date = trade_date or await self._resolve_latest_trade_date(now_cn().date().isoformat())
        if not target_trade_date:
            return {"success": False, "message": "无法确定交易日", "trade_date": "", "symbol_count": 0}

        td_fmt = target_trade_date.replace("-", "")

        state = self.store.get_sync_state()
        self.store.set_sync_state(
            attempt_trade_date=target_trade_date,
            success_trade_date=state.get("last_success_trade_date"),
            status="running",
            symbol_count=0,
            total_symbols=0,
            synced_symbols=0,
            success_symbols=0,
            failed_symbols=0,
            task_id=None,
            trigger_mode=trigger_mode,
            updated_at=now_cn().isoformat(),
            message=f"增量同步 {target_trade_date}",
        )

        symbols = await self._load_symbol_list()
        if not symbols:
            self.store.set_sync_state(
                attempt_trade_date=target_trade_date,
                success_trade_date=state.get("last_success_trade_date"),
                status="failed",
                symbol_count=0,
                total_symbols=0,
                synced_symbols=0,
                success_symbols=0,
                failed_symbols=0,
                task_id=None,
                trigger_mode=trigger_mode,
                updated_at=now_cn().isoformat(),
                message="股票列表为空",
            )
            return {"success": False, "message": "股票列表为空", "trade_date": target_trade_date, "symbol_count": 0}

        task_id = uuid.uuid4().hex
        started_at = now_cn().isoformat()
        total = len(symbols)
        self.store.start_sync_task(
            task_id=task_id,
            trigger_mode=trigger_mode,
            trade_date=target_trade_date,
            total_symbols=total,
            started_at=started_at,
        )
        result = await self._concurrent_fetch(
            symbols=symbols,
            start_date=td_fmt,
            end_date=td_fmt,
            task_id=task_id,
            target_trade_date=target_trade_date,
            state=state,
            trigger_mode=trigger_mode,
            total=total,
            label="增量同步",
        )
        completed, success_count, failed_count = result

        final_status = "success" if completed > 0 else "failed"
        final_msg = f"增量同步完成 {target_trade_date}" if completed > 0 else f"增量同步失败 {target_trade_date}"
        self.store.finish_sync_task(
            task_id=task_id,
            status=final_status,
            finished_at=now_cn().isoformat(),
            message=final_msg,
        )
        self.store.set_sync_state(
            attempt_trade_date=target_trade_date,
            success_trade_date=target_trade_date if completed > 0 else state.get("last_success_trade_date"),
            status=final_status,
            symbol_count=completed,
            total_symbols=total,
            synced_symbols=success_count + failed_count,
            success_symbols=success_count,
            failed_symbols=failed_count,
            task_id=task_id,
            trigger_mode=trigger_mode,
            updated_at=now_cn().isoformat(),
            message=final_msg,
        )
        return {
            "success": completed > 0,
            "message": final_msg,
            "trade_date": target_trade_date,
            "symbol_count": completed,
            "task_id": task_id,
            "total_symbols": total,
            "synced_symbols": success_count + failed_count,
            "mode": "incremental",
        }

    async def _concurrent_fetch(
        self,
        symbols: list[str],
        start_date: str,
        end_date: str,
        task_id: str,
        target_trade_date: str,
        state: dict,
        trigger_mode: str,
        total: int,
        label: str = "同步",
    ) -> tuple[int, int, int]:
        sem = asyncio.Semaphore(_CONCURRENCY)
        completed = 0
        success_count = 0
        failed_count = 0
        now_iso = now_cn().isoformat()
        lock = asyncio.Lock()

        async def _fetch_one(symbol: str) -> None:
            nonlocal completed, success_count, failed_count
            async with sem:
                t0 = pytime.time()
                hist = await self.provider.get_hist(symbol, start_date, end_date)
                rows = self._normalize_hist(hist)
                elapsed = int((pytime.time() - t0) * 1000)

            async with lock:
                if rows:
                    self.store.upsert_symbol_klines(symbol, rows, now_iso)
                    completed += 1
                    success_count += 1
                    self.store.add_sync_task_detail(
                        task_id=task_id, symbol=symbol, status="success",
                        elapsed_ms=elapsed, error_message="", created_at=now_cn().isoformat(),
                    )
                else:
                    failed_count += 1
                    self.store.add_sync_task_detail(
                        task_id=task_id, symbol=symbol, status="failed",
                        elapsed_ms=elapsed, error_message="数据为空", created_at=now_cn().isoformat(),
                    )

                synced = success_count + failed_count
                if synced % 50 == 0 or synced == total:
                    self.store.update_sync_task_progress(
                        task_id=task_id, synced_symbols=synced,
                        success_symbols=success_count, failed_symbols=failed_count,
                        message=f"{label} {synced}/{total}",
                    )
                    self.store.set_sync_state(
                        attempt_trade_date=target_trade_date,
                        success_trade_date=state.get("last_success_trade_date"),
                        status="running",
                        symbol_count=completed,
                        total_symbols=total,
                        synced_symbols=synced,
                        success_symbols=success_count,
                        failed_symbols=failed_count,
                        task_id=task_id,
                        trigger_mode=trigger_mode,
                        updated_at=now_cn().isoformat(),
                        message=f"{label} {synced}/{total}",
                    )

        batch_size = 200
        for i in range(0, len(symbols), batch_size):
            batch = symbols[i : i + batch_size]
            await asyncio.gather(*[_fetch_one(s) for s in batch])

        return completed, success_count, failed_count

    def get_kline(self, symbol: str, days: int = 30) -> list[dict[str, Any]]:
        clean_symbol = str(symbol).strip()
        return self.store.get_kline(clean_symbol, days)

    def get_sync_state(self) -> dict[str, Any]:
        return self.store.get_sync_state()

    def get_sync_progress(self) -> dict[str, Any]:
        return self.store.get_sync_state()

    def list_sync_logs(self, page: int = 1, page_size: int = 20) -> dict[str, Any]:
        return self.store.list_sync_tasks(page=page, page_size=page_size)

    def get_sync_log_detail(self, task_id: str) -> dict[str, Any] | None:
        return self.store.get_sync_task_detail(task_id)

    def build_snapshot_for_screen(self, trade_date: str) -> pd.DataFrame:
        rows = []
        import sqlite3

        conn = sqlite3.connect(str(self.store.db_path))
        conn.row_factory = sqlite3.Row
        try:
            all_rows = conn.execute(
                """
                SELECT symbol, trade_date, open, high, low, close, volume, amount
                FROM kline_daily
                WHERE trade_date <= ?
                ORDER BY symbol ASC, trade_date ASC
                """,
                (trade_date,),
            ).fetchall()
            if not all_rows:
                return pd.DataFrame()
        finally:
            conn.close()

        latest_map: dict[str, dict[str, Any]] = {}
        prev_close_map: dict[str, float] = {}
        for row in all_rows:
            symbol = str(row["symbol"])
            if symbol in latest_map:
                prev_close_map[symbol] = float(latest_map[symbol]["close"])
            latest_map[symbol] = {
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
                "amount": float(row["amount"]),
            }

        # build_snapshot_for_screen is called from sync context in funnel_service;
        # provider.get_symbol_name_map is now async, so use cached data if available.
        name_map: dict[str, str] = {}
        if self.provider.symbol_name_cache is not None:
            _, name_map = self.provider.symbol_name_cache
        for symbol, item in latest_map.items():
            close = float(item["close"])
            pre_close = float(prev_close_map.get(symbol, close))
            pct = ((close / max(pre_close, 0.01)) - 1) * 100
            rows.append(
                {
                    "代码": symbol,
                    "名称": name_map.get(symbol, symbol),
                    "今开": float(item["open"]),
                    "昨收": pre_close,
                    "最新价": close,
                    "最高": float(item["high"]),
                    "最低": float(item["low"]),
                    "成交量": float(item["volume"]),
                    "成交额": float(item["amount"]),
                    "涨跌幅": pct,
                    "总市值": pd.NA,
                }
            )
        return pd.DataFrame(rows)

    async def _resolve_latest_trade_date(self, base_date: str) -> str:
        trade_days = await self.provider.get_trade_days()
        if trade_days.empty or "trade_date" not in trade_days.columns:
            return ""
        days = pd.to_datetime(trade_days["trade_date"], errors="coerce").dropna().dt.date
        target = pd.to_datetime(base_date).date()
        valid = days[days <= target]
        if valid.empty:
            return ""
        return valid.iloc[-1].isoformat()

    async def _resolve_window(self, trade_date: str, days: int) -> tuple[str, str]:
        trade_days = await self.provider.get_trade_days()
        if trade_days.empty or "trade_date" not in trade_days.columns:
            return "", ""
        dates = pd.to_datetime(trade_days["trade_date"], errors="coerce").dropna().dt.date
        target = pd.to_datetime(trade_date).date()
        selected = dates[dates <= target].tail(days)
        if len(selected) < days:
            return "", ""
        return selected.iloc[0].strftime("%Y%m%d"), selected.iloc[-1].strftime("%Y%m%d")

    async def _load_symbol_list(self) -> list[str]:
        symbols: list[str] = []
        name_map = await self.provider.get_symbol_name_map()
        for code, name in name_map.items():
            n = str(name).upper()
            if not code or "ST" in n:
                continue
            if code.startswith(("00", "60")):
                symbols.append(code)

        if symbols:
            return sorted(set(symbols))

        snapshot = await self.provider.get_snapshot_spot()
        if snapshot.empty:
            snapshot = await self.provider.get_realtime_snapshot()
        if snapshot.empty:
            return []
        for _, row in snapshot.iterrows():
            code = str(row.get("代码", "")).strip()
            name = str(row.get("名称", "")).strip().upper()
            if not code or "ST" in name:
                continue
            if code.startswith(("00", "60")):
                symbols.append(code)
        return sorted(set(symbols))

    @staticmethod
    def _normalize_hist(hist: pd.DataFrame) -> list[dict[str, Any]]:
        if hist is None or hist.empty:
            return []

        rows: list[dict[str, Any]] = []
        for _, row in hist.iterrows():
            trade_date = str(row.get("日期", "")).strip()
            if not trade_date:
                continue
            rows.append(
                {
                    "trade_date": trade_date,
                    "open": to_float(row.get("开盘")),
                    "high": to_float(row.get("最高")),
                    "low": to_float(row.get("最低")),
                    "close": to_float(row.get("收盘")),
                    "volume": to_float(row.get("成交量")),
                    "amount": to_float(row.get("成交额")),
                }
            )
        return rows
