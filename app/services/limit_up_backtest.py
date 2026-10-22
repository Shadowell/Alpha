"""#26 打板可成交性、容量与真实交易约束回测引擎（纯服务层）。

设计约束（与 issue #26 / AGENTS.md 对齐）：

* **纯服务**：本模块不接 HTTP、不注册 API、不改 ``app/main.py``；主 agent 后续
  通过 :func:`run_limit_up_backtest` 等入口接线。
* **无未来函数**：T 日排队成交概率只用 T 日收盘前可知的字段
  （``fd_amount`` / ``amount`` / ``first_time`` / ``open_times`` / ``limit_flag``）；
  绝不使用 T+1 数据反推 T 日成交。
* **不造数**：数据缺失（涨停表无记录、无 ``up_limit``、停牌、基准缺数）一律显式
  记录 skip / note，不做插值或伪造。
* **strict 默认**：无法确认成交的信号在 strict 模式一律不计入收益；loose 模式
  用可复现的概率模型（固定 ``seed``）允许模型估计成交。

数据来源（只读，市场库 ``market_kline.db``）：

* ``limit_board_daily``（#22 契约：金额单位元，空值 NULL，``limit_flag`` U/D/Z）；
* ``kline_daily``（日线 OHLCV + amount，元）；
* ``kline_daily_extras``（daily_basic 落库表：``up_limit`` / ``down_limit`` /
  ``circ_mv`` / ``adj_factor`` 等）；
* ``index_daily``（指数基准）。

覆盖范围限制：本引擎假设 T+0..T+N 全部在 ``kline_daily`` 覆盖窗口内；``as_of``
之后的数据一律不可见（PIT）。

主要公开入口（供主 agent 接入 API 的函数签名）::

    run_limit_up_backtest(signals, *, db_path, as_of=None, mode="strict", ...) -> dict
    run_strict_loose_comparison(signals, *, db_path, as_of=None, ...) -> dict
    capacity_curve(signals, *, db_path, capitals, ...) -> dict
    legacy_lab_diff_report(legacy_samples, report) -> dict
    compute_fill_stats(report) -> dict   # report 内已含，单独暴露便于聚合
"""

from __future__ import annotations

import bisect
import hashlib
import json
import math
import random
import sqlite3
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# ──────────────────────────── 配置（全部可配置并写进报告） ────────────────────────────


@dataclass
class FeeConfig:
    """交易费用模型（默认值即 #26 约定口径）。"""

    commission_rate: float = 2.5e-4  # 佣金万 2.5
    min_commission: float = 5.0  # 单笔最低 5 元
    stamp_tax_rate: float = 5e-4  # 印花税（仅卖出）万 5
    transfer_fee_rate: float = 1e-5  # 过户费万 0.1（双边，可关）
    transfer_fee_enabled: bool = True

    def buy_costs(self, gross_amount: float) -> dict[str, float]:
        commission = max(gross_amount * self.commission_rate, self.min_commission)
        transfer = (
            gross_amount * self.transfer_fee_rate if self.transfer_fee_enabled else 0.0
        )
        return {"commission": commission, "transfer_fee": transfer, "stamp_tax": 0.0}

    def sell_costs(self, gross_amount: float) -> dict[str, float]:
        commission = max(gross_amount * self.commission_rate, self.min_commission)
        transfer = (
            gross_amount * self.transfer_fee_rate if self.transfer_fee_enabled else 0.0
        )
        stamp = gross_amount * self.stamp_tax_rate
        return {"commission": commission, "transfer_fee": transfer, "stamp_tax": stamp}


@dataclass
class SlippageConfig:
    """动态滑点 / 冲击成本：参与率越高滑点越大。

    ``slip_bps = base_bps + impact_bps_at_ref * min(participation / ref_participation, cap)``
    其中 participation = 成交金额 / 当日成交额，ref 与 cap 可配置。
    """

    base_bps: float = 10.0
    impact_bps_at_ref: float = 30.0
    ref_participation: float = 0.05
    max_multiplier: float = 4.0


@dataclass
class CapacityConfig:
    """容量约束：单票单日成交参与率上限；超限缩量或跳过。"""

    max_participation: float = 0.05
    action: str = "scale"  # "scale" 按比例缩量 / "skip" 直接跳过

    def __post_init__(self) -> None:
        if self.action not in ("scale", "skip"):
            raise ValueError("CapacityConfig.action 必须是 'scale' 或 'skip'")


@dataclass
class QueueFillConfig:
    """T 日涨停排队成交概率模型（纯启发式，无 L2 数据，标注为模型假设）。

    只用 T 日收盘前可知字段：封单/成交额比（seal_ratio = fd_amount/amount）、
    首封时间 ``first_time``、开板次数 ``open_times``、``limit_flag``。
    """

    # 封单比阈值：seal_ratio >= high 视为封死（成交概率下限），<= low 视为薄封。
    seal_high_ratio: float = 0.30
    seal_low_ratio: float = 0.03
    yizhi_prob_cap: float = 0.25  # 一字板 loose 成交概率上限
    reopen_prob_base: float = 0.35  # 盘中炸板回封的排队成交概率基准
    zhaban_fill: bool = True  # 尾盘未封（Z）→ 排队单必然在开板期间成交（确定性）


# 市值分层默认阈值（元），基于 limit_board_daily.float_mv / extras.circ_mv
DEFAULT_MCAP_BUCKETS: tuple[float, ...] = (2e8, 5e8, 1e9, 5e9)

TRADING_DAYS_PER_YEAR = 242


# ──────────────────────────── 只读数据访问层 ────────────────────────────


class MarketDataView:
    """对 market_kline.db 的只读、point-in-time 视图。

    直接开 sqlite 只读连接，不实例化 KlineSQLiteStore，避免对生产库文件
    触发迁移副作用。
    """

    def __init__(self, db_path: str | Path, as_of: str | None = None) -> None:
        self.db_path = Path(db_path)
        if not self.db_path.exists():
            raise FileNotFoundError(f"市场数据库不存在: {self.db_path}")
        self.as_of = as_of
        self._calendar: list[str] | None = None
        self._kline_cache: dict[str, dict[str, dict[str, Any]]] = {}
        self._extras_cache: dict[str, dict[str, dict[str, Any]]] = {}
        self._board_cache: dict[str, dict[str, dict[str, Any]]] = {}

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    # -- 交易日历 -------------------------------------------------------

    @property
    def calendar(self) -> list[str]:
        """kline_daily 中的全部交易日（升序，受 as_of 截断）。"""
        if self._calendar is None:
            with self._connect() as conn:
                if self.as_of:
                    rows = conn.execute(
                        "SELECT DISTINCT trade_date FROM kline_daily "
                        "WHERE trade_date <= ? ORDER BY trade_date ASC",
                        (self.as_of,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT DISTINCT trade_date FROM kline_daily ORDER BY trade_date ASC"
                    ).fetchall()
            self._calendar = [r["trade_date"] for r in rows]
        return self._calendar

    def next_trade_day(self, date: str) -> str | None:
        """严格大于 ``date`` 的首个交易日（date 本身在日历中则取其后一日）。"""
        cal = self.calendar
        idx = bisect.bisect_right(cal, date)
        return cal[idx] if idx < len(cal) else None

    def has_trade_day(self, date: str) -> bool:
        return date in self.calendar

    # -- 单表读取（按 symbol 缓存） --------------------------------------

    def kline(self, symbol: str) -> dict[str, dict[str, Any]]:
        if symbol not in self._kline_cache:
            with self._connect() as conn:
                if self.as_of:
                    rows = conn.execute(
                        "SELECT trade_date, open, high, low, close, volume, amount "
                        "FROM kline_daily WHERE symbol = ? AND trade_date <= ? "
                        "ORDER BY trade_date ASC",
                        (symbol, self.as_of),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT trade_date, open, high, low, close, volume, amount "
                        "FROM kline_daily WHERE symbol = ? ORDER BY trade_date ASC",
                        (symbol,),
                    ).fetchall()
            self._kline_cache[symbol] = {r["trade_date"]: dict(r) for r in rows}
        return self._kline_cache[symbol]

    def extras(self, symbol: str) -> dict[str, dict[str, Any]]:
        if symbol not in self._extras_cache:
            with self._connect() as conn:
                try:
                    if self.as_of:
                        rows = conn.execute(
                            "SELECT trade_date, turnover_rate, total_mv, circ_mv, "
                            "up_limit, down_limit, adj_factor FROM kline_daily_extras "
                            "WHERE symbol = ? AND trade_date <= ? ORDER BY trade_date ASC",
                            (symbol, self.as_of),
                        ).fetchall()
                    else:
                        rows = conn.execute(
                            "SELECT trade_date, turnover_rate, total_mv, circ_mv, "
                            "up_limit, down_limit, adj_factor FROM kline_daily_extras "
                            "WHERE symbol = ? ORDER BY trade_date ASC",
                            (symbol,),
                        ).fetchall()
                except sqlite3.OperationalError:
                    rows = []
            self._extras_cache[symbol] = {r["trade_date"]: dict(r) for r in rows}
        return self._extras_cache[symbol]

    def board(self, symbol: str) -> dict[str, dict[str, Any]]:
        if symbol not in self._board_cache:
            with self._connect() as conn:
                try:
                    if self.as_of:
                        rows = conn.execute(
                            "SELECT * FROM limit_board_daily "
                            "WHERE symbol = ? AND trade_date <= ? ORDER BY trade_date ASC",
                            (symbol, self.as_of),
                        ).fetchall()
                    else:
                        rows = conn.execute(
                            "SELECT * FROM limit_board_daily WHERE symbol = ? "
                            "ORDER BY trade_date ASC",
                            (symbol,),
                        ).fetchall()
                except sqlite3.OperationalError:
                    rows = []
            self._board_cache[symbol] = {r["trade_date"]: dict(r) for r in rows}
        return self._board_cache[symbol]

    def index_daily(self, ts_code: str) -> dict[str, dict[str, Any]]:
        with self._connect() as conn:
            try:
                if self.as_of:
                    rows = conn.execute(
                        "SELECT trade_date, close, pct_chg FROM index_daily "
                        "WHERE ts_code = ? AND trade_date <= ? ORDER BY trade_date ASC",
                        (ts_code, self.as_of),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT trade_date, close, pct_chg FROM index_daily "
                        "WHERE ts_code = ? ORDER BY trade_date ASC",
                        (ts_code,),
                    ).fetchall()
            except sqlite3.OperationalError:
                rows = []
        return {r["trade_date"]: dict(r) for r in rows}

    def equal_weight_returns(self, start: str, end: str | None) -> dict[str, float]:
        """等权全市场基准：每交易日所有股票 pct 收益均值（SQLite 窗口函数）。

        LAG 取数窗口向 start 前多取一个交易日做 warm-up，避免首日收益全为空。
        """
        if not (start and self.calendar):
            return {}
        end = end or self.as_of or self.calendar[-1]
        idx = bisect.bisect_left(self.calendar, start)
        warm_start = self.calendar[idx - 1] if idx > 0 else start
        sql = """
            WITH r AS (
                SELECT trade_date,
                       close / LAG(close) OVER (PARTITION BY symbol ORDER BY trade_date) - 1.0 AS ret
                FROM kline_daily
                WHERE trade_date >= ? AND trade_date <= ?
            )
            SELECT trade_date, AVG(ret) AS eq_ret FROM r
            WHERE ret IS NOT NULL AND ret BETWEEN -0.35 AND 0.35 AND trade_date >= ?
            GROUP BY trade_date ORDER BY trade_date ASC
        """
        with self._connect() as conn:
            try:
                rows = conn.execute(sql, (warm_start, end, start)).fetchall()
            except sqlite3.OperationalError:
                return {}
        return {r["trade_date"]: float(r["eq_ret"]) for r in rows}

    def data_watermark(self) -> dict[str, Any]:
        """数据版本快照：各表最大日期 + 行数，供结果复现。"""
        out: dict[str, Any] = {"db": str(self.db_path), "as_of": self.as_of}
        try:
            stat = self.db_path.stat()
            out["db_size"] = stat.st_size
            out["db_mtime"] = int(stat.st_mtime)
        except OSError:
            pass
        with self._connect() as conn:
            for table, date_col in (
                ("kline_daily", "trade_date"),
                ("limit_board_daily", "trade_date"),
                ("kline_daily_extras", "trade_date"),
                ("index_daily", "trade_date"),
            ):
                try:
                    row = conn.execute(
                        f"SELECT COUNT(*) AS n, MAX({date_col}) AS mx FROM {table}"  # noqa: S608
                    ).fetchone()
                    out[f"{table}_rows"] = int(row["n"])
                    out[f"{table}_max_date"] = row["mx"]
                except sqlite3.OperationalError:
                    out[f"{table}_rows"] = None
        return out


def bisect_left_str(sorted_dates: list[str], date: str) -> int:
    return bisect.bisect_left(sorted_dates, date)


# ──────────────────────────── 可成交性判定原语 ────────────────────────────


def _limit_pct_for(symbol: str, name: str | None) -> float:
    """涨停幅度（小数）。与本仓库扫描器口径一致：ST 5%，创业板/科创板 20%，主板 10%。"""
    sym = (symbol or "").strip()
    nm = (name or "").upper()
    if "ST" in nm:
        return 0.05
    if sym.startswith(("30", "688")):
        return 0.20
    return 0.10


def round_limit_price(prev_close: float, pct: float) -> float:
    """A 股涨停价 = round(prev_close*(1+pct), 2)。"""
    return round(prev_close * (1.0 + pct), 2)


@dataclass
class BoardContext:
    """单个 (symbol, trade_date) 的可成交性上下文。"""

    symbol: str
    trade_date: str
    bar: dict[str, Any] | None
    board_row: dict[str, Any] | None
    extras_row: dict[str, Any] | None
    up_limit: float | None
    down_limit: float | None
    limit_pct: float
    notes: list[str] = field(default_factory=list)

    # ---- 板型判断 -------------------------------------------------------

    @property
    def suspended(self) -> bool:
        return self.bar is None

    @property
    def sealed_at_close(self) -> bool:
        """收盘封在涨停价上（limit_flag=U 或 close==up_limit 推断）。"""
        if self.suspended:
            return False
        flag = str((self.board_row or {}).get("limit_flag") or "").upper()
        if flag in ("U", "D", "Z"):
            return flag == "U"
        close = float(self.bar["close"])
        return self.up_limit is not None and abs(close - self.up_limit) < 1e-6

    @property
    def zhaban(self) -> bool:
        """炸板：盘中触及涨停但收盘未封（limit_flag=Z 或推断）。"""
        if self.suspended:
            return False
        flag = str((self.board_row or {}).get("limit_flag") or "").upper()
        if flag == "Z":
            return True
        if flag == "U":
            return False
        high = float(self.bar["high"])
        close = float(self.bar["close"])
        return (
            self.up_limit is not None
            and abs(high - self.up_limit) < 1e-6
            and close < self.up_limit - 1e-6
        )

    @property
    def yizhi(self) -> bool:
        """一字涨停板：开高低收全部等于涨停价。"""
        if self.suspended or self.up_limit is None:
            return False
        b = self.bar
        eps = 1e-6
        return (
            abs(float(b["open"]) - self.up_limit) < eps
            and abs(float(b["low"]) - self.up_limit) < eps
            and abs(float(b["high"]) - self.up_limit) < eps
            and abs(float(b["close"]) - self.up_limit) < eps
        )

    @property
    def down_sealed_at_close(self) -> bool:
        """收盘封死跌停（卖出受阻判断）。"""
        if self.suspended:
            return False
        flag = str((self.board_row or {}).get("limit_flag") or "").upper()
        close = float(self.bar["close"])
        if flag == "D":
            return True
        return self.down_limit is not None and abs(close - self.down_limit) < 1e-6

    @property
    def yizhi_down(self) -> bool:
        """一字跌停：开=高=低=收=跌停价（开盘即无法卖出）。"""
        if self.suspended or self.down_limit is None:
            return False
        b = self.bar
        eps = 1e-6
        return (
            abs(float(b["open"]) - self.down_limit) < eps
            and abs(float(b["high"]) - self.down_limit) < eps
            and abs(float(b["low"]) - self.down_limit) < eps
            and abs(float(b["close"]) - self.down_limit) < eps
        )

    @property
    def seal_ratio(self) -> float | None:
        """封单/成交额比（fd_amount / amount），只用 T 日收盘前可知字段。"""
        if not self.board_row:
            return None
        fd = self.board_row.get("fd_amount")
        amt = self.board_row.get("amount")
        if fd is None or amt is None or float(amt) <= 0:
            return None
        return float(fd) / float(amt)

    @property
    def limit_times(self) -> int | None:
        v = (self.board_row or {}).get("limit_times")
        return int(v) if v is not None else None

    @property
    def open_times(self) -> int:
        v = (self.board_row or {}).get("open_times")
        return int(v) if v is not None else 0

    @property
    def float_mv(self) -> float | None:
        for row, key in ((self.board_row, "float_mv"), (self.extras_row, "circ_mv")):
            if row and row.get(key) is not None:
                return float(row[key])
        return None


def build_board_context(
    view: MarketDataView, symbol: str, date: str, name: str | None
) -> BoardContext:
    """组装 (symbol, date) 上下文；缺 up_limit 时用 prev_close×涨停幅度推算并标注。"""
    kline = view.kline(symbol)
    bar = kline.get(date)
    extras_all = view.extras(symbol)
    extras_row = extras_all.get(date)
    board_row = view.board(symbol).get(date)

    up_limit: float | None = None
    down_limit: float | None = None
    notes: list[str] = []
    if extras_row:
        if extras_row.get("up_limit") is not None:
            up_limit = float(extras_row["up_limit"])
            down_limit = (
                float(extras_row["down_limit"])
                if extras_row.get("down_limit") is not None
                else None
            )
    pct = _limit_pct_for(symbol, name or (board_row or {}).get("name"))
    if bar is not None:
        prev_dates = [d for d in kline if d < date]
        prev_close = None
        if prev_dates:
            prev_close = float(kline[prev_dates[-1]]["close"])
        if up_limit is None and prev_close:
            up_limit = round_limit_price(prev_close, pct)
            notes.append("up_limit_inferred_from_prev_close")
        if down_limit is None and prev_close:
            down_limit = round(prev_close * (1.0 - pct), 2)
        if bar.get("amount") and float(bar["amount"]) <= 0:
            notes.append("zero_amount_bar")
    if board_row is None:
        notes.append("no_limit_board_row(kline_infer_only)")
    return BoardContext(
        symbol=symbol,
        trade_date=date,
        bar=bar,
        board_row=board_row,
        extras_row=extras_row,
        up_limit=up_limit,
        down_limit=down_limit,
        limit_pct=pct,
        notes=notes,
    )


# ──────────────────────────── 成交概率模型 ────────────────────────────


def queue_fill_probability(
    ctx: BoardContext, cfg: QueueFillConfig = QueueFillConfig()
) -> float | None:
    """T 日涨停价排队成交概率（0~1）。

    返回 ``None`` 表示不可评估（缺封单/成交额数据 → 不造数）。
    * 炸板且收盘未封：排队单在开板期间成交 → 1.0（确定性，双模式均成交）。
    * 一字板：封单比越高、首封越早 → 概率越低，上限 ``yizhi_prob_cap``。
    * 盘中开板后回封：中等概率。
    """
    if ctx.suspended:
        return 0.0
    if not ctx.board_row:
        # 没有涨停表记录 → 无法评估封单质量；只按 K 线推断炸板/一字。
        if ctx.zhaban:
            return 1.0
        if ctx.sealed_at_close:
            return None  # 缺数据，不造数
        return None
    if ctx.zhaban and cfg.zhaban_fill:
        return 1.0
    if not ctx.sealed_at_close:
        return None
    seal = ctx.seal_ratio
    first_time = str((ctx.board_row or {}).get("first_time") or "")
    late_bonus = 0.0
    if first_time >= "14:00":
        late_bonus = 0.10
    elif first_time >= "11:00":
        late_bonus = 0.03
    if seal is None:
        return None
    soft = max(0.0, min(1.0, 1.0 - seal / max(cfg.seal_high_ratio, 1e-9)))
    if ctx.yizhi:
        return max(0.02, min(cfg.yizhi_prob_cap, 0.03 + 0.12 * soft + late_bonus))
    return max(0.20, min(0.80, cfg.reopen_prob_base + 0.30 * soft + late_bonus))


def _rng_for(seed: int, *parts: str) -> random.Random:
    digest = hashlib.sha256(f"{seed}|{'|'.join(parts)}".encode("utf-8")).hexdigest()
    return random.Random(int(digest[:16], 16))


# ──────────────────────────── 单笔交易模拟（不含资金曲线） ────────────────────────────

ENTRY_SCENES = ("queue_t", "next_auction", "next_open")
EXIT_SCENES = ("next_auction", "next_open", "next_close", "hold_days")


@dataclass(eq=False)
class TradePlan:
    """成交可行性模拟结果：只描述"能不能成交、什么价"，不含仓位与费用。

    ``eq=False``：保持身份哈希，供组合层用 plan 对象作持仓字典的键。
    """

    symbol: str
    name: str
    signal_date: str
    entry_scene: str
    mode: str
    filled: bool
    fail_reason: str | None = None
    entry_date: str | None = None
    entry_price: float | None = None
    entry_price_source: str | None = (
        None  # "limit_queue" | "open_proxy" | "auction" ...
    )
    exit_date: str | None = None
    exit_price: float | None = None
    exit_price_source: str | None = None
    exit_reason: str | None = None
    defer_days: int = 0
    defer_reasons: list[str] = field(default_factory=list)
    hold_days: int | None = None
    board_snapshot: dict[str, Any] = field(default_factory=dict)
    assumptions: list[str] = field(default_factory=list)
    queue_fill_prob: float | None = None
    queue_fill_draw: float | None = None
    raw_return_pct: float | None = None  # 未含费用（除权修正后）的价格收益 %
    layers: dict[str, Any] = field(default_factory=dict)


def _simulate_entry(
    view: MarketDataView,
    symbol: str,
    name: str,
    signal_date: str,
    entry_scene: str,
    mode: str,
    seed: int,
    queue_cfg: QueueFillConfig | None = None,
) -> tuple[
    bool,
    str | None,
    str | None,
    float | None,
    str | None,
    list[str],
    float | None,
    float | None,
]:
    """返回 (filled, fail_reason, entry_date, entry_price, price_source, assumptions, prob, draw)。"""
    queue_cfg = queue_cfg or QueueFillConfig()
    assumptions: list[str] = []
    if entry_scene == "queue_t":
        ctx = build_board_context(view, symbol, signal_date, name)
        prob = queue_fill_probability(ctx, queue_cfg)
        if ctx.suspended:
            return False, "t_day_suspended", None, None, None, assumptions, prob, None
        if ctx.up_limit is None:
            return (
                False,
                "missing_price_limits",
                None,
                None,
                None,
                assumptions,
                prob,
                None,
            )
        if signal_date not in view.calendar:
            return (
                False,
                "signal_date_not_in_calendar",
                None,
                None,
                None,
                assumptions,
                prob,
                None,
            )
        if not (ctx.sealed_at_close or ctx.zhaban):
            return (
                False,
                "t_day_not_limit_up",
                None,
                None,
                None,
                assumptions,
                prob,
                None,
            )
        draw = _rng_for(seed, symbol, signal_date, "queue").random()
        if prob is None:
            # 缺封单质量数据 → strict/loose 都按未确认处理（不造数）
            return (
                False,
                "queue_fill_unassessable(no_seal_data)",
                None,
                None,
                None,
                assumptions,
                prob,
                None,
            )
        if ctx.zhaban:
            assumptions.append("炸板未封：涨停价排队在开板期间成交（确定性假设）")
            return (
                True,
                None,
                signal_date,
                ctx.up_limit,
                "limit_queue",
                assumptions,
                prob,
                draw,
            )
        if mode == "strict":
            return (
                False,
                "sealed_board_strict",
                None,
                None,
                None,
                assumptions,
                prob,
                draw,
            )
        if draw <= prob:
            assumptions.append(
                f"一字/回封板 loose 模式按模型概率成交 (p={prob:.3f}, draw={draw:.3f})"
            )
            return (
                True,
                None,
                signal_date,
                ctx.up_limit,
                "limit_queue",
                assumptions,
                prob,
                draw,
            )
        return (
            False,
            "queue_not_filled(prob)",
            None,
            None,
            None,
            assumptions,
            prob,
            draw,
        )

    # T+1 竞价 / 开盘买入
    t_ctx = build_board_context(view, symbol, signal_date, name)
    entry_date = view.next_trade_day(signal_date)
    if entry_date is None:
        return (
            False,
            "no_next_trade_day(data_end)",
            None,
            None,
            None,
            assumptions,
            None,
            None,
        )
    ctx = build_board_context(view, symbol, entry_date, name)
    if ctx.suspended or ctx.bar is None:
        return False, "t1_suspended", None, None, None, assumptions, None, None
    if t_ctx.up_limit is None or ctx.up_limit is None:
        return False, "missing_price_limits", None, None, None, assumptions, None, None
    open_price = float(ctx.bar["open"])
    at_limit_open = abs(open_price - ctx.up_limit) < 1e-6
    if ctx.yizhi:
        # 次日一字涨停：全天封死，买不到（strict/loose 一致，禁止虚假成交）
        return False, "t1_yizhi_unbuyable", None, None, None, assumptions, None, None
    if at_limit_open:
        if mode == "strict":
            return (
                False,
                "t1_open_at_limit_strict",
                None,
                None,
                None,
                assumptions,
                None,
                None,
            )
        # loose：竞价挂涨停价，只有开盘后炸板才可能成交——但"是否炸板"是盘后才知
        # 的信息（未来函数），因此用当日竞价量占比估计，缺数据则不成交。
        prob = 0.30  # 模型假设：竞价买在涨停价的平均成交率
        draw = _rng_for(seed, symbol, entry_date, "auction").random()
        if draw <= prob:
            assumptions.append(f"竞价买在涨停价 loose 成交 (p={prob}, draw={draw:.3f})")
            return (
                True,
                None,
                entry_date,
                ctx.up_limit,
                "auction_limit",
                assumptions,
                prob,
                draw,
            )
        return (
            False,
            "t1_auction_at_limit_not_filled",
            None,
            None,
            None,
            assumptions,
            prob,
            draw,
        )
    source = "open_proxy(auction_missing)"
    assumptions.append("无 stk_auction 数据，竞价成交价用开盘价代理")
    return True, None, entry_date, open_price, source, assumptions, None, None


def _simulate_exit(
    view: MarketDataView,
    symbol: str,
    name: str,
    entry_date: str,
    entry_price: float,
    exit_scene: str,
    hold_days: int,
    tp_pct: float | None,
    sl_pct: float | None,
    mode: str,
    max_exit_date: str | None,
) -> tuple[str | None, float | None, str | None, str | None, int, list[str], list[str]]:
    """从最早可卖日（T+1 制度：entry 次一交易日）开始扫描卖出。

    口径：
      * ``next_auction`` / ``next_open``：在**首个可确认卖出交易日**的开盘价离场
        （竞价无数据时以开盘价代理）。当日不再检查日内 tp/sl（开盘离场先于盘中，
        检查即未来函数）。
      * ``next_close`` / ``hold_days``：计划离场日 = entry 后第 ``target_idx`` 个
        **可确认卖出日**（停牌/跌停顺延不消耗计划日）；此前各日先检查日内 tp/sl。
      * 跌停封死（收盘封跌停 / 一字跌停 / 开盘即跌停价 strict）→ 卖出失败顺延。

    返回 (exit_date, exit_price, exit_reason, price_source, defer_days, defer_reasons, assumptions)。
    """
    assumptions: list[str] = []
    defers: list[str] = []
    kline = view.kline(symbol)
    cal = view.calendar
    start_idx = bisect.bisect_right(cal, entry_date)
    sellable_dates = cal[start_idx:]  # T+1 制度：买入当日不可卖

    if exit_scene in ("next_auction", "next_open"):
        target_idx = 0
        tp_price = sl_price = None
    else:
        target_idx = max(0, int(hold_days) - 1)
        if exit_scene == "next_close":
            target_idx = 0
        tp_price = entry_price * (1 + tp_pct / 100.0) if tp_pct is not None else None
        sl_price = entry_price * (1 + sl_pct / 100.0) if sl_pct is not None else None

    attempts = 0  # 已"消耗"的可确认卖出日数（停牌/封死顺延日不消耗）
    for day in sellable_dates:
        if max_exit_date and day > max_exit_date:
            return None, None, "open_at_end", None, len(defers), defers, assumptions
        bar = kline.get(day)
        if bar is None:
            defers.append(f"{day}:suspended")
            continue
        ctx = build_board_context(view, symbol, day, name)
        open_price = float(bar["open"])
        if exit_scene in ("next_auction", "next_open"):
            # 首个可卖日开盘离场：一字跌停 / 开盘即跌停价（strict）→ 顺延
            if ctx.yizhi_down:
                defers.append(f"{day}:yizhi_down_unsellable")
                continue
            if ctx.down_limit is not None and abs(open_price - ctx.down_limit) < 1e-6:
                if mode == "strict":
                    defers.append(f"{day}:open_at_down_limit_strict")
                    continue
                assumptions.append(
                    "开盘价即跌停价，loose 按开盘价成交（模型假设，成交确认存疑）"
                )
                return (
                    day,
                    open_price,
                    "planned_open_loose",
                    "open_proxy",
                    len(defers),
                    defers,
                    assumptions,
                )
            assumptions.append("无 stk_auction 数据，竞价卖出成交价用开盘价代理")
            return (
                day,
                open_price,
                "planned_open",
                "open_proxy",
                len(defers),
                defers,
                assumptions,
            )
        # 收盘价离场场景（next_close / hold_days）
        # ── 日内止盈优先（严格在计划离场日之前，或计划日盘中先于收盘触发）──
        if tp_price is not None:
            trig = tp_price
            if ctx.up_limit is not None and trig > ctx.up_limit:
                trig = ctx.up_limit  # 止盈价被涨停价封顶，触发条件同步降为涨停价
            if float(bar["high"]) >= trig:
                if trig < tp_price - 1e-9:
                    assumptions.append(
                        "止盈价高于涨停价，按涨停价卖出（涨停有买方排队，卖出可成交）"
                    )
                return day, trig, "tp", "tp_trigger", len(defers), defers, assumptions
        if sl_price is not None and float(bar["low"]) <= sl_price:
            if open_price <= sl_price + 1e-9:
                # 开盘跳空低于止损价：成交价只能是开盘价（更差），不能按止损价乐观成交
                if ctx.yizhi_down or (
                    ctx.down_limit is not None
                    and abs(open_price - ctx.down_limit) < 1e-6
                ):
                    # 开盘即封跌停：卖不出
                    if mode == "strict":
                        defers.append(f"{day}:gap_down_open_down_limit_strict")
                        continue
                    assumptions.append(
                        "开盘封跌停跳空穿止损：loose 按跌停价成交（模型假设，实际确认存疑）"
                    )
                    return (
                        day,
                        ctx.down_limit,
                        "sl_gap_down_limit_loose",
                        "down_limit",
                        len(defers),
                        defers,
                        assumptions,
                    )
                assumptions.append("跳空低开穿越止损价，按开盘价成交（非止损价）")
                return (
                    day,
                    open_price,
                    "sl_gap_open",
                    "open",
                    len(defers),
                    defers,
                    assumptions,
                )
            price = max(sl_price, ctx.down_limit) if ctx.down_limit else sl_price
            return day, price, "sl", "sl_trigger", len(defers), defers, assumptions
        # ── 计划离场尝试 ──
        if attempts >= target_idx:
            if ctx.down_sealed_at_close:
                defers.append(f"{day}:down_limit_close_defer")
                continue
            reason = "planned_close" if exit_scene == "next_close" else "hold_end"
            return (
                day,
                float(bar["close"]),
                reason,
                "close",
                len(defers),
                defers,
                assumptions,
            )
        attempts += 1
    return None, None, "open_at_end", None, len(defers), defers, assumptions


# ──────────────────────────── 信号规格化 ────────────────────────────


def _normalize_signals(signals: Any) -> list[dict[str, Any]]:
    """接受 pandas DataFrame / list[dict]，输出 [{symbol, signal_date, name}]。"""
    rows: list[dict[str, Any]] = []
    if hasattr(signals, "to_dict"):  # DataFrame / 类 DataFrame
        records = signals.to_dict(orient="records")
    elif isinstance(signals, Iterable):
        records = list(signals)
    else:
        raise TypeError("signals 必须是 pandas DataFrame 或 list[dict]")
    for rec in records:
        rec = dict(rec)
        symbol = str(rec.get("symbol") or rec.get("code") or "").strip()
        date = str(
            rec.get("signal_date") or rec.get("trade_date") or rec.get("date") or ""
        ).strip()
        if not symbol or not date:
            raise ValueError(f"信号缺少 symbol/signal_date: {rec!r}")
        rows.append(
            {
                "symbol": symbol.zfill(6),
                "signal_date": date,
                "name": str(rec.get("name") or ""),
            }
        )
    return rows


# ──────────────────────────── 主回测入口 ────────────────────────────


def run_limit_up_backtest(
    signals: Any,
    *,
    db_path: str | Path,
    as_of: str | None = None,
    mode: str = "strict",
    entry_scene: str = "next_open",
    exit_scene: str = "hold_days",
    hold_days: int = 1,
    tp_pct: float | None = None,
    sl_pct: float | None = None,
    initial_capital: float = 1_000_000.0,
    position_size_pct: float = 0.2,
    fees: FeeConfig | None = None,
    slippage: SlippageConfig | None = None,
    capacity: CapacityConfig | None = None,
    queue_fill: QueueFillConfig | None = None,
    regime_by_date: dict[str, str] | None = None,
    benchmark_ts_codes: Iterable[str] = (),
    include_equal_weight_benchmark: bool = True,
    use_adj_factor: bool = True,
    mcap_buckets: tuple[float, ...] = DEFAULT_MCAP_BUCKETS,
    seed: int = 20261022,
    persist_path: str | Path | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """打板可成交性回测（纯函数式；返回 JSON 可序列化报告 dict）。

    参数口径：
      * ``mode``：``"strict"``（默认，未确认成交一律不计收益）/ ``"loose"``（概率模型估计成交）。
      * ``entry_scene``：``"queue_t"``（T 日涨停价排队）/ ``"next_auction"``、``"next_open"``（T+1 竞价/开盘，
        无竞价数据时竞价用开盘价代理并标注）。
      * ``exit_scene``：``"next_auction"``/``"next_open"``/``"next_close"``/``"hold_days"``（配合 tp/sl 优先触发）。
      * ``hold_days``：``exit_scene="hold_days"`` 时持有 N 个交易日（≥1，T+1 制度自然满足）。
    """
    if mode not in ("strict", "loose"):
        raise ValueError(f"mode 必须是 strict/loose，收到 {mode!r}")
    if entry_scene not in ENTRY_SCENES:
        raise ValueError(f"entry_scene 必须是 {ENTRY_SCENES}")
    if exit_scene not in EXIT_SCENES:
        raise ValueError(f"exit_scene 必须是 {EXIT_SCENES}")
    if exit_scene == "hold_days" and hold_days < 1:
        raise ValueError("hold_days 必须 >= 1（T+1 制度下买入日不可卖）")

    fees = fees or FeeConfig()
    slippage = slippage or SlippageConfig()
    capacity = capacity or CapacityConfig()
    queue_fill = queue_fill or QueueFillConfig()
    signal_rows = _normalize_signals(signals)
    view = MarketDataView(db_path, as_of=as_of)

    data_notes: list[str] = []
    if not view.calendar:
        data_notes.append("empty_calendar: kline_daily 无任何交易日数据")

    # ── 1. 成交可行性模拟（每信号） ──
    plans: list[TradePlan] = []
    for sig in signal_rows:
        plan = TradePlan(
            symbol=sig["symbol"],
            name=sig["name"],
            signal_date=sig["signal_date"],
            entry_scene=entry_scene,
            mode=mode,
            filled=False,
        )
        filled, fail, e_date, e_price, e_src, e_assump, prob, draw = _simulate_entry(
            view,
            sig["symbol"],
            sig["name"],
            sig["signal_date"],
            entry_scene,
            mode,
            seed,
            queue_fill,
        )
        plan.queue_fill_prob = prob
        plan.queue_fill_draw = draw
        plan.assumptions.extend(e_assump)
        ctx_t = build_board_context(
            view, sig["symbol"], sig["signal_date"], sig["name"]
        )
        plan.board_snapshot = {
            "limit_flag": (ctx_t.board_row or {}).get("limit_flag"),
            "first_time": (ctx_t.board_row or {}).get("first_time"),
            "open_times": ctx_t.open_times,
            "limit_times": ctx_t.limit_times,
            "seal_ratio": ctx_t.seal_ratio,
            "fd_amount": (ctx_t.board_row or {}).get("fd_amount"),
            "amount": (ctx_t.board_row or {}).get("amount"),
            "yizhi": ctx_t.yizhi,
            "zhaban": ctx_t.zhaban,
            "up_limit": ctx_t.up_limit,
        }
        plan.layers = _layer_keys(sig, ctx_t, view, regime_by_date, mcap_buckets)
        if not filled or e_date is None or e_price is None:
            plan.fail_reason = fail
            plans.append(plan)
            continue
        plan.filled = True
        plan.entry_date = e_date
        plan.entry_price = e_price
        plan.entry_price_source = e_src
        x_date, x_price, x_reason, x_src, defers, defer_list, x_assump = _simulate_exit(
            view,
            sig["symbol"],
            sig["name"],
            e_date,
            e_price,
            exit_scene,
            hold_days,
            tp_pct,
            sl_pct,
            mode,
            max_exit_date=as_of,
        )
        plan.assumptions.extend(x_assump)
        plan.defer_days = defers
        plan.defer_reasons = defer_list
        plan.exit_date = x_date
        plan.exit_price = x_price
        plan.exit_reason = x_reason
        plan.exit_price_source = x_src
        if x_date is not None:
            plan.hold_days = _trading_days_between(view, e_date, x_date)
            plan.raw_return_pct = _adjusted_return_pct(
                view, sig["symbol"], e_date, e_price, x_date, x_price, use_adj_factor
            )
        plans.append(plan)

    # ── 2. 组合资金曲线（等权仓位 + 容量 + 费用 + 滑点 + 除权股数修正） ──
    portfolio = _run_portfolio(
        view,
        plans,
        initial_capital,
        position_size_pct,
        fees,
        slippage,
        capacity,
        data_notes,
        use_adj_factor,
    )

    # ── 3. 绩效指标 ──
    metrics = _performance_metrics(portfolio["nav_series"])
    trade_stats = _trade_stats(portfolio["executed"])
    premium = _premium_metrics(view, signal_rows, data_notes)
    fill_stats = _compute_fill_stats(plans, portfolio["executed"])
    layered = _layered_metrics(portfolio["executed"], regime_by_date)
    benchmarks, bench_skips = _benchmark_metrics(
        view,
        portfolio["nav_series"],
        benchmark_ts_codes,
        include_equal_weight_benchmark,
    )

    report: dict[str, Any] = {
        "engine": "limit_up_backtest/v1",
        "mode": mode,
        "generated_at": generated_at
        or datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "params": {
            "as_of": as_of,
            "entry_scene": entry_scene,
            "exit_scene": exit_scene,
            "hold_days": hold_days,
            "tp_pct": tp_pct,
            "sl_pct": sl_pct,
            "initial_capital": initial_capital,
            "position_size_pct": position_size_pct,
            "use_adj_factor": use_adj_factor,
            "seed": seed,
            "fees": asdict(fees),
            "slippage": asdict(slippage),
            "capacity": asdict(capacity),
            "queue_fill_model": asdict(queue_fill),
            "mcap_buckets": list(mcap_buckets),
            "benchmark_ts_codes": list(benchmark_ts_codes),
            "include_equal_weight_benchmark": include_equal_weight_benchmark,
        },
        "assumptions": _assumption_block(
            fees, slippage, capacity, queue_fill, entry_scene, mode
        ),
        "fill_stats": fill_stats,
        "capacity": portfolio["capacity"],
        "metrics": metrics,
        "trade_stats": trade_stats,
        "premium_metrics": premium,
        "layered_metrics": layered,
        "benchmarks": benchmarks,
        "benchmark_skipped": bench_skips,
        "nav_series": portfolio["nav_series"],
        "trades": portfolio["executed"],
        "signal_details": [asdict(p) for p in plans],
        "data_quality_notes": sorted(set(data_notes)),
        "data_watermark": view.data_watermark(),
        "open_positions": portfolio["open_positions"],
        "repro": {
            "seed": seed,
            "signal_count": len(signal_rows),
            "signals_hash": _signals_hash(signal_rows),
        },
    }
    if persist_path:
        Path(persist_path).parent.mkdir(parents=True, exist_ok=True)
        Path(persist_path).write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    return report


def _signals_hash(rows: list[dict[str, Any]]) -> str:
    payload = "|".join(
        f"{r['symbol']}:{r['signal_date']}"
        for r in sorted(rows, key=lambda x: (x["signal_date"], x["symbol"]))
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _trading_days_between(view: MarketDataView, d1: str, d2: str) -> int:
    cal = view.calendar
    i1 = bisect_left_str(cal, d1)
    i2 = bisect_left_str(cal, d2)
    return max(0, i2 - i1)


def _adjusted_return_pct(
    view: MarketDataView,
    symbol: str,
    e_date: str,
    e_price: float,
    x_date: str,
    x_price: float | None,
    use_adj_factor: bool,
) -> float | None:
    """价格收益（%），若可用则用 adj_factor 做除权修正（复权口径）。"""
    if x_price is None or e_price <= 0:
        return None
    ratio_e, ratio_x = 1.0, 1.0
    if use_adj_factor:
        extras = view.extras(symbol)
        a_e = (extras.get(e_date) or {}).get("adj_factor")
        a_x = (extras.get(x_date) or {}).get("adj_factor")
        if a_e and a_x:
            ratio_e, ratio_x = float(a_e), float(a_x)
    adj_entry = e_price * ratio_e
    adj_exit = x_price * ratio_x
    return (adj_exit / adj_entry - 1.0) * 100.0


def _layer_keys(
    sig: dict[str, Any],
    ctx_t: BoardContext,
    view: MarketDataView,
    regime_by_date: dict[str, str] | None,
    mcap_buckets: tuple[float, ...],
) -> dict[str, Any]:
    layers: dict[str, Any] = {}
    layers["regime"] = (regime_by_date or {}).get(
        sig["signal_date"], "unknown" if regime_by_date is None else "missing"
    )
    height = ctx_t.limit_times
    if height is None and ctx_t.board_row is None:
        layers["height"] = "unknown"
    else:
        layers["height"] = int(height or 1)
    if ctx_t.zhaban:
        layers["board_type"] = "炸板"
    elif ctx_t.yizhi:
        layers["board_type"] = "一字"
    elif ctx_t.sealed_at_close:
        layers["board_type"] = "换手板"
    else:
        layers["board_type"] = "非涨停/未知"
    bt = (ctx_t.board_row or {}).get("board_type")
    if bt:
        layers["board_type"] = str(bt)
    mv = ctx_t.float_mv
    if mv is None:
        layers["mcap"] = "unknown"
    else:
        bucket = 0
        for i, th in enumerate(mcap_buckets):
            if mv < th:
                bucket = i
                break
        else:
            bucket = len(mcap_buckets)
        labels = (
            [f"<{mcap_buckets[0]:.0e}"]
            + [
                f"{mcap_buckets[i]:.0e}-{mcap_buckets[i + 1]:.0e}"
                for i in range(len(mcap_buckets) - 1)
            ]
            + [f">={mcap_buckets[-1]:.0e}"]
        )
        layers["mcap"] = labels[bucket]
    layers["theme"] = (ctx_t.board_row or {}).get("theme") or "unknown"
    return layers


# ──────────────────────────── 组合资金曲线 ────────────────────────────


def _run_portfolio(
    view: MarketDataView,
    plans: list[TradePlan],
    initial_capital: float,
    position_size_pct: float,
    fees: FeeConfig,
    slippage: SlippageConfig,
    capacity: CapacityConfig,
    data_notes: list[str],
    use_adj_factor: bool = True,
) -> dict[str, Any]:
    """按交易日历逐日结算：出场回款 → 除权股数修正 → 入场 sizing/容量/费用/滑点，输出真实净值曲线。"""

    filled_plans = [p for p in plans if p.filled and p.entry_date and p.entry_price]
    entries_by_day: dict[str, list[TradePlan]] = {}
    for p in filled_plans:
        entries_by_day.setdefault(p.entry_date, []).append(p)
    exits_by_day: dict[str, list[TradePlan]] = {}
    for p in filled_plans:
        if p.exit_date and p.exit_price:
            exits_by_day.setdefault(p.exit_date, []).append(p)

    cal = view.calendar
    if filled_plans:
        lo = min(str(p.entry_date) for p in filled_plans)
        hi = max(str(p.exit_date or p.entry_date) for p in filled_plans)
        start_idx = max(0, bisect.bisect_left(cal, lo) - 1)
        end_idx = bisect.bisect_right(cal, hi)
        nav_days = cal[start_idx:end_idx]
    else:
        nav_days = []

    cash = initial_capital
    open_pos: dict[TradePlan, dict[str, Any]] = {}
    nav_series: list[dict[str, float]] = []
    executed: list[dict[str, Any]] = []
    capacity_hits = 0
    capacity_skips = 0
    capacity_total_gross = 0.0
    capacity_capped_gross = 0.0

    for day in nav_days:
        # 1) 除权/送转股数修正（先于出场，保证除权日以新股本按当日价格卖出）
        if use_adj_factor:
            for pos in open_pos.values():
                _apply_ex_rights(pos, day, view, data_notes)
        # 2) 出场（先卖后买，回笼资金）
        for plan in exits_by_day.get(day, []):
            pos = open_pos.pop(plan, None)
            if pos is None:
                continue
            net_proceeds = _sell(
                plan, pos, day, view, fees, slippage, executed, capacity, data_notes
            )
            cash += net_proceeds
        # 3) 入场
        day_nav_prev = cash + sum(
            p["shares"] * p["last_close"] for p in open_pos.values()
        )
        for plan in sorted(entries_by_day.get(day, []), key=lambda p: str(p.symbol)):
            bar = view.kline(plan.symbol).get(day)
            if bar is None:
                data_notes.append(f"{plan.symbol}@{day}:entry_bar_missing")
                plan.filled = False
                plan.fail_reason = "entry_bar_missing"
                continue
            price = float(plan.entry_price or 0.0)
            target_value = day_nav_prev * position_size_pct
            cap_notional = None
            day_amount = float(bar.get("amount") or 0.0)
            if day_amount > 0:
                cap_notional = day_amount * capacity.max_participation
                if target_value > cap_notional:
                    capacity_hits += 1
                    if capacity.action == "skip":
                        capacity_skips += 1
                        executed.append(
                            _failed_exec_record(
                                plan, "capacity_skip", cap_notional, target_value
                            )
                        )
                        continue
                    target_value = cap_notional
            if price <= 0:
                continue
            approx_shares = math.floor(target_value / price / 100) * 100
            if approx_shares <= 0:
                executed.append(
                    _failed_exec_record(
                        plan,
                        "insufficient_capacity_or_cash",
                        cap_notional,
                        target_value,
                    )
                )
                plan.filled = False
                plan.fail_reason = "insufficient_capacity_or_cash"
                continue
            participation = (
                min(approx_shares * price / day_amount, 1.0) if day_amount > 0 else 1.0
            )
            slip = _slippage_pct(slippage, participation)
            buy_eff_price = price * (1.0 + slip)
            shares = (
                math.floor(min(target_value, max(cash, 0.0)) / buy_eff_price / 100)
                * 100
            )
            if shares <= 0:
                executed.append(
                    _failed_exec_record(
                        plan, "insufficient_cash", cap_notional, target_value
                    )
                )
                plan.filled = False
                plan.fail_reason = "insufficient_cash"
                continue
            gross = shares * buy_eff_price
            cost_items = fees.buy_costs(gross)
            total_cost = gross + sum(cost_items.values())
            if total_cost > cash:
                # 费用预留后重新缩量（保守：不为凑整手透支现金）
                eff = buy_eff_price * (
                    1
                    + fees.commission_rate
                    + (fees.transfer_fee_rate if fees.transfer_fee_enabled else 0.0)
                )
                shares = math.floor(cash / eff / 100) * 100
                if shares <= 0:
                    executed.append(
                        _failed_exec_record(
                            plan, "insufficient_cash", cap_notional, target_value
                        )
                    )
                    plan.filled = False
                    plan.fail_reason = "insufficient_cash"
                    continue
                gross = shares * buy_eff_price
                cost_items = fees.buy_costs(gross)
                total_cost = gross + sum(cost_items.values())
                if total_cost > cash:
                    executed.append(
                        _failed_exec_record(
                            plan, "insufficient_cash", cap_notional, target_value
                        )
                    )
                    plan.filled = False
                    plan.fail_reason = "insufficient_cash"
                    continue
            cash -= total_cost
            capacity_total_gross += target_value
            capacity_capped_gross += gross
            adj0 = _adj_factor(view, plan.symbol, day)
            open_pos[plan] = {
                "symbol": plan.symbol,
                "shares": shares,
                "last_close": float(bar["close"]),
                "buy_gross": gross,
                "buy_costs": cost_items,
                "buy_slip_pct": slip,
                "prev_adj": adj0,
                "cap_notional": cap_notional,
                "target_value": position_size_pct * day_nav_prev,
            }
        # 4) 逐日 mark-to-market（停牌无 bar 时用最近收盘价）
        for pos in open_pos.values():
            bar = view.kline(str(pos["symbol"])).get(day)
            if bar is not None:
                pos["last_close"] = float(bar["close"])
        nav = cash + sum(p["shares"] * p["last_close"] for p in open_pos.values())
        nav_series.append({"date": day, "nav": round(nav, 4), "cash": round(cash, 4)})

    open_positions = [
        {
            "symbol": p.symbol,
            "signal_date": p.signal_date,
            "entry_date": p.entry_date,
            "shares": pos["shares"],
            "mark": pos["last_close"],
            "mark_value": round(pos["shares"] * pos["last_close"], 2),
        }
        for p, pos in open_pos.items()
    ]
    capacity_summary = {
        "max_participation": capacity.max_participation,
        "action": capacity.action,
        "capacity_hit_count": capacity_hits,
        "capacity_skip_count": capacity_skips,
        "capped_gross_vs_target": round(capacity_capped_gross / capacity_total_gross, 4)
        if capacity_total_gross
        else None,
    }
    return {
        "nav_series": nav_series,
        "executed": executed,
        "open_positions": open_positions,
        "capacity": capacity_summary,
    }


def _apply_ex_rights(
    pos: dict[str, Any], day: str, view: MarketDataView, data_notes: list[str]
) -> None:
    """除权除息近似：adj_factor 变化 → 按因子比值等效调整持仓股数。

    无逐笔分红/送股事件表，用复权因子比值近似股数调整（现金分红并入该近似）；
    这是模型假设，写入 assumptions / data_quality_notes。
    """
    symbol = str(pos.get("symbol") or "")
    factor = _adj_factor(view, symbol, day)
    prev = pos.get("prev_adj")
    if factor is None or prev is None or prev <= 0 or abs(factor - prev) < 1e-12:
        if factor is not None:
            pos["prev_adj"] = factor
        return
    ratio = factor / prev
    new_shares = pos["shares"] * ratio
    data_notes.append(
        f"{symbol}@{day}:ex_rights_adj_ratio={ratio:.6f}(shares adjusted)"
    )
    pos["shares"] = new_shares
    pos["ex_rights_adjusted"] = True
    pos["prev_adj"] = factor


def _adj_factor(view: MarketDataView, symbol: str, day: str) -> float | None:
    row = view.extras(symbol).get(day)
    if row and row.get("adj_factor") is not None:
        return float(row["adj_factor"])
    return None


def _sell(
    plan: TradePlan,
    pos: dict[str, Any],
    day: str,
    view: MarketDataView,
    fees: FeeConfig,
    slippage: SlippageConfig,
    executed: list[dict[str, Any]],
    capacity: CapacityConfig,
    data_notes: list[str],
) -> float:
    bar = view.kline(plan.symbol).get(day)
    price = plan.exit_price
    if price is None:
        price = pos["last_close"]
    day_amount = float(bar.get("amount") or 0.0) if bar else 0.0
    sell_gross_pre = pos["shares"] * price
    participation = min(sell_gross_pre / day_amount, 1.0) if day_amount > 0 else 1.0
    slip = _slippage_pct(slippage, participation)
    sell_eff = price * (1.0 - slip)
    gross = pos["shares"] * sell_eff
    cost_items = fees.sell_costs(gross)
    net_proceeds = gross - sum(cost_items.values())
    buy_total = pos["buy_gross"] + sum(pos["buy_costs"].values())
    net_pnl = net_proceeds - buy_total
    ret_pct = net_pnl / buy_total * 100.0 if buy_total > 0 else 0.0
    executed.append(
        {
            "symbol": plan.symbol,
            "name": plan.name,
            "signal_date": plan.signal_date,
            "status": "filled",
            "entry_scene": plan.entry_scene,
            "entry_date": plan.entry_date,
            "entry_price_gross": round(plan.entry_price or 0.0, 4),
            "entry_slip_pct": round(pos["buy_slip_pct"], 6),
            "shares": pos["shares"],
            "buy_gross": round(pos["buy_gross"], 2),
            "buy_costs": {k: round(v, 2) for k, v in pos["buy_costs"].items()},
            "exit_date": day,
            "exit_price_gross": round(price, 4),
            "exit_reason": plan.exit_reason,
            "exit_price_source": plan.exit_price_source,
            "exit_slip_pct": round(slip, 6),
            "sell_gross": round(gross, 2),
            "sell_costs": {k: round(v, 2) for k, v in cost_items.items()},
            "net_pnl": round(net_pnl, 2),
            "net_pnl_pct": round(ret_pct, 4),
            "gross_return_pct": (
                round(plan.raw_return_pct, 4)
                if plan.raw_return_pct is not None
                else None
            ),
            "hold_days": plan.hold_days,
            "defer_days": plan.defer_days,
            "defer_reasons": plan.defer_reasons,
            "layers": plan.layers,
            "capacity_capped": bool(pos["cap_notional"])
            and pos["buy_gross"] < 0.999 * pos["target_value"],
            "board_snapshot": plan.board_snapshot,
            "assumptions": plan.assumptions,
        }
    )
    return net_proceeds


def _failed_exec_record(
    plan: TradePlan, reason: str, cap: float | None, target: float
) -> dict[str, Any]:
    return {
        "symbol": plan.symbol,
        "name": plan.name,
        "signal_date": plan.signal_date,
        "status": "skipped",
        "fail_reason": reason,
        "entry_scene": plan.entry_scene,
        "entry_date": plan.entry_date,
        "capacity_cap": round(cap, 2) if cap else None,
        "target_value": round(target, 2),
        "layers": plan.layers,
    }


def _slippage_pct(cfg: SlippageConfig, participation: float) -> float:
    if participation <= 0:
        return cfg.base_bps / 1e4
    mult = min(participation / max(cfg.ref_participation, 1e-9), cfg.max_multiplier)
    return (cfg.base_bps + cfg.impact_bps_at_ref * mult) / 1e4


# ──────────────────────────── 绩效指标 ────────────────────────────


def _performance_metrics(nav_series: list[dict[str, float]]) -> dict[str, Any]:
    if len(nav_series) < 2:
        return {
            "nav_days": len(nav_series),
            "total_return_pct": 0.0 if nav_series else None,
            "annualized_return_pct": None,
            "max_drawdown_pct": 0.0 if nav_series else None,
            "sharpe": None,
            "sortino": None,
            "calmar": None,
            "turnover_note": "无可成交仓位，净值曲线退化为常数",
        }
    navs = [x["nav"] for x in nav_series]
    start, end = navs[0], navs[-1]
    rets = [navs[i] / navs[i - 1] - 1.0 for i in range(1, len(navs)) if navs[i - 1] > 0]
    n_years = max(len(rets) / TRADING_DAYS_PER_YEAR, 1e-9)
    total = end / start - 1.0 if start > 0 else 0.0
    ann = (end / start) ** (1.0 / n_years) - 1.0 if start > 0 else 0.0
    peak = navs[0]
    mdd = 0.0
    for v in navs:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, v / peak - 1.0)
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / max(len(rets) - 1, 1)
    std = math.sqrt(var)
    downside = [r for r in rets if r < 0]
    dstd = (
        math.sqrt(sum(r**2 for r in downside) / max(len(rets) - 1, 1))
        if downside
        else 0.0
    )
    sharpe = mean / std * math.sqrt(TRADING_DAYS_PER_YEAR) if std > 0 else None
    sortino = mean / dstd * math.sqrt(TRADING_DAYS_PER_YEAR) if dstd > 0 else None
    calmar = ann / abs(mdd) if mdd < 0 else None
    return {
        "nav_days": len(nav_series),
        "start_nav": round(start, 4),
        "end_nav": round(end, 4),
        "total_return_pct": round(total * 100.0, 4),
        "annualized_return_pct": round(ann * 100.0, 4),
        "max_drawdown_pct": round(mdd * 100.0, 4),
        "sharpe": round(sharpe, 4) if sharpe is not None else None,
        "sortino": round(sortino, 4) if sortino is not None else None,
        "calmar": round(calmar, 4) if calmar is not None else None,
        "daily_vol_pct": round(std * 100.0, 4),
    }


def _trade_stats(executed: list[dict[str, Any]]) -> dict[str, Any]:
    closed = [t for t in executed if t.get("status") == "filled"]
    if not closed:
        return {
            "closed_trades": 0,
            "win_rate_pct": None,
            "payoff_ratio": None,
            "avg_hold_days": None,
            "fee_ratio": None,
            "total_fees": 0.0,
        }
    wins = [t for t in closed if t["net_pnl"] > 0]
    losses = [t for t in closed if t["net_pnl"] <= 0]
    avg_win = sum(t["net_pnl"] for t in wins) / len(wins) if wins else 0.0
    avg_loss = abs(sum(t["net_pnl"] for t in losses) / len(losses)) if losses else 0.0
    total_fees = sum(
        sum(t["buy_costs"].values()) + sum(t["sell_costs"].values()) for t in closed
    )
    total_buy = sum(t["buy_gross"] for t in closed) or 1.0
    total_amount = total_buy + sum(t["sell_gross"] for t in closed)
    return {
        "closed_trades": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(len(wins) / len(closed) * 100.0, 2),
        "payoff_ratio": round(avg_win / avg_loss, 4) if avg_loss > 0 else None,
        "avg_hold_days": round(
            sum(t["hold_days"] or 0 for t in closed) / len(closed), 2
        ),
        "avg_defer_days": round(sum(t["defer_days"] for t in closed) / len(closed), 2),
        "total_fees": round(total_fees, 2),
        "fee_ratio_of_buy_pct": round(total_fees / total_buy * 100.0, 4),
        "fee_ratio_of_gross_turnover_pct": round(total_fees / total_amount * 100.0, 4),
        "sum_single_trade_pnl_pct": round(sum(t["net_pnl_pct"] for t in closed), 4),
        "note": "sum_single_trade_pnl_pct 仅作对照，绩效以 nav 曲线为准（禁止单笔相加口径）",
    }


def _premium_metrics(
    view: MarketDataView, signals: list[dict[str, Any]], data_notes: list[str]
) -> dict[str, Any]:
    """次日竞价(开盘价代理)溢价、开盘溢价、3/5 日收益 —— 对全部信号统计（与成交无关）。"""
    next_open: list[float] = []
    r3: list[float] = []
    r5: list[float] = []
    missing = 0
    for sig in signals:
        kline = view.kline(sig["symbol"])
        t_bar = kline.get(sig["signal_date"])
        if t_bar is None:
            missing += 1
            continue
        t_close = float(t_bar["close"])
        if t_close <= 0:
            missing += 1
            continue
        nxt = view.next_trade_day(sig["signal_date"])
        if nxt and nxt in kline:
            next_open.append((float(kline[nxt]["open"]) / t_close - 1.0) * 100.0)
        for n, acc in ((3, r3), (5, r5)):
            day_n = _nth_trade_day_after(view, sig["signal_date"], n)
            if day_n and day_n in kline:
                acc.append((float(kline[day_n]["close"]) / t_close - 1.0) * 100.0)
    if missing:
        data_notes.append(
            f"premium_metrics: {missing} 个信号缺少 T 日 K 线，未计入溢价统计"
        )

    def _s(vals: list[float]) -> dict[str, Any]:
        if not vals:
            return {
                "count": 0,
                "mean_pct": None,
                "median_pct": None,
                "note": "数据不足，skip（不造数）",
            }
        s = sorted(vals)
        return {
            "count": len(vals),
            "mean_pct": round(sum(vals) / len(vals), 4),
            "median_pct": round(s[len(s) // 2], 4),
        }

    return {
        "next_open_premium": _s(next_open),
        "ret_3d": _s(r3),
        "ret_5d": _s(r5),
        "note": "无 stk_auction 数据，竞价溢价以次日开盘价代理",
    }


def _nth_trade_day_after(view: MarketDataView, date: str, n: int) -> str | None:
    cal = view.calendar
    idx = bisect_left_str(cal, date)
    if idx >= len(cal) or cal[idx] != date:
        return None
    target = idx + n
    return cal[target] if target < len(cal) else None


def _compute_fill_stats(
    plans: list[TradePlan], executed: list[dict[str, Any]]
) -> dict[str, Any]:
    attempted = len(plans)
    filled = [p for p in plans if p.filled]
    failed = [p for p in plans if not p.filled]
    reasons: dict[str, int] = {}
    for p in failed:
        key = p.fail_reason or "unknown"
        reasons[key] = reasons.get(key, 0) + 1
    defers = sum(p.defer_days for p in filled)
    defer_reason_counter: dict[str, int] = {}
    for p in filled:
        for d in p.defer_reasons:
            tag = d.split(":", 1)[1] if ":" in d else d
            defer_reason_counter[tag] = defer_reason_counter.get(tag, 0) + 1
    skipped_by_portfolio = sum(1 for t in executed if t.get("status") == "skipped")
    return {
        "signals_attempted": attempted,
        "entry_filled": len(filled),
        "entry_failed": len(failed),
        "entry_fail_reasons": reasons,
        "sell_defer_days": defers,
        "sell_defer_reasons": defer_reason_counter,
        "portfolio_skips_after_fill": skipped_by_portfolio,
        "fill_rate_pct": round(len(filled) / attempted * 100.0, 2)
        if attempted
        else None,
    }


def _layered_metrics(
    executed: list[dict[str, Any]], regime_by_date: dict[str, str] | None
) -> dict[str, Any]:
    closed = [t for t in executed if t.get("status") == "filled"]
    out: dict[str, Any] = {
        "_note": "分层基于信号日可知的板型/市值/连板高度/regime；缺数记 unknown，不伪造"
    }
    if regime_by_date is None:
        out["_regime_layer"] = (
            "skipped: 未提供 regime_by_date（#25 表未接入），不伪造分层"
        )
    for dim in ("regime", "board_type", "mcap", "height", "theme"):
        buckets: dict[str, dict[str, Any]] = {}
        for t in closed:
            key = str((t.get("layers") or {}).get(dim, "unknown"))
            b = buckets.setdefault(
                key,
                {
                    "trades": 0,
                    "wins": 0,
                    "sum_pnl": 0.0,
                    "sum_pnl_pct": 0.0,
                    "max_dd_proxy_pct": 0.0,
                },
            )
            b["trades"] += 1
            b["wins"] += 1 if t["net_pnl"] > 0 else 0
            b["sum_pnl"] += t["net_pnl"]
            b["sum_pnl_pct"] += t["net_pnl_pct"]
            b["max_dd_proxy_pct"] = min(b["max_dd_proxy_pct"], t["net_pnl_pct"])
        for b in buckets.values():
            b["win_rate_pct"] = round(b["wins"] / b["trades"] * 100.0, 2)
            b["avg_net_pnl_pct"] = round(b["sum_pnl_pct"] / b["trades"], 4)
            b["sum_pnl"] = round(b["sum_pnl"], 2)
            b["worst_trade_pct"] = round(b["max_dd_proxy_pct"], 4)
            del b["sum_pnl_pct"]
        out[dim] = buckets
    return out


def _benchmark_metrics(
    view: MarketDataView,
    nav_series: list[dict[str, float]],
    ts_codes: Iterable[str],
    include_equal_weight: bool,
) -> tuple[list[dict[str, Any]], list[str]]:
    benchmarks: list[dict[str, Any]] = []
    skips: list[str] = []
    if not nav_series:
        return [], ["no_nav_window"]
    start, end = nav_series[0]["date"], nav_series[-1]["date"]
    windows = [x["date"] for x in nav_series]
    for code in ts_codes:
        rows = view.index_daily(code)
        series = [
            rows[d]["close"]
            for d in windows
            if d in rows and rows[d].get("close") is not None
        ]
        if len(series) < max(2, int(len(windows) * 0.8)):
            skips.append(
                f"{code}: index_daily 数据不足（覆盖 {len(series)}/{len(windows)} 天），skip，不造数"
            )
            continue
        bench_total = series[-1] / series[0] - 1.0
        benchmarks.append(
            {
                "name": f"index:{code}",
                "total_return_pct": round(bench_total * 100.0, 4),
                "strategy_excess_pct": None,
            }
        )
    if include_equal_weight:
        eq = view.equal_weight_returns(start, end)
        coverage = len(eq) / max(len(windows), 1)
        if coverage < 0.8:
            skips.append(
                f"equal_weight: 全市场等权日收益覆盖不足（{len(eq)}/{len(windows)}），skip，不造数"
            )
        else:
            navs = [x["nav"] for x in nav_series]
            strat_total = navs[-1] / navs[0] - 1.0 if navs[0] > 0 else 0.0
            eq_total = 1.0
            for d in windows:
                if d in eq:
                    eq_total *= 1.0 + eq[d]
            benchmarks.append(
                {
                    "name": "equal_weight_all_market",
                    "total_return_pct": round((eq_total - 1.0) * 100.0, 4),
                    "strategy_excess_pct": round(
                        (strat_total - (eq_total - 1.0)) * 100.0, 4
                    ),
                    "coverage": round(coverage, 4),
                }
            )
    return benchmarks, skips


def _assumption_block(
    fees: FeeConfig,
    slippage: SlippageConfig,
    capacity: CapacityConfig,
    queue_fill: QueueFillConfig,
    entry_scene: str,
    mode: str,
) -> dict[str, Any]:
    return {
        "commission": f"万 {fees.commission_rate * 1e4:.2f}，最低 {fees.min_commission} 元/笔",
        "stamp_tax": f"卖出万 {fees.stamp_tax_rate * 1e4:.2f}",
        "transfer_fee": (
            f"双边万 {fees.transfer_fee_rate * 1e4:.2f}"
            if fees.transfer_fee_enabled
            else "关闭（可配置）"
        ),
        "slippage": f"基准 {slippage.base_bps}bps + 参与率冲击（{slippage.ref_participation:.0%} 时 +{slippage.impact_bps_at_ref}bps，上限 ×{slippage.max_multiplier}）",
        "capacity": f"单票单日参与率上限 {capacity.max_participation:.0%}，超限动作={capacity.action}",
        "queue_fill_model": (
            f"T 日排队概率为启发式模型（seal_ratio 阈值 {queue_fill.seal_high_ratio}，一字上限 {queue_fill.yizhi_prob_cap}，"
            f"炸板确定性成交={queue_fill.zhaban_fill}）；无 L2 数据，仅模型假设，非精确"
        ),
        "auction": "无 stk_auction 数据时竞价成交价用开盘价代理（entry_scene/exit_scene 均标注 open_proxy）",
        "ex_rights": "除权除息用 adj_factor 比值近似调整持仓股数（分红并入股数等效），非精确现金流",
        "t_plus_1": "买入次一交易日方可卖出；跌停封死/停牌卖出顺延",
        "strict_mode": (
            "strict：所有无法确认成交的信号不计收益（保守口径，默认）"
            if mode == "strict"
            else "loose：概率模型估计成交计入收益（非确认成交，报告需标注）"
        ),
        "no_lookahead": "排队成交概率只用 T 日收盘前字段；as_of 之后数据不可见",
        "lot_size": "A 股 100 股整手",
        "entry_scene": entry_scene,
    }


# ──────────────────────────── 辅助公开入口 ────────────────────────────


def run_strict_loose_comparison(
    signals: Any,
    *,
    db_path: str | Path,
    as_of: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """同批信号 strict / loose 双口径对照报告（口径差异 = 概率模型成交的增量）。"""
    strict = run_limit_up_backtest(
        signals, db_path=db_path, as_of=as_of, mode="strict", **kwargs
    )
    loose = run_limit_up_backtest(
        signals, db_path=db_path, as_of=as_of, mode="loose", **kwargs
    )

    def _by_key(rep: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
        return {
            (t["symbol"], t["signal_date"]): t
            for t in rep["trades"]
            if t.get("status") == "filled"
        }

    s_map, l_map = _by_key(strict), _by_key(loose)
    only_loose = [f"{k[0]}@{k[1]}" for k in l_map.keys() - s_map.keys()]
    delta = {
        "strict_total_return_pct": strict["metrics"]["total_return_pct"],
        "loose_total_return_pct": loose["metrics"]["total_return_pct"],
        "return_delta_pct": None,
        "strict_entry_filled": strict["fill_stats"]["entry_filled"],
        "loose_entry_filled": loose["fill_stats"]["entry_filled"],
        "extra_trades_only_in_loose": only_loose,
        "explanation": "loose 相对 strict 多出的成交全部来自排队/竞价概率模型（seed 可复现），非确认成交",
    }
    if None not in (delta["strict_total_return_pct"], delta["loose_total_return_pct"]):
        delta["return_delta_pct"] = round(
            delta["loose_total_return_pct"] - delta["strict_total_return_pct"], 4
        )
    return {"strict": strict, "loose": loose, "delta": delta}


def capacity_curve(
    signals: Any,
    *,
    db_path: str | Path,
    as_of: str | None = None,
    capitals: Iterable[float] = (1e5, 5e5, 1e6, 5e6, 1e7, 5e7),
    **kwargs: Any,
) -> dict[str, Any]:
    """资金规模 vs 收益 / 换手 的容量曲线（每个规模独立完整跑一遍引擎）。"""
    rows: list[dict[str, Any]] = []
    for cap in capitals:
        rep = run_limit_up_backtest(
            signals, db_path=db_path, as_of=as_of, initial_capital=float(cap), **kwargs
        )
        closed = [t for t in rep["trades"] if t.get("status") == "filled"]
        turnover = (
            sum(t["buy_gross"] + t["sell_gross"] for t in closed) / cap if cap else None
        )
        rows.append(
            {
                "initial_capital": float(cap),
                "total_return_pct": rep["metrics"]["total_return_pct"],
                "annualized_return_pct": rep["metrics"]["annualized_return_pct"],
                "max_drawdown_pct": rep["metrics"]["max_drawdown_pct"],
                "closed_trades": len(closed),
                "capacity_hit_count": rep["capacity"]["capacity_hit_count"],
                "capacity_skip_count": rep["capacity"]["capacity_skip_count"],
                "turnover_ratio": round(turnover, 4) if turnover is not None else None,
            }
        )
    return {"curve": rows, "params": dict(kwargs), "as_of": as_of}


def legacy_lab_diff_report(
    legacy_samples: Any, report: dict[str, Any]
) -> dict[str, Any]:
    """与旧 BacktestLab（无条件次日开盘买入 + 单笔相加）在同批信号上的差异对照。

    纯函数：不修改旧模块，只把旧 samples(list[dict]) 与新报告 trades 对齐比较。
    """
    if hasattr(legacy_samples, "to_dict"):
        legacy = list(legacy_samples.to_dict(orient="records"))
    else:
        legacy = list(legacy_samples or [])
    l_map = {
        (str(r["symbol"]).zfill(6), str(r.get("signal_date", ""))): r for r in legacy
    }
    n_map = {
        (str(t["symbol"]), str(t["signal_date"])): t
        for t in report.get("trades", [])
        if t.get("status") == "filled"
    }
    skipped_new = [
        (
            str(d["symbol"]),
            str(d["signal_date"]),
            d.get("fail_reason") or "entry_not_filled",
        )
        for d in report.get("signal_details", [])
        if not d.get("filled")
    ] + [
        (
            str(t["symbol"]),
            str(t["signal_date"]),
            t.get("fail_reason") or t.get("status"),
        )
        for t in report.get("trades", [])
        if t.get("status") != "filled"
    ]
    common = sorted(l_map.keys() & n_map.keys())
    diffs = [
        round(float(n_map[k]["net_pnl_pct"]) - float(l_map[k].get("pnl_pct", 0.0)), 4)
        for k in common
    ]
    legacy_only = [f"{k[0]}@{k[1]}" for k in sorted(l_map.keys() - n_map.keys())]
    new_only = [f"{k[0]}@{k[1]}" for k in sorted(n_map.keys() - l_map.keys())]
    legacy_sum = sum(float(l_map[k].get("pnl_pct", 0.0)) for k in l_map)
    return {
        "legacy_samples": len(l_map),
        "new_filled_trades": len(n_map),
        "new_skipped": skipped_new,
        "common_signals": len(common),
        "legacy_only_signals": legacy_only,
        "new_only_signals": new_only,
        "legacy_only_note": "旧口径计入但这些信号实际买不到（一字/停牌/strict 未确认），系统性高估来源",
        "mean_pnl_diff_pct(new-legacy)": round(sum(diffs) / len(diffs), 4)
        if diffs
        else None,
        "legacy_sum_single_trade_pct": round(legacy_sum, 4),
        "new_nav_total_return_pct": report.get("metrics", {}).get("total_return_pct"),
        "nav_vs_sum_note": "新绩效为净值曲线口径；legacy_sum 是单笔收益率相加，仅作对照不作结论",
    }


__all__ = [
    "FeeConfig",
    "SlippageConfig",
    "CapacityConfig",
    "QueueFillConfig",
    "MarketDataView",
    "BoardContext",
    "build_board_context",
    "queue_fill_probability",
    "run_limit_up_backtest",
    "run_strict_loose_comparison",
    "capacity_curve",
    "legacy_lab_diff_report",
    "DEFAULT_MCAP_BUCKETS",
    "TRADING_DAYS_PER_YEAR",
]
