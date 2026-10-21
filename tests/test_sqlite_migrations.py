from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.services.kline_store import KlineSQLiteStore
from app.services.paper_trading import PaperTradingService
from app.services.sqlite_migrations import (
    FUNNEL_DB_MIGRATIONS,
    FUNNEL_SCHEMA_VERSION,
    MARKET_KLINE_DB_MIGRATIONS,
    MARKET_KLINE_SCHEMA_VERSION,
    apply_migrations,
)
from app.services.sqlite_store import SQLiteStateStore


def _user_version(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0] or 0)
    finally:
        conn.close()


def _table_names(db_path: Path) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    finally:
        conn.close()
    return {row[0] for row in rows}


def _column_names(db_path: Path, table: str) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    finally:
        conn.close()
    return {row[1] for row in rows}


def _capture_sql(monkeypatch) -> list[str]:
    """Trace every connection opened by the store modules."""

    statements: list[str] = []
    real_connect = sqlite3.connect

    def tracing_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(sqlite3, "connect", tracing_connect)
    return statements


def _ddl_statements(statements: list[str]) -> list[str]:
    tokens = ("CREATE ", "ALTER ", "DROP ", "PRAGMA USER_VERSION =")
    return [sql for sql in statements if any(token in sql.upper() for token in tokens)]


def test_empty_funnel_database_is_created_at_latest_version(tmp_path):
    db_path = tmp_path / "funnel_state.db"
    SQLiteStateStore(str(db_path))

    assert _user_version(db_path) == FUNNEL_SCHEMA_VERSION
    assert {
        "funnel_state",
        "strategy_profiles",
        "notice_state",
        "kv_store",
        "custom_strategies",
        "paper_positions",
        "paper_trades",
    } <= _table_names(db_path)


def test_empty_market_database_is_created_at_latest_version(tmp_path):
    db_path = tmp_path / "market_kline.db"
    KlineSQLiteStore(str(db_path))

    assert _user_version(db_path) == MARKET_KLINE_SCHEMA_VERSION
    assert {
        "kline_daily",
        "kline_sync_state",
        "kline_sync_tasks",
        "kline_sync_task_details",
        "kline_check_reports",
        "symbol_names",
        # v2: Tushare enrichment tables already present on main.
        "kline_daily_extras",
        "moneyflow_daily",
        "market_capital_flow",
        "index_daily",
        "kline_minute",
        "enrichment_sync_state",
    } <= _table_names(db_path)


def test_funnel_and_paper_stores_share_one_migration_version(tmp_path):
    db_path = tmp_path / "funnel_state.db"
    state = SQLiteStateStore(str(db_path))
    paper = PaperTradingService(db_path)

    assert _user_version(db_path) == FUNNEL_SCHEMA_VERSION
    # Both stores can write after the shared migration.
    state.set_kv("shared", {"ok": True})
    paper.open_position("600001", "测试", 10.0, qty=100)
    assert paper.get_open_positions()

    # Order must not matter either.
    other_path = tmp_path / "funnel_state_reversed.db"
    PaperTradingService(other_path)
    SQLiteStateStore(str(other_path))
    assert _user_version(other_path) == FUNNEL_SCHEMA_VERSION
    assert {"paper_positions", "paper_trades", "kv_store"} <= _table_names(other_path)


def test_repeated_startup_is_idempotent_and_runs_no_ddl(tmp_path, monkeypatch):
    db_path = tmp_path / "funnel_state.db"
    SQLiteStateStore(str(db_path)).set_kv("k", 1)

    statements = _capture_sql(monkeypatch)
    SQLiteStateStore(str(db_path))
    second_store = SQLiteStateStore(str(db_path))

    assert _user_version(db_path) == FUNNEL_SCHEMA_VERSION
    assert second_store.get_kv("k") == 1
    assert _ddl_statements(statements) == []
    assert not any("BEGIN IMMEDIATE" in sql.upper() for sql in statements)


def test_legacy_funnel_database_is_upgraded_without_data_loss(tmp_path):
    db_path = tmp_path / "funnel_state.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE funnel_state (
                id INTEGER PRIMARY KEY CHECK(id = 1),
                trade_date TEXT NOT NULL,
                entries_json TEXT NOT NULL,
                hot_concepts_json TEXT NOT NULL,
                hot_stocks_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE notice_state (
                id INTEGER PRIMARY KEY CHECK(id = 1),
                trade_date TEXT NOT NULL,
                entries_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO funnel_state VALUES (1, '2026-01-05', '{}', '[]', '[]', '2026-01-05T15:00:00')"
        )
        conn.execute(
            "INSERT INTO notice_state VALUES (1, '2026-01-05', '{}', '2026-01-05T15:00:00')"
        )
        conn.commit()
    finally:
        conn.close()

    store = SQLiteStateStore(str(db_path))

    assert _user_version(db_path) == FUNNEL_SCHEMA_VERSION
    assert {"frozen"} <= _column_names(db_path, "funnel_state")
    assert {"llm_enabled", "source"} <= _column_names(db_path, "notice_state")
    loaded = store.load_state()
    assert loaded is not None
    assert loaded["trade_date"] == "2026-01-05"
    assert loaded["frozen"] is False

    # Columns added by the migration are immediately writable.
    store.save_notice_state(
        {
            "trade_date": "2026-01-05",
            "entries": {},
            "updated_at": "now",
            "source": "llm",
        }
    )
    notice = store.load_notice_state()
    assert notice is not None
    assert notice["source"] == "llm"


def test_legacy_market_database_gets_new_columns_and_keeps_rows(tmp_path):
    db_path = tmp_path / "market_kline.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE kline_daily (
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
            CREATE TABLE kline_sync_state (
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
        conn.execute(
            """
            INSERT INTO kline_daily VALUES
                ('600519', '2026-01-05', 10, 11, 9.5, 10.5, 1000, 10000, '2026-01-05T16:00:00')
            """
        )
        conn.execute(
            "INSERT INTO kline_sync_state VALUES (1, '2026-01-05', '2026-01-05', 'success', 1, '2026-01-05T16:00:00', 'ok')"
        )
        conn.commit()
    finally:
        conn.close()

    store = KlineSQLiteStore(str(db_path))

    assert _user_version(db_path) == MARKET_KLINE_SCHEMA_VERSION
    assert {"source", "fallback_reason"} <= _column_names(db_path, "kline_daily")
    assert {
        "total_symbols",
        "synced_symbols",
        "success_symbols",
        "failed_symbols",
        "task_id",
        "trigger_mode",
    } <= _column_names(db_path, "kline_sync_state")

    rows = store.get_kline("600519", days=1)
    assert rows[0]["close"] == 10.5
    assert rows[0]["source"] == "unknown"
    assert rows[0]["fallback_reason"] == ""

    state = store.get_sync_state()
    assert state["status"] == "success"
    assert state["total_symbols"] == 0
    assert state["trigger_mode"] == "auto"

    # New columns are writable through the normal path.
    store.upsert_symbol_klines(
        "600519",
        [
            {
                "trade_date": "2026-01-06",
                "open": 10.5,
                "high": 11.2,
                "low": 10.4,
                "close": 11.0,
                "volume": 1200,
                "amount": 13000,
                "source": "tushare",
                "fallback_reason": "",
            }
        ],
        "2026-01-06T16:00:00",
    )
    assert store.get_kline("600519", days=1)[0]["source"] == "tushare"


def test_market_database_upgrades_from_v1_to_latest(tmp_path):
    db_path = tmp_path / "market_kline.db"
    conn = sqlite3.connect(str(db_path))
    try:
        assert apply_migrations(conn, MARKET_KLINE_DB_MIGRATIONS[:1]) == 1
    finally:
        conn.close()
    assert _user_version(db_path) == 1
    assert "kline_daily_extras" not in _table_names(db_path)

    KlineSQLiteStore(str(db_path))

    assert _user_version(db_path) == MARKET_KLINE_SCHEMA_VERSION
    assert {
        "kline_daily",
        "kline_daily_extras",
        "moneyflow_daily",
        "market_capital_flow",
        "index_daily",
        "kline_minute",
        "enrichment_sync_state",
    } <= _table_names(db_path)


def test_read_paths_do_not_execute_ddl_or_migration_writes(tmp_path, monkeypatch):
    funnel_db = tmp_path / "funnel_state.db"
    market_db = tmp_path / "market_kline.db"

    state_store = SQLiteStateStore(str(funnel_db))
    state_store.set_kv("k", {"v": 1})
    state_store.save_state(
        {"trade_date": "2026-01-05", "entries": {}, "updated_at": "now"}
    )
    paper = PaperTradingService(funnel_db)
    paper.open_position("600001", "测试", 10.0, qty=100)

    kline_store = KlineSQLiteStore(str(market_db))
    kline_store.upsert_symbol_klines(
        "600519",
        [
            {
                "trade_date": "2026-01-05",
                "open": 10,
                "high": 11,
                "low": 9,
                "close": 10.5,
                "volume": 100,
                "amount": 1000,
            }
        ],
        "2026-01-05T16:00:00",
    )

    statements = _capture_sql(monkeypatch)

    def read_everything(_: int) -> None:
        assert state_store.get_kv("k") == {"v": 1}
        assert state_store.load_state() is not None
        assert state_store.load_notice_state() is None
        assert state_store.list_custom_strategies() == []
        assert paper.get_open_positions()
        assert paper.get_trades(limit=5)
        assert kline_store.get_kline("600519", days=5)
        assert kline_store.get_stats()["row_count"] == 1
        assert kline_store.get_sync_state()["status"] == "idle"
        assert kline_store.list_sync_tasks()["total"] == 0

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(read_everything, range(16)))

    assert _ddl_statements(statements) == []
    assert not any("BEGIN IMMEDIATE" in sql.upper() for sql in statements)
    assert not any(
        sql.upper().lstrip().startswith(("INSERT", "UPDATE", "DELETE", "BEGIN"))
        for sql in statements
    )


def test_future_schema_version_is_rejected(tmp_path):
    db_path = tmp_path / "funnel_state.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA user_version = 999")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(RuntimeError, match="newer"):
        SQLiteStateStore(str(db_path))


def test_apply_migrations_noop_when_current(tmp_path):
    db_path = tmp_path / "funnel_state.db"
    SQLiteStateStore(str(db_path))
    conn = sqlite3.connect(str(db_path))
    try:
        assert apply_migrations(conn, FUNNEL_DB_MIGRATIONS) == FUNNEL_SCHEMA_VERSION
    finally:
        conn.close()
