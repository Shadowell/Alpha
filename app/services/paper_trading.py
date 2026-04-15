"""模拟盘交易服务 — 下单、持仓、平仓、盈亏计算。

数据存储在 data/funnel_state.db 的 paper_positions / paper_trades 表中。
"""
from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from app.services.time_utils import now_cn


_DB_PATH = Path("data/funnel_state.db")


@dataclass
class Position:
    id: str
    symbol: str
    name: str
    direction: str          # "long"
    qty: int                # 股数
    cost_price: float       # 成本价
    current_price: float    # 最新价
    pnl: float              # 浮动盈亏
    pnl_pct: float          # 浮动盈亏%
    opened_at: str          # ISO datetime
    status: str             # "open" / "closed"
    closed_at: str | None = None
    close_price: float | None = None
    realized_pnl: float | None = None
    realized_pnl_pct: float | None = None
    note: str = ""


class PaperTradingService:
    def __init__(self, db_path: str | Path = _DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ── schema ──

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self):
        with self._connect() as conn:
            conn.execute("""
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
                    note TEXT DEFAULT ''
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS paper_trades (
                    id TEXT PRIMARY KEY,
                    position_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    name TEXT NOT NULL DEFAULT '',
                    action TEXT NOT NULL,
                    qty INTEGER NOT NULL,
                    price REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    note TEXT DEFAULT ''
                )
            """)
            conn.commit()

    # ── 开仓 ──

    def open_position(
        self,
        symbol: str,
        name: str,
        price: float,
        qty: int = 100,
        note: str = "",
    ) -> Position:
        pid = uuid.uuid4().hex[:12]
        tid = uuid.uuid4().hex[:12]
        ts = now_cn().isoformat(timespec="seconds")

        with self._connect() as conn:
            conn.execute(
                """INSERT INTO paper_positions
                   (id, symbol, name, direction, qty, cost_price, current_price, opened_at, status, note)
                   VALUES (?, ?, ?, 'long', ?, ?, ?, ?, 'open', ?)""",
                (pid, symbol, name, qty, price, price, ts, note),
            )
            conn.execute(
                """INSERT INTO paper_trades
                   (id, position_id, symbol, name, action, qty, price, created_at, note)
                   VALUES (?, ?, ?, ?, 'buy', ?, ?, ?, ?)""",
                (tid, pid, symbol, name, qty, price, ts, note),
            )
            conn.commit()

        return Position(
            id=pid, symbol=symbol, name=name, direction="long",
            qty=qty, cost_price=price, current_price=price,
            pnl=0, pnl_pct=0, opened_at=ts, status="open", note=note,
        )

    # ── 平仓 ──

    def close_position(self, position_id: str, price: float, note: str = "") -> Position | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM paper_positions WHERE id = ? AND status = 'open'",
                (position_id,),
            ).fetchone()
            if not row:
                return None

            ts = now_cn().isoformat(timespec="seconds")
            tid = uuid.uuid4().hex[:12]
            qty = row["qty"]
            cost = row["cost_price"]
            rpnl = (price - cost) * qty
            rpnl_pct = (price / cost - 1) * 100 if cost else 0

            conn.execute(
                """UPDATE paper_positions
                   SET status='closed', closed_at=?, close_price=?, current_price=?
                   WHERE id=?""",
                (ts, price, price, position_id),
            )
            conn.execute(
                """INSERT INTO paper_trades
                   (id, position_id, symbol, name, action, qty, price, created_at, note)
                   VALUES (?, ?, ?, ?, 'sell', ?, ?, ?, ?)""",
                (tid, position_id, row["symbol"], row["name"], qty, price, ts, note),
            )
            conn.commit()

            return Position(
                id=position_id, symbol=row["symbol"], name=row["name"],
                direction="long", qty=qty, cost_price=cost,
                current_price=price, pnl=rpnl, pnl_pct=round(rpnl_pct, 2),
                opened_at=row["opened_at"], status="closed",
                closed_at=ts, close_price=price,
                realized_pnl=round(rpnl, 2), realized_pnl_pct=round(rpnl_pct, 2),
                note=note,
            )

    # ── 批量更新持仓价格 ──

    def update_prices(self, price_map: dict[str, float]):
        """用最新行情更新所有 open 持仓的 current_price。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, symbol FROM paper_positions WHERE status='open'"
            ).fetchall()
            for r in rows:
                p = price_map.get(r["symbol"])
                if p and p > 0:
                    conn.execute(
                        "UPDATE paper_positions SET current_price=? WHERE id=?",
                        (p, r["id"]),
                    )
            conn.commit()

    # ── 查询 ──

    def get_open_positions(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM paper_positions WHERE status='open' ORDER BY opened_at DESC"
            ).fetchall()
        return [self._to_position_dict(r) for r in rows]

    def get_closed_positions(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM paper_positions WHERE status='closed' ORDER BY closed_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._to_position_dict(r) for r in rows]

    def get_summary(self) -> dict[str, Any]:
        """汇总统计：持仓数、总浮盈、已平仓总盈亏、胜率。"""
        opens = self.get_open_positions()
        closed = self.get_closed_positions(limit=9999)

        total_float_pnl = sum(p["pnl"] for p in opens)
        total_float_cost = sum(p["cost_price"] * p["qty"] for p in opens)

        total_realized = sum(p.get("realized_pnl") or 0 for p in closed)
        wins = sum(1 for p in closed if (p.get("realized_pnl") or 0) > 0)
        win_rate = (wins / len(closed) * 100) if closed else 0

        return {
            "open_count": len(opens),
            "closed_count": len(closed),
            "total_float_pnl": round(total_float_pnl, 2),
            "total_float_pnl_pct": round(total_float_pnl / total_float_cost * 100, 2) if total_float_cost else 0,
            "total_realized_pnl": round(total_realized, 2),
            "win_count": wins,
            "lose_count": len(closed) - wins,
            "win_rate": round(win_rate, 1),
        }

    def get_trades(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM paper_trades ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── helpers ──

    def _to_position_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        cost = d["cost_price"]
        cur = d["current_price"]
        qty = d["qty"]
        if d["status"] == "open":
            d["pnl"] = round((cur - cost) * qty, 2)
            d["pnl_pct"] = round((cur / cost - 1) * 100, 2) if cost else 0
            d["realized_pnl"] = None
            d["realized_pnl_pct"] = None
        else:
            cp = d.get("close_price") or cur
            d["pnl"] = round((cp - cost) * qty, 2)
            d["pnl_pct"] = round((cp / cost - 1) * 100, 2) if cost else 0
            d["realized_pnl"] = d["pnl"]
            d["realized_pnl_pct"] = d["pnl_pct"]
        return d
