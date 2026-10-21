"""后台任务持久化 — 独立 data/jobs.db，使用 PRAGMA user_version 管理迁移。

与 funnel_state.db / market_kline.db 解耦：
- 任务生命周期写放大独立，不影响行情/策略库的 checkpoint；
- 重启后由 JobManager.recover() 将 queued/running 标记为 interrupted。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from app.services.time_utils import now_cn

SCHEMA_VERSION = 1

_ACTIVE_STATUSES = ("queued", "running")
_JOB_FIELDS = {
    "status",
    "progress",
    "result",
    "error",
    "updated_at",
    "finished_at",
}

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    dedupe_key  TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL,
    progress    TEXT NOT NULL DEFAULT '{}',
    params      TEXT NOT NULL DEFAULT '{}',
    result      TEXT,
    error       TEXT,
    request_id  TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_kind_status ON jobs(kind, status);
CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at DESC);
"""


def _dumps(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, default=str)


def _loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


class JobStore:
    def __init__(self, db_path: str | Path = "data/jobs.db") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._migrate()

    # ── 连接与迁移 ──

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=2.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=2000")
        return conn

    def _migrate(self) -> None:
        with self._lock, self._connect() as conn:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version < 1:
                conn.executescript(_SCHEMA_V1)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                conn.commit()

    # ── 写入 ──

    def create(
        self,
        *,
        job_id: str,
        kind: str,
        dedupe_key: str = "",
        params: dict[str, Any] | None = None,
        request_id: str = "",
    ) -> dict[str, Any]:
        ts = now_cn().isoformat(timespec="seconds")
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO jobs
                   (id, kind, dedupe_key, status, progress, params, request_id, created_at, updated_at)
                   VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?)""",
                (
                    job_id,
                    kind,
                    dedupe_key or "",
                    _dumps({"percent": 0.0, "message": "排队中"}),
                    _dumps(params or {}),
                    request_id or "",
                    ts,
                    ts,
                ),
            )
            conn.commit()
        job = self.get(job_id)
        assert job is not None
        return job

    def update(self, job_id: str, **fields: Any) -> dict[str, Any] | None:
        unknown = set(fields) - _JOB_FIELDS
        if unknown:
            raise ValueError(f"unsupported job fields: {sorted(unknown)}")
        if not fields:
            return self.get(job_id)
        payload: dict[str, Any] = {}
        for key, value in fields.items():
            if key == "progress":
                payload[key] = _dumps(value)
            elif key == "result":
                payload[key] = _dumps(value)
            elif key == "error":
                payload[key] = None if value is None else str(value)
            else:
                payload[key] = value
        payload.setdefault("updated_at", now_cn().isoformat(timespec="seconds"))
        assignments = ", ".join(f"{key} = ?" for key in payload)
        with self._lock, self._connect() as conn:
            conn.execute(
                f"UPDATE jobs SET {assignments} WHERE id = ?",
                (*payload.values(), job_id),
            )
            conn.commit()
        return self.get(job_id)

    def mark_stale_interrupted(self, message: str) -> int:
        ts = now_cn().isoformat(timespec="seconds")
        progress = _dumps({"percent": 100.0, "message": message})
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """UPDATE jobs
                   SET status='interrupted', error=?, progress=?, updated_at=?, finished_at=?
                   WHERE status IN ('queued', 'running')""",
                (message, progress, ts, ts),
            )
            conn.commit()
            return int(cursor.rowcount or 0)

    def prune(self, max_retained: int = 200) -> int:
        """保留最近 N 条（活跃任务永不删除）。"""
        keep = max(1, int(max_retained))
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """DELETE FROM jobs
                   WHERE status NOT IN ('queued', 'running')
                     AND id NOT IN (
                         SELECT id FROM jobs ORDER BY created_at DESC, rowid DESC LIMIT ?
                     )""",
                (keep,),
            )
            conn.commit()
            return int(cursor.rowcount or 0)

    # ── 查询 ──

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._row_to_dict(row)

    def find_active(self, kind: str, dedupe_key: str = "") -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT * FROM jobs
                   WHERE kind = ? AND dedupe_key = ? AND status IN ('queued', 'running')
                   ORDER BY created_at DESC, rowid DESC LIMIT 1""",
                (kind, dedupe_key or ""),
            ).fetchone()
        return self._row_to_dict(row)

    def list_jobs(self, *, kind: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 200))
        with self._connect() as conn:
            if kind:
                rows = conn.execute(
                    "SELECT * FROM jobs WHERE kind = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
                    (kind, safe_limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM jobs ORDER BY created_at DESC, rowid DESC LIMIT ?",
                    (safe_limit,),
                ).fetchall()
        return [self._row_to_dict(row) for row in rows if row is not None]

    def stats(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS c FROM jobs GROUP BY status"
            ).fetchall()
        counts = {str(row["status"]): int(row["c"]) for row in rows}
        return {
            "queued": counts.get("queued", 0),
            "running": counts.get("running", 0),
            "succeeded": counts.get("succeeded", 0),
            "failed": counts.get("failed", 0),
            "interrupted": counts.get("interrupted", 0),
        }

    @staticmethod
    def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "progress": _loads(row["progress"], {"percent": 0.0, "message": ""}),
            "params": _loads(row["params"], {}),
            "result": _loads(row["result"], None),
            "error": row["error"],
            "request_id": row["request_id"] or "",
            "dedupe_key": row["dedupe_key"] or "",
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "finished_at": row["finished_at"],
        }
