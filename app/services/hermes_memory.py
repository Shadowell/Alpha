"""Hermes 记忆层 — agent_tasks / agent_monitor_* 持久化。"""
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
- 末尾附「10分钟内执行摘要」（3行以内）

## 预测数据规则（严格遵守）
- 数据中如包含「Kronos 模型预测」部分，这些是真实 AI 模型推理结果
- 引用预测时必须标注"Kronos预测"，并如实引用数据
- 严禁自行编造、推算、臆测任何股价预测数值
- 若某股票没有 Kronos 预测数据，则不对其做任何价格预测"""

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
