from __future__ import annotations

import json
import math
import os
import sqlite3
from pathlib import Path
from typing import Any

from app.services.sqlite_migrations import MARKET_KLINE_DB_MIGRATIONS, migrate_database


class KlineSQLiteStore:
    """market_kline.db 读写入口。

    ``_OVERVIEW_QUERIES`` 是增强/打板数据集的状态看板清单：同步调度
    （``_retry_pending_enrichment``）与只读 API 都以此为唯一真相源，
    新增数据集只需要在这里加一行 + 写一条 ``enrichment_sync_state``。
    """

    _OVERVIEW_QUERIES: tuple[tuple[str, str, str], ...] = (
        (
            "daily_extras",
            "日频增强",
            "SELECT MAX(trade_date) AS latest_date, COUNT(*) AS total FROM kline_daily_extras",
        ),
        (
            "moneyflow",
            "个股资金流",
            "SELECT MAX(trade_date) AS latest_date, COUNT(*) AS total FROM moneyflow_daily",
        ),
        (
            "hsgt",
            "北向资金",
            "SELECT MAX(trade_date) AS latest_date, COUNT(*) AS total FROM market_capital_flow",
        ),
        (
            "index_daily",
            "基准指数",
            "SELECT MAX(trade_date) AS latest_date, COUNT(*) AS total FROM index_daily",
        ),
        (
            "limit_board",
            "涨停/跌停/炸板",
            "SELECT MAX(trade_date) AS latest_date, COUNT(*) AS total FROM limit_board_daily",
        ),
        (
            "limit_board_theme",
            "涨停题材/板型",
            "SELECT MAX(trade_date) AS latest_date, COUNT(*) AS total "
            "FROM limit_board_daily WHERE theme IS NOT NULL",
        ),
        (
            "limit_ladder",
            "连板天梯",
            "SELECT MAX(trade_date) AS latest_date, COUNT(*) AS total FROM limit_ladder_daily",
        ),
        (
            "dragon_tiger",
            "龙虎榜",
            "SELECT MAX(trade_date) AS latest_date, COUNT(*) AS total FROM dragon_tiger_daily",
        ),
        (
            "dragon_tiger_seat",
            "龙虎榜席位",
            "SELECT MAX(trade_date) AS latest_date, COUNT(*) AS total FROM dragon_tiger_seat_daily",
        ),
        (
            "hot_money_seat",
            "游资席位",
            "SELECT MAX(last_trade_date) AS latest_date, COUNT(*) AS total FROM hot_money_seat",
        ),
        (
            "hsgt_top10",
            "北向十大成交股",
            "SELECT MAX(trade_date) AS latest_date, COUNT(*) AS total FROM hsgt_top10_daily",
        ),
        (
            "minute",
            "买入池分钟线",
            "SELECT MAX(trade_date) AS latest_date, COUNT(*) AS total, "
            "MAX(bar_time) AS latest_bar FROM kline_minute",
        ),
    )

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
            "buy_sm_amount",
            "sell_sm_amount",
            "buy_md_amount",
            "sell_md_amount",
            "buy_lg_amount",
            "sell_lg_amount",
            "buy_elg_amount",
            "sell_elg_amount",
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

    def upsert_market_capital_flow(
        self, rows: list[dict[str, Any]], updated_at: str
    ) -> int:
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
                for row in conn.execute(
                    "SELECT * FROM enrichment_sync_state"
                ).fetchall()
            }
            stats = {
                dataset: conn.execute(sql).fetchone()
                for dataset, _, sql in self._OVERVIEW_QUERIES
            }

        overview: list[dict[str, Any]] = []
        for dataset, label, _sql in self._OVERVIEW_QUERIES:
            state = states.get(dataset, {})
            row = stats.get(dataset)
            latest_date = str(row["latest_date"] or "") if row is not None else ""
            item: dict[str, Any] = {
                "dataset": dataset,
                "label": label,
                "trade_date": state.get("trade_date") or "",
                "status": state.get("status") or "none",
                "row_count": int(state.get("row_count") or 0),
                "message": state.get("message") or "",
                "updated_at": state.get("updated_at") or "",
                "latest_trade_date": latest_date,
                "total_rows": int(row["total"] or 0) if row is not None else 0,
            }
            if dataset == "minute" and row is not None:
                item["latest_bar_time"] = str(row["latest_bar"] or "")
            overview.append(item)
        return overview

    # ── 打板核心数据底座（issue #22）──────────────────────────

    @staticmethod
    def _optional_text(row: dict[str, Any], key: str) -> str | None:
        value = row.get(key)
        if value is None:
            return None
        text = str(value).strip()
        if not text or text.lower() in {"nan", "none", "nat"}:
            return None
        return text

    @staticmethod
    def _optional_int(row: dict[str, Any], key: str) -> int | None:
        number = KlineSQLiteStore._optional_float(row, key)
        return None if number is None else int(round(number))

    def upsert_limit_board_daily(
        self, rows: list[dict[str, Any]], synced_at: str
    ) -> int:
        """涨停/跌停/炸板主表；``(symbol, trade_date)`` 幂等 upsert。

        金额单位统一为元；``None`` 表示源数据缺失，重复同步时用 COALESCE
        保留已有值，避免后到的空响应把已定型的封板质量字段擦掉。
        """
        text_fields = [
            "ts_code",
            "name",
            "industry",
            "limit_flag",
            "first_time",
            "last_time",
            "up_stat",
        ]
        int_fields = ["open_times", "limit_times"]
        num_fields = [
            "fd_amount",
            "amount",
            "limit_amount",
            "turnover_ratio",
            "float_mv",
            "total_mv",
            "close",
            "pct_chg",
        ]
        payload = []
        for row in rows:
            symbol = str(row.get("symbol") or "").strip()
            trade_date = str(row.get("trade_date") or "").strip()
            if not symbol or not trade_date:
                continue
            payload.append(
                (
                    symbol,
                    trade_date,
                    self._optional_text(row, "ts_code"),
                    self._optional_text(row, "name"),
                    self._optional_text(row, "industry"),
                    # 源字段名是 Tushare 的 ``limit``；列名为 ``limit_flag``（保留字）。
                    self._optional_text(row, "limit")
                    or self._optional_text(row, "limit_flag"),
                    self._optional_text(row, "first_time"),
                    self._optional_text(row, "last_time"),
                    self._optional_int(row, "open_times"),
                    self._optional_int(row, "limit_times"),
                    self._optional_text(row, "up_stat"),
                    *[self._optional_float(row, field) for field in num_fields],
                    str(row.get("source") or "tushare.limit_list_d"),
                    synced_at,
                )
            )
        if not payload:
            return 0
        column_list = [
            "symbol",
            "trade_date",
            "ts_code",
            "name",
            "industry",
            "limit_flag",
            "first_time",
            "last_time",
            "open_times",
            "limit_times",
            "up_stat",
            *num_fields,
            "source",
            "synced_at",
        ]
        columns = ", ".join(column_list)
        marks = ", ".join("?" for _ in column_list)
        updates = ", ".join(
            f"{field}=COALESCE(excluded.{field}, limit_board_daily.{field})"
            for field in [
                *text_fields,
                *int_fields,
                *num_fields,
                "source",
            ]
        )
        with self._connect() as conn:
            conn.executemany(
                f"""
                INSERT INTO limit_board_daily({columns})
                VALUES ({marks})
                ON CONFLICT(symbol, trade_date) DO UPDATE SET
                    {updates},
                    synced_at=excluded.synced_at
                """,
                payload,
            )
            conn.commit()
        return len(payload)

    def update_limit_board_theme(
        self, rows: list[dict[str, Any]], synced_at: str
    ) -> int:
        """用 ``kpl_list`` 回填板型/题材/涨停原因；只更新已存在的行。

        返回被更新的行数（未命中的行不写入，避免造出只有题材没有封板质量的数据）。
        """
        payload = [
            (
                self._optional_text(row, "board_type"),
                self._optional_text(row, "theme"),
                self._optional_text(row, "limit_reason"),
                synced_at,
                str(row.get("symbol") or "").strip(),
                str(row.get("trade_date") or "").strip(),
            )
            for row in rows
            if str(row.get("symbol") or "").strip()
            and str(row.get("trade_date") or "").strip()
        ]
        if not payload:
            return 0
        with self._connect() as conn:
            cur = conn.executemany(
                """
                UPDATE limit_board_daily
                SET board_type = COALESCE(?, board_type),
                    theme = COALESCE(?, theme),
                    limit_reason = COALESCE(?, limit_reason),
                    synced_at = ?
                WHERE symbol = ? AND trade_date = ?
                """,
                payload,
            )
            conn.commit()
        return int(cur.rowcount or 0)

    def upsert_limit_ladder(self, rows: list[dict[str, Any]], synced_at: str) -> int:
        payload = [
            (
                str(row.get("trade_date") or "").strip(),
                self._optional_int(row, "ladder_level"),
                int(row.get("stock_count") or 0),
                json.dumps(row.get("symbols") or [], ensure_ascii=False),
                self._optional_float(row, "fd_amount_sum"),
                self._optional_float(row, "amount_sum"),
                self._optional_float(row, "avg_open_times"),
                self._optional_int(row, "prev_level_count"),
                self._optional_float(row, "promotion_ratio"),
                str(row.get("source") or "limit_list_d"),
                synced_at,
            )
            for row in rows
            if str(row.get("trade_date") or "").strip()
            and self._optional_int(row, "ladder_level") is not None
        ]
        if not payload:
            return 0
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO limit_ladder_daily(
                    trade_date, ladder_level, stock_count, symbols_json,
                    fd_amount_sum, amount_sum, avg_open_times,
                    prev_level_count, promotion_ratio, source, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trade_date, ladder_level) DO UPDATE SET
                    stock_count=excluded.stock_count,
                    symbols_json=excluded.symbols_json,
                    fd_amount_sum=COALESCE(excluded.fd_amount_sum, limit_ladder_daily.fd_amount_sum),
                    amount_sum=COALESCE(excluded.amount_sum, limit_ladder_daily.amount_sum),
                    avg_open_times=COALESCE(excluded.avg_open_times, limit_ladder_daily.avg_open_times),
                    prev_level_count=COALESCE(excluded.prev_level_count, limit_ladder_daily.prev_level_count),
                    promotion_ratio=COALESCE(excluded.promotion_ratio, limit_ladder_daily.promotion_ratio),
                    source=excluded.source,
                    synced_at=excluded.synced_at
                """,
                payload,
            )
            conn.commit()
        return len(payload)

    def upsert_dragon_tiger(self, rows: list[dict[str, Any]], synced_at: str) -> int:
        numeric = [
            "close",
            "pct_change",
            "turnover_rate",
            "amount",
            "l_buy",
            "l_sell",
            "l_amount",
            "net_amount",
            "net_rate",
            "amount_rate",
            "float_values",
        ]
        payload = []
        for row in rows:
            symbol = str(row.get("symbol") or "").strip()
            trade_date = str(row.get("trade_date") or "").strip()
            if not symbol or not trade_date:
                continue
            payload.append(
                (
                    symbol,
                    trade_date,
                    str(row.get("reason") or "").strip(),
                    self._optional_text(row, "ts_code"),
                    self._optional_text(row, "name"),
                    *[self._optional_float(row, field) for field in numeric],
                    str(row.get("source") or "tushare.top_list"),
                    synced_at,
                )
            )
        if not payload:
            return 0
        tiger_columns = [
            "symbol",
            "trade_date",
            "reason",
            "ts_code",
            "name",
            *numeric,
            "source",
            "synced_at",
        ]
        columns = ", ".join(tiger_columns)
        marks = ", ".join("?" for _ in tiger_columns)
        updates = ", ".join(
            [
                f"{field}=COALESCE(excluded.{field}, dragon_tiger_daily.{field})"
                for field in ["ts_code", "name", *numeric]
            ]
            + ["source=excluded.source"]
        )
        with self._connect() as conn:
            conn.executemany(
                f"""
                INSERT INTO dragon_tiger_daily({columns})
                VALUES ({marks})
                ON CONFLICT(symbol, trade_date, reason) DO UPDATE SET
                    {updates},
                    synced_at=excluded.synced_at
                """,
                payload,
            )
            conn.commit()
        return len(payload)

    def upsert_dragon_tiger_seats(
        self, rows: list[dict[str, Any]], synced_at: str
    ) -> int:
        """营业部席位；同 ``(date, symbol, seat, side, reason)`` 多席位聚合。"""
        numeric = ["buy_amount", "buy_rate", "sell_amount", "sell_rate", "net_amount"]
        payload = []
        for row in rows:
            symbol = str(row.get("symbol") or "").strip()
            trade_date = str(row.get("trade_date") or "").strip()
            seat_name = str(row.get("seat_name") or "").strip()
            side = str(row.get("side") or "").strip()
            if not symbol or not trade_date or not seat_name or not side:
                continue
            payload.append(
                (
                    trade_date,
                    symbol,
                    seat_name,
                    side,
                    str(row.get("reason") or "").strip(),
                    self._optional_text(row, "ts_code"),
                    self._optional_text(row, "stock_name"),
                    *[self._optional_float(row, field) for field in numeric],
                    int(row.get("seat_count") or 1),
                    str(row.get("source") or "tushare.top_inst"),
                    synced_at,
                )
            )
        if not payload:
            return 0
        seat_columns = [
            "trade_date",
            "symbol",
            "seat_name",
            "side",
            "reason",
            "ts_code",
            "stock_name",
            *numeric,
            "seat_count",
            "source",
            "synced_at",
        ]
        columns = ", ".join(seat_columns)
        marks = ", ".join("?" for _ in seat_columns)
        updates = ", ".join(
            [
                f"{field}=COALESCE(excluded.{field}, dragon_tiger_seat_daily.{field})"
                for field in ["ts_code", "stock_name", *numeric]
            ]
            + ["seat_count=excluded.seat_count", "source=excluded.source"]
        )
        with self._connect() as conn:
            conn.executemany(
                f"""
                INSERT INTO dragon_tiger_seat_daily({columns})
                VALUES ({marks})
                ON CONFLICT(trade_date, symbol, seat_name, side, reason) DO UPDATE SET
                    {updates},
                    synced_at=excluded.synced_at
                """,
                payload,
            )
            conn.commit()
        return len(payload)

    def upsert_hot_money_seats(self, rows: list[dict[str, Any]], synced_at: str) -> int:
        payload = []
        for row in rows:
            seat_name = str(row.get("seat_name") or "").strip()
            if not seat_name:
                continue
            payload.append(
                (
                    seat_name,
                    str(row.get("org_code") or "").strip(),
                    self._optional_text(row, "category"),
                    self._optional_text(row, "description"),
                    json.dumps(row.get("orgs") or [], ensure_ascii=False),
                    int(row.get("appear_count") or 0),
                    self._optional_text(row, "last_trade_date"),
                    str(row.get("source") or "tushare.hm_list"),
                    synced_at,
                )
            )
        if not payload:
            return 0
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO hot_money_seat(
                    seat_name, org_code, category, description, orgs_json,
                    appear_count, last_trade_date, source, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(seat_name, org_code) DO UPDATE SET
                    category=COALESCE(excluded.category, hot_money_seat.category),
                    description=COALESCE(excluded.description, hot_money_seat.description),
                    orgs_json=excluded.orgs_json,
                    appear_count=CASE WHEN excluded.appear_count > 0
                        THEN excluded.appear_count ELSE hot_money_seat.appear_count END,
                    last_trade_date=COALESCE(excluded.last_trade_date, hot_money_seat.last_trade_date),
                    source=excluded.source,
                    synced_at=excluded.synced_at
                """,
                payload,
            )
            conn.commit()
        return len(payload)

    def upsert_hsgt_top10(self, rows: list[dict[str, Any]], synced_at: str) -> int:
        numeric = [
            "close",
            "change",
            "amount",
            "net_amount",
            "buy_amount",
            "sell_amount",
        ]
        payload = []
        for row in rows:
            symbol = str(row.get("symbol") or "").strip()
            trade_date = str(row.get("trade_date") or "").strip()
            market_type = str(row.get("market_type") or "").strip()
            if not symbol or not trade_date or not market_type:
                continue
            payload.append(
                (
                    trade_date,
                    symbol,
                    market_type,
                    self._optional_text(row, "ts_code"),
                    self._optional_text(row, "name"),
                    self._optional_int(row, "rank_no"),
                    *[self._optional_float(row, field) for field in numeric],
                    str(row.get("source") or "tushare.hsgt_top10"),
                    synced_at,
                )
            )
        if not payload:
            return 0
        hsgt_columns = [
            "trade_date",
            "symbol",
            "market_type",
            "ts_code",
            "name",
            "rank_no",
            *numeric,
            "source",
            "synced_at",
        ]
        columns = ", ".join(hsgt_columns)
        marks = ", ".join("?" for _ in hsgt_columns)
        updates = ", ".join(
            [
                f"{field}=COALESCE(excluded.{field}, hsgt_top10_daily.{field})"
                for field in ["ts_code", "name", "rank_no", *numeric]
            ]
            + ["source=excluded.source"]
        )
        with self._connect() as conn:
            conn.executemany(
                f"""
                INSERT INTO hsgt_top10_daily({columns})
                VALUES ({marks})
                ON CONFLICT(trade_date, symbol, market_type) DO UPDATE SET
                    {updates},
                    synced_at=excluded.synced_at
                """,
                payload,
            )
            conn.commit()
        return len(payload)

    def get_limit_board_daily(
        self,
        trade_date: str,
        *,
        board: str | None = None,
        min_limit_times: int | None = None,
        limit: int = 300,
    ) -> list[dict[str, Any]]:
        query_limit = max(1, min(int(limit), 3000))
        sql = "SELECT * FROM limit_board_daily WHERE trade_date = ?"
        params: list[Any] = [trade_date]
        if board:
            sql += " AND limit_flag = ?"
            params.append(board)
        if min_limit_times is not None:
            sql += " AND limit_times >= ?"
            params.append(int(min_limit_times))
        sql += f" ORDER BY COALESCE(fd_amount, 0) DESC LIMIT {query_limit}"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def get_limit_ladder(self, trade_date: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT trade_date, ladder_level, stock_count, symbols_json,
                       fd_amount_sum, amount_sum, avg_open_times,
                       prev_level_count, promotion_ratio, source, synced_at
                FROM limit_ladder_daily WHERE trade_date = ?
                ORDER BY ladder_level DESC
                """,
                (trade_date,),
            ).fetchall()
        items = []
        for row in rows:
            try:
                symbols = json.loads(row["symbols_json"] or "[]")
            except (TypeError, json.JSONDecodeError):
                symbols = []
            items.append(
                {
                    "trade_date": row["trade_date"],
                    "ladder_level": int(row["ladder_level"]),
                    "stock_count": int(row["stock_count"] or 0),
                    "symbols": symbols,
                    "fd_amount_sum": row["fd_amount_sum"],
                    "amount_sum": row["amount_sum"],
                    "avg_open_times": row["avg_open_times"],
                    "prev_level_count": row["prev_level_count"],
                    "promotion_ratio": row["promotion_ratio"],
                    "source": row["source"],
                    "synced_at": row["synced_at"],
                }
            )
        return items

    def get_dragon_tiger(
        self, trade_date: str, symbol: str | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM dragon_tiger_daily WHERE trade_date = ?"
        params: list[Any] = [trade_date]
        if symbol:
            sql += " AND symbol = ?"
            params.append(symbol)
        sql += " ORDER BY COALESCE(net_amount, 0) DESC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def get_dragon_tiger_seats(
        self,
        trade_date: str,
        symbol: str | None = None,
        *,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        query_limit = max(1, min(int(limit), 3000))
        sql = "SELECT * FROM dragon_tiger_seat_daily WHERE trade_date = ?"
        params: list[Any] = [trade_date]
        if symbol:
            sql += " AND symbol = ?"
            params.append(symbol)
        sql += f" ORDER BY symbol ASC, COALESCE(net_amount, 0) DESC LIMIT {query_limit}"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def get_hot_money_seats(
        self, keyword: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        query_limit = max(1, min(int(limit), 1000))
        sql = "SELECT * FROM hot_money_seat"
        params: list[Any] = []
        if keyword:
            sql += " WHERE seat_name LIKE ? OR description LIKE ?"
            like = f"%{keyword}%"
            params.extend([like, like])
        sql += f" ORDER BY appear_count DESC, seat_name ASC LIMIT {query_limit}"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        items = []
        for row in rows:
            try:
                orgs = json.loads(row["orgs_json"] or "[]")
            except (TypeError, json.JSONDecodeError):
                orgs = []
            item = dict(row)
            item["orgs"] = orgs
            item.pop("orgs_json", None)
            items.append(item)
        return items

    def get_hsgt_top10(self, trade_date: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM hsgt_top10_daily WHERE trade_date = ? "
                "ORDER BY market_type ASC, COALESCE(rank_no, 0) ASC",
                (trade_date,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_daily_with_limits(self, trade_date: str) -> list[dict[str, Any]]:
        """日线 + 涨跌停价，供 ``limit_list_d`` 无权限时的自算降级。

        只返回当条记录，不做任何涨停判定（判定集中在服务层，便于单测）。
        """
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT k.symbol,
                       k.trade_date,
                       k.open,
                       k.high,
                       k.low,
                       k.close,
                       k.volume,
                       k.amount,
                       e.up_limit,
                       e.down_limit,
                       e.circ_mv,
                       e.total_mv,
                       e.turnover_rate
                FROM kline_daily k
                LEFT JOIN kline_daily_extras e
                    ON e.symbol = k.symbol AND e.trade_date = k.trade_date
                WHERE k.trade_date = ?
                """,
                (trade_date,),
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_board_trade_date(self) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(trade_date) AS d FROM limit_board_daily"
            ).fetchone()
        return str(row["d"]) if row and row["d"] else ""

    def previous_board_trade_date(self, trade_date: str) -> str:
        """严格早于 ``trade_date`` 的最近一个已有涨停板数据的交易日。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(trade_date) AS d FROM limit_board_daily WHERE trade_date < ?",
                (trade_date,),
            ).fetchone()
        return str(row["d"]) if row and row["d"] else ""

    def get_limit_board_coverage(self, trade_date: str) -> dict[str, Any]:
        """覆盖率统计：行数、涨停/跌停/炸板分布与关键字段非空率。

        ``non_null_ratio`` 以当日全部打板记录为分母；``up_non_null_ratio``
        只以涨停（U）记录为分母 —— issue #22 的验收口径是后者
        （``limit_times``/``first_time``/``open_times``/``fd_amount`` 非空率 ≥ 90%）。
        """
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN limit_flag = 'U' THEN 1 ELSE 0 END) AS up_count,
                    SUM(CASE WHEN limit_flag = 'D' THEN 1 ELSE 0 END) AS down_count,
                    SUM(CASE WHEN limit_flag = 'Z' THEN 1 ELSE 0 END) AS broken_count,
                    COUNT(first_time) AS first_time_rows,
                    COUNT(last_time) AS last_time_rows,
                    COUNT(open_times) AS open_times_rows,
                    COUNT(limit_times) AS limit_times_rows,
                    COUNT(fd_amount) AS fd_amount_rows,
                    COUNT(up_stat) AS up_stat_rows,
                    COUNT(theme) AS theme_rows,
                    MAX(synced_at) AS synced_at
                FROM limit_board_daily WHERE trade_date = ?
                """,
                (trade_date,),
            ).fetchone()
            up_row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    COUNT(first_time) AS first_time_rows,
                    COUNT(open_times) AS open_times_rows,
                    COUNT(limit_times) AS limit_times_rows,
                    COUNT(fd_amount) AS fd_amount_rows
                FROM limit_board_daily WHERE trade_date = ? AND limit_flag = 'U'
                """,
                (trade_date,),
            ).fetchone()
        total = int(row["total"] or 0)
        up_total = int(up_row["total"] or 0)

        def _ratio(key: str, base: int) -> float | None:
            if not base:
                return None
            return round(int(row[key] or 0) / base, 4)

        return {
            "trade_date": trade_date,
            "total_rows": total,
            "up_count": int(row["up_count"] or 0),
            "down_count": int(row["down_count"] or 0),
            "broken_count": int(row["broken_count"] or 0),
            "non_null_ratio": {
                "first_time": _ratio("first_time_rows", total),
                "last_time": _ratio("last_time_rows", total),
                "open_times": _ratio("open_times_rows", total),
                "limit_times": _ratio("limit_times_rows", total),
                "fd_amount": _ratio("fd_amount_rows", total),
                "up_stat": _ratio("up_stat_rows", total),
                "theme": _ratio("theme_rows", total),
            },
            "up_non_null_ratio": {
                "first_time": (
                    round(int(up_row["first_time_rows"] or 0) / up_total, 4)
                    if up_total
                    else None
                ),
                "open_times": (
                    round(int(up_row["open_times_rows"] or 0) / up_total, 4)
                    if up_total
                    else None
                ),
                "limit_times": (
                    round(int(up_row["limit_times_rows"] or 0) / up_total, 4)
                    if up_total
                    else None
                ),
                "fd_amount": (
                    round(int(up_row["fd_amount_rows"] or 0) / up_total, 4)
                    if up_total
                    else None
                ),
            },
            "synced_at": row["synced_at"] or "",
        }

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
