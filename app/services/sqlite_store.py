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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS strategy_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                config_json TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS notice_state (
                id INTEGER PRIMARY KEY CHECK(id = 1),
                trade_date TEXT NOT NULL,
                entries_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                llm_enabled INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS kv_store (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS custom_strategies (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                rules_json TEXT NOT NULL,
                is_builtin INTEGER NOT NULL DEFAULT 0,
                is_default INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.commit()

    def get_kv(self, key: str) -> Any | None:
        with self._connect() as conn:
            self._init_schema(conn)
            row = conn.execute("SELECT value_json FROM kv_store WHERE key = ?", (key,)).fetchone()
            if row is None:
                return None
            return self._loads_json(row["value_json"], default=None)

    def set_kv(self, key: str, value: Any) -> None:
        from datetime import datetime
        with self._connect() as conn:
            self._init_schema(conn)
            conn.execute(
                """
                INSERT INTO kv_store (key, value_json, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at
                """,
                (key, self._dumps_json(value), datetime.now().isoformat()),
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

    def get_active_strategy_profile(self) -> dict[str, Any] | None:
        with self._connect() as conn:
            self._init_schema(conn)
            row = conn.execute(
                "SELECT id, name, config_json, updated_at FROM strategy_profiles WHERE is_active = 1 ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            return {
                "id": int(row["id"]),
                "name": row["name"],
                "config": self._loads_json(row["config_json"], default={}),
                "updated_at": row["updated_at"],
            }

    def upsert_single_active_strategy_profile(self, name: str, config: dict[str, Any], updated_at: str) -> dict[str, Any]:
        with self._connect() as conn:
            self._init_schema(conn)
            conn.execute("UPDATE strategy_profiles SET is_active = 0 WHERE is_active = 1")
            conn.execute(
                """
                INSERT INTO strategy_profiles(name, config_json, is_active, updated_at)
                VALUES (?, ?, 1, ?)
                """,
                (name, self._dumps_json(config), updated_at),
            )
            conn.commit()

        active = self.get_active_strategy_profile()
        return active if active is not None else {"id": 0, "name": name, "config": config, "updated_at": updated_at}

    def load_notice_state(self) -> dict[str, Any] | None:
        with self._connect() as conn:
            self._init_schema(conn)
            row = conn.execute("SELECT * FROM notice_state WHERE id = 1").fetchone()
            if row is None:
                return None
            return {
                "trade_date": row["trade_date"],
                "entries": self._loads_json(row["entries_json"], default={}),
                "updated_at": row["updated_at"],
                "llm_enabled": bool(row["llm_enabled"]),
                "source": row["source"] or "",
            }

    def save_notice_state(self, payload: dict[str, Any]) -> None:
        with self._connect() as conn:
            self._init_schema(conn)
            conn.execute(
                """
                INSERT INTO notice_state(id, trade_date, entries_json, updated_at, llm_enabled, source)
                VALUES (1, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    trade_date=excluded.trade_date,
                    entries_json=excluded.entries_json,
                    updated_at=excluded.updated_at,
                    llm_enabled=excluded.llm_enabled,
                    source=excluded.source
                """,
                (
                    payload.get("trade_date", ""),
                    self._dumps_json(payload.get("entries", {})),
                    payload.get("updated_at", ""),
                    1 if payload.get("llm_enabled", False) else 0,
                    payload.get("source", ""),
                ),
            )
            conn.commit()

    # ─── 自定义策略 CRUD ───

    def list_custom_strategies(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            self._init_schema(conn)
            rows = conn.execute(
                "SELECT id, name, description, rules_json, is_builtin, is_default, created_at, updated_at FROM custom_strategies ORDER BY is_builtin DESC, name ASC"
            ).fetchall()
            return [self._strategy_row_to_dict(r) for r in rows]

    def get_custom_strategy(self, strategy_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            self._init_schema(conn)
            row = conn.execute(
                "SELECT id, name, description, rules_json, is_builtin, is_default, created_at, updated_at FROM custom_strategies WHERE id = ?",
                (strategy_id,),
            ).fetchone()
            return self._strategy_row_to_dict(row) if row else None

    def get_default_custom_strategy(self) -> dict[str, Any] | None:
        with self._connect() as conn:
            self._init_schema(conn)
            row = conn.execute(
                "SELECT id, name, description, rules_json, is_builtin, is_default, created_at, updated_at FROM custom_strategies WHERE is_default = 1 ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
            return self._strategy_row_to_dict(row) if row else None

    def upsert_custom_strategy(self, payload: dict[str, Any]) -> dict[str, Any]:
        from datetime import datetime
        sid = str(payload.get("id") or "").strip()
        if not sid:
            import uuid
            sid = uuid.uuid4().hex
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            self._init_schema(conn)
            existing = conn.execute("SELECT id, created_at, is_builtin FROM custom_strategies WHERE id = ?", (sid,)).fetchone()
            created_at = existing["created_at"] if existing else now
            is_builtin = bool(payload.get("is_builtin", False))
            if existing:
                is_builtin = bool(existing["is_builtin"])  # builtin 不可改成 false
            conn.execute(
                """
                INSERT INTO custom_strategies (id, name, description, rules_json, is_builtin, is_default, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,
                    description=excluded.description,
                    rules_json=excluded.rules_json,
                    is_default=excluded.is_default,
                    updated_at=excluded.updated_at
                """,
                (
                    sid,
                    str(payload.get("name", "未命名策略")),
                    str(payload.get("description", "")),
                    self._dumps_json(payload.get("rules", [])),
                    1 if is_builtin else 0,
                    1 if payload.get("is_default") else 0,
                    created_at,
                    now,
                ),
            )
            conn.commit()
        out = self.get_custom_strategy(sid)
        return out or {}

    def delete_custom_strategy(self, strategy_id: str) -> bool:
        with self._connect() as conn:
            self._init_schema(conn)
            row = conn.execute("SELECT is_builtin FROM custom_strategies WHERE id = ?", (strategy_id,)).fetchone()
            if not row:
                return False
            if int(row["is_builtin"]) == 1:
                raise ValueError("内置策略不可删除")
            conn.execute("DELETE FROM custom_strategies WHERE id = ?", (strategy_id,))
            conn.commit()
            return True

    def set_default_custom_strategy(self, strategy_id: str) -> bool:
        with self._connect() as conn:
            self._init_schema(conn)
            exists = conn.execute("SELECT 1 FROM custom_strategies WHERE id = ?", (strategy_id,)).fetchone()
            if not exists:
                return False
            conn.execute("UPDATE custom_strategies SET is_default = 0 WHERE is_default = 1")
            conn.execute("UPDATE custom_strategies SET is_default = 1 WHERE id = ?", (strategy_id,))
            conn.commit()
            return True

    def ensure_builtin_custom_strategies(self, defaults: list[dict[str, Any]]) -> int:
        """幂等写入内置策略：仅当 id 不存在时插入（保留用户修改）。返回新插入条数。"""
        from datetime import datetime
        inserted = 0
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            self._init_schema(conn)
            for s in defaults:
                sid = str(s.get("id") or "").strip()
                if not sid:
                    continue
                exists = conn.execute("SELECT 1 FROM custom_strategies WHERE id = ?", (sid,)).fetchone()
                if exists:
                    continue
                conn.execute(
                    """
                    INSERT INTO custom_strategies (id, name, description, rules_json, is_builtin, is_default, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 1, ?, ?, ?)
                    """,
                    (
                        sid,
                        str(s.get("name", "内置策略")),
                        str(s.get("description", "")),
                        self._dumps_json(s.get("rules", [])),
                        1 if s.get("is_default") else 0,
                        str(s.get("created_at") or now),
                        str(s.get("updated_at") or now),
                    ),
                )
                inserted += 1
            # 若没有任何 is_default=1 的策略，把 defaults 里第一个 is_default 的设为默认
            any_default = conn.execute("SELECT 1 FROM custom_strategies WHERE is_default = 1 LIMIT 1").fetchone()
            if not any_default:
                for s in defaults:
                    if s.get("is_default"):
                        conn.execute("UPDATE custom_strategies SET is_default = 1 WHERE id = ?", (str(s["id"]),))
                        break
            conn.commit()
        return inserted

    def _strategy_row_to_dict(self, row) -> dict[str, Any]:
        if row is None:
            return {}
        return {
            "id": row["id"],
            "name": row["name"],
            "description": row["description"],
            "rules": self._loads_json(row["rules_json"], default=[]),
            "is_builtin": bool(row["is_builtin"]),
            "is_default": bool(row["is_default"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

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
