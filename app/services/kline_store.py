from __future__ import annotations

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
                updated_at TEXT NOT NULL,
                message TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_kline_symbol_date ON kline_daily(symbol, trade_date DESC)")
        conn.commit()

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
        updated_at: str,
        message: str = "",
    ) -> None:
        with self._connect() as conn:
            self._init_schema(conn)
            conn.execute(
                """
                INSERT INTO kline_sync_state (
                    id, last_attempt_trade_date, last_success_trade_date, status, symbol_count, updated_at, message
                ) VALUES (1, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    last_attempt_trade_date=excluded.last_attempt_trade_date,
                    last_success_trade_date=excluded.last_success_trade_date,
                    status=excluded.status,
                    symbol_count=excluded.symbol_count,
                    updated_at=excluded.updated_at,
                    message=excluded.message
                """,
                (attempt_trade_date, success_trade_date, status, symbol_count, updated_at, message),
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
                    "updated_at": "",
                    "message": "",
                }
            return {
                "last_attempt_trade_date": row["last_attempt_trade_date"],
                "last_success_trade_date": row["last_success_trade_date"],
                "status": row["status"],
                "symbol_count": int(row["symbol_count"]),
                "updated_at": row["updated_at"],
                "message": row["message"],
            }
