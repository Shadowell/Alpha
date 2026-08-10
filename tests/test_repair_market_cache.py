from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from scripts import repair_market_cache


def _make_database(path):
    with sqlite3.connect(path) as conn:
        conn.executescript(
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
                PRIMARY KEY (symbol, trade_date)
            );
            CREATE TABLE kline_sync_state (
                id INTEGER PRIMARY KEY,
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
            );
            CREATE TABLE kline_sync_tasks (
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
            );
            CREATE TABLE kline_sync_task_details (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                status TEXT NOT NULL,
                elapsed_ms INTEGER NOT NULL DEFAULT 0,
                error_message TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                data_source TEXT NOT NULL DEFAULT '',
                fallback_reason TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE kline_check_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                check_time TEXT NOT NULL,
                report_json TEXT NOT NULL
            );
            INSERT INTO kline_daily VALUES
                ('000001', '1992-05-04', 1, 1, 1, 1, 1, 1),
                ('000001', '2026-08-08', 2, 2, 2, 2, 2, 2);
            INSERT INTO kline_sync_tasks VALUES
                ('bad-task', 'auto', '1992-05-04', 'success', 1, 1, 1, 0, 'now', 'now', ''),
                ('good-task', 'auto', '2026-08-08', 'success', 1, 1, 1, 0, 'now', 'now', '');
            INSERT INTO kline_sync_task_details
                (task_id, symbol, status, created_at)
                VALUES ('bad-task', '000001', 'success', 'now'),
                       ('good-task', '000001', 'success', 'now');
            INSERT INTO kline_sync_state VALUES
                (1, '1992-05-04', '1992-05-04', 'success', 1, 1, 1, 1, 0,
                 'bad-task', 'auto', 'now', 'bad state');
            INSERT INTO kline_check_reports(check_time, report_json)
                VALUES ('now', '{"trade_date":"1992-05-04"}'),
                       ('now', '{"trade_date":"2026-08-08"}');
            """
        )


def test_dry_run_reports_without_mutation(tmp_path):
    db_path = tmp_path / "market.db"
    _make_database(db_path)
    before = db_path.read_bytes()

    report = repair_market_cache.inspect_database(db_path)

    assert report["mode"] == "dry-run"
    assert report["invalid_dates"] == ["1992-05-04"]
    assert report["affected"] == {
        "kline_daily": 1,
        "kline_sync_tasks": 1,
        "kline_sync_task_details": 1,
        "kline_sync_state": 1,
        "kline_check_reports": 1,
    }
    assert db_path.read_bytes() == before


def test_apply_creates_backup_and_repairs_transactionally(tmp_path):
    db_path = tmp_path / "market.db"
    backup_dir = tmp_path / "backups"
    _make_database(db_path)

    report = repair_market_cache.repair_database(db_path, backup_dir)

    backup_path = Path(report["backup_path"])
    assert backup_path.is_file()
    assert report["total_affected_rows"] == 0
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM kline_daily").fetchone()[0] == 1
        assert (
            conn.execute("SELECT trade_date FROM kline_daily").fetchone()[0]
            == "2026-08-08"
        )
        assert (
            conn.execute("SELECT task_id FROM kline_sync_tasks").fetchone()[0]
            == "good-task"
        )
        assert (
            conn.execute("SELECT task_id FROM kline_sync_task_details").fetchone()[0]
            == "good-task"
        )
        state = conn.execute(
            "SELECT last_attempt_trade_date, last_success_trade_date, status, task_id FROM kline_sync_state"
        ).fetchone()
        assert state == (None, None, "idle", None)
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    with sqlite3.connect(backup_path) as backup:
        assert backup.execute("SELECT COUNT(*) FROM kline_daily").fetchone()[0] == 2
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_apply_is_idempotent_and_skips_second_backup(tmp_path):
    db_path = tmp_path / "market.db"
    backup_dir = tmp_path / "backups"
    _make_database(db_path)

    repair_market_cache.repair_database(db_path, backup_dir)
    second = repair_market_cache.repair_database(db_path, backup_dir)

    assert second["applied"] is True
    assert second["total_affected_rows"] == 0
    assert second["backup_path"] is None
    assert len(list(backup_dir.glob("*.db"))) == 1


def test_backup_failure_prevents_mutation(tmp_path, monkeypatch):
    db_path = tmp_path / "market.db"
    _make_database(db_path)
    before = db_path.read_bytes()

    def fail_backup(*args, **kwargs):
        raise OSError("backup unavailable")

    monkeypatch.setattr(repair_market_cache, "create_backup", fail_backup)
    with pytest.raises(OSError, match="backup unavailable"):
        repair_market_cache.repair_database(db_path, tmp_path / "backups")

    assert db_path.read_bytes() == before


def test_delete_failure_rolls_back_database(tmp_path):
    db_path = tmp_path / "market.db"
    _make_database(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TRIGGER block_corrupt_delete
            BEFORE DELETE ON kline_daily
            WHEN OLD.trade_date = '1992-05-04'
            BEGIN
                SELECT RAISE(ABORT, 'blocked for rollback test');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="blocked for rollback test"):
        repair_market_cache.repair_database(db_path, tmp_path / "backups")

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM kline_daily").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM kline_sync_tasks").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM kline_sync_task_details").fetchone()[0] == 2
