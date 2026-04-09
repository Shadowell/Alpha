from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


class SQLiteStateStore:
    def __init__(self, db_path: str = "data/funnel_state.db") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS funnel_state (
                id INTEGER PRIMARY KEY CHECK(id = 1),
                trade_date TEXT NOT NULL,
                entries_json TEXT NOT NULL,
                hot_concepts_json TEXT NOT NULL,
                hot_stocks_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                frozen INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.commit()

    def load_state(self) -> dict[str, Any] | None:
        with self._connect() as conn:
            self._init_schema(conn)
            row = conn.execute("SELECT * FROM funnel_state WHERE id = 1").fetchone()
            if row is None:
                return None

            return {
                "trade_date": row["trade_date"],
                "entries": self._loads_json(row["entries_json"], default={}),
                "hot_concepts": self._loads_json(row["hot_concepts_json"], default=[]),
                "hot_stocks": self._loads_json(row["hot_stocks_json"], default=[]),
                "updated_at": row["updated_at"],
                "frozen": bool(row["frozen"]),
            }

    def save_state(self, payload: dict[str, Any]) -> None:
        with self._connect() as conn:
            self._init_schema(conn)
            conn.execute(
                """
                INSERT INTO funnel_state (
                    id, trade_date, entries_json, hot_concepts_json, hot_stocks_json, updated_at, frozen
                ) VALUES (1, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    trade_date=excluded.trade_date,
                    entries_json=excluded.entries_json,
                    hot_concepts_json=excluded.hot_concepts_json,
                    hot_stocks_json=excluded.hot_stocks_json,
                    updated_at=excluded.updated_at,
                    frozen=excluded.frozen
                """,
                (
                    payload.get("trade_date", ""),
                    self._dumps_json(payload.get("entries", {})),
                    self._dumps_json(payload.get("hot_concepts", [])),
                    self._dumps_json(payload.get("hot_stocks", [])),
                    payload.get("updated_at", ""),
                    1 if payload.get("frozen", False) else 0,
                ),
            )
            conn.commit()

    @staticmethod
    def _dumps_json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False)

    @staticmethod
    def _loads_json(raw: str | None, default: Any) -> Any:
        if not raw:
            return default
        try:
            return json.loads(raw)
        except Exception:
            return default
