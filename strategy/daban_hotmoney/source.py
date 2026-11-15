"""只读数据装载与 PIT 价格账本（issue #34 数据访问层）。

只连 ``market_kline.db`` 的 #22 底座表，**以只读模式打开**（``mode=ro``，
失败则退回 ``PRAGMA query_only=ON``），绝不写库、绝不触发迁移；
单测请传入自己创建的 ``tmp_path`` 数据库，禁止指向仓库 ``data/*.db``。

PIT 口径
--------
``as_of_date`` 表示"该交易日**收盘后**"这一时点：

* 龙虎榜（``dragon_tiger_seat_daily`` / ``dragon_tiger_daily``）：
  只能看到 ``trade_date <= as_of_date`` 的记录（盘后披露）；
* 结果字段（次日溢价 / 3 日收益 / 次日是否再涨停）：要求**观察日**
  ``<= as_of_date``。所以 T 日榜单上的席位，其 T 日事件要等到 T+1 收盘后
  才进入统计，T 日榜单本身在 T 日收盘后即可用于 T+1 入场决策。
* 除权除息日：优先用 ``kline_daily_extras.adj_factor`` 识别，比值异常
  的样本直接丢弃并计数（``ex_rights_dropped``），避免把复权跳空当成席位溢价。
"""

from __future__ import annotations

import sqlite3
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

SEAT_TABLE = "dragon_tiger_seat_daily"
DRAGON_TABLE = "dragon_tiger_daily"
LABEL_TABLE = "hot_money_seat"
BOARD_TABLE = "limit_board_daily"
KLINE_TABLE = "kline_daily"
EXTRAS_TABLE = "kline_daily_extras"

#: #22 降级路径写入的游资名录来源前缀（hm_list / hm_detail 无权限时用营业部名代理）。
PROXY_LABEL_SOURCES = ("proxy.top_inst", "proxy:dragon_tiger_seat_daily")


def open_readonly(db_path: str | Path) -> sqlite3.Connection:
    """以只读方式打开市场库；文件不存在时抛出 ``FileNotFoundError``。"""
    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(f"市场数据库不存在: {path}")
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.OperationalError:
        # WAL 库在只读模式下缺 -shm 时会失败；退回 query_only 保证不可写。
        conn = sqlite3.connect(str(path), timeout=5.0)
        conn.execute("PRAGMA query_only=ON")
    conn.row_factory = sqlite3.Row
    return conn


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (table,)
    ).fetchone()
    return row is not None


def _chunks(items: list[str], size: int = 400) -> Iterable[list[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


# ─── 数据可用性（PARTIAL 判定的依据）───────────────────────────────


def data_availability(conn: sqlite3.Connection) -> dict[str, Any]:
    """各底座的行数与最新交易日；空表要明确报出来，供验收项标 PARTIAL。"""
    out: dict[str, Any] = {}
    for key, table, date_col in (
        ("dragon_tiger_seat", SEAT_TABLE, "trade_date"),
        ("dragon_tiger", DRAGON_TABLE, "trade_date"),
        ("hot_money_seat", LABEL_TABLE, "last_trade_date"),
        ("limit_board", BOARD_TABLE, "trade_date"),
        ("kline_daily", KLINE_TABLE, "trade_date"),
    ):
        if not table_exists(conn, table):
            out[key] = {"table": table, "exists": False, "rows": 0, "latest_date": None}
            continue
        row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()  # noqa: S608
        latest = None
        if row and row["n"]:
            latest_row = conn.execute(
                f"SELECT MAX({date_col}) AS d FROM {table}"  # noqa: S608
            ).fetchone()
            latest = latest_row["d"] if latest_row else None
        out[key] = {
            "table": table,
            "exists": True,
            "rows": int(row["n"]) if row else 0,
            "latest_date": latest,
        }
    required = ("dragon_tiger_seat", "kline_daily")
    usable = all(out[key]["rows"] > 0 for key in required)
    out["usable_for_profiling"] = usable
    out["blocking_datasets"] = [
        key
        for key in ("dragon_tiger_seat", "kline_daily", "limit_board")
        if out[key]["rows"] == 0
    ]
    return out


def label_source_flags(conn: sqlite3.Connection) -> dict[str, Any]:
    """游资名录是否来自 #22 的营业部名代理（决定 ``hot_money_label_is_proxy``）。"""
    if not table_exists(conn, LABEL_TABLE):
        return {
            "label_table_rows": 0,
            "label_source": "missing",
            "hot_money_label_is_proxy": 1,
            "label_sources": [],
        }
    rows = conn.execute(
        f"SELECT source, COUNT(*) AS n FROM {LABEL_TABLE} GROUP BY source"  # noqa: S608
    ).fetchall()
    counts = {str(r["source"] or ""): int(r["n"]) for r in rows}
    total = sum(counts.values())
    proxy_rows = sum(n for src, n in counts.items() if src.startswith("proxy"))
    if total == 0:
        source = "missing"
    elif proxy_rows == total:
        source = "seat_name_proxy"
    elif proxy_rows:
        source = "mixed"
    else:
        source = "hm_list"
    return {
        "label_table_rows": total,
        "label_source": source,
        "hot_money_label_is_proxy": int(
            source in {"seat_name_proxy", "mixed", "missing"}
        ),
        "label_sources": counts,
    }


# ─── 龙虎榜席位事件 ───────────────────────────────────────────────


def load_seat_events(
    conn: sqlite3.Connection,
    *,
    start_date: str | None = None,
    upto_date: str,
    symbols: Iterable[str] | None = None,
    limit: int = 400_000,
) -> list[dict[str, Any]]:
    """读取 ``trade_date <= upto_date`` 的席位明细（PIT 上界强制生效）。"""
    if not table_exists(conn, SEAT_TABLE):
        return []
    sql = f"SELECT * FROM {SEAT_TABLE} WHERE trade_date <= ?"  # noqa: S608
    params: list[Any] = [upto_date]
    if start_date:
        sql += " AND trade_date >= ?"
        params.append(start_date)
    symbol_list = [str(s).zfill(6) for s in symbols or []]
    if symbol_list:
        marks = ",".join("?" for _ in symbol_list)
        sql += f" AND symbol IN ({marks})"
        params.extend(symbol_list)
    sql += " ORDER BY trade_date ASC, symbol ASC, seat_name ASC LIMIT ?"
    params.append(int(limit))
    return _rows_to_dicts(conn.execute(sql, params).fetchall())


def load_dragon_tiger(
    conn: sqlite3.Connection, *, upto_date: str, start_date: str | None = None
) -> list[dict[str, Any]]:
    if not table_exists(conn, DRAGON_TABLE):
        return []
    sql = f"SELECT * FROM {DRAGON_TABLE} WHERE trade_date <= ?"  # noqa: S608
    params: list[Any] = [upto_date]
    if start_date:
        sql += " AND trade_date >= ?"
        params.append(start_date)
    return _rows_to_dicts(conn.execute(sql, params).fetchall())


def load_board_rows(
    conn: sqlite3.Connection, *, upto_date: str, start_date: str | None = None
) -> list[dict[str, Any]]:
    """涨停板日线行（板型 / 题材 / 市值 / 连板高度），供偏好与交互项使用。"""
    if not table_exists(conn, BOARD_TABLE):
        return []
    sql = (
        f"SELECT symbol, trade_date, limit_flag, limit_times, up_stat, board_type, "
        f"theme, float_mv, total_mv, close, pct_chg, amount, fd_amount "
        f"FROM {BOARD_TABLE} WHERE trade_date <= ?"
    )  # noqa: S608
    params: list[Any] = [upto_date]
    if start_date:
        sql += " AND trade_date >= ?"
        params.append(start_date)
    return _rows_to_dicts(conn.execute(sql, params).fetchall())


def load_kline_rows(
    conn: sqlite3.Connection,
    *,
    upto_date: str,
    start_date: str | None = None,
    symbols: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    if not table_exists(conn, KLINE_TABLE):
        return []
    sql = (
        f"SELECT k.symbol, k.trade_date, k.open, k.high, k.low, k.close, k.volume, k.amount "
        f"FROM {KLINE_TABLE} k WHERE k.trade_date <= ?"
    )  # noqa: S608
    params: list[Any] = [upto_date]
    if start_date:
        sql += " AND k.trade_date >= ?"
        params.append(start_date)
    symbol_list = [str(s).zfill(6) for s in symbols or []]
    if symbol_list:
        marks = ",".join("?" for _ in symbol_list)
        sql += f" AND k.symbol IN ({marks})"
        params.extend(symbol_list)
    sql += " ORDER BY k.symbol ASC, k.trade_date ASC"
    return _rows_to_dicts(conn.execute(sql, params).fetchall())


def load_adj_factors(
    conn: sqlite3.Connection, *, upto_date: str, start_date: str | None = None
) -> dict[tuple[str, str], float]:
    """``(symbol, trade_date) -> adj_factor``；表缺失或字段为空时返回空 dict。"""
    if not table_exists(conn, EXTRAS_TABLE):
        return {}
    sql = (
        f"SELECT symbol, trade_date, adj_factor FROM {EXTRAS_TABLE} "
        "WHERE trade_date <= ? AND adj_factor IS NOT NULL"
    )
    params: list[Any] = [upto_date]
    if start_date:
        sql += " AND trade_date >= ?"
        params.append(start_date)
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {
        (str(r["symbol"]).zfill(6), str(r["trade_date"])): float(r["adj_factor"])
        for r in rows
        if r["adj_factor"]
    }


# ─── PIT 价格账本 ─────────────────────────────────────────────────


@dataclass(slots=True)
class PitPriceBook:
    """按 ``<= as_of_date`` 截断后的日线 / 涨停板 / 复权因子账本。

    日历来自 ``kline_daily`` 的交易日集合：任何"第 n 个交易日之后"的索引
    都只能在日历内前进，越界返回 ``None``（= 观察日尚未到来，PIT 拒绝）。
    """

    as_of_date: str
    calendar: list[str]
    kline: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    board: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    adj_factors: dict[tuple[str, str], float] = field(default_factory=dict)
    notes: dict[str, int] = field(default_factory=dict)

    # -- 日历 ----------------------------------------------------------

    def index(self, date: str) -> int | None:
        pos = bisect_left(self.calendar, date)
        if pos < len(self.calendar) and self.calendar[pos] == date:
            return pos
        return None

    def nth_trade_day_after(self, date: str, n: int) -> str | None:
        """严格大于 ``date`` 的第 n 个交易日；**超过 as_of 收盘则返回 None**。"""
        if n <= 0:
            raise ValueError("nth_trade_day_after 的 n 必须为正整数")
        pos = bisect_right(self.calendar, date)
        target = pos + n - 1
        if target >= len(self.calendar):
            return None
        day = self.calendar[target]
        return day if day <= self.as_of_date else None

    def first_trade_day_on_or_after(self, date: str) -> str | None:
        pos = bisect_left(self.calendar, date)
        if pos >= len(self.calendar):
            return None
        day = self.calendar[pos]
        return day if day <= self.as_of_date else None

    # -- 行情 ----------------------------------------------------------

    def bar(self, symbol: str, date: str | None) -> dict[str, Any] | None:
        if not date:
            return None
        return self.kline.get(str(symbol).zfill(6), {}).get(date)

    def close(self, symbol: str, date: str | None) -> float | None:
        bar = self.bar(symbol, date)
        if bar is None or bar.get("close") is None:
            return None
        return float(bar["close"])

    def board_row(self, symbol: str, date: str | None) -> dict[str, Any] | None:
        if not date:
            return None
        return self.board.get(str(symbol).zfill(6), {}).get(date)

    def is_limit_up(self, symbol: str, date: str | None) -> bool | None:
        """T+1 是否封住涨停：有板行时看 ``limit_flag='U'``；板表缺数据返回 None。"""
        row = self.board_row(symbol, date)
        if row is None:
            return None
        flag = row.get("limit_flag")
        if flag is None:
            return None
        return str(flag).upper() == "U"

    # -- 复权 ----------------------------------------------------------

    def ex_rights_between(self, symbol: str, d1: str, d2: str) -> bool:
        """两日之间是否发生除权除息（adj_factor 变动 > 0.1%）。

        无复权因子时返回 ``False``（不阻断统计），由调用方在 notes 里统计
        ``adj_factor_missing`` 比例，避免"因子缺失 → 静默全样本失真"。
        """
        f1 = self.adj_factors.get((str(symbol).zfill(6), d1))
        f2 = self.adj_factors.get((str(symbol).zfill(6), d2))
        if not f1 or not f2:
            return False
        return abs(f2 / f1 - 1.0) > 1e-3


def build_price_book(
    conn: sqlite3.Connection,
    *,
    as_of_date: str,
    start_date: str | None = None,
    symbols: Iterable[str] | None = None,
) -> PitPriceBook:
    """一次性把窗口内日线 / 板行 / 复权因子读进内存（只读）。"""
    kline_rows = load_kline_rows(
        conn, upto_date=as_of_date, start_date=start_date, symbols=symbols
    )
    calendar = sorted({str(r["trade_date"]) for r in kline_rows})
    if not calendar:
        # 日线为空时退化成"用板表日期当日历"，但仍要求 <= as_of
        board_rows = load_board_rows(conn, upto_date=as_of_date, start_date=start_date)
        calendar = sorted({str(r["trade_date"]) for r in board_rows})
    kline: dict[str, dict[str, dict[str, Any]]] = {}
    for row in kline_rows:
        kline.setdefault(str(row["symbol"]).zfill(6), {})[str(row["trade_date"])] = row
    board: dict[str, dict[str, dict[str, Any]]] = {}
    for row in load_board_rows(conn, upto_date=as_of_date, start_date=start_date):
        board.setdefault(str(row["symbol"]).zfill(6), {})[str(row["trade_date"])] = row
    return PitPriceBook(
        as_of_date=as_of_date,
        calendar=calendar,
        kline=kline,
        board=board,
        adj_factors=load_adj_factors(conn, upto_date=as_of_date, start_date=start_date),
    )


# ─── 席位事件的结果字段（严格 PIT）─────────────────────────────────


@dataclass(frozen=True, slots=True)
class EventOutcome:
    """一个席位事件（T 日某股某席位）在 PIT 约束下的可观察结果。"""

    usable: bool
    reason: str | None = None
    next_day: str | None = None
    day3: str | None = None
    next_open_premium: float | None = None
    next_close_premium: float | None = None
    ret_3d: float | None = None
    next_is_limit_up: bool | None = None
    next_day_drop: float | None = None
    outcome_observed_through: str | None = None


def compute_event_outcome(
    book: PitPriceBook,
    symbol: str,
    trade_date: str,
    *,
    dump_threshold: float = -0.05,
) -> EventOutcome:
    """计算"次日开盘溢价 / 次日收盘溢价 / 3 日收益 / 次日是否再板 / 次日大跌"。

    任何观察日晚于 ``as_of_date`` 的字段一律不可用（``usable=False``），
    这是防前视泄漏的硬约束：T 日榜单的结果要到 T+1（甚至 T+3）收盘后才成立。
    """
    close_t = book.close(symbol, trade_date)
    if close_t is None or close_t <= 0:
        return EventOutcome(False, "no_t_close")
    next_day = book.nth_trade_day_after(trade_date, 1)
    day3 = book.nth_trade_day_after(trade_date, 3)
    if next_day is None:
        return EventOutcome(False, "outcome_not_settled_yet")
    if book.ex_rights_between(symbol, trade_date, next_day):
        return EventOutcome(False, "ex_rights", next_day=next_day, day3=day3)
    bar_next = book.bar(symbol, next_day)
    if bar_next is None or bar_next.get("open") is None:
        return EventOutcome(False, "no_next_bar")
    open_premium = float(bar_next["open"]) / close_t - 1.0
    close_next = book.close(symbol, next_day)
    if close_next is None:
        return EventOutcome(False, "no_next_close")
    close_premium = close_next / close_t - 1.0
    ret_3d: float | None = None
    if day3 is not None:
        close_3 = book.close(symbol, day3)
        if close_3 is not None and not book.ex_rights_between(symbol, trade_date, day3):
            ret_3d = close_3 / close_t - 1.0
    observed_through = max(filter(None, [next_day, day3] if day3 else [next_day]))
    return EventOutcome(
        usable=True,
        reason=None,
        next_day=next_day,
        day3=day3,
        next_open_premium=open_premium,
        next_close_premium=close_premium,
        ret_3d=ret_3d,
        next_is_limit_up=book.is_limit_up(symbol, next_day),
        next_day_drop=1.0 if close_premium <= dump_threshold else 0.0,
        outcome_observed_through=observed_through,
    )


__all__ = [
    "BOARD_TABLE",
    "DRAGON_TABLE",
    "EXTRAS_TABLE",
    "EventOutcome",
    "KLINE_TABLE",
    "LABEL_TABLE",
    "PitPriceBook",
    "PROXY_LABEL_SOURCES",
    "SEAT_TABLE",
    "build_price_book",
    "compute_event_outcome",
    "data_availability",
    "label_source_flags",
    "load_adj_factors",
    "load_board_rows",
    "load_dragon_tiger",
    "load_kline_rows",
    "load_seat_events",
    "open_readonly",
    "table_exists",
]
