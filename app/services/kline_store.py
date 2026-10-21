from __future__ import annotations

import json
import math
import os
import sqlite3
from pathlib import Path
from typing import Any

from app.services.sqlite_migrations import MARKET_KLINE_DB_MIGRATIONS, migrate_database


class KlineSQLiteStore:
    def __init__(self, db_path: str = "data/market_kline.db") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # Create/upgrade the schema once at startup; every query below uses a
        # short-lived connection without DDL.
        migrate_database(self.db_path, MARKET_KLINE_DB_MIGRATIONS)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=1.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=1000")
        return conn

    def upsert_symbol_klines(
        self, symbol: str, rows: list[dict[str, Any]], updated_at: str
    ) -> int:
        if not rows:
            return 0

        with self._connect() as conn:
            payload = [
                (
                    symbol,
                    str(r.get("trade_date", "")),
                    float(r.get("open", 0.0)),
                    float(r.get("high", 0.0)),
                    float(r.get("low", 0.0)),
                    float(r.get("close", 0.0)),
                    float(r.get("volume", 0.0)),
                    float(r.get("amount", 0.0)),
                    str(r.get("source") or "unknown"),
                    str(r.get("fallback_reason") or ""),
                    updated_at,
                )
                for r in rows
                if str(r.get("trade_date", ""))
            ]
            conn.executemany(
                """
                INSERT INTO kline_daily(
                    symbol, trade_date, open, high, low, close, volume, amount,
                    source, fallback_reason, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, trade_date) DO UPDATE SET
                    open=excluded.open,
                    high=excluded.high,
                    low=excluded.low,
                    close=excluded.close,
                    volume=excluded.volume,
                    amount=excluded.amount,
                    source=excluded.source,
                    fallback_reason=excluded.fallback_reason,
                    updated_at=excluded.updated_at
                """,
                payload,
            )
            conn.commit()
            return len(payload)

    def upsert_many_klines(
        self, items: list[tuple[str, dict[str, Any]]], updated_at: str
    ) -> int:
        if not items:
            return 0

        payload = [
            (
                str(symbol),
                str(row.get("trade_date", "")),
                float(row.get("open", 0.0)),
                float(row.get("high", 0.0)),
                float(row.get("low", 0.0)),
                float(row.get("close", 0.0)),
                float(row.get("volume", 0.0)),
                float(row.get("amount", 0.0)),
                str(row.get("source") or "unknown"),
                str(row.get("fallback_reason") or ""),
                updated_at,
            )
            for symbol, row in items
            if str(symbol) and str(row.get("trade_date", ""))
        ]
        if not payload:
            return 0

        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO kline_daily(
                    symbol, trade_date, open, high, low, close, volume, amount,
                    source, fallback_reason, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, trade_date) DO UPDATE SET
                    open=excluded.open,
                    high=excluded.high,
                    low=excluded.low,
                    close=excluded.close,
                    volume=excluded.volume,
                    amount=excluded.amount,
                    source=excluded.source,
                    fallback_reason=excluded.fallback_reason,
                    updated_at=excluded.updated_at
                """,
                payload,
            )
            conn.commit()
            return len(payload)

    def get_kline(self, symbol: str, days: int = 30) -> list[dict[str, Any]]:
        query_days = max(1, min(days, 365))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT trade_date, open, high, low, close, volume, amount, source, fallback_reason
                FROM kline_daily
                WHERE symbol = ?
                ORDER BY trade_date DESC
                LIMIT ?
                """,
                (symbol, query_days),
            ).fetchall()

        items = []
        for row in reversed(rows):
            items.append(
                {
                    "date": row["trade_date"],
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": float(row["volume"]),
                    "amount": float(row["amount"]),
                    "source": str(row["source"] or "unknown"),
                    "fallback_reason": str(row["fallback_reason"] or ""),
                }
            )
        return items

    def set_sync_state(
        self,
        *,
        attempt_trade_date: str,
        success_trade_date: str | None,
        status: str,
        symbol_count: int,
        total_symbols: int = 0,
        synced_symbols: int = 0,
        success_symbols: int = 0,
        failed_symbols: int = 0,
        task_id: str | None = None,
        trigger_mode: str = "auto",
        updated_at: str,
        message: str = "",
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO kline_sync_state (
                    id, last_attempt_trade_date, last_success_trade_date, status, symbol_count,
                    total_symbols, synced_symbols, success_symbols, failed_symbols, task_id, trigger_mode,
                    updated_at, message
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    last_attempt_trade_date=excluded.last_attempt_trade_date,
                    last_success_trade_date=excluded.last_success_trade_date,
                    status=excluded.status,
                    symbol_count=excluded.symbol_count,
                    total_symbols=excluded.total_symbols,
                    synced_symbols=excluded.synced_symbols,
                    success_symbols=excluded.success_symbols,
                    failed_symbols=excluded.failed_symbols,
                    task_id=excluded.task_id,
                    trigger_mode=excluded.trigger_mode,
                    updated_at=excluded.updated_at,
                    message=excluded.message
                """,
                (
                    attempt_trade_date,
                    success_trade_date,
                    status,
                    symbol_count,
                    total_symbols,
                    synced_symbols,
                    success_symbols,
                    failed_symbols,
                    task_id,
                    trigger_mode,
                    updated_at,
                    message,
                ),
            )
            conn.commit()

    def start_sync_task(
        self,
        *,
        task_id: str,
        trigger_mode: str,
        trade_date: str,
        total_symbols: int,
        started_at: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO kline_sync_tasks(
                    task_id, trigger_mode, trade_date, status, total_symbols, synced_symbols, success_symbols, failed_symbols,
                    started_at, finished_at, message
                ) VALUES (?, ?, ?, 'running', ?, 0, 0, 0, ?, NULL, '开始同步')
                """,
                (task_id, trigger_mode, trade_date, total_symbols, started_at),
            )
            conn.commit()

    def update_sync_task_progress(
        self,
        *,
        task_id: str,
        synced_symbols: int,
        success_symbols: int,
        failed_symbols: int,
        message: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE kline_sync_tasks
                SET synced_symbols = ?, success_symbols = ?, failed_symbols = ?, message = ?
                WHERE task_id = ?
                """,
                (synced_symbols, success_symbols, failed_symbols, message, task_id),
            )
            conn.commit()

    def finish_sync_task(
        self, *, task_id: str, status: str, finished_at: str, message: str
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE kline_sync_tasks
                SET status = ?, finished_at = ?, message = ?
                WHERE task_id = ?
                """,
                (status, finished_at, message, task_id),
            )
            conn.commit()

    def add_sync_task_detail(
        self,
        *,
        task_id: str,
        symbol: str,
        status: str,
        elapsed_ms: int,
        error_message: str,
        data_source: str = "",
        fallback_reason: str = "",
        created_at: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO kline_sync_task_details(
                    task_id, symbol, status, elapsed_ms, error_message,
                    data_source, fallback_reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    symbol,
                    status,
                    elapsed_ms,
                    error_message,
                    data_source,
                    fallback_reason,
                    created_at,
                ),
            )
            conn.commit()

    def add_sync_task_details(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        payload = [
            (
                str(r.get("task_id", "")),
                str(r.get("symbol", "")),
                str(r.get("status", "")),
                int(r.get("elapsed_ms", 0)),
                str(r.get("error_message", "")),
                str(r.get("data_source", "")),
                str(r.get("fallback_reason", "")),
                str(r.get("created_at", "")),
            )
            for r in rows
            if r.get("task_id") and r.get("symbol") and r.get("status")
        ]
        if not payload:
            return
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO kline_sync_task_details(
                    task_id, symbol, status, elapsed_ms, error_message,
                    data_source, fallback_reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                payload,
            )
            conn.commit()

    def record_sync_batch(
        self,
        *,
        kline_items: list[tuple[str, dict[str, Any]]],
        detail_rows: list[dict[str, Any]],
        updated_at: str,
        task_id: str,
        synced_symbols: int,
        success_symbols: int,
        failed_symbols: int,
        attempt_trade_date: str,
        success_trade_date: str | None,
        status: str,
        symbol_count: int,
        total_symbols: int,
        trigger_mode: str,
        message: str,
    ) -> int:
        """Persist one sync batch in a single transaction."""
        kline_payload = [
            (
                str(symbol),
                str(row.get("trade_date", "")),
                float(row.get("open", 0.0)),
                float(row.get("high", 0.0)),
                float(row.get("low", 0.0)),
                float(row.get("close", 0.0)),
                float(row.get("volume", 0.0)),
                float(row.get("amount", 0.0)),
                str(row.get("source") or "unknown"),
                str(row.get("fallback_reason") or ""),
                updated_at,
            )
            for symbol, row in kline_items
            if str(symbol) and str(row.get("trade_date", ""))
        ]
        detail_payload = [
            (
                str(r.get("task_id", "")),
                str(r.get("symbol", "")),
                str(r.get("status", "")),
                int(r.get("elapsed_ms", 0)),
                str(r.get("error_message", "")),
                str(r.get("data_source", "")),
                str(r.get("fallback_reason", "")),
                str(r.get("created_at", "")),
            )
            for r in detail_rows
            if r.get("task_id") and r.get("symbol") and r.get("status")
        ]

        with self._connect() as conn:
            if kline_payload:
                conn.executemany(
                    """
                    INSERT INTO kline_daily(
                        symbol, trade_date, open, high, low, close, volume, amount,
                        source, fallback_reason, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(symbol, trade_date) DO UPDATE SET
                        open=excluded.open,
                        high=excluded.high,
                        low=excluded.low,
                        close=excluded.close,
                        volume=excluded.volume,
                        amount=excluded.amount,
                        source=excluded.source,
                        fallback_reason=excluded.fallback_reason,
                        updated_at=excluded.updated_at
                    """,
                    kline_payload,
                )
            if detail_payload:
                conn.executemany(
                    """
                    INSERT INTO kline_sync_task_details(
                        task_id, symbol, status, elapsed_ms, error_message,
                        data_source, fallback_reason, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    detail_payload,
                )
            conn.execute(
                """
                UPDATE kline_sync_tasks
                SET synced_symbols = ?, success_symbols = ?, failed_symbols = ?, message = ?
                WHERE task_id = ?
                """,
                (synced_symbols, success_symbols, failed_symbols, message, task_id),
            )
            conn.execute(
                """
                INSERT INTO kline_sync_state (
                    id, last_attempt_trade_date, last_success_trade_date, status, symbol_count,
                    total_symbols, synced_symbols, success_symbols, failed_symbols, task_id, trigger_mode,
                    updated_at, message
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    last_attempt_trade_date=excluded.last_attempt_trade_date,
                    last_success_trade_date=excluded.last_success_trade_date,
                    status=excluded.status,
                    symbol_count=excluded.symbol_count,
                    total_symbols=excluded.total_symbols,
                    synced_symbols=excluded.synced_symbols,
                    success_symbols=excluded.success_symbols,
                    failed_symbols=excluded.failed_symbols,
                    task_id=excluded.task_id,
                    trigger_mode=excluded.trigger_mode,
                    updated_at=excluded.updated_at,
                    message=excluded.message
                """,
                (
                    attempt_trade_date,
                    success_trade_date,
                    status,
                    symbol_count,
                    total_symbols,
                    synced_symbols,
                    success_symbols,
                    failed_symbols,
                    task_id,
                    trigger_mode,
                    updated_at,
                    message,
                ),
            )
            conn.commit()
        return len(kline_payload)

    def fail_running_sync_tasks(
        self, *, finished_at: str, message: str, task_id: str | None = None
    ) -> int:
        with self._connect() as conn:
            if task_id:
                cur = conn.execute(
                    """
                    UPDATE kline_sync_tasks
                    SET status = 'failed', finished_at = ?, message = ?
                    WHERE status = 'running' AND task_id = ?
                    """,
                    (finished_at, message, task_id),
                )
            else:
                cur = conn.execute(
                    """
                    UPDATE kline_sync_tasks
                    SET status = 'failed', finished_at = ?, message = ?
                    WHERE status = 'running'
                    """,
                    (finished_at, message),
                )
            conn.commit()
            return int(cur.rowcount or 0)

    def get_sync_state(self) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM kline_sync_state WHERE id = 1").fetchone()
            if row is None:
                return {
                    "last_attempt_trade_date": None,
                    "last_success_trade_date": None,
                    "status": "idle",
                    "symbol_count": 0,
                    "total_symbols": 0,
                    "synced_symbols": 0,
                    "success_symbols": 0,
                    "failed_symbols": 0,
                    "task_id": None,
                    "trigger_mode": "auto",
                    "updated_at": "",
                    "message": "",
                }
            total_symbols = int(row["total_symbols"] or 0)
            synced_symbols = int(row["synced_symbols"] or 0)
            return {
                "last_attempt_trade_date": row["last_attempt_trade_date"],
                "last_success_trade_date": row["last_success_trade_date"],
                "status": row["status"],
                "symbol_count": int(row["symbol_count"]),
                "total_symbols": total_symbols,
                "synced_symbols": synced_symbols,
                "success_symbols": int(row["success_symbols"] or 0),
                "failed_symbols": int(row["failed_symbols"] or 0),
                "task_id": row["task_id"],
                "trigger_mode": row["trigger_mode"] or "auto",
                "progress_pct": round((synced_symbols / total_symbols) * 100, 2)
                if total_symbols > 0
                else 0.0,
                "updated_at": row["updated_at"],
                "message": row["message"],
            }

    def list_sync_tasks(self, page: int = 1, page_size: int = 20) -> dict[str, Any]:
        safe_page = max(1, int(page))
        safe_size = max(1, min(int(page_size), 200))
        offset = (safe_page - 1) * safe_size
        with self._connect() as conn:
            total = conn.execute(
                "SELECT COUNT(*) AS c FROM kline_sync_tasks"
            ).fetchone()["c"]
            rows = conn.execute(
                """
                SELECT task_id, trigger_mode, trade_date, status, total_symbols, synced_symbols, success_symbols, failed_symbols,
                       started_at, finished_at, message
                FROM kline_sync_tasks
                ORDER BY started_at DESC
                LIMIT ? OFFSET ?
                """,
                (safe_size, offset),
            ).fetchall()
        items = [
            {
                "task_id": row["task_id"],
                "trigger_mode": row["trigger_mode"],
                "trade_date": row["trade_date"],
                "status": row["status"],
                "total_symbols": int(row["total_symbols"] or 0),
                "synced_symbols": int(row["synced_symbols"] or 0),
                "success_symbols": int(row["success_symbols"] or 0),
                "failed_symbols": int(row["failed_symbols"] or 0),
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
                "message": row["message"],
            }
            for row in rows
        ]
        return {
            "page": safe_page,
            "page_size": safe_size,
            "total": int(total),
            "items": items,
        }

    def get_sync_task_detail(self, task_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            task = conn.execute(
                """
                SELECT task_id, trigger_mode, trade_date, status, total_symbols, synced_symbols, success_symbols, failed_symbols,
                       started_at, finished_at, message
                FROM kline_sync_tasks
                WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()
            if task is None:
                return None
            details = conn.execute(
                """
                SELECT symbol, status, elapsed_ms, error_message, created_at
                FROM kline_sync_task_details
                WHERE task_id = ?
                ORDER BY id ASC
                """,
                (task_id,),
            ).fetchall()
        return {
            "task": {
                "task_id": task["task_id"],
                "trigger_mode": task["trigger_mode"],
                "trade_date": task["trade_date"],
                "status": task["status"],
                "total_symbols": int(task["total_symbols"] or 0),
                "synced_symbols": int(task["synced_symbols"] or 0),
                "success_symbols": int(task["success_symbols"] or 0),
                "failed_symbols": int(task["failed_symbols"] or 0),
                "started_at": task["started_at"],
                "finished_at": task["finished_at"],
                "message": task["message"],
            },
            "items": [
                {
                    "symbol": row["symbol"],
                    "status": row["status"],
                    "elapsed_ms": int(row["elapsed_ms"] or 0),
                    "error_message": row["error_message"] or "",
                    "created_at": row["created_at"],
                }
                for row in details
            ],
        }

    # ── 日频增强 / 资金流 / 指数 / 分钟线 ──────────────────

    @staticmethod
    def _optional_float(row: dict[str, Any], key: str) -> float | None:
        value = row.get(key)
        if value is None:
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return None if math.isnan(number) else number

    def upsert_daily_extras(self, rows: list[dict[str, Any]], updated_at: str) -> int:
        payload = [
            (
                str(row.get("symbol", "")),
                str(row.get("trade_date", "")),
                self._optional_float(row, "pe_ttm"),
                self._optional_float(row, "pb"),
                self._optional_float(row, "ps_ttm"),
                self._optional_float(row, "turnover_rate"),
                self._optional_float(row, "turnover_rate_f"),
                self._optional_float(row, "volume_ratio"),
                self._optional_float(row, "total_mv"),
                self._optional_float(row, "circ_mv"),
                self._optional_float(row, "up_limit"),
                self._optional_float(row, "down_limit"),
                self._optional_float(row, "adj_factor"),
                updated_at,
            )
            for row in rows
            if str(row.get("symbol", "")) and str(row.get("trade_date", ""))
        ]
        if not payload:
            return 0
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO kline_daily_extras(
                    symbol, trade_date, pe_ttm, pb, ps_ttm, turnover_rate,
                    turnover_rate_f, volume_ratio, total_mv, circ_mv,
                    up_limit, down_limit, adj_factor, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, trade_date) DO UPDATE SET
                    pe_ttm=COALESCE(excluded.pe_ttm, kline_daily_extras.pe_ttm),
                    pb=COALESCE(excluded.pb, kline_daily_extras.pb),
                    ps_ttm=COALESCE(excluded.ps_ttm, kline_daily_extras.ps_ttm),
                    turnover_rate=COALESCE(excluded.turnover_rate, kline_daily_extras.turnover_rate),
                    turnover_rate_f=COALESCE(excluded.turnover_rate_f, kline_daily_extras.turnover_rate_f),
                    volume_ratio=COALESCE(excluded.volume_ratio, kline_daily_extras.volume_ratio),
                    total_mv=COALESCE(excluded.total_mv, kline_daily_extras.total_mv),
                    circ_mv=COALESCE(excluded.circ_mv, kline_daily_extras.circ_mv),
                    up_limit=COALESCE(excluded.up_limit, kline_daily_extras.up_limit),
                    down_limit=COALESCE(excluded.down_limit, kline_daily_extras.down_limit),
                    adj_factor=COALESCE(excluded.adj_factor, kline_daily_extras.adj_factor),
                    updated_at=excluded.updated_at
                """,
                payload,
            )
            conn.commit()
            return len(payload)

    def upsert_moneyflow(self, rows: list[dict[str, Any]], updated_at: str) -> int:
        fields = [
            "buy_sm_amount", "sell_sm_amount", "buy_md_amount", "sell_md_amount",
            "buy_lg_amount", "sell_lg_amount", "buy_elg_amount", "sell_elg_amount",
            "net_mf_amount",
        ]
        payload = [
            (
                str(row.get("symbol", "")),
                str(row.get("trade_date", "")),
                *[self._optional_float(row, field) for field in fields],
                updated_at,
            )
            for row in rows
            if str(row.get("symbol", "")) and str(row.get("trade_date", ""))
        ]
        if not payload:
            return 0
        with self._connect() as conn:
            conn.executemany(
                f"""
                INSERT INTO moneyflow_daily(
                    symbol, trade_date, {", ".join(fields)}, updated_at
                ) VALUES ({", ".join(["?"] * (len(fields) + 3))})
                ON CONFLICT(symbol, trade_date) DO UPDATE SET
                    {", ".join(f"{field}=excluded.{field}" for field in fields)},
                    updated_at=excluded.updated_at
                """,
                payload,
            )
            conn.commit()
            return len(payload)

    def upsert_market_capital_flow(self, rows: list[dict[str, Any]], updated_at: str) -> int:
        payload = [
            (
                str(row.get("trade_date", "")),
                self._optional_float(row, "hgt"),
                self._optional_float(row, "sgt"),
                self._optional_float(row, "north_money"),
                self._optional_float(row, "south_money"),
                updated_at,
            )
            for row in rows
            if str(row.get("trade_date", ""))
        ]
        if not payload:
            return 0
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO market_capital_flow(
                    trade_date, hgt, sgt, north_money, south_money, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(trade_date) DO UPDATE SET
                    hgt=excluded.hgt,
                    sgt=excluded.sgt,
                    north_money=excluded.north_money,
                    south_money=excluded.south_money,
                    updated_at=excluded.updated_at
                """,
                payload,
            )
            conn.commit()
            return len(payload)

    def upsert_index_daily(self, rows: list[dict[str, Any]], updated_at: str) -> int:
        payload = [
            (
                str(row.get("ts_code", "")),
                str(row.get("trade_date", "")),
                self._optional_float(row, "open"),
                self._optional_float(row, "high"),
                self._optional_float(row, "low"),
                self._optional_float(row, "close"),
                self._optional_float(row, "pre_close"),
                self._optional_float(row, "change"),
                self._optional_float(row, "pct_chg"),
                self._optional_float(row, "volume"),
                self._optional_float(row, "amount"),
                updated_at,
            )
            for row in rows
            if str(row.get("ts_code", "")) and str(row.get("trade_date", ""))
        ]
        if not payload:
            return 0
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO index_daily(
                    ts_code, trade_date, open, high, low, close, pre_close,
                    change, pct_chg, volume, amount, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ts_code, trade_date) DO UPDATE SET
                    open=excluded.open,
                    high=excluded.high,
                    low=excluded.low,
                    close=excluded.close,
                    pre_close=excluded.pre_close,
                    change=excluded.change,
                    pct_chg=excluded.pct_chg,
                    volume=excluded.volume,
                    amount=excluded.amount,
                    updated_at=excluded.updated_at
                """,
                payload,
            )
            conn.commit()
            return len(payload)

    def upsert_minute_bars(self, rows: list[dict[str, Any]], updated_at: str) -> int:
        payload = [
            (
                str(row.get("symbol", "")),
                str(row.get("bar_time", "")),
                str(row.get("trade_date", "")),
                self._optional_float(row, "open"),
                self._optional_float(row, "high"),
                self._optional_float(row, "low"),
                self._optional_float(row, "close"),
                self._optional_float(row, "volume"),
                self._optional_float(row, "amount"),
                str(row.get("source") or "tushare.rt_min"),
                updated_at,
            )
            for row in rows
            if str(row.get("symbol", "")) and str(row.get("bar_time", ""))
        ]
        if not payload:
            return 0
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO kline_minute(
                    symbol, bar_time, trade_date, open, high, low, close,
                    volume, amount, source, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, bar_time) DO UPDATE SET
                    trade_date=excluded.trade_date,
                    open=excluded.open,
                    high=excluded.high,
                    low=excluded.low,
                    close=excluded.close,
                    volume=excluded.volume,
                    amount=excluded.amount,
                    source=excluded.source,
                    updated_at=excluded.updated_at
                """,
                payload,
            )
            conn.commit()
            return len(payload)

    def set_enrichment_state(
        self,
        dataset: str,
        *,
        trade_date: str,
        status: str,
        row_count: int,
        message: str,
        updated_at: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO enrichment_sync_state(
                    dataset, trade_date, status, row_count, message, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(dataset) DO UPDATE SET
                    trade_date=excluded.trade_date,
                    status=excluded.status,
                    row_count=excluded.row_count,
                    message=excluded.message,
                    updated_at=excluded.updated_at
                """,
                (dataset, trade_date, status, int(row_count), message, updated_at),
            )
            conn.commit()

    def get_daily_extras(self, symbol: str, days: int = 30) -> list[dict[str, Any]]:
        query_days = max(1, min(days, 365))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT trade_date, pe_ttm, pb, ps_ttm, turnover_rate, turnover_rate_f,
                       volume_ratio, total_mv, circ_mv, up_limit, down_limit, adj_factor
                FROM kline_daily_extras
                WHERE symbol = ?
                ORDER BY trade_date DESC
                LIMIT ?
                """,
                (symbol, query_days),
            ).fetchall()
        return [
            {
                "trade_date": row["trade_date"],
                "pe_ttm": row["pe_ttm"],
                "pb": row["pb"],
                "ps_ttm": row["ps_ttm"],
                "turnover_rate": row["turnover_rate"],
                "turnover_rate_f": row["turnover_rate_f"],
                "volume_ratio": row["volume_ratio"],
                "total_mv": row["total_mv"],
                "circ_mv": row["circ_mv"],
                "up_limit": row["up_limit"],
                "down_limit": row["down_limit"],
                "adj_factor": row["adj_factor"],
            }
            for row in reversed(rows)
        ]

    def get_moneyflow(self, symbol: str, days: int = 30) -> list[dict[str, Any]]:
        query_days = max(1, min(days, 365))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM moneyflow_daily
                WHERE symbol = ?
                ORDER BY trade_date DESC
                LIMIT ?
                """,
                (symbol, query_days),
            ).fetchall()
        return [
            {
                "trade_date": row["trade_date"],
                "buy_sm_amount": row["buy_sm_amount"],
                "sell_sm_amount": row["sell_sm_amount"],
                "buy_md_amount": row["buy_md_amount"],
                "sell_md_amount": row["sell_md_amount"],
                "buy_lg_amount": row["buy_lg_amount"],
                "sell_lg_amount": row["sell_lg_amount"],
                "buy_elg_amount": row["buy_elg_amount"],
                "sell_elg_amount": row["sell_elg_amount"],
                "net_mf_amount": row["net_mf_amount"],
            }
            for row in reversed(rows)
        ]

    def get_market_capital_flow(self, days: int = 30) -> list[dict[str, Any]]:
        query_days = max(1, min(days, 365))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT trade_date, hgt, sgt, north_money, south_money
                FROM market_capital_flow
                ORDER BY trade_date DESC
                LIMIT ?
                """,
                (query_days,),
            ).fetchall()
        return [
            {
                "trade_date": row["trade_date"],
                "hgt": row["hgt"],
                "sgt": row["sgt"],
                "north_money": row["north_money"],
                "south_money": row["south_money"],
            }
            for row in reversed(rows)
        ]

    def get_index_daily(self, ts_code: str, days: int = 30) -> list[dict[str, Any]]:
        query_days = max(1, min(days, 365))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT trade_date, open, high, low, close, pre_close, change,
                       pct_chg, volume, amount
                FROM index_daily
                WHERE ts_code = ?
                ORDER BY trade_date DESC
                LIMIT ?
                """,
                (ts_code, query_days),
            ).fetchall()
        return [
            {
                "trade_date": row["trade_date"],
                "open": row["open"],
                "high": row["high"],
                "low": row["low"],
                "close": row["close"],
                "pre_close": row["pre_close"],
                "change": row["change"],
                "pct_chg": row["pct_chg"],
                "volume": row["volume"],
                "amount": row["amount"],
            }
            for row in reversed(rows)
        ]

    def get_minute_bars(
        self,
        symbol: str,
        trade_date: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        query_limit = max(1, min(limit, 2000))
        with self._connect() as conn:
            if trade_date:
                rows = conn.execute(
                    """
                    SELECT bar_time, trade_date, open, high, low, close, volume, amount, source
                    FROM kline_minute
                    WHERE symbol = ? AND trade_date = ?
                    ORDER BY bar_time DESC
                    LIMIT ?
                    """,
                    (symbol, trade_date, query_limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT bar_time, trade_date, open, high, low, close, volume, amount, source
                    FROM kline_minute
                    WHERE symbol = ?
                    ORDER BY bar_time DESC
                    LIMIT ?
                    """,
                    (symbol, query_limit),
                ).fetchall()
        return [
            {
                "bar_time": row["bar_time"],
                "trade_date": row["trade_date"],
                "open": row["open"],
                "high": row["high"],
                "low": row["low"],
                "close": row["close"],
                "volume": row["volume"],
                "amount": row["amount"],
                "source": row["source"],
            }
            for row in reversed(rows)
        ]

    def get_enrichment_overview(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            states = {
                str(row["dataset"]): dict(row)
                for row in conn.execute("SELECT * FROM enrichment_sync_state").fetchall()
            }
            table_stats = {
                "daily_extras": conn.execute(
                    "SELECT MAX(trade_date) AS latest_date, COUNT(*) AS total FROM kline_daily_extras"
                ).fetchone(),
                "moneyflow": conn.execute(
                    "SELECT MAX(trade_date) AS latest_date, COUNT(*) AS total FROM moneyflow_daily"
                ).fetchone(),
                "hsgt": conn.execute(
                    "SELECT MAX(trade_date) AS latest_date, COUNT(*) AS total FROM market_capital_flow"
                ).fetchone(),
                "index_daily": conn.execute(
                    "SELECT MAX(trade_date) AS latest_date, COUNT(*) AS total FROM index_daily"
                ).fetchone(),
            }
            minute_row = conn.execute(
                "SELECT MAX(trade_date) AS latest_date, MAX(bar_time) AS latest_bar, COUNT(*) AS total FROM kline_minute"
            ).fetchone()

        labels = {
            "daily_extras": "日频增强",
            "moneyflow": "个股资金流",
            "hsgt": "北向资金",
            "index_daily": "基准指数",
            "minute": "买入池分钟线",
        }
        overview: list[dict[str, Any]] = []
        for dataset, label in labels.items():
            state = states.get(dataset, {})
            if dataset == "minute":
                stats_row = minute_row
                latest_date = stats_row["latest_date"] if stats_row else ""
                extra = {"latest_bar_time": stats_row["latest_bar"] if stats_row else ""}
            else:
                stats_row = table_stats.get(dataset)
                latest_date = stats_row["latest_date"] if stats_row else ""
                extra = {}
            overview.append(
                {
                    "dataset": dataset,
                    "label": label,
                    "trade_date": state.get("trade_date") or "",
                    "status": state.get("status") or "none",
                    "row_count": int(state.get("row_count") or 0),
                    "message": state.get("message") or "",
                    "updated_at": state.get("updated_at") or "",
                    "latest_trade_date": latest_date or "",
                    "total_rows": int(stats_row["total"] or 0) if stats_row else 0,
                    **extra,
                }
            )
        return overview

    # ── 数据统计 ─────────────────────────────────────────────

    def get_stats(self) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(DISTINCT symbol) AS symbol_count,
                    COUNT(*) AS row_count,
                    MIN(trade_date) AS min_date,
                    MAX(trade_date) AS max_date
                FROM kline_daily
                """
            ).fetchone()
        db_size_bytes = 0
        try:
            db_size_bytes = os.path.getsize(str(self.db_path))
        except OSError:
            pass
        return {
            "symbol_count": int(row["symbol_count"] or 0),
            "row_count": int(row["row_count"] or 0),
            "min_date": row["min_date"] or "",
            "max_date": row["max_date"] or "",
            "db_size_bytes": db_size_bytes,
            "db_size_mb": round(db_size_bytes / (1024 * 1024), 2),
        }

    def get_trade_dates_from_db(self, limit: int = 3000) -> list[str]:
        """从 kline_daily 提取所有不重复的交易日（升序）。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT trade_date FROM kline_daily ORDER BY trade_date ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return [r["trade_date"] for r in rows]

    def get_all_symbols(self) -> list[str]:
        """从 kline_daily 提取所有不重复的 symbol。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT symbol FROM kline_daily ORDER BY symbol ASC"
            ).fetchall()
        return [r["symbol"] for r in rows]

    def get_latest_snapshot(
        self, trade_date: str | None = None
    ) -> list[dict[str, Any]]:
        """从 kline_daily 构建最近交易日的类行情快照，用于替代实时接口。"""
        with self._connect() as conn:
            if not trade_date:
                row = conn.execute(
                    "SELECT MAX(trade_date) AS d FROM kline_daily"
                ).fetchone()
                trade_date = row["d"] if row and row["d"] else ""
            if not trade_date:
                return []

            rows = conn.execute(
                """
                WITH latest AS (
                    SELECT symbol, trade_date, open, high, low, close, volume, amount
                    FROM kline_daily WHERE trade_date = ?
                ),
                prev AS (
                    SELECT k.symbol, k.close AS prev_close
                    FROM kline_daily k
                    INNER JOIN (
                        SELECT symbol, MAX(trade_date) AS td
                        FROM kline_daily WHERE trade_date < ?
                        GROUP BY symbol
                    ) p ON k.symbol = p.symbol AND k.trade_date = p.td
                )
                SELECT l.symbol, l.open, l.high, l.low, l.close, l.volume, l.amount,
                       COALESCE(p.prev_close, l.close) AS prev_close
                FROM latest l
                LEFT JOIN prev p ON l.symbol = p.symbol
                """,
                (trade_date, trade_date),
            ).fetchall()

        return [
            {
                "symbol": r["symbol"],
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": float(r["volume"]),
                "amount": float(r["amount"]),
                "prev_close": float(r["prev_close"]),
                "trade_date": trade_date,
            }
            for r in rows
        ]

    def upsert_symbol_names(self, name_map: dict[str, str], updated_at: str) -> int:
        """批量写入股票代码到名称的映射（幂等）。返回写入条数。"""
        if not name_map:
            return 0
        payload = [
            (str(code), str(name), updated_at)
            for code, name in name_map.items()
            if code and name
        ]
        if not payload:
            return 0
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO symbol_names(symbol, name, updated_at)
                VALUES(?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    name = excluded.name,
                    updated_at = excluded.updated_at
                """,
                payload,
            )
            conn.commit()
        return len(payload)

    def load_symbol_names(self) -> dict[str, str]:
        """读取全部 symbol->name 映射。"""
        with self._connect() as conn:
            rows = conn.execute("SELECT symbol, name FROM symbol_names").fetchall()
        return {r["symbol"]: r["name"] for r in rows}

    def get_existing_pairs(self, trade_dates: list[str]) -> set[tuple[str, str]]:
        """Return set of (symbol, trade_date) that already exist in kline_daily."""
        if not trade_dates:
            return set()
        with self._connect() as conn:
            placeholders = ",".join("?" for _ in trade_dates)
            rows = conn.execute(
                f"SELECT symbol, trade_date FROM kline_daily WHERE trade_date IN ({placeholders})",
                trade_dates,
            ).fetchall()
        return {(r["symbol"], r["trade_date"]) for r in rows}

    # ── 检查报告持久化 ───────────────────────────────────────

    def save_check_report(self, report: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO kline_check_reports(check_time, report_json) VALUES (?, ?)",
                (report.get("check_time", ""), json.dumps(report, ensure_ascii=False)),
            )
            conn.execute(
                "DELETE FROM kline_check_reports WHERE id NOT IN (SELECT id FROM kline_check_reports ORDER BY id DESC LIMIT 50)"
            )
            conn.commit()

    def get_latest_check_report(self) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT report_json FROM kline_check_reports ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["report_json"])
        except (json.JSONDecodeError, TypeError):
            return None
