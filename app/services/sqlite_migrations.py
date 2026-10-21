"""Versioned SQLite schema migrations for Alpha's local databases.

SQLite stores schema state in ``PRAGMA user_version``; this module owns the
migration lists for the two long-lived databases:

* ``data/funnel_state.db`` - shared by :class:`SQLiteStateStore` and
  :class:`PaperTradingService`, therefore both use the *same* migration list
  (``FUNNEL_DB_MIGRATIONS``) so they can never disagree about the version.
* ``data/market_kline.db`` - owned by :class:`KlineSQLiteStore`
  (``MARKET_KLINE_DB_MIGRATIONS``).

Migrations are executed exactly once at store construction.  Read/write paths
no longer run DDL: they only open short-lived connections.  Version 1 upgrades
legacy databases in place (``CREATE TABLE IF NOT EXISTS`` + ``_ensure_column``)
so existing installs keep their data, while brand-new databases jump straight
to the latest schema.

The runner uses ``BEGIN IMMEDIATE`` and re-reads ``user_version`` after taking
the write lock, which makes concurrent startup (e.g. two uvicorn workers)
idempotent.  A database written by a newer code version raises
``RuntimeError`` instead of being silently downgraded.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class Migration:
    version: int
    description: str
    up: Callable[[sqlite3.Connection], None]


def _ensure_column(
    conn: sqlite3.Connection, table_name: str, col_name: str, col_def: str
) -> None:
    """Add a column when upgrading a legacy table (no-op on current schema)."""

    cols = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    existing = {str(row[1]) for row in cols}
    if col_name not in existing:
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_def}")


# ─── funnel_state.db (state + paper trading share this database) ───


def _migrate_funnel_v1(conn: sqlite3.Connection) -> None:
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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS paper_positions (
            id TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            direction TEXT NOT NULL DEFAULT 'long',
            qty INTEGER NOT NULL,
            cost_price REAL NOT NULL,
            current_price REAL NOT NULL DEFAULT 0,
            opened_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            closed_at TEXT,
            close_price REAL,
            buy_fee REAL NOT NULL DEFAULT 0,
            sell_fee REAL NOT NULL DEFAULT 0,
            note TEXT DEFAULT ''
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS paper_trades (
            id TEXT PRIMARY KEY,
            position_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            action TEXT NOT NULL,
            qty INTEGER NOT NULL,
            price REAL NOT NULL,
            fee REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            note TEXT DEFAULT ''
        )
        """
    )
    # Historical upgrades for databases created before these columns existed.
    _ensure_column(conn, "funnel_state", "frozen", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "notice_state", "llm_enabled", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "notice_state", "source", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "custom_strategies", "description", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(
        conn, "custom_strategies", "is_builtin", "INTEGER NOT NULL DEFAULT 0"
    )
    _ensure_column(
        conn, "custom_strategies", "is_default", "INTEGER NOT NULL DEFAULT 0"
    )
    _ensure_column(conn, "paper_positions", "buy_fee", "REAL NOT NULL DEFAULT 0")
    _ensure_column(conn, "paper_positions", "sell_fee", "REAL NOT NULL DEFAULT 0")
    _ensure_column(conn, "paper_trades", "fee", "REAL NOT NULL DEFAULT 0")


FUNNEL_DB_MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        1, "funnel state, strategies and paper trading base schema", _migrate_funnel_v1
    ),
)


# ─── market_kline.db ───


def _migrate_market_kline_v1(conn: sqlite3.Connection) -> None:
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
            source TEXT NOT NULL DEFAULT 'unknown',
            fallback_reason TEXT NOT NULL DEFAULT '',
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
            data_source TEXT NOT NULL DEFAULT '',
            fallback_reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS kline_check_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            check_time TEXT NOT NULL,
            report_json TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS symbol_names (
            symbol TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_kline_sync_tasks_started_at "
        "ON kline_sync_tasks(started_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_kline_sync_task_details_task_id "
        "ON kline_sync_task_details(task_id, id DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_kline_symbol_date "
        "ON kline_daily(symbol, trade_date DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_check_reports_time "
        "ON kline_check_reports(check_time DESC)"
    )
    # Historical upgrades.
    _ensure_column(conn, "kline_daily", "source", "TEXT NOT NULL DEFAULT 'unknown'")
    _ensure_column(conn, "kline_daily", "fallback_reason", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(
        conn, "kline_sync_state", "total_symbols", "INTEGER NOT NULL DEFAULT 0"
    )
    _ensure_column(
        conn, "kline_sync_state", "synced_symbols", "INTEGER NOT NULL DEFAULT 0"
    )
    _ensure_column(
        conn, "kline_sync_state", "success_symbols", "INTEGER NOT NULL DEFAULT 0"
    )
    _ensure_column(
        conn, "kline_sync_state", "failed_symbols", "INTEGER NOT NULL DEFAULT 0"
    )
    _ensure_column(conn, "kline_sync_state", "task_id", "TEXT")
    _ensure_column(
        conn, "kline_sync_state", "trigger_mode", "TEXT NOT NULL DEFAULT 'auto'"
    )
    _ensure_column(
        conn, "kline_sync_task_details", "data_source", "TEXT NOT NULL DEFAULT ''"
    )
    _ensure_column(
        conn, "kline_sync_task_details", "fallback_reason", "TEXT NOT NULL DEFAULT ''"
    )


MARKET_KLINE_DB_MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        1, "market kline, sync task and report base schema", _migrate_market_kline_v1
    ),
)


FUNNEL_SCHEMA_VERSION = max(m.version for m in FUNNEL_DB_MIGRATIONS)
MARKET_KLINE_SCHEMA_VERSION = max(m.version for m in MARKET_KLINE_DB_MIGRATIONS)


def latest_version(migrations: Sequence[Migration]) -> int:
    return max((m.version for m in migrations), default=0)


def apply_migrations(conn: sqlite3.Connection, migrations: Iterable[Migration]) -> int:
    """Apply pending migrations on an existing connection; return new version."""

    ordered = sorted(migrations, key=lambda m: m.version)
    latest = latest_version(ordered)

    current = int(conn.execute("PRAGMA user_version").fetchone()[0] or 0)
    if current > latest:
        raise RuntimeError(
            f"SQLite schema version {current} is newer than the bundled "
            f"migrations ({latest}); refusing to downgrade the database."
        )
    if current == latest:
        return current

    previous_isolation = conn.isolation_level
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Re-read under the write lock: another process may have migrated
            # between our first read and BEGIN IMMEDIATE.
            current = int(conn.execute("PRAGMA user_version").fetchone()[0] or 0)
            if current > latest:
                raise RuntimeError(
                    f"SQLite schema version {current} is newer than the bundled "
                    f"migrations ({latest}); refusing to downgrade the database."
                )
            pending = [m for m in ordered if m.version > current]
            for migration in pending:
                migration.up(conn)
            if pending:
                conn.execute(f"PRAGMA user_version = {latest}")
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()
    finally:
        conn.isolation_level = previous_isolation

    return latest


def migrate_database(
    db_path: str | Path,
    migrations: Sequence[Migration],
    *,
    timeout: float = 5.0,
    busy_timeout_ms: int = 5000,
) -> int:
    """Open ``db_path`` once, apply pending migrations and close it.

    ``journal_mode=WAL`` is set before the migration transaction (it is a
    no-op when the database is already in WAL mode).  The connection is always
    closed so startup does not leak handles.
    """

    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=timeout)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        return apply_migrations(conn, migrations)
    finally:
        conn.close()
