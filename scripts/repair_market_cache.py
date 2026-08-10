#!/usr/bin/env python3
"""Safely inspect and repair invalid dates in Alpha's SQLite market cache."""

from __future__ import annotations

import argparse
from datetime import date, datetime
import json
import os
from pathlib import Path
import sqlite3
from typing import Any
from zoneinfo import ZoneInfo


DEFAULT_DB = Path("data/market_kline.db")
DEFAULT_BACKUP_DIR = Path("data/backups")
DEFAULT_MIN_YEAR = 2000


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def _is_invalid_trade_date(value: str | None, min_year: int) -> bool:
    if value is None or not str(value).strip():
        return False
    try:
        parsed = date.fromisoformat(str(value))
    except ValueError:
        return True
    return parsed.year < min_year


def _invalid_dates(
    conn: sqlite3.Connection, table: str, column: str, min_year: int
) -> list[str]:
    if not _table_exists(conn, table):
        return []
    values = conn.execute(
        f"SELECT DISTINCT {column} FROM {table} WHERE {column} IS NOT NULL"
    ).fetchall()
    return sorted(
        {str(row[0]) for row in values if _is_invalid_trade_date(row[0], min_year)}
    )


def _placeholders(values: list[str]) -> str:
    return ",".join("?" for _ in values)


def inspect_connection(conn: sqlite3.Connection, min_year: int) -> dict[str, Any]:
    kline_dates = _invalid_dates(conn, "kline_daily", "trade_date", min_year)
    task_dates = _invalid_dates(conn, "kline_sync_tasks", "trade_date", min_year)
    state_dates = sorted(
        set(_invalid_dates(conn, "kline_sync_state", "last_attempt_trade_date", min_year))
        | set(_invalid_dates(conn, "kline_sync_state", "last_success_trade_date", min_year))
    )
    all_invalid_dates = sorted(set(kline_dates) | set(task_dates) | set(state_dates))

    kline_rows = 0
    if kline_dates:
        kline_rows = conn.execute(
            f"SELECT COUNT(*) FROM kline_daily WHERE trade_date IN ({_placeholders(kline_dates)})",
            kline_dates,
        ).fetchone()[0]

    invalid_task_ids: list[str] = []
    if task_dates:
        invalid_task_ids = [
            str(row[0])
            for row in conn.execute(
                f"SELECT task_id FROM kline_sync_tasks WHERE trade_date IN ({_placeholders(task_dates)})",
                task_dates,
            ).fetchall()
        ]

    detail_rows = 0
    if invalid_task_ids and _table_exists(conn, "kline_sync_task_details"):
        detail_rows = conn.execute(
            f"SELECT COUNT(*) FROM kline_sync_task_details WHERE task_id IN ({_placeholders(invalid_task_ids)})",
            invalid_task_ids,
        ).fetchone()[0]

    state_rows = 0
    if _table_exists(conn, "kline_sync_state"):
        for row in conn.execute(
            "SELECT last_attempt_trade_date, last_success_trade_date, task_id FROM kline_sync_state"
        ).fetchall():
            if (
                _is_invalid_trade_date(row[0], min_year)
                or _is_invalid_trade_date(row[1], min_year)
                or (row[2] is not None and str(row[2]) in invalid_task_ids)
            ):
                state_rows += 1

    report_ids: list[int] = []
    if all_invalid_dates and _table_exists(conn, "kline_check_reports"):
        clauses = " OR ".join("instr(report_json, ?) > 0" for _ in all_invalid_dates)
        report_ids = [
            int(row[0])
            for row in conn.execute(
                f"SELECT id FROM kline_check_reports WHERE {clauses}", all_invalid_dates
            ).fetchall()
        ]

    affected = {
        "kline_daily": int(kline_rows),
        "kline_sync_tasks": len(invalid_task_ids),
        "kline_sync_task_details": int(detail_rows),
        "kline_sync_state": int(state_rows),
        "kline_check_reports": len(report_ids),
    }
    return {
        "min_year": min_year,
        "invalid_dates": all_invalid_dates,
        "invalid_task_ids": invalid_task_ids,
        "invalid_report_ids": report_ids,
        "affected": affected,
        "total_affected_rows": sum(affected.values()),
    }


def inspect_database(db_path: Path, min_year: int = DEFAULT_MIN_YEAR) -> dict[str, Any]:
    if not db_path.is_file():
        raise FileNotFoundError(f"database not found: {db_path}")
    with sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True, timeout=10) as conn:
        report = inspect_connection(conn, min_year)
        report["integrity_check"] = conn.execute("PRAGMA integrity_check").fetchone()[0]
    report["database"] = str(db_path)
    report["mode"] = "dry-run"
    report["applied"] = False
    report["backup_path"] = None
    return report


def create_backup(db_path: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M%S-%f")
    backup_path = backup_dir / f"{db_path.stem}-{stamp}.db"
    temporary_path = backup_path.with_suffix(".db.tmp")
    if backup_path.exists() or temporary_path.exists():
        raise FileExistsError(f"backup path already exists: {backup_path}")

    try:
        with sqlite3.connect(
            f"file:{db_path.resolve()}?mode=ro", uri=True, timeout=10
        ) as source, sqlite3.connect(temporary_path) as target:
            source.backup(target)
            target.commit()
            integrity = target.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError(f"backup integrity_check failed: {integrity}")
        os.replace(temporary_path, backup_path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    return backup_path


def repair_database(
    db_path: Path,
    backup_dir: Path = DEFAULT_BACKUP_DIR,
    min_year: int = DEFAULT_MIN_YEAR,
) -> dict[str, Any]:
    initial = inspect_database(db_path, min_year)
    if initial["integrity_check"] != "ok":
        raise RuntimeError(
            f"source integrity_check failed: {initial['integrity_check']}"
        )
    if initial["total_affected_rows"] == 0:
        initial["mode"] = "apply"
        initial["applied"] = True
        return initial

    backup_path = create_backup(db_path, backup_dir)
    deleted: dict[str, int] = {}

    with sqlite3.connect(db_path, timeout=10) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            conn.execute("BEGIN IMMEDIATE")
            current = inspect_connection(conn, min_year)
            task_ids = current["invalid_task_ids"]
            if task_ids and _table_exists(conn, "kline_sync_task_details"):
                cursor = conn.execute(
                    f"DELETE FROM kline_sync_task_details WHERE task_id IN ({_placeholders(task_ids)})",
                    task_ids,
                )
                deleted["kline_sync_task_details"] = cursor.rowcount
            if task_ids:
                cursor = conn.execute(
                    f"DELETE FROM kline_sync_tasks WHERE task_id IN ({_placeholders(task_ids)})",
                    task_ids,
                )
                deleted["kline_sync_tasks"] = cursor.rowcount

            invalid_dates = current["invalid_dates"]
            if invalid_dates:
                cursor = conn.execute(
                    f"DELETE FROM kline_daily WHERE trade_date IN ({_placeholders(invalid_dates)})",
                    invalid_dates,
                )
                deleted["kline_daily"] = cursor.rowcount

            report_ids = current["invalid_report_ids"]
            if report_ids:
                cursor = conn.execute(
                    f"DELETE FROM kline_check_reports WHERE id IN ({_placeholders(report_ids)})",
                    report_ids,
                )
                deleted["kline_check_reports"] = cursor.rowcount

            if current["affected"]["kline_sync_state"]:
                cursor = conn.execute(
                    """
                    UPDATE kline_sync_state
                    SET last_attempt_trade_date = NULL,
                        last_success_trade_date = NULL,
                        status = 'idle',
                        symbol_count = 0,
                        total_symbols = 0,
                        synced_symbols = 0,
                        success_symbols = 0,
                        failed_symbols = 0,
                        task_id = NULL,
                        trigger_mode = 'maintenance',
                        updated_at = ?,
                        message = 'Market cache repaired; refill required'
                    """,
                    (datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),),
                )
                deleted["kline_sync_state_reset"] = cursor.rowcount

            remaining = inspect_connection(conn, min_year)
            if remaining["total_affected_rows"] != 0:
                raise RuntimeError(f"repair postcondition failed: {remaining}")
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError(f"post-repair integrity_check failed: {integrity}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    final = inspect_database(db_path, min_year)
    final.update(
        {
            "mode": "apply",
            "applied": True,
            "backup_path": str(backup_path),
            "deleted": deleted,
            "initial": initial,
        }
    )
    return final


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect or repair invalid trade dates in market_kline.db"
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    parser.add_argument("--min-year", type=int, default=DEFAULT_MIN_YEAR)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="create a verified backup and apply the transactional repair",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.min_year < 1990 or args.min_year > date.today().year:
        raise SystemExit("--min-year must be between 1990 and the current year")
    report = (
        repair_database(args.db, args.backup_dir, args.min_year)
        if args.apply
        else inspect_database(args.db, args.min_year)
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
