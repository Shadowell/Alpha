from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any


class KlineSQLiteStore:
    def __init__(self, db_path: str = "data/market_kline.db") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS kline_daily (
                symbol TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume REAL NOT NULL,
                amount REAL NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (symbol, trade_date)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS kline_sync_state (
                id INTEGER PRIMARY KEY CHECK(id = 1),
                last_attempt_trade_date TEXT,
                last_success_trade_date TEXT,
                status TEXT NOT NULL,
                symbol_count INTEGER NOT NULL DEFAULT 0,
                total_symbols INTEGER NOT NULL DEFAULT 0,
                synced_symbols INTEGER NOT NULL DEFAULT 0,
                success_symbols INTEGER NOT NULL DEFAULT 0,
                failed_symbols INTEGER NOT NULL DEFAULT 0,
                task_id TEXT,
                trigger_mode TEXT NOT NULL DEFAULT 'auto',
                updated_at TEXT NOT NULL,
                message TEXT NOT NULL DEFAULT ''
            )
            """
        )
        self._ensure_column(conn, "kline_sync_state", "total_symbols", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column(conn, "kline_sync_state", "synced_symbols", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column(conn, "kline_sync_state", "success_symbols", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column(conn, "kline_sync_state", "failed_symbols", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column(conn, "kline_sync_state", "task_id", "TEXT")
        self._ensure_column(conn, "kline_sync_state", "trigger_mode", "TEXT NOT NULL DEFAULT 'auto'")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS kline_sync_tasks (
                task_id TEXT PRIMARY KEY,
                trigger_mode TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                status TEXT NOT NULL,
                total_symbols INTEGER NOT NULL DEFAULT 0,
                synced_symbols INTEGER NOT NULL DEFAULT 0,
                success_symbols INTEGER NOT NULL DEFAULT 0,
                failed_symbols INTEGER NOT NULL DEFAULT 0,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                message TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS kline_sync_task_details (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                status TEXT NOT NULL,
                elapsed_ms INTEGER NOT NULL DEFAULT 0,
                error_message TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_kline_sync_tasks_started_at ON kline_sync_tasks(started_at DESC)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_kline_sync_task_details_task_id ON kline_sync_task_details(task_id, id DESC)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_kline_symbol_date ON kline_daily(symbol, trade_date DESC)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS kline_check_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                check_time TEXT NOT NULL,
                report_json TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_check_reports_time ON kline_check_reports(check_time DESC)")
        conn.commit()

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table_name: str, col_name: str, col_def: str) -> None:
        cols = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        existing = {str(row[1]) for row in cols}
        if col_name not in existing:
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_def}")

    def upsert_symbol_klines(self, symbol: str, rows: list[dict[str, Any]], updated_at: str) -> int:
        if not rows:
            return 0

        with self._connect() as conn:
            self._init_schema(conn)
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
                    updated_at,
                )
                for r in rows
                if str(r.get("trade_date", ""))
            ]
            conn.executemany(
                """
                INSERT INTO kline_daily(symbol, trade_date, open, high, low, close, volume, amount, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, trade_date) DO UPDATE SET
                    open=excluded.open,
                    high=excluded.high,
                    low=excluded.low,
                    close=excluded.close,
                    volume=excluded.volume,
                    amount=excluded.amount,
                    updated_at=excluded.updated_at
                """,
                payload,
            )
            conn.commit()
            return len(payload)

    def get_kline(self, symbol: str, days: int = 30) -> list[dict[str, Any]]:
        query_days = max(1, min(days, 365))
        with self._connect() as conn:
            self._init_schema(conn)
            rows = conn.execute(
                """
                SELECT trade_date, open, high, low, close, volume, amount
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
            self._init_schema(conn)
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

    def start_sync_task(self, *, task_id: str, trigger_mode: str, trade_date: str, total_symbols: int, started_at: str) -> None:
        with self._connect() as conn:
            self._init_schema(conn)
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
            self._init_schema(conn)
            conn.execute(
                """
                UPDATE kline_sync_tasks
                SET synced_symbols = ?, success_symbols = ?, failed_symbols = ?, message = ?
                WHERE task_id = ?
                """,
                (synced_symbols, success_symbols, failed_symbols, message, task_id),
            )
            conn.commit()

    def finish_sync_task(self, *, task_id: str, status: str, finished_at: str, message: str) -> None:
        with self._connect() as conn:
            self._init_schema(conn)
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
        created_at: str,
    ) -> None:
        with self._connect() as conn:
            self._init_schema(conn)
            conn.execute(
                """
                INSERT INTO kline_sync_task_details(task_id, symbol, status, elapsed_ms, error_message, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (task_id, symbol, status, elapsed_ms, error_message, created_at),
            )
            conn.commit()

    def get_sync_state(self) -> dict[str, Any]:
        with self._connect() as conn:
            self._init_schema(conn)
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
                "progress_pct": round((synced_symbols / total_symbols) * 100, 2) if total_symbols > 0 else 0.0,
                "updated_at": row["updated_at"],
                "message": row["message"],
            }

    def list_sync_tasks(self, page: int = 1, page_size: int = 20) -> dict[str, Any]:
        safe_page = max(1, int(page))
        safe_size = max(1, min(int(page_size), 200))
        offset = (safe_page - 1) * safe_size
        with self._connect() as conn:
            self._init_schema(conn)
            total = conn.execute("SELECT COUNT(*) AS c FROM kline_sync_tasks").fetchone()["c"]
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
        return {"page": safe_page, "page_size": safe_size, "total": int(total), "items": items}

    def get_sync_task_detail(self, task_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            self._init_schema(conn)
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

    # ── 数据统计 ─────────────────────────────────────────────

    def get_stats(self) -> dict[str, Any]:
        with self._connect() as conn:
            self._init_schema(conn)
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
            self._init_schema(conn)
            rows = conn.execute(
                "SELECT DISTINCT trade_date FROM kline_daily ORDER BY trade_date ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return [r["trade_date"] for r in rows]

    def get_all_symbols(self) -> list[str]:
        """从 kline_daily 提取所有不重复的 symbol。"""
        with self._connect() as conn:
            self._init_schema(conn)
            rows = conn.execute(
                "SELECT DISTINCT symbol FROM kline_daily ORDER BY symbol ASC"
            ).fetchall()
        return [r["symbol"] for r in rows]

    def get_latest_snapshot(self, trade_date: str | None = None) -> list[dict[str, Any]]:
        """从 kline_daily 构建最近交易日的类行情快照，用于替代实时接口。"""
        with self._connect() as conn:
            self._init_schema(conn)
            if not trade_date:
                row = conn.execute("SELECT MAX(trade_date) AS d FROM kline_daily").fetchone()
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

    def get_existing_pairs(self, trade_dates: list[str]) -> set[tuple[str, str]]:
        """Return set of (symbol, trade_date) that already exist in kline_daily."""
        if not trade_dates:
            return set()
        with self._connect() as conn:
            self._init_schema(conn)
            placeholders = ",".join("?" for _ in trade_dates)
            rows = conn.execute(
                f"SELECT symbol, trade_date FROM kline_daily WHERE trade_date IN ({placeholders})",
                trade_dates,
            ).fetchall()
        return {(r["symbol"], r["trade_date"]) for r in rows}

    # ── 检查报告持久化 ───────────────────────────────────────

    def save_check_report(self, report: dict[str, Any]) -> None:
        with self._connect() as conn:
            self._init_schema(conn)
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
            self._init_schema(conn)
            row = conn.execute(
                "SELECT report_json FROM kline_check_reports ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["report_json"])
        except (json.JSONDecodeError, TypeError):
            return None
