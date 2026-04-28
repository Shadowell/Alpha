from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, time
import uuid
import time as pytime
from typing import Any

import pandas as pd

from app.services.data_provider import AkshareDataProvider, to_float
from app.services.kline_store import KlineSQLiteStore
from app.services.market_data_client import EastmoneyMarketDataClient
from app.services.time_utils import now_cn


_CONCURRENCY = 8
_FETCH_BATCH_SIZE = 200


class KlineCacheService:
    def __init__(
        self,
        provider: AkshareDataProvider,
        store: KlineSQLiteStore | None = None,
        market_data_client: EastmoneyMarketDataClient | None = None,
        schedule_after: time = time(15, 20),
        window_days: int = 180,
    ) -> None:
        self.provider = provider
        self.store = store or KlineSQLiteStore()
        self.market_data_client = market_data_client or EastmoneyMarketDataClient(store=self.store)
        self.schedule_after = schedule_after
        self.window_days = max(10, min(window_days, 365))
        self._syncing = False
        self._queue: list[tuple[str, dict[str, Any]]] = []
        self._queue_task: asyncio.Task | None = None

    def enqueue_sync_trade_date(
        self,
        trade_date: str | None = None,
        force: bool = False,
        trigger_mode: str = "manual",
        window_days: int | None = None,
    ) -> dict[str, Any]:
        return self._enqueue_job(
            "full",
            {
                "trade_date": trade_date,
                "force": force,
                "trigger_mode": trigger_mode,
                "window_days": window_days,
            },
        )

    def enqueue_incremental_sync(
        self,
        trade_date: str | None = None,
        trigger_mode: str = "manual",
    ) -> dict[str, Any]:
        return self._enqueue_job(
            "incremental",
            {
                "trade_date": trade_date,
                "trigger_mode": trigger_mode,
            },
        )

    def enqueue_incremental_range(
        self,
        start_date: str,
        end_date: str,
        trigger_mode: str = "manual",
    ) -> dict[str, Any]:
        dates = self._weekdays_between(start_date, end_date)
        if not dates:
            return {
                "success": False,
                "accepted": False,
                "message": "日期范围内没有可同步日期",
                "submitted": 0,
                "queue_size": len(self._queue),
            }
        for item in dates:
            self._queue.append(("incremental", {"trade_date": item, "trigger_mode": trigger_mode}))
        queue_size = len(self._queue)
        if self._queue_task is None or self._queue_task.done():
            self._queue_task = asyncio.create_task(self._run_queue())
        return {
            "success": True,
            "accepted": True,
            "status": "queued" if self._syncing or queue_size > len(dates) else "running",
            "submitted": len(dates),
            "start_date": dates[0],
            "end_date": dates[-1],
            "queue_size": queue_size,
            "message": f"已提交{len(dates)}个日期到后台同步队列",
        }

    def _enqueue_job(self, job_type: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        self._queue.append((job_type, kwargs))
        queue_size = len(self._queue)
        if self._queue_task is None or self._queue_task.done():
            self._queue_task = asyncio.create_task(self._run_queue())
        return {
            "success": True,
            "accepted": True,
            "status": "queued" if self._syncing or queue_size > 1 else "running",
            "queue_size": queue_size,
            "message": "同步任务已提交后台执行",
        }

    async def _run_queue(self) -> None:
        while self._queue:
            job_type, kwargs = self._queue.pop(0)
            try:
                if job_type == "incremental":
                    await self.incremental_sync(**kwargs)
                else:
                    await self.sync_trade_date(**kwargs)
            except Exception as exc:
                print(f"[kline-cache] queued {job_type} sync failed: {exc}")
                self._fail_stale_running_tasks(f"同步异常中断: {str(exc)[:160]}")

    @staticmethod
    def _weekdays_between(start_date: str, end_date: str, max_days: int = 180) -> list[str]:
        try:
            start = datetime.strptime(start_date, "%Y-%m-%d").date()
            end = datetime.strptime(end_date, "%Y-%m-%d").date()
        except ValueError:
            return []
        if start > end:
            return []
        dates: list[str] = []
        cur = start
        while cur <= end and len(dates) < max_days:
            if cur.weekday() < 5:
                dates.append(cur.isoformat())
            cur += timedelta(days=1)
        return dates

    async def run_if_due(self) -> dict[str, Any] | None:
        """检查是否到达自动同步时间，到达则执行同步并返回结果 dict，未触发返回 None。"""
        now = now_cn()
        cur_time = now.timetz().replace(tzinfo=None)
        if cur_time < self.schedule_after:
            return None

        trade_date = await self._resolve_latest_trade_date(now.date().isoformat())
        if not trade_date:
            return None

        state = self.store.get_sync_state()
        if state.get("last_success_trade_date") == trade_date:
            return None

        return await self.incremental_sync(trade_date=trade_date, trigger_mode="auto")

    async def sync_trade_date(
        self,
        trade_date: str | None = None,
        force: bool = False,
        trigger_mode: str = "manual",
        window_days: int | None = None,
    ) -> dict[str, Any]:
        if self._syncing:
            return {"success": False, "message": "同步任务正在执行中", "trade_date": trade_date or "", "symbol_count": 0}
        self._syncing = True
        try:
            return await self._do_sync(
                trade_date=trade_date, force=force, trigger_mode=trigger_mode, window_days=window_days,
            )
        finally:
            self._syncing = False

    async def _do_sync(
        self,
        trade_date: str | None = None,
        force: bool = False,
        trigger_mode: str = "manual",
        window_days: int | None = None,
    ) -> dict[str, Any]:
        t0 = pytime.time()
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
            message="检查数据缺失...",
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

        effective_window = max(10, min(window_days if window_days is not None else self.window_days, 365))
        trade_dates_list = await self._resolve_trade_dates(target_trade_date, effective_window)
        if not trade_dates_list:
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

        existing_pairs = self.store.get_existing_pairs(trade_dates_list)
        missing_by_date: dict[str, list[str]] = {}
        for td in trade_dates_list:
            missing_symbols = [s for s in symbols if (s, td) not in existing_pairs]
            if missing_symbols:
                missing_by_date[td] = missing_symbols

        total_missing = sum(len(v) for v in missing_by_date.values())
        if total_missing == 0:
            self.store.set_sync_state(
                attempt_trade_date=target_trade_date,
                success_trade_date=target_trade_date,
                status="success",
                symbol_count=len(symbols),
                total_symbols=len(symbols),
                synced_symbols=0,
                success_symbols=0,
                failed_symbols=0,
                task_id=None,
                trigger_mode=trigger_mode,
                updated_at=now_cn().isoformat(),
                message="数据完整，无需同步",
            )
            return {
                "success": True,
                "message": "数据完整，无需同步",
                "trade_date": target_trade_date,
                "symbol_count": len(symbols),
                "total_symbols": len(symbols),
                "synced_symbols": 0,
                "success_symbols": 0,
                "failed_symbols": 0,
                "missing_filled": 0,
                "elapsed_sec": round(pytime.time() - t0, 1),
            }

        unique_missing_symbols = sorted({s for syms in missing_by_date.values() for s in syms})
        start_date = min(missing_by_date.keys()).replace("-", "")
        end_date = max(missing_by_date.keys()).replace("-", "")

        task_id = uuid.uuid4().hex
        started_at = now_cn().isoformat()
        total = len(unique_missing_symbols)
        self.store.set_sync_state(
            attempt_trade_date=target_trade_date,
            success_trade_date=state.get("last_success_trade_date"),
            status="running",
            symbol_count=0,
            total_symbols=total,
            synced_symbols=0,
            success_symbols=0,
            failed_symbols=0,
            task_id=task_id,
            trigger_mode=trigger_mode,
            updated_at=now_cn().isoformat(),
            message=f"补缺 {total_missing} 条({len(missing_by_date)}天×{total}股)",
        )
        self.store.start_sync_task(
            task_id=task_id,
            trigger_mode=trigger_mode,
            trade_date=target_trade_date,
            total_symbols=total,
            started_at=started_at,
        )
        result = await self._concurrent_fetch(
            symbols=unique_missing_symbols,
            start_date=start_date,
            end_date=end_date,
            task_id=task_id,
            target_trade_date=target_trade_date,
            state=state,
            trigger_mode=trigger_mode,
            total=total,
            label="补缺同步",
            force_remote=True,
        )
        completed, success_count, failed_count = result

        filled = self._count_filled(existing_pairs, missing_by_date)
        unfillable = total_missing - filled
        final_status = "success" if completed > 0 else "failed"
        if completed > 0:
            parts = [f"补缺同步完成(补入{filled}/{total_missing}条)"]
            if unfillable > 0 and filled == 0:
                parts = [f"补缺同步完成({total_missing}条缺失均为停牌/未上市)"]
            elif unfillable > 0:
                parts.append(f" {unfillable}条停牌/未上市无数据")
            final_msg = "".join(parts)
        else:
            final_msg = "补缺同步失败"
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
            "success_symbols": success_count,
            "failed_symbols": failed_count,
            "missing_total": total_missing,
            "missing_filled": filled,
            "missing_unfillable": unfillable,
            "elapsed_sec": round(pytime.time() - t0, 1),
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
            message=f"增量同步 {target_trade_date} 检查缺失中...",
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

        existing_pairs = self.store.get_existing_pairs([target_trade_date])
        missing_symbols = [s for s in symbols if (s, target_trade_date) not in existing_pairs]

        if not missing_symbols:
            self.store.set_sync_state(
                attempt_trade_date=target_trade_date,
                success_trade_date=target_trade_date,
                status="success",
                symbol_count=len(symbols),
                total_symbols=len(symbols),
                synced_symbols=0,
                success_symbols=0,
                failed_symbols=0,
                task_id=None,
                trigger_mode=trigger_mode,
                updated_at=now_cn().isoformat(),
                message=f"增量同步 {target_trade_date} 数据完整，无需同步",
            )
            return {
                "success": True,
                "message": f"{target_trade_date} 数据已完整({len(symbols)}只)，无需同步",
                "trade_date": target_trade_date,
                "symbol_count": len(symbols),
                "total_symbols": len(symbols),
                "synced_symbols": 0,
                "missing_filled": 0,
                "mode": "incremental",
            }

        td_fmt = target_trade_date.replace("-", "")
        task_id = uuid.uuid4().hex
        started_at = now_cn().isoformat()
        total = len(missing_symbols)
        self.store.set_sync_state(
            attempt_trade_date=target_trade_date,
            success_trade_date=state.get("last_success_trade_date"),
            status="running",
            symbol_count=0,
            total_symbols=total,
            synced_symbols=0,
            success_symbols=0,
            failed_symbols=0,
            task_id=task_id,
            trigger_mode=trigger_mode,
            updated_at=now_cn().isoformat(),
            message=f"增量补缺 {target_trade_date} 缺失{total}只(共{len(symbols)}只)",
        )
        self.store.start_sync_task(
            task_id=task_id,
            trigger_mode=trigger_mode,
            trade_date=target_trade_date,
            total_symbols=total,
            started_at=started_at,
        )
        completed = 0
        success_count = 0
        failed_count = 0
        fallback_symbols = missing_symbols

        latest_trade_date = await self._resolve_latest_trade_date(now_cn().date().isoformat())
        if target_trade_date == latest_trade_date:
            batch_result = await self._batch_fill_from_spot(
                symbols=missing_symbols,
                trade_date=target_trade_date,
                task_id=task_id,
                target_trade_date=target_trade_date,
                state=state,
                trigger_mode=trigger_mode,
                total=total,
            )
            completed += batch_result["completed"]
            success_count += batch_result["success_count"]
            fallback_symbols = batch_result["remaining_symbols"]

        if fallback_symbols:
            result = await self._concurrent_fetch(
                symbols=fallback_symbols,
                start_date=td_fmt,
                end_date=td_fmt,
                task_id=task_id,
                target_trade_date=target_trade_date,
                state=state,
                trigger_mode=trigger_mode,
                total=total,
                label="增量补缺",
                force_remote=True,
                initial_completed=completed,
                initial_success_count=success_count,
                initial_failed_count=failed_count,
            )
            completed, success_count, failed_count = result

        final_status = "success" if completed > 0 else "failed"
        final_msg = (
            f"增量补缺完成 {target_trade_date} 补缺{completed}只(原缺失{total}只)"
            if completed > 0 else f"增量同步失败 {target_trade_date}"
        )
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
            "missing_filled": completed,
            "mode": "incremental",
        }

    async def _batch_fill_from_spot(
        self,
        *,
        symbols: list[str],
        trade_date: str,
        task_id: str,
        target_trade_date: str,
        state: dict,
        trigger_mode: str,
        total: int,
    ) -> dict[str, Any]:
        t0 = pytime.time()
        wanted = set(symbols)
        try:
            snapshot = await self.market_data_client.fetch_spot()
        except Exception as exc:
            print(f"[kline-cache] batch spot fetch failed: {exc!r}")
            return {"completed": 0, "success_count": 0, "remaining_symbols": symbols}
        if snapshot is None or snapshot.empty or "代码" not in snapshot.columns:
            return {"completed": 0, "success_count": 0, "remaining_symbols": symbols}

        rows: list[tuple[str, dict[str, Any]]] = []
        valid_symbols: set[str] = set()
        for _, item in snapshot.iterrows():
            symbol = str(item.get("代码") or "").strip()
            if symbol not in wanted:
                continue
            close = to_float(item.get("最新价"))
            if close <= 0:
                continue
            open_price = to_float(item.get("今开")) or close
            high = to_float(item.get("最高")) or max(open_price, close)
            low = to_float(item.get("最低")) or min(open_price, close)
            rows.append(
                (
                    symbol,
                    {
                        "trade_date": trade_date,
                        "open": open_price,
                        "high": high,
                        "low": low,
                        "close": close,
                        "volume": to_float(item.get("成交量")),
                        "amount": to_float(item.get("成交额")),
                    },
                )
            )
            valid_symbols.add(symbol)

        if not rows:
            print("[kline-cache] batch spot returned no usable kline rows")
            return {"completed": 0, "success_count": 0, "remaining_symbols": symbols}

        now_iso = now_cn().isoformat()
        elapsed_ms = int((pytime.time() - t0) * 1000)
        completed = len(valid_symbols)
        self.store.record_sync_batch(
            kline_items=rows,
            detail_rows=[
                {
                    "task_id": task_id,
                    "symbol": symbol,
                    "status": "success",
                    "elapsed_ms": elapsed_ms,
                    "error_message": "eastmoney spot batch",
                    "created_at": now_iso,
                }
                for symbol in sorted(valid_symbols)
            ],
            updated_at=now_iso,
            task_id=task_id,
            synced_symbols=completed,
            success_symbols=completed,
            failed_symbols=0,
            attempt_trade_date=target_trade_date,
            success_trade_date=state.get("last_success_trade_date"),
            status="running",
            symbol_count=completed,
            total_symbols=total,
            trigger_mode=trigger_mode,
            message=f"批量行情补缺 {completed}/{total}",
        )
        remaining = [s for s in symbols if s not in valid_symbols]
        print(f"[kline-cache] batch spot filled {completed}/{total}, fallback={len(remaining)}")
        return {"completed": completed, "success_count": completed, "remaining_symbols": remaining}

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
        force_remote: bool = False,
        initial_completed: int = 0,
        initial_success_count: int = 0,
        initial_failed_count: int = 0,
    ) -> tuple[int, int, int]:
        sem = asyncio.Semaphore(_CONCURRENCY)
        completed = initial_completed
        success_count = initial_success_count
        failed_count = initial_failed_count
        async def _fetch_one(symbol: str) -> dict[str, Any]:
            error_message = ""
            async with sem:
                t0 = pytime.time()
                try:
                    hist = await self.market_data_client.fetch_hist(symbol, start_date, end_date, adjust="qfq")
                except Exception as exc:
                    hist = pd.DataFrame()
                    error_message = str(exc)[:200]
                rows = self._normalize_hist(hist)
                elapsed = int((pytime.time() - t0) * 1000)

            return {
                "symbol": symbol,
                "rows": rows,
                "elapsed_ms": elapsed,
                "error_message": error_message,
            }

        for i in range(0, len(symbols), _FETCH_BATCH_SIZE):
            batch = symbols[i : i + _FETCH_BATCH_SIZE]
            results = await asyncio.gather(*[_fetch_one(s) for s in batch])
            now_iso = now_cn().isoformat()
            kline_items: list[tuple[str, dict[str, Any]]] = []
            detail_rows: list[dict[str, Any]] = []
            for item in results:
                symbol = str(item["symbol"])
                rows = item["rows"]
                if rows:
                    completed += 1
                    success_count += 1
                    kline_items.extend((symbol, row) for row in rows)
                    status = "success"
                    error_message = ""
                else:
                    failed_count += 1
                    status = "failed"
                    error_message = item["error_message"] or "数据为空"
                detail_rows.append(
                    {
                        "task_id": task_id,
                        "symbol": symbol,
                        "status": status,
                        "elapsed_ms": int(item["elapsed_ms"] or 0),
                        "error_message": error_message,
                        "created_at": now_iso,
                    }
                )

            synced = success_count + failed_count
            self.store.record_sync_batch(
                kline_items=kline_items,
                detail_rows=detail_rows,
                updated_at=now_iso,
                task_id=task_id,
                synced_symbols=synced,
                success_symbols=success_count,
                failed_symbols=failed_count,
                attempt_trade_date=target_trade_date,
                success_trade_date=state.get("last_success_trade_date"),
                status="running",
                symbol_count=completed,
                total_symbols=total,
                trigger_mode=trigger_mode,
                message=f"{label} {synced}/{total}",
            )

        return completed, success_count, failed_count

    def get_kline(self, symbol: str, days: int = 30) -> list[dict[str, Any]]:
        clean_symbol = str(symbol).strip()
        return self.store.get_kline(clean_symbol, days)

    def get_sync_state(self) -> dict[str, Any]:
        state = self.store.get_sync_state()
        if state.get("status") == "running" and not self._has_active_work():
            self.store.fail_running_sync_tasks(
                finished_at=now_cn().isoformat(),
                message="上次同步中断，请重新提交同步任务",
            )
            self.store.set_sync_state(
                attempt_trade_date=state.get("last_attempt_trade_date") or "",
                success_trade_date=state.get("last_success_trade_date"),
                status="failed",
                symbol_count=int(state.get("symbol_count") or 0),
                total_symbols=int(state.get("total_symbols") or 0),
                synced_symbols=int(state.get("synced_symbols") or 0),
                success_symbols=int(state.get("success_symbols") or 0),
                failed_symbols=int(state.get("failed_symbols") or 0),
                task_id=state.get("task_id"),
                trigger_mode=state.get("trigger_mode") or "manual",
                updated_at=now_cn().isoformat(),
                message="上次同步中断，请重新提交同步任务",
            )
            state = self.store.get_sync_state()
        return state

    def _has_active_work(self) -> bool:
        return self._syncing or (self._queue_task is not None and not self._queue_task.done())

    def _fail_stale_running_tasks(self, message: str) -> None:
        state = self.store.get_sync_state()
        if state.get("status") != "running":
            return
        now_iso = now_cn().isoformat()
        self.store.fail_running_sync_tasks(finished_at=now_iso, message=message)
        self.store.set_sync_state(
            attempt_trade_date=state.get("last_attempt_trade_date") or "",
            success_trade_date=state.get("last_success_trade_date"),
            status="failed",
            symbol_count=int(state.get("symbol_count") or 0),
            total_symbols=int(state.get("total_symbols") or 0),
            synced_symbols=int(state.get("synced_symbols") or 0),
            success_symbols=int(state.get("success_symbols") or 0),
            failed_symbols=int(state.get("failed_symbols") or 0),
            task_id=state.get("task_id"),
            trigger_mode=state.get("trigger_mode") or "manual",
            updated_at=now_iso,
            message=message,
        )

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

    # ── 数据完整性检查 ─────────────────────────────────────

    async def check_data_integrity(self, days: int = 30) -> dict[str, Any]:
        """交叉比对交易日历×股票列表与实际DB记录，生成完整性报告。"""
        check_time = now_cn().isoformat()

        base_date = await self._resolve_latest_trade_date(now_cn().date().isoformat())
        if not base_date:
            return {"check_time": check_time, "status": "error", "message": "无法确定交易日"}

        trade_dates_list = await self._resolve_trade_dates(base_date, days)
        if not trade_dates_list:
            return {"check_time": check_time, "status": "error", "message": "交易日历为空"}

        symbols = await self._load_symbol_list()
        if not symbols:
            return {"check_time": check_time, "status": "error", "message": "股票列表为空"}

        symbol_set = set(symbols)
        total_expected = len(trade_dates_list) * len(symbols)
        existing_pairs = self.store.get_existing_pairs(trade_dates_list)
        total_actual = len(existing_pairs)
        total_missing = total_expected - total_actual

        missing_by_date: list[dict[str, Any]] = []
        symbol_miss_count: dict[str, int] = {}

        for td in trade_dates_list:
            missing_symbols = [s for s in symbols if (s, td) not in existing_pairs]
            if missing_symbols:
                missing_by_date.append({
                    "date": td,
                    "missing_count": len(missing_symbols),
                    "total": len(symbols),
                    "coverage_pct": round((1 - len(missing_symbols) / len(symbols)) * 100, 2),
                })
                for s in missing_symbols:
                    symbol_miss_count[s] = symbol_miss_count.get(s, 0) + 1

        worst_symbols = sorted(symbol_miss_count.items(), key=lambda x: -x[1])[:20]
        coverage_pct = round((total_actual / total_expected) * 100, 2) if total_expected > 0 else 100.0
        status = "complete" if total_missing == 0 else "incomplete"

        report: dict[str, Any] = {
            "check_time": check_time,
            "trade_days_checked": len(trade_dates_list),
            "total_symbols": len(symbols),
            "total_expected": total_expected,
            "total_actual": total_actual,
            "total_missing": total_missing,
            "coverage_pct": coverage_pct,
            "status": status,
            "missing_by_date": missing_by_date,
            "missing_dates_summary": [d["date"] for d in missing_by_date],
            "worst_symbols": [{"symbol": s, "missing_days": c} for s, c in worst_symbols],
        }

        self.store.save_check_report(report)
        return report

    def get_latest_check_report(self) -> dict[str, Any] | None:
        return self.store.get_latest_check_report()

    def get_stats(self) -> dict[str, Any]:
        return self.store.get_stats()

    # ── 日期工具 ─────────────────────────────────────────────

    async def _resolve_trade_dates(self, trade_date: str, days: int) -> list[str]:
        """返回截至 trade_date 最近 days 个交易日的 ISO 日期列表。"""
        trade_days = await self.market_data_client.fetch_trade_days(min_days=days)
        if trade_days.empty or "trade_date" not in trade_days.columns:
            return []
        dates = pd.to_datetime(trade_days["trade_date"], errors="coerce").dropna().dt.date
        target = pd.to_datetime(trade_date).date()
        selected = dates[dates <= target].tail(days)
        return [d.isoformat() for d in selected]

    async def _resolve_latest_trade_date(self, base_date: str) -> str:
        trade_days = await self.market_data_client.fetch_trade_days()
        if trade_days.empty or "trade_date" not in trade_days.columns:
            return ""
        days = pd.to_datetime(trade_days["trade_date"], errors="coerce").dropna().dt.date
        target = pd.to_datetime(base_date).date()
        valid = days[days <= target]
        if valid.empty:
            return ""
        return valid.iloc[-1].isoformat()

    async def _resolve_window(self, trade_date: str, days: int) -> tuple[str, str]:
        trade_days = await self.market_data_client.fetch_trade_days(min_days=days)
        if trade_days.empty or "trade_date" not in trade_days.columns:
            return "", ""
        dates = pd.to_datetime(trade_days["trade_date"], errors="coerce").dropna().dt.date
        target = pd.to_datetime(trade_date).date()
        selected = dates[dates <= target].tail(days)
        if len(selected) < days:
            return "", ""
        return selected.iloc[0].strftime("%Y%m%d"), selected.iloc[-1].strftime("%Y%m%d")

    async def _load_symbol_list(self) -> list[str]:
        """加载待同步 symbol 列表。

        权威源：全市场 symbol->name 映射（东财 spot，5300+）
        兜底：DB 已有 symbol（冷启动、全市场接口故障时用）
        过滤：主板/创业板/科创板（00/30/60/68 前缀）+ 剔除 ST/退市
        """
        symbols: set[str] = set()

        name_map: dict[str, str] = {}
        try:
            if self.provider.symbol_name_cache is not None:
                _, cached_names = self.provider.symbol_name_cache
                name_map = dict(cached_names or {})
        except Exception:
            name_map = {}

        if not name_map:
            try:
                name_map = self.store.load_symbol_names()
                if name_map:
                    self.provider.symbol_name_cache = (datetime.now(), dict(name_map))
            except Exception as exc:
                print(f"[kline_cache] load_symbol_names failed: {exc}")
                name_map = {}

        if not name_map:
            try:
                snapshot = await self.market_data_client.fetch_spot()
                if not snapshot.empty and "代码" in snapshot.columns and "名称" in snapshot.columns:
                    name_map = {
                        str(r["代码"]): str(r["名称"])
                        for _, r in snapshot.iterrows()
                        if r.get("代码") and r.get("名称")
                    }
                    if name_map:
                        self.provider.symbol_name_cache = (datetime.now(), dict(name_map))
                        self.store.upsert_symbol_names(name_map, now_cn().isoformat())
            except Exception as exc:
                print(f"[kline_cache] fetch symbol names from eastmoney failed: {exc}")
                name_map = {}

        for code, name in (name_map or {}).items():
            if not code or not code.startswith(("00", "30", "60", "68")):
                continue
            n = str(name or "").upper()
            if "ST" in n or "退" in n:
                continue
            symbols.add(code)

        # 合并 DB 已知 symbol（兜底 + 去除已退市）
        for s in self.store.get_all_symbols():
            if s and s.startswith(("00", "30", "60", "68")):
                symbols.add(s)

        return sorted(symbols)

    def _count_filled(
        self,
        old_existing: set[tuple[str, str]],
        missing_by_date: dict[str, list[str]],
    ) -> int:
        """补缺后复验：统计 missing_by_date 中实际被填上的条数。"""
        check_dates = sorted(missing_by_date.keys())
        new_existing = self.store.get_existing_pairs(check_dates)
        filled = 0
        for td, syms in missing_by_date.items():
            for s in syms:
                if (s, td) not in old_existing and (s, td) in new_existing:
                    filled += 1
        return filled

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
