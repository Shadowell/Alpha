from __future__ import annotations

import asyncio
from datetime import time
from typing import Any

import pandas as pd

from app.services.data_provider import AkshareDataProvider, to_float
from app.services.kline_store import KlineSQLiteStore
from app.services.time_utils import now_cn


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
        self.lock = asyncio.Lock()

    async def run_if_due(self) -> bool:
        now = now_cn()
        cur_time = now.timetz().replace(tzinfo=None)
        if cur_time < self.schedule_after:
            return False

        trade_date = self._resolve_latest_trade_date(now.date().isoformat())
        if not trade_date:
            return False

        state = self.store.get_sync_state()
        if state.get("last_success_trade_date") == trade_date:
            return False

        await self.sync_trade_date(trade_date=trade_date, force=True)
        return True

    async def sync_trade_date(self, trade_date: str | None = None, force: bool = False) -> dict[str, Any]:
        async with self.lock:
            target_trade_date = trade_date or self._resolve_latest_trade_date(now_cn().date().isoformat())
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
                updated_at=now_cn().isoformat(),
                message="开始同步",
            )

            symbols = self._load_symbol_list()
            if not symbols:
                self.store.set_sync_state(
                    attempt_trade_date=target_trade_date,
                    success_trade_date=state.get("last_success_trade_date"),
                    status="failed",
                    symbol_count=0,
                    updated_at=now_cn().isoformat(),
                    message="股票列表为空",
                )
                return {"success": False, "message": "股票列表为空", "trade_date": target_trade_date, "symbol_count": 0}

            start_date, end_date = self._resolve_window(target_trade_date, self.window_days)
            if not start_date or not end_date:
                self.store.set_sync_state(
                    attempt_trade_date=target_trade_date,
                    success_trade_date=state.get("last_success_trade_date"),
                    status="failed",
                    symbol_count=0,
                    updated_at=now_cn().isoformat(),
                    message="交易窗口计算失败",
                )
                return {"success": False, "message": "交易窗口计算失败", "trade_date": target_trade_date, "symbol_count": 0}

            completed = 0
            now_iso = now_cn().isoformat()
            for symbol in symbols:
                hist = self.provider.get_hist(symbol, start_date, end_date)
                rows = self._normalize_hist(hist)
                if rows:
                    self.store.upsert_symbol_klines(symbol, rows, now_iso)
                    completed += 1

            self.store.set_sync_state(
                attempt_trade_date=target_trade_date,
                success_trade_date=target_trade_date,
                status="success",
                symbol_count=completed,
                updated_at=now_cn().isoformat(),
                message="同步完成",
            )
            return {
                "success": True,
                "message": "同步完成",
                "trade_date": target_trade_date,
                "symbol_count": completed,
            }

    def get_kline(self, symbol: str, days: int = 30) -> list[dict[str, Any]]:
        clean_symbol = str(symbol).strip()
        return self.store.get_kline(clean_symbol, days)

    def get_sync_state(self) -> dict[str, Any]:
        return self.store.get_sync_state()

    def _resolve_latest_trade_date(self, base_date: str) -> str:
        trade_days = self.provider.get_trade_days()
        if trade_days.empty or "trade_date" not in trade_days.columns:
            return ""
        days = pd.to_datetime(trade_days["trade_date"], errors="coerce").dropna().dt.date
        target = pd.to_datetime(base_date).date()
        valid = days[days <= target]
        if valid.empty:
            return ""
        return valid.iloc[-1].isoformat()

    def _resolve_window(self, trade_date: str, days: int) -> tuple[str, str]:
        trade_days = self.provider.get_trade_days()
        if trade_days.empty or "trade_date" not in trade_days.columns:
            return "", ""
        dates = pd.to_datetime(trade_days["trade_date"], errors="coerce").dropna().dt.date
        target = pd.to_datetime(trade_date).date()
        selected = dates[dates <= target].tail(days)
        if len(selected) < days:
            return "", ""
        return selected.iloc[0].strftime("%Y%m%d"), selected.iloc[-1].strftime("%Y%m%d")

    def _load_symbol_list(self) -> list[str]:
        snapshot = self.provider.get_realtime_snapshot()
        if snapshot.empty:
            return []

        symbols: list[str] = []
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
