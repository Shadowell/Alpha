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

# A股交易费用默认值
DEFAULT_COMMISSION_RATE = 0.00025    # 佣金费率 万2.5（双向）
DEFAULT_MIN_COMMISSION = 5.0         # 最低佣金 5元
DEFAULT_STAMP_TAX_RATE = 0.0005      # 印花税 万5（卖出单向）
DEFAULT_SLIPPAGE_RATE = 0.001        # 滑点 0.1%


@dataclass
class Position:
    id: str
    symbol: str
    name: str
    direction: str          # "long"
    qty: int                # 股数
    cost_price: float       # 成本价（含买入费用分摊）
    current_price: float    # 最新价
    pnl: float              # 浮动盈亏
    pnl_pct: float          # 浮动盈亏%
    opened_at: str          # ISO datetime
    status: str             # "open" / "closed"
    closed_at: str | None = None
    close_price: float | None = None
    realized_pnl: float | None = None
    realized_pnl_pct: float | None = None
    buy_fee: float = 0.0    # 买入总费用
    sell_fee: float = 0.0   # 卖出总费用
    note: str = ""


class PaperTradingService:
    def __init__(self, db_path: str | Path = _DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.commission_rate = DEFAULT_COMMISSION_RATE
        self.min_commission = DEFAULT_MIN_COMMISSION
        self.stamp_tax_rate = DEFAULT_STAMP_TAX_RATE
        self.slippage_rate = DEFAULT_SLIPPAGE_RATE
        self._init_schema()

    def update_settings(self, **kwargs):
        if "commission_rate" in kwargs:
            self.commission_rate = float(kwargs["commission_rate"])
        if "min_commission" in kwargs:
            self.min_commission = float(kwargs["min_commission"])
        if "stamp_tax_rate" in kwargs:
            self.stamp_tax_rate = float(kwargs["stamp_tax_rate"])
        if "slippage_rate" in kwargs:
            self.slippage_rate = float(kwargs["slippage_rate"])

    def get_settings(self) -> dict:
        return {
            "commission_rate": self.commission_rate,
            "min_commission": self.min_commission,
            "stamp_tax_rate": self.stamp_tax_rate,
            "slippage_rate": self.slippage_rate,
        }

    def _calc_buy_cost(self, price: float, qty: int) -> tuple[float, float]:
        """计算买入实际成交价和费用。返回 (实际成交价, 总费用)。"""
        slip_price = price * (1 + self.slippage_rate)
        amount = slip_price * qty
        commission = max(amount * self.commission_rate, self.min_commission)
        total_fee = commission
        return round(slip_price, 4), round(total_fee, 2)

    def _calc_sell_cost(self, price: float, qty: int) -> tuple[float, float]:
        """计算卖出实际成交价和费用。返回 (实际成交价, 总费用)。"""
        slip_price = price * (1 - self.slippage_rate)
        amount = slip_price * qty
        commission = max(amount * self.commission_rate, self.min_commission)
        stamp_tax = amount * self.stamp_tax_rate
        total_fee = commission + stamp_tax
        return round(slip_price, 4), round(total_fee, 2)

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
                    buy_fee REAL NOT NULL DEFAULT 0,
                    sell_fee REAL NOT NULL DEFAULT 0,
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
                    fee REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    note TEXT DEFAULT ''
                )
            """)
            # 兼容旧表：如缺字段则加
            for col, typ in [("buy_fee", "REAL DEFAULT 0"), ("sell_fee", "REAL DEFAULT 0")]:
                try:
                    conn.execute(f"ALTER TABLE paper_positions ADD COLUMN {col} {typ}")
                except Exception:
                    pass
            try:
                conn.execute("ALTER TABLE paper_trades ADD COLUMN fee REAL DEFAULT 0")
            except Exception:
                pass
            conn.commit()

    # ── 开仓 ──

    def open_position(
        self,
        symbol: str,
        name: str,
        price: float,
        qty: int = 100,
        note: str = "",
    ) -> dict:
        pid = uuid.uuid4().hex[:12]
        tid = uuid.uuid4().hex[:12]
        ts = now_cn().isoformat(timespec="seconds")

        actual_price, buy_fee = self._calc_buy_cost(price, qty)

        with self._connect() as conn:
            conn.execute(
                """INSERT INTO paper_positions
                   (id, symbol, name, direction, qty, cost_price, current_price, opened_at, status, buy_fee, note)
                   VALUES (?, ?, ?, 'long', ?, ?, ?, ?, 'open', ?, ?)""",
                (pid, symbol, name, qty, actual_price, actual_price, ts, buy_fee, note),
            )
            conn.execute(
                """INSERT INTO paper_trades
                   (id, position_id, symbol, name, action, qty, price, fee, created_at, note)
                   VALUES (?, ?, ?, ?, 'buy', ?, ?, ?, ?, ?)""",
                (tid, pid, symbol, name, qty, actual_price, buy_fee, ts, note),
            )
            conn.commit()

        return {
            "id": pid, "symbol": symbol, "name": name,
            "qty": qty, "cost_price": actual_price, "buy_fee": buy_fee,
            "market_price": price, "note": note,
        }

    # ── 平仓 ──

    def close_position(self, position_id: str, price: float, note: str = "") -> dict | None:
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
            buy_fee = row["buy_fee"] if "buy_fee" in row.keys() else 0

            actual_sell, sell_fee = self._calc_sell_cost(price, qty)
            total_fee = buy_fee + sell_fee
            rpnl = (actual_sell - cost) * qty - total_fee
            invested = cost * qty
            rpnl_pct = rpnl / invested * 100 if invested else 0

            conn.execute(
                """UPDATE paper_positions
                   SET status='closed', closed_at=?, close_price=?, current_price=?, sell_fee=?
                   WHERE id=?""",
                (ts, actual_sell, actual_sell, sell_fee, position_id),
            )
            conn.execute(
                """INSERT INTO paper_trades
                   (id, position_id, symbol, name, action, qty, price, fee, created_at, note)
                   VALUES (?, ?, ?, ?, 'sell', ?, ?, ?, ?, ?)""",
                (tid, position_id, row["symbol"], row["name"], qty, actual_sell, sell_fee, ts, note),
            )
            conn.commit()

            return {
                "id": position_id, "symbol": row["symbol"], "name": row["name"],
                "qty": qty, "cost_price": cost, "close_price": actual_sell,
                "market_price": price, "buy_fee": buy_fee, "sell_fee": sell_fee,
                "realized_pnl": round(rpnl, 2), "realized_pnl_pct": round(rpnl_pct, 2),
            }

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
        """汇总统计：持仓数、总浮盈、已平仓总盈亏、胜率、总费用。"""
        opens = self.get_open_positions()
        closed = self.get_closed_positions(limit=9999)

        total_float_pnl = sum(p["pnl"] for p in opens)
        total_float_cost = sum(p["cost_price"] * p["qty"] for p in opens)

        total_realized = sum(p.get("realized_pnl") or 0 for p in closed)
        wins = sum(1 for p in closed if (p.get("realized_pnl") or 0) > 0)
        win_rate = (wins / len(closed) * 100) if closed else 0

        total_fee = sum((p.get("buy_fee") or 0) + (p.get("sell_fee") or 0)
                        for p in opens + closed)

        return {
            "open_count": len(opens),
            "closed_count": len(closed),
            "total_float_pnl": round(total_float_pnl, 2),
            "total_float_pnl_pct": round(total_float_pnl / total_float_cost * 100, 2) if total_float_cost else 0,
            "total_realized_pnl": round(total_realized, 2),
            "win_count": wins,
            "lose_count": len(closed) - wins,
            "win_rate": round(win_rate, 1),
            "total_fee": round(total_fee, 2),
            "settings": self.get_settings(),
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
        buy_fee = d.get("buy_fee") or 0
        sell_fee = d.get("sell_fee") or 0
        if d["status"] == "open":
            _, est_sell_fee = self._calc_sell_cost(cur, qty)
            gross = (cur - cost) * qty
            d["pnl"] = round(gross - buy_fee - est_sell_fee, 2)
            invested = cost * qty
            d["pnl_pct"] = round(d["pnl"] / invested * 100, 2) if invested else 0
            d["buy_fee"] = round(buy_fee, 2)
            d["sell_fee"] = round(est_sell_fee, 2)
            d["realized_pnl"] = None
            d["realized_pnl_pct"] = None
        else:
            cp = d.get("close_price") or cur
            total_fee = buy_fee + sell_fee
            rpnl = (cp - cost) * qty - total_fee
            invested = cost * qty
            d["pnl"] = round(rpnl, 2)
            d["pnl_pct"] = round(rpnl / invested * 100, 2) if invested else 0
            d["buy_fee"] = round(buy_fee, 2)
            d["sell_fee"] = round(sell_fee, 2)
            d["realized_pnl"] = d["pnl"]
            d["realized_pnl_pct"] = d["pnl_pct"]
        return d
