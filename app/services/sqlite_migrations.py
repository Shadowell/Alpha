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


def _migrate_funnel_v2(conn: sqlite3.Connection) -> None:
    """Persist the hot-stocks freshness timestamp separately from the bucket timestamp (#21)."""
    _ensure_column(conn, "funnel_state", "hot_stocks_updated_at", "TEXT")
    conn.execute(
        """
        UPDATE funnel_state
        SET hot_stocks_updated_at = updated_at
        WHERE hot_stocks_updated_at IS NULL OR hot_stocks_updated_at = ''
        """
    )


FUNNEL_DB_MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        1, "funnel state, strategies and paper trading base schema", _migrate_funnel_v1
    ),
    Migration(2, "persist hot stocks freshness timestamp", _migrate_funnel_v2),
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


def _migrate_market_kline_v2(conn: sqlite3.Connection) -> None:
    """Tushare enrichment tables (already shipped on main as a later feature).

    Kept as an explicit version so databases created by this branch and by
    main converge on the same schema after merge; all statements are
    idempotent for fresh installs.
    """

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS kline_daily_extras (
            symbol TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            pe_ttm REAL,
            pb REAL,
            ps_ttm REAL,
            turnover_rate REAL,
            turnover_rate_f REAL,
            volume_ratio REAL,
            total_mv REAL,
            circ_mv REAL,
            up_limit REAL,
            down_limit REAL,
            adj_factor REAL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (symbol, trade_date)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS moneyflow_daily (
            symbol TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            buy_sm_amount REAL,
            sell_sm_amount REAL,
            buy_md_amount REAL,
            sell_md_amount REAL,
            buy_lg_amount REAL,
            sell_lg_amount REAL,
            buy_elg_amount REAL,
            sell_elg_amount REAL,
            net_mf_amount REAL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (symbol, trade_date)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS market_capital_flow (
            trade_date TEXT PRIMARY KEY,
            hgt REAL,
            sgt REAL,
            north_money REAL,
            south_money REAL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS index_daily (
            ts_code TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            pre_close REAL,
            change REAL,
            pct_chg REAL,
            volume REAL,
            amount REAL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (ts_code, trade_date)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS kline_minute (
            symbol TEXT NOT NULL,
            bar_time TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume REAL,
            amount REAL,
            source TEXT NOT NULL DEFAULT 'tushare',
            updated_at TEXT NOT NULL,
            PRIMARY KEY (symbol, bar_time)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_kline_minute_symbol_date "
        "ON kline_minute(symbol, trade_date)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS enrichment_sync_state (
            dataset TEXT PRIMARY KEY,
            trade_date TEXT,
            status TEXT NOT NULL,
            row_count INTEGER NOT NULL DEFAULT 0,
            message TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        )
        """
    )


def _migrate_market_kline_v3(conn: sqlite3.Connection) -> None:
    """打板核心数据底座（issue #22）。

    所有金额列统一为**元**（换算集中在 ``limit_board_service`` 的字段映射层），
    缺失值保持 NULL，绝不写 0，避免因子层把"无数据"当成有效信号。

    口径说明（按 2026-10-21 真实响应核对）：

    * ``limit_list_d`` 单只股票每日只有一条记录，故 ``limit_board_daily``
      用 ``(symbol, trade_date)`` 作主键；
    * ``top_list`` 同一股票会因多个上榜原因各出现一次，故龙虎榜主键含 ``reason``；
    * ``top_inst`` 同一 ``(date, symbol, 营业部, side, reason)`` 会出现多条
      （典型是"机构专用"多个席位同名），落库时按该键聚合金额并记录 ``seat_count``；
    * ``hsgt_top10`` 同一股票在沪/深两个通道各出现一次，主键含 ``market_type``。
    """

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS limit_board_daily (
            symbol TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            ts_code TEXT,
            name TEXT,
            industry TEXT,
            -- Tushare ``limit`` 字段（U=涨停 / D=跌停 / Z=炸板）；``limit`` 是
            -- SQLite 保留字，落库列名统一为 ``limit_flag``。
            limit_flag TEXT,
            first_time TEXT,
            last_time TEXT,
            open_times INTEGER,
            limit_times INTEGER,
            up_stat TEXT,
            fd_amount REAL,
            amount REAL,
            limit_amount REAL,
            turnover_ratio REAL,
            float_mv REAL,
            total_mv REAL,
            close REAL,
            pct_chg REAL,
            board_type TEXT,
            theme TEXT,
            limit_reason TEXT,
            source TEXT NOT NULL DEFAULT 'tushare.limit_list_d',
            synced_at TEXT NOT NULL,
            PRIMARY KEY (symbol, trade_date)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_limit_board_date_limit "
        "ON limit_board_daily(trade_date, limit_flag)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_limit_board_date_times "
        "ON limit_board_daily(trade_date, limit_times)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS limit_ladder_daily (
            trade_date TEXT NOT NULL,
            ladder_level INTEGER NOT NULL,
            stock_count INTEGER NOT NULL DEFAULT 0,
            symbols_json TEXT NOT NULL DEFAULT '[]',
            fd_amount_sum REAL,
            amount_sum REAL,
            avg_open_times REAL,
            prev_level_count INTEGER,
            promotion_ratio REAL,
            source TEXT NOT NULL DEFAULT 'limit_list_d',
            synced_at TEXT NOT NULL,
            PRIMARY KEY (trade_date, ladder_level)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dragon_tiger_daily (
            symbol TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            ts_code TEXT,
            name TEXT,
            close REAL,
            pct_change REAL,
            turnover_rate REAL,
            amount REAL,
            l_buy REAL,
            l_sell REAL,
            l_amount REAL,
            net_amount REAL,
            net_rate REAL,
            amount_rate REAL,
            float_values REAL,
            source TEXT NOT NULL DEFAULT 'tushare.top_list',
            synced_at TEXT NOT NULL,
            PRIMARY KEY (symbol, trade_date, reason)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dragon_tiger_date "
        "ON dragon_tiger_daily(trade_date)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dragon_tiger_seat_daily (
            trade_date TEXT NOT NULL,
            symbol TEXT NOT NULL,
            seat_name TEXT NOT NULL,
            side TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            ts_code TEXT,
            stock_name TEXT,
            buy_amount REAL,
            buy_rate REAL,
            sell_amount REAL,
            sell_rate REAL,
            net_amount REAL,
            seat_count INTEGER NOT NULL DEFAULT 1,
            source TEXT NOT NULL DEFAULT 'tushare.top_inst',
            synced_at TEXT NOT NULL,
            PRIMARY KEY (trade_date, symbol, seat_name, side, reason)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dragon_seat_date "
        "ON dragon_tiger_seat_daily(trade_date, symbol)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS hot_money_seat (
            seat_name TEXT NOT NULL,
            org_code TEXT NOT NULL DEFAULT '',
            category TEXT,
            description TEXT,
            orgs_json TEXT NOT NULL DEFAULT '[]',
            appear_count INTEGER NOT NULL DEFAULT 0,
            last_trade_date TEXT,
            source TEXT NOT NULL DEFAULT 'tushare.hm_list',
            synced_at TEXT NOT NULL,
            PRIMARY KEY (seat_name, org_code)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS hsgt_top10_daily (
            trade_date TEXT NOT NULL,
            symbol TEXT NOT NULL,
            market_type TEXT NOT NULL,
            ts_code TEXT,
            name TEXT,
            close REAL,
            change REAL,
            rank_no INTEGER,
            amount REAL,
            net_amount REAL,
            buy_amount REAL,
            sell_amount REAL,
            source TEXT NOT NULL DEFAULT 'tushare.hsgt_top10',
            synced_at TEXT NOT NULL,
            PRIMARY KEY (trade_date, symbol, market_type)
        )
        """
    )


MARKET_KLINE_DB_MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        1, "market kline, sync task and report base schema", _migrate_market_kline_v1
    ),
    Migration(2, "Tushare enrichment tables", _migrate_market_kline_v2),
    Migration(3, "limit-up board data foundation (#22)", _migrate_market_kline_v3),
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
