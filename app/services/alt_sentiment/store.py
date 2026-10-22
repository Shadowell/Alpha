"""另类情绪数据落库（issue #33）。

为了让本包能独立开发 / 测试且不与并行改动冲突，这里**自带建表语句**并使用
独立库文件 ``data/alt_sentiment.db``（路径通过既有 ``app.services.paths`` 解析），
不修改 ``app/services/sqlite_migrations.py``。

主 agent 若要把它并入 ``funnel_state.db`` 的版本化迁移，见交付报告里的
「集成说明」片段（``alt_sentiment_raw`` / ``alt_sentiment_daily`` 两表 +
``(symbol, trade_date)`` upsert）。

一致性要点：

* 原始条目按 ``(source, symbol, content_hash)`` 去重，重复同步是幂等 no-op；
* 因子行按 ``(symbol, trade_date)`` upsert，保留最早 ``first_seen_as_of``；
* 支持按标的 / 来源 / 日期区间物理删除（可审计、可删除）。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Sequence

from app.services.alt_sentiment.records import (
    AltRecord,
    AltSentimentDaily,
    as_cn_datetime,
)
from app.services.paths import DATA_DIR, data_path
from app.services.time_utils import now_cn

ALT_DB_FILENAME = "alt_sentiment.db"
ALT_DB_PATH = data_path(ALT_DB_FILENAME)

RAW_TABLE = "alt_sentiment_raw"
DAILY_TABLE = "alt_sentiment_daily"

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {RAW_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    symbol TEXT NOT NULL,
    external_id TEXT,
    event_time TEXT,
    as_of_time TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    title TEXT,
    text TEXT,
    url TEXT,
    metrics_json TEXT NOT NULL DEFAULT '{{}}',
    meta_json TEXT NOT NULL DEFAULT '{{}}',
    fetched_at TEXT,
    content_hash TEXT NOT NULL,
    UNIQUE(source, symbol, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_alt_raw_symbol_date
    ON {RAW_TABLE} (source, symbol, trade_date);
CREATE INDEX IF NOT EXISTS idx_alt_raw_as_of
    ON {RAW_TABLE} (as_of_time);
CREATE TABLE IF NOT EXISTS {DAILY_TABLE} (
    symbol TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    as_of_time TEXT NOT NULL,
    first_seen_as_of TEXT,
    values_json TEXT NOT NULL DEFAULT '{{}}',
    availability_json TEXT NOT NULL DEFAULT '{{}}',
    sources_json TEXT NOT NULL DEFAULT '[]',
    alt_attention_score REAL,
    record_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'ok',
    notes_json TEXT NOT NULL DEFAULT '{{}}',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (symbol, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_alt_daily_date ON {DAILY_TABLE} (trade_date);
"""


def default_db_path() -> Path:
    return DATA_DIR / ALT_DB_FILENAME


class AltSentimentStore:
    """短连接 + WAL 的另类情绪存储（读路径不执行 DDL）。"""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        timeout: float = 5.0,
        busy_timeout_ms: int = 5000,
    ) -> None:
        self.db_path = (
            Path(db_path) if db_path is not None else default_db_path()
        ).expanduser()
        if not self.db_path.is_absolute():
            self.db_path = data_path(self.db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self.busy_timeout_ms = max(0, int(busy_timeout_ms))
        self._migrate()

    # ─── 连接与建表 ───

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=self.timeout)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _migrate(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def checkpoint(self) -> None:
        """供 lifespan / 运维脚本调用，规避 macOS WAL fsync 卡死。"""
        conn = sqlite3.connect(str(self.db_path), timeout=self.timeout)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()

    @property
    def db_file(self) -> Path:
        return self.db_path

    # ─── 原始记录 ───

    def upsert_records(self, records: Sequence[AltRecord]) -> dict[str, int]:
        """幂等写入原始条目，返回 ``{"inserted": n, "skipped": m}``。"""
        if not records:
            return {"inserted": 0, "skipped": 0}
        inserted = 0
        with self._connect() as conn:
            for record in records:
                row = record.to_row()
                cursor = conn.execute(
                    f"""
                    INSERT INTO {RAW_TABLE} (
                        source, symbol, external_id, event_time, as_of_time, trade_date,
                        title, text, url, metrics_json, meta_json, fetched_at, content_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source, symbol, content_hash) DO NOTHING
                    """,
                    (
                        row["source"],
                        row["symbol"],
                        row["external_id"],
                        row["event_time"],
                        row["as_of_time"],
                        row["trade_date"],
                        row["title"],
                        row["text"],
                        row["url"],
                        json.dumps(row["metrics"], ensure_ascii=False),
                        json.dumps(row["meta"], ensure_ascii=False),
                        row["fetched_at"],
                        row["content_hash"],
                    ),
                )
                inserted += int(cursor.rowcount or 0)
        return {"inserted": inserted, "skipped": len(records) - inserted}

    def load_records(
        self,
        *,
        symbol: str | None = None,
        source: str | None = None,
        trade_date: str | None = None,
        until_as_of: datetime | str | None = None,
        limit: int = 5000,
    ) -> list[AltRecord]:
        """按 Point-in-Time 读取：``until_as_of`` 之后的记录一律不可见。"""
        clauses: list[str] = []
        params: list[Any] = []
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol)
        if source:
            clauses.append("source = ?")
            params.append(source)
        if trade_date:
            clauses.append("trade_date = ?")
            params.append(trade_date)
        cutoff = as_cn_datetime(until_as_of) if until_as_of else None
        if cutoff is not None:
            clauses.append("as_of_time <= ?")
            params.append(cutoff.isoformat())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM {RAW_TABLE} {where} ORDER BY as_of_time ASC LIMIT ?"
        with self._connect() as conn:
            rows = conn.execute(sql, (*params, int(limit))).fetchall()
        return [self._row_to_record(row) for row in rows]

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> AltRecord:
        return AltRecord(
            source=str(row["source"]),
            symbol=str(row["symbol"]),
            event_time=as_cn_datetime(row["event_time"]),
            as_of_time=as_cn_datetime(row["as_of_time"]),
            title=row["title"],
            text=row["text"],
            url=row["url"],
            external_id=row["external_id"],
            metrics=_load_json(row["metrics_json"], expected=dict),
            meta=_load_json(row["meta_json"], expected=dict),
            fetched_at=as_cn_datetime(row["fetched_at"]),
        )

    def delete_records(
        self,
        *,
        symbol: str | None = None,
        source: str | None = None,
        before_date: str | None = None,
        after_date: str | None = None,
    ) -> int:
        """物理删除（合规要求：数据可删除、可审计）。无过滤条件时拒绝执行。"""
        clauses: list[str] = []
        params: list[Any] = []
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol)
        if source:
            clauses.append("source = ?")
            params.append(source)
        if before_date:
            clauses.append("trade_date < ?")
            params.append(before_date)
        if after_date:
            clauses.append("trade_date > ?")
            params.append(after_date)
        if not clauses:
            raise ValueError("delete_records requires at least one filter")
        with self._connect() as conn:
            cursor = conn.execute(
                f"DELETE FROM {RAW_TABLE} WHERE {' AND '.join(clauses)}", tuple(params)
            )
            return int(cursor.rowcount or 0)

    def record_audit(
        self, *, symbol: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        """审计视图：只返回结构与时间，不返回正文，避免日志泄漏。"""
        clauses = []
        params: list[Any] = []
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT source, symbol, trade_date, as_of_time, event_time, url,
                       fetched_at, content_hash
                FROM {RAW_TABLE} {where}
                ORDER BY as_of_time DESC LIMIT ?
                """,
                (*params, int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    # ─── 因子行 ───

    def upsert_daily(self, rows: Sequence[AltSentimentDaily]) -> int:
        if not rows:
            return 0
        written = 0
        with self._connect() as conn:
            for row in rows:
                payload = row.to_row()
                conn.execute(
                    f"""
                    INSERT INTO {DAILY_TABLE} (
                        symbol, trade_date, as_of_time, first_seen_as_of, values_json,
                        availability_json, sources_json, alt_attention_score,
                        record_count, status, notes_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(symbol, trade_date) DO UPDATE SET
                        as_of_time=excluded.as_of_time,
                        first_seen_as_of=COALESCE({DAILY_TABLE}.first_seen_as_of,
                                                   excluded.first_seen_as_of),
                        values_json=excluded.values_json,
                        availability_json=excluded.availability_json,
                        sources_json=excluded.sources_json,
                        alt_attention_score=excluded.alt_attention_score,
                        record_count=excluded.record_count,
                        status=excluded.status,
                        notes_json=excluded.notes_json,
                        updated_at=excluded.updated_at
                    """,
                    (
                        payload["symbol"],
                        payload["trade_date"],
                        payload["as_of_time"],
                        payload["as_of_time"],
                        json.dumps(payload["values"], ensure_ascii=False),
                        json.dumps(payload["availability"], ensure_ascii=False),
                        json.dumps(payload["sources"], ensure_ascii=False),
                        payload["values"].get("alt_attention_score"),
                        payload["record_count"],
                        payload["status"],
                        json.dumps(payload["notes"], ensure_ascii=False),
                        now_cn().isoformat(),
                    ),
                )
                written += 1
        return written

    def load_daily(
        self,
        *,
        symbol: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 5000,
    ) -> list[AltSentimentDaily]:
        clauses: list[str] = []
        params: list[Any] = []
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol)
        if start_date:
            clauses.append("trade_date >= ?")
            params.append(start_date)
        if end_date:
            clauses.append("trade_date <= ?")
            params.append(end_date)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM {DAILY_TABLE} {where}
                ORDER BY trade_date ASC, symbol ASC LIMIT ?
                """,
                (*params, int(limit)),
            ).fetchall()
        out: list[AltSentimentDaily] = []
        for row in rows:
            values = _load_json(row["values_json"], expected=dict)
            availability = _load_json(row["availability_json"], expected=dict)
            as_of = as_cn_datetime(row["as_of_time"])
            if as_of is None:
                continue
            out.append(
                AltSentimentDaily(
                    symbol=str(row["symbol"]),
                    trade_date=str(row["trade_date"]),
                    as_of_time=as_of,
                    values=values,
                    availability=availability,
                    sources=list(_load_json(row["sources_json"], expected=list) or []),
                    record_count=int(row["record_count"] or 0),
                    status=str(row["status"] or "ok"),
                    notes=_load_json(row["notes_json"], expected=dict),
                    updated_at=as_cn_datetime(row["updated_at"]),
                )
            )
        return out

    def delete_daily(
        self, *, symbol: str | None = None, before_date: str | None = None
    ) -> int:
        clauses: list[str] = []
        params: list[Any] = []
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol)
        if before_date:
            clauses.append("trade_date < ?")
            params.append(before_date)
        if not clauses:
            raise ValueError("delete_daily requires at least one filter")
        with self._connect() as conn:
            cursor = conn.execute(
                f"DELETE FROM {DAILY_TABLE} WHERE {' AND '.join(clauses)}",
                tuple(params),
            )
            return int(cursor.rowcount or 0)

    def coverage(self, trade_date: str, symbols: Sequence[str]) -> dict[str, Any]:
        """另类数据对候选池的覆盖率（验收标准：≥80% 涨停候选）。"""
        unique = [str(s) for s in dict.fromkeys(symbols) if s]
        if not unique:
            return {"trade_date": trade_date, "total": 0, "covered": 0, "ratio": None}
        placeholders = ",".join("?" for _ in unique)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT DISTINCT symbol FROM {DAILY_TABLE}
                WHERE trade_date = ? AND record_count > 0 AND symbol IN ({placeholders})
                """,
                (trade_date, *unique),
            ).fetchall()
        covered = {str(row["symbol"]) for row in rows}
        return {
            "trade_date": trade_date,
            "total": len(unique),
            "covered": len(covered),
            "ratio": round(len(covered) / len(unique), 6),
            "missing_symbols": sorted(set(unique) - covered),
        }

    def stats(self) -> dict[str, Any]:
        with self._connect() as conn:
            raw = conn.execute(
                f"SELECT source, COUNT(*) AS n, MIN(trade_date) AS first_date,"
                f" MAX(trade_date) AS last_date FROM {RAW_TABLE} GROUP BY source"
            ).fetchall()
            daily = conn.execute(
                f"""
                SELECT COUNT(*) AS rows, COUNT(DISTINCT trade_date) AS dates,
                       MAX(trade_date) AS last_date
                FROM {DAILY_TABLE}
                """
            ).fetchone()
        return {
            "db_path": str(self.db_path),
            "raw_by_source": [
                {
                    "source": str(row["source"]),
                    "count": int(row["n"]),
                    "first_date": row["first_date"],
                    "last_date": row["last_date"],
                }
                for row in raw
            ],
            "daily_rows": int(daily["rows"] or 0),
            "daily_dates": int(daily["dates"] or 0),
            "daily_last_date": daily["last_date"],
        }


def _load_json(raw: Any, *, expected: type) -> Any:
    if raw is None or str(raw).strip() == "":
        return expected()
    try:
        loaded = json.loads(raw)
    except (TypeError, ValueError):
        return expected()
    return loaded if isinstance(loaded, expected) else expected()
