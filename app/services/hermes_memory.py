"""Hermes 记忆层 — agent_tasks / agent_proposals / agent_feedback 持久化。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from app.services.time_utils import now_cn


class HermesMemory:
    def __init__(self, db_path: str = "data/funnel_state.db") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent_tasks (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_type     TEXT NOT NULL,
                    trigger       TEXT NOT NULL,
                    status        TEXT NOT NULL,
                    input_summary TEXT,
                    output_summary TEXT,
                    observations  TEXT,
                    tool_calls    TEXT,
                    error_message TEXT,
                    started_at    TEXT NOT NULL,
                    finished_at   TEXT,
                    elapsed_ms    INTEGER,
                    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
                );
                CREATE INDEX IF NOT EXISTS idx_agent_tasks_type ON agent_tasks(task_type);
                CREATE INDEX IF NOT EXISTS idx_agent_tasks_created ON agent_tasks(created_at);

                CREATE TABLE IF NOT EXISTS agent_proposals (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id       INTEGER REFERENCES agent_tasks(id),
                    type          TEXT NOT NULL,
                    title         TEXT NOT NULL,
                    risk_level    TEXT NOT NULL,
                    status        TEXT NOT NULL DEFAULT 'pending',
                    reasoning     TEXT,
                    diff_payload  TEXT,
                    expected_impact TEXT,
                    confidence    REAL,
                    evidence      TEXT,
                    approved_by   TEXT,
                    approved_at   TEXT,
                    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
                );
                CREATE INDEX IF NOT EXISTS idx_agent_proposals_status ON agent_proposals(status);
                CREATE INDEX IF NOT EXISTS idx_agent_proposals_type ON agent_proposals(type);

                CREATE TABLE IF NOT EXISTS agent_feedback (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    proposal_id   INTEGER NOT NULL REFERENCES agent_proposals(id),
                    action        TEXT NOT NULL,
                    note          TEXT,
                    outcome       TEXT,
                    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
                );
                CREATE INDEX IF NOT EXISTS idx_agent_feedback_proposal ON agent_feedback(proposal_id);

                CREATE TABLE IF NOT EXISTS agent_outcome_tracking (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    proposal_id   INTEGER NOT NULL REFERENCES agent_proposals(id),
                    baseline      TEXT NOT NULL,
                    check_after_days INTEGER NOT NULL DEFAULT 3,
                    check_date    TEXT NOT NULL,
                    status        TEXT NOT NULL DEFAULT 'pending',
                    outcome       TEXT,
                    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                    checked_at    TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_outcome_tracking_status ON agent_outcome_tracking(status);
                CREATE INDEX IF NOT EXISTS idx_outcome_tracking_date ON agent_outcome_tracking(check_date);

                CREATE TABLE IF NOT EXISTS agent_monitor_config (
                    id                INTEGER PRIMARY KEY CHECK (id = 1),
                    system_prompt     TEXT NOT NULL,
                    interval_minutes  INTEGER NOT NULL DEFAULT 10,
                    enabled           INTEGER NOT NULL DEFAULT 0,
                    updated_at        TEXT NOT NULL DEFAULT (datetime('now','localtime'))
                );

                CREATE TABLE IF NOT EXISTS agent_monitor_messages (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    content     TEXT NOT NULL,
                    trigger     TEXT NOT NULL DEFAULT 'scheduled',
                    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
                );
                CREATE INDEX IF NOT EXISTS idx_monitor_messages_created ON agent_monitor_messages(created_at);
                """
            )

    # ── agent_tasks ──

    def create_task(self, task_type: str, trigger: str, input_summary: dict | None = None) -> int:
        ts = now_cn().isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO agent_tasks(task_type, trigger, status, input_summary, started_at, created_at)
                   VALUES (?, ?, 'running', ?, ?, ?)""",
                (task_type, trigger, _dumps(input_summary), ts, ts),
            )
            conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    def finish_task(
        self,
        task_id: int,
        *,
        status: str,
        output_summary: dict | None = None,
        observations: dict | None = None,
        tool_calls: list | None = None,
        error_message: str | None = None,
        elapsed_ms: int | None = None,
    ) -> None:
        ts = now_cn().isoformat()
        with self._connect() as conn:
            conn.execute(
                """UPDATE agent_tasks
                   SET status=?, output_summary=?, observations=?, tool_calls=?,
                       error_message=?, finished_at=?, elapsed_ms=?
                   WHERE id=?""",
                (status, _dumps(output_summary), _dumps(observations), _dumps(tool_calls),
                 error_message, ts, elapsed_ms, task_id),
            )
            conn.commit()

    def get_recent_tasks(self, limit: int = 10) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_tasks ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [_row_to_dict(r) for r in rows]

    def get_last_task(self, task_type: str | None = None) -> dict | None:
        with self._connect() as conn:
            if task_type:
                row = conn.execute(
                    "SELECT * FROM agent_tasks WHERE task_type=? ORDER BY id DESC LIMIT 1",
                    (task_type,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM agent_tasks ORDER BY id DESC LIMIT 1"
                ).fetchone()
            return _row_to_dict(row) if row else None

    # ── agent_proposals ──

    def create_proposal(
        self,
        task_id: int,
        *,
        proposal_type: str,
        title: str,
        risk_level: str,
        reasoning: str = "",
        diff_payload: dict | None = None,
        expected_impact: str = "",
        confidence: float = 0.0,
        evidence: list | None = None,
    ) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO agent_proposals(task_id, type, title, risk_level, reasoning,
                       diff_payload, expected_impact, confidence, evidence)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (task_id, proposal_type, title, risk_level, reasoning,
                 _dumps(diff_payload), expected_impact, confidence, _dumps(evidence)),
            )
            conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    def list_proposals(
        self,
        status: str | None = None,
        proposal_type: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[dict], int]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status=?")
            params.append(status)
        if proposal_type:
            clauses.append("type=?")
            params.append(proposal_type)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        with self._connect() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM agent_proposals {where}", params).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM agent_proposals {where} ORDER BY id DESC LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()
            return [_row_to_dict(r) for r in rows], total

    def get_proposal(self, proposal_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM agent_proposals WHERE id=?", (proposal_id,)).fetchone()
            return _row_to_dict(row) if row else None

    def update_proposal_status(self, proposal_id: int, status: str, approved_by: str = "") -> bool:
        ts = now_cn().isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE agent_proposals SET status=?, approved_by=?, approved_at=? WHERE id=?",
                (status, approved_by, ts, proposal_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def count_by_status(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM agent_proposals GROUP BY status"
            ).fetchall()
            return {r["status"]: r["cnt"] for r in rows}

    # ── agent_feedback ──

    def record_feedback(self, proposal_id: int, action: str, note: str = "") -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO agent_feedback(proposal_id, action, note) VALUES (?, ?, ?)",
                (proposal_id, action, note),
            )
            conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    def get_feedback_for_proposal(self, proposal_id: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_feedback WHERE proposal_id=? ORDER BY id", (proposal_id,)
            ).fetchall()
            return [_row_to_dict(r) for r in rows]

    # ── agent_monitor_config ──

    _DEFAULT_MONITOR_PROMPT = """你是A股盘中机会监控雷达。根据实时数据输出简洁的机会分析。

## 格式要求
最多3条主线，按等级排序。每条主线格式：

【等级】主线N：标题（一句话概括）

2) 逻辑链（判断）
- 一句话核心逻辑

3) 关注个股（主板）
- 代码 名称：一句话理由（最多4只）

4) 失效条件/主要风险
- 一句话风险

## 约束
- 仅沪深主板，剔除创业板/科创板/北交所
- 等级用【高】【中高】【中】
- 每条主线控制在10行以内，整体不超过50行
- 末尾附「10分钟内执行摘要」（3行以内）"""

    def get_monitor_config(self) -> dict:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM agent_monitor_config WHERE id=1").fetchone()
            if row:
                return dict(row)
            return {
                "id": 1,
                "system_prompt": self._DEFAULT_MONITOR_PROMPT,
                "interval_minutes": 10,
                "enabled": 0,
                "updated_at": None,
            }

    def save_monitor_config(
        self,
        system_prompt: str | None = None,
        interval_minutes: int | None = None,
        enabled: bool | None = None,
    ) -> dict:
        current = self.get_monitor_config()
        prompt = system_prompt if system_prompt is not None else current["system_prompt"]
        interval = interval_minutes if interval_minutes is not None else current["interval_minutes"]
        ena = (1 if enabled else 0) if enabled is not None else current["enabled"]
        ts = now_cn().isoformat()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO agent_monitor_config(id, system_prompt, interval_minutes, enabled, updated_at)
                   VALUES (1, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     system_prompt=excluded.system_prompt,
                     interval_minutes=excluded.interval_minutes,
                     enabled=excluded.enabled,
                     updated_at=excluded.updated_at""",
                (prompt, interval, ena, ts),
            )
            conn.commit()
        return self.get_monitor_config()

    # ── agent_monitor_messages ──

    def create_monitor_message(self, content: str, trigger: str = "scheduled") -> int:
        ts = now_cn().isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO agent_monitor_messages(content, trigger, created_at) VALUES (?, ?, ?)",
                (content, trigger, ts),
            )
            conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    def list_monitor_messages(self, limit: int = 50, offset: int = 0, today_only: bool = True) -> tuple[list[dict], int]:
        where = ""
        params: list[Any] = []
        if today_only:
            today = now_cn().date().isoformat()
            where = "WHERE created_at >= ?"
            params.append(today)
        with self._connect() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM agent_monitor_messages {where}", params).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM agent_monitor_messages {where} ORDER BY id DESC LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()
            return [dict(r) for r in rows], total

    def get_latest_monitor_message(self) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM agent_monitor_messages ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return dict(row) if row else None

    # ── agent_outcome_tracking ──

    def create_outcome_tracking(
        self,
        proposal_id: int,
        baseline: dict,
        check_after_days: int = 3,
    ) -> int:
        from datetime import timedelta
        check_date = (now_cn() + timedelta(days=check_after_days)).date().isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO agent_outcome_tracking(proposal_id, baseline, check_after_days, check_date)
                   VALUES (?, ?, ?, ?)""",
                (proposal_id, _dumps(baseline), check_after_days, check_date),
            )
            conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    def get_pending_outcome_checks(self, today: str | None = None) -> list[dict]:
        if today is None:
            today = now_cn().date().isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT ot.*, ap.title as proposal_title, ap.type as proposal_type,
                          ap.diff_payload
                   FROM agent_outcome_tracking ot
                   JOIN agent_proposals ap ON ot.proposal_id = ap.id
                   WHERE ot.status = 'pending' AND ot.check_date <= ?
                   ORDER BY ot.check_date""",
                (today,),
            ).fetchall()
            return [_row_to_dict(r) for r in rows]

    def complete_outcome_check(self, tracking_id: int, outcome: dict) -> None:
        ts = now_cn().isoformat()
        with self._connect() as conn:
            conn.execute(
                """UPDATE agent_outcome_tracking
                   SET status='checked', outcome=?, checked_at=?
                   WHERE id=?""",
                (_dumps(outcome), ts, tracking_id),
            )
            conn.execute(
                """UPDATE agent_feedback
                   SET outcome=?
                   WHERE proposal_id=(SELECT proposal_id FROM agent_outcome_tracking WHERE id=?)
                   AND action='approve'""",
                (_dumps(outcome), tracking_id),
            )
            conn.commit()


# ── helpers ──

def _dumps(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    for key in ("input_summary", "output_summary", "observations", "tool_calls",
                "diff_payload", "evidence"):
        if key in d and isinstance(d[key], str):
            try:
                d[key] = json.loads(d[key])
            except Exception:
                pass
    return d
