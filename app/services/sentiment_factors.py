"""市场情绪周期与连板梯队日频因子族 —— issue #25 纯计算层。

本模块只负责**计算**：从 #22 已落库的 ``market_kline.db``
（``kline_daily`` / ``kline_daily`` 历史 + ``limit_board_daily`` /
``index_daily`` / ``market_capital_flow`` / ``symbol_names`` /
``kline_minute``）读取原始行，产出可回测的日频因子表。建表、持久化、
API 与前端由主仓库后续统一接线（``market_sentiment_daily`` schema 不在
本模块职责内）。

口径与 PIT 约定（验收标准"公式文档化"即指本节）：

1. **严格 PIT（收盘口径）**：主键日 ``D`` 的收盘行只使用
   ``trade_date <= D`` 的数据。"昨日涨停今日溢价"按**观察日**记账：
   涨停池日 ``T`` 的溢价写入 ``T+1`` 行（``yesterday_limit_up_premium_*``），
   因此 ``D`` 行在 ``D`` 收盘后即可一次写死、永不回填修改 —— 不存在
   未来函数；增量跑批与全量历史回填使用同一函数、同一公式（幂等）。
2. **盘中隔离**：盘中估算只能通过 ``compute_intraday_estimates`` 产出，
   所有键带 ``intraday_`` 前缀，且收盘 regime 的规则字段被
   ``market_regime.RULE_INPUT_FIELDS`` 白名单锁死，``intraday_*``
   在结构上无法污染收盘行（测试锁定）。
3. **缺失 ≠ 0**：任何不可计算指标返回 ``None`` 并在 ``missing_flags``
   （JSON 数组，键名有序）中说明原因；分母为 0、上游表无当日数据、
   涨停池字段缺失等一律为缺失。
4. **口径参数化**：``market_scope`` 支持
   ``all``(沪深北)/``sh_sz``(沪深)/``main``(沪深主板)/``sh_main``/
   ``sz_main``/``chinext``/``star``/``bse``；``include_st``、
   ``exclude_new_days`` 控制 ST/新股。默认全市场、剔除 ST、剔除上市
   不足 5 个交易日的新股。
5. **幸存者偏差**：涨跌家数/成交额按**当日实际有行情记录的股票**统计
   （当日停牌自然不在内），不用当前股票列表回看；涨停池取
   ``limit_board_daily``（Tushare limit_list_d 当日快照）。
6. **停牌/退市**：``halted_or_missing_count`` = 前一交易日有行情、
   当日无行情的标的数（含停牌与退市尾部，仅收盘时点可知，只作报表，
   不参与打分）。退市股停止披露后自然退出样本。
7. **数据缺失降级**：``limit_board_daily`` 当日无任何行 → 涨停/跌停/
   炸板/梯队族全部 ``None`` + ``limit_board_missing`` 标记，广度族照常。

单位：``market_amount`` 为元（与 #22 v3 落库口径一致）；
``northbound_net_flow`` 透传 ``market_capital_flow.north_money``，
单位与同步层一致，不在本层换算。指数收益率转为小数（Tushare
``pct_chg`` 为百分数，除以 100）。

依赖：仅标准库 + pandas（用于组装结果帧）+ 同目录
``market_regime``；不 import 任何服务容器/路由/模型层代码。
"""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
from bisect import bisect_left
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd

from app.services.market_regime import (
    RegimeRule,
    classify_regime,
    compute_sentiment_score,
    num_field,
)

INTRADAY_PREFIX = "intraday_"

# 主板 10%、创业/科创 20%、北交所 30%；ST 主板 5%（创业/科创 ST 10%）。
# 容忍度与 quiet_breakout_scanner/features.py 口径一致。
_LIMIT_PCT_MAIN = 0.10
_LIMIT_PCT_CHINEXT_STAR = 0.20
_LIMIT_PCT_BSE = 0.30
_LIMIT_PCT_ST_MAIN = 0.05
_LIMIT_PCT_ST_GROWTH = 0.10
_LIMIT_TOLERANCE = 0.0025

_SCOPE_PREFIXES: Mapping[str, tuple[str, ...]] = {
    "main": ("60", "00"),
    "sh_main": ("60",),
    "sz_main": ("00",),
    "chinext": ("30",),
    "star": ("68",),
    "bse": ("43", "83", "87", "920"),
    "sh_sz": ("60", "00", "30", "68"),
    "all": ("60", "00", "30", "68", "43", "83", "87", "920"),
}

VALID_MARKET_SCOPES = tuple(sorted(_SCOPE_PREFIXES))

#: 收盘因子输出列（顺序稳定，供主 agent 建 ``market_sentiment_daily`` 参考）。
OUTPUT_COLUMNS: tuple[str, ...] = (
    "trade_date",
    "market_scope",
    # 样本与口径
    "traded_count",
    "breadth_sample_count",
    "halted_or_missing_count",
    "excluded_st_count",
    "excluded_new_count",
    # A. 广度
    "limit_up_count",
    "limit_down_count",
    "limit_up_down_ratio",
    "up_count",
    "down_count",
    "flat_count",
    "up_ratio",
    "adv_dec_ratio",
    "mean_return",
    "median_return",
    "strong_count",
    "weak_count",
    "market_amount",
    "market_amount_ratio_5d",
    # B. 连板梯队
    "max_consecutive_boards",
    "board_count_2",
    "board_count_3",
    "board_count_4_plus",
    "first_board_count",
    "continuation_count",
    "promotion_rate_1_to_2",
    "promotion_rate_2_to_3",
    "promotion_rate_3_to_4",
    "first_board_promotion_rate",
    "ladder_integrity",
    "ladder_concentration",
    # C. 赚钱/亏钱效应
    "broken_board_count",
    "broken_board_rate",
    "big_loss_count",
    "big_loss_rate",
    "yesterday_limit_up_premium_open_mean",
    "yesterday_limit_up_premium_open_median",
    "yesterday_limit_up_premium_close_mean",
    "yesterday_limit_up_premium_close_median",
    "yesterday_limit_up_premium_high_mean",
    "yesterday_limit_up_premium_high_median",
    "yesterday_continuation_premium_open_mean",
    "yesterday_continuation_premium_close_mean",
    "yesterday_continuation_premium_close_median",
    "money_making_effect",
    "loss_effect",
    # D. 题材/资金（主题聚合依赖 #8，先占位列保持 schema 前向兼容）
    "index_return_1d",
    "index_return_5d",
    "index_return_20d",
    "index_volatility_20d",
    "northbound_net_flow",
    "main_theme_count",
    "theme_diffusion",
    "margin_balance_change",
    "limit_theme_count",
    # E. 分数与标签
    "sentiment_score",
    "sentiment_score_breadth",
    "sentiment_score_ladder",
    "sentiment_score_effect",
    "prev_sentiment_score",
    "regime",
    "prev_regime",
    "missing_flags",
)

#: 盘中估算输出键（全部 intraday_ 前缀 + 元数据）。
INTRADAY_OUTPUT_COLUMNS: tuple[str, ...] = (
    "trade_date",
    "market_scope",
    "intraday_as_of_time",
    "intraday_traded_count",
    "intraday_limit_up_count",
    "intraday_limit_down_count",
    "intraday_limit_up_down_ratio",
    "intraday_broken_board_count",
    "intraday_broken_board_rate",
    "intraday_up_count",
    "intraday_down_count",
    "intraday_up_ratio",
    "intraday_adv_dec_ratio",
    "intraday_mean_return",
    "intraday_median_return",
    "intraday_strong_count",
    "intraday_weak_count",
    "intraday_market_amount",
    "intraday_max_consecutive_boards",
    "intraday_board_count_2",
    "intraday_board_count_3",
    "intraday_board_count_4_plus",
    "intraday_first_board_count",
    "intraday_continuation_count",
    "intraday_promotion_rate_1_to_2",
    "intraday_ladder_integrity",
    "intraday_ladder_concentration",
    "intraday_big_loss_rate",
    "intraday_yesterday_limit_up_premium_close_mean",
    "intraday_money_making_effect",
    "intraday_loss_effect",
    "intraday_sentiment_score",
    "intraday_sentiment_score_breadth",
    "intraday_sentiment_score_ladder",
    "intraday_sentiment_score_effect",
    "intraday_regime",
    "intraday_missing_flags",
)


# ─── 基础工具 ───


def normalize_symbol(raw: Any) -> str:
    """去掉 ts_code 后缀，统一成 6 位代码：``600519.SH`` -> ``600519``。"""
    symbol = str(raw or "").strip().upper()
    if "." in symbol:
        head, tail = symbol.split(".", 1)
        # 兼容 ``SH.600519`` 反写形式。
        if head.isdigit():
            symbol = head
        elif tail.isdigit():
            symbol = tail
    return symbol.split(".")[0]


def market_scope_matches(symbol: str, scope: str) -> bool:
    prefixes = _SCOPE_PREFIXES[scope]
    return normalize_symbol(symbol).startswith(prefixes)


def _validate_scope(market_scope: str) -> None:
    if market_scope not in _SCOPE_PREFIXES:
        raise ValueError(
            f"unknown market_scope={market_scope!r}; "
            f"valid: {', '.join(VALID_MARKET_SCOPES)}"
        )


def limit_pct_for(symbol: str, is_st: bool) -> float:
    code = normalize_symbol(symbol)
    if code.startswith(("43", "83", "87", "920")):
        return _LIMIT_PCT_BSE
    if code.startswith(("30", "68")):
        return _LIMIT_PCT_ST_GROWTH if is_st else _LIMIT_PCT_CHINEXT_STAR
    return _LIMIT_PCT_ST_MAIN if is_st else _LIMIT_PCT_MAIN


def _round6(value: float | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 6)


def _mean(values: Sequence[float]) -> float | None:
    clean = [float(v) for v in values if v is not None and not _is_dirty(v)]
    return _round6(statistics.fmean(clean)) if clean else None


def _median(values: Sequence[float]) -> float | None:
    clean = [float(v) for v in values if v is not None and not _is_dirty(v)]
    return _round6(statistics.median(clean)) if clean else None


def _is_dirty(value: Any) -> bool:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return True
    return math.isnan(v) or math.isinf(v)


def _ratio(numerator: float, denominator: float | None) -> float | None:
    if denominator is None or denominator == 0:
        return None
    return _round6(numerator / denominator)


def _as_of_date(value: str) -> date:
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def _shift_date(value: str, days: int) -> str:
    return (_as_of_date(value) + timedelta(days=days)).isoformat()


# ─── 数据装载 ───


@dataclass
class _Dataset:
    """一次计算需要的原始行（全部只读、限定日期窗口）。"""

    missing_tables: set[str] = field(default_factory=set)
    kline_by_date: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    kline_dates_by_symbol: dict[str, list[str]] = field(default_factory=dict)
    limit_by_date: dict[str, dict[str, dict[str, dict[str, Any]]]] = field(
        default_factory=dict
    )
    limit_names: dict[tuple[str, str], str] = field(default_factory=dict)
    index_rows: list[dict[str, Any]] = field(default_factory=list)
    flow_by_date: dict[str, dict[str, Any]] = field(default_factory=dict)
    name_by_symbol: dict[str, str] = field(default_factory=dict)
    minute_by_symbol: dict[str, dict[str, Any]] = field(default_factory=dict)
    calendar: list[str] = field(default_factory=list)
    calendar_index: dict[str, int] = field(default_factory=dict)


def _connect_ro(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"database not found: {path}")
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _fetch(
    conn: sqlite3.Connection, dataset: _Dataset, sql: str, params: tuple, table: str
) -> list[sqlite3.Row]:
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        # 表不存在（旧库/未同步）按缺失处理，不抛异常。
        if "no such table" in str(exc):
            dataset.missing_tables.add(table)
        else:
            raise
        return []


def _load_dataset(
    conn: sqlite3.Connection,
    *,
    window_start: str,
    window_end: str,
    market_scope: str,
    index_ts_code: str,
    trade_date_for_minutes: str | None = None,
    minute_cutoff: str | None = None,
) -> _Dataset:
    dataset = _Dataset()

    rows = _fetch(
        conn,
        dataset,
        """
        SELECT symbol, trade_date, open, high, low, close, volume, amount
        FROM kline_daily
        WHERE trade_date >= ? AND trade_date <= ?
        ORDER BY symbol ASC, trade_date ASC
        """,
        (window_start, window_end),
        "kline_daily",
    )
    for row in rows:
        symbol = normalize_symbol(row["symbol"])
        trade_date = str(row["trade_date"])
        if not market_scope_matches(symbol, market_scope):
            continue
        dataset.kline_by_date.setdefault(trade_date, {})[symbol] = {
            "symbol": symbol,
            "trade_date": trade_date,
            "open": row["open"],
            "high": row["high"],
            "low": row["low"],
            "close": row["close"],
            "volume": row["volume"],
            "amount": row["amount"],
        }
        dataset.kline_dates_by_symbol.setdefault(symbol, []).append(trade_date)

    rows = _fetch(
        conn,
        dataset,
        """
        SELECT symbol, trade_date, limit_flag, limit_times, close, name,
               pct_chg, open_times, fd_amount, theme
        FROM limit_board_daily
        WHERE trade_date >= ? AND trade_date <= ?
        ORDER BY trade_date ASC, symbol ASC
        """,
        (window_start, window_end),
        "limit_board_daily",
    )
    for row in rows:
        symbol = normalize_symbol(row["symbol"])
        if not market_scope_matches(symbol, market_scope):
            continue
        trade_date = str(row["trade_date"])
        flag = str(row["limit_flag"] or "").upper()
        if row["name"] not in (None, ""):
            dataset.limit_names[(trade_date, symbol)] = str(row["name"])
        dataset.limit_by_date.setdefault(trade_date, {}).setdefault(flag, {})[
            symbol
        ] = {
            "symbol": symbol,
            "trade_date": trade_date,
            "limit_times": row["limit_times"],
            "close": row["close"],
            "name": row["name"],
            "pct_chg": row["pct_chg"],
            "open_times": row["open_times"],
            "fd_amount": row["fd_amount"],
            "theme": row["theme"],
        }

    rows = _fetch(
        conn,
        dataset,
        """
        SELECT trade_date, open, high, low, close, pre_close, pct_chg
        FROM index_daily
        WHERE ts_code = ? AND trade_date >= ? AND trade_date <= ?
        ORDER BY trade_date ASC
        """,
        (index_ts_code, window_start, window_end),
        "index_daily",
    )
    dataset.index_rows = [dict(row) for row in rows]

    rows = _fetch(
        conn,
        dataset,
        """
        SELECT trade_date, hgt, sgt, north_money, south_money
        FROM market_capital_flow
        WHERE trade_date >= ? AND trade_date <= ?
        ORDER BY trade_date ASC
        """,
        (window_start, window_end),
        "market_capital_flow",
    )
    for row in rows:
        dataset.flow_by_date[str(row["trade_date"])] = dict(row)

    rows = _fetch(
        conn,
        dataset,
        "SELECT symbol, name FROM symbol_names",
        (),
        "symbol_names",
    )
    for row in rows:
        dataset.name_by_symbol[normalize_symbol(row["symbol"])] = str(row["name"])

    if trade_date_for_minutes is not None and minute_cutoff is not None:
        rows = _fetch(
            conn,
            dataset,
            """
            SELECT symbol, bar_time, open, high, low, close, volume, amount
            FROM kline_minute
            WHERE trade_date = ? AND bar_time <= ?
            ORDER BY symbol ASC, bar_time ASC
            """,
            (trade_date_for_minutes, minute_cutoff),
            "kline_minute",
        )
        for row in rows:
            symbol = normalize_symbol(row["symbol"])
            if not market_scope_matches(symbol, market_scope):
                continue
            acc = dataset.minute_by_symbol.setdefault(
                symbol,
                {
                    "last_close": None,
                    "last_bar": "",
                    "high": None,
                    "amount": 0.0,
                    "amount_seen": False,
                },
            )
            bar_time = str(row["bar_time"])
            if bar_time >= acc["last_bar"]:
                acc["last_bar"] = bar_time
                acc["last_close"] = row["close"]
            highs = [v for v in (acc["high"], row["high"]) if v is not None]
            acc["high"] = max(highs) if highs else None
            if row["amount"] is not None and not _is_dirty(row["amount"]):
                acc["amount"] += float(row["amount"])
                acc["amount_seen"] = True

    dates = set(dataset.kline_by_date) | set(dataset.limit_by_date)
    dataset.calendar = sorted(dates)
    dataset.calendar_index = {d: i for i, d in enumerate(dataset.calendar)}
    return dataset


# ─── 过滤规则（ST / 新股） ───


def _is_st_name(name: Any) -> bool:
    text = str(name or "").upper()
    return "ST" in text


def _st_by_name(dataset: _Dataset, symbol: str, trade_date: str) -> bool:
    """纯名称判定的 ST 标记（不受 ``include_st`` 开关影响）。"""
    idx = dataset.calendar_index.get(trade_date)
    if idx is not None:
        days = list(reversed(dataset.calendar[: idx + 1]))
    else:
        days = sorted(
            (d for d, s in dataset.limit_names if s == symbol and d <= trade_date),
            reverse=True,
        )
    for day in days:
        name = dataset.limit_names.get((day, symbol))
        if name:
            return _is_st_name(name)
    snapshot = dataset.name_by_symbol.get(symbol)
    return _is_st_name(snapshot) if snapshot else False


@dataclass
class _Filters:
    include_st: bool
    exclude_new_days: int
    dataset: _Dataset
    st_cache: dict[tuple[str, str], bool] = field(default_factory=dict)

    def is_st(self, symbol: str, trade_date: str) -> bool:
        """当日/更早涨停行名称优先，退回当前 ``symbol_names`` 快照。

        名称随时间变化（中途戴帽/摘帽），历史回填应尽量用"当时"的名称：
        涨停池行自带当日 ``name``；只有 K 线侧标的才退到快照名，存在
        幸存者偏差，已在模块 docstring 中声明。
        """
        if self.include_st:
            return False
        cached = self.st_cache.get((symbol, trade_date))
        if cached is not None:
            return cached
        result = _st_by_name(self.dataset, symbol, trade_date)
        self.st_cache[(symbol, trade_date)] = result
        return result

    def is_new(self, symbol: str, trade_date: str) -> bool:
        """上市不足 ``exclude_new_days`` 个交易日视为新股（首日不计入）。"""
        if self.exclude_new_days <= 0:
            return False
        dates = self.dataset.kline_dates_by_symbol.get(symbol)
        if not dates:
            return False
        first = dates[0]
        idx = self.dataset.calendar_index
        if first not in idx or trade_date not in idx:
            return False
        return idx[trade_date] - idx[first] < self.exclude_new_days


def _make_filters(
    dataset: _Dataset, include_st: bool, exclude_new_days: int
) -> _Filters:
    return _Filters(
        include_st=include_st,
        exclude_new_days=exclude_new_days,
        dataset=dataset,
    )


# ─── 因子族计算 ───


def _pct_from(row_close: float | None, prev_close: float | None) -> float | None:
    if (
        row_close is None
        or prev_close is None
        or _is_dirty(row_close)
        or (_is_dirty(prev_close))
    ):
        return None
    prev = float(prev_close)
    if prev <= 0:
        return None
    return float(row_close) / prev - 1.0


def _cohort(
    dataset: _Dataset, filters: _Filters, trade_date: str, flag: str
) -> dict[str, dict[str, Any]] | None:
    """当日涨停(U)/跌停(D)/炸板(Z)池（已按 scope/ST/新股过滤）。

    返回 ``None`` 表示当日 limit_board_daily 无任何行（缺数），
    空 dict 表示真实为 0（例：真实无涨停）。
    """
    bucket = dataset.limit_by_date.get(trade_date)
    if bucket is None:
        return None
    rows = bucket.get(flag, {})
    result: dict[str, dict[str, Any]] = {}
    for symbol, row in rows.items():
        if filters.is_st(symbol, trade_date):
            continue
        if filters.is_new(symbol, trade_date):
            continue
        result[symbol] = row
    return result


def _breadth_factors(
    dataset: _Dataset,
    filters: _Filters,
    trade_date: str,
    flags: set[str],
) -> dict[str, Any]:
    today = dataset.kline_by_date.get(trade_date, {})
    prev_day = _prev_calendar_date(dataset, trade_date)
    prev_map = dataset.kline_by_date.get(prev_day, {}) if prev_day else {}

    returns: list[float] = []
    up = down = flat = strong = weak = 0
    amount_total: float | None = 0.0
    amount_any_missing = False
    excluded_st = excluded_new = traded = 0

    for symbol, row in sorted(today.items()):
        traded += 1
        if filters.is_st(symbol, trade_date):
            excluded_st += 1
            continue
        if filters.is_new(symbol, trade_date):
            excluded_new += 1
            continue
        # 收益率：优先用"前一交易日"同列表盘，缺失时回退到该股自身
        # 最近一条更早的行情（复牌补跌与 features.py shift(1) 同口径）。
        prev_close = None
        prev_row = prev_map.get(symbol)
        if prev_row is not None:
            prev_close = prev_row["close"]
        else:
            dates = dataset.kline_dates_by_symbol.get(symbol) or []
            pos = bisect_left(dates, trade_date)
            if pos > 0:
                earlier = dates[pos - 1]
                prev_close = dataset.kline_by_date.get(earlier, {}).get(symbol, {})[
                    "close"
                ]
        ret = _pct_from(row["close"], prev_close)
        if ret is None:
            flags.add("return_prev_close_missing")
        else:
            returns.append(ret)
            if ret > 0:
                up += 1
            elif ret < 0:
                down += 1
            else:
                flat += 1
            if ret >= 0.05:
                strong += 1
            if ret <= -0.05:
                weak += 1
        if row["amount"] is None or _is_dirty(row["amount"]):
            amount_any_missing = True
        else:
            amount_total = (amount_total or 0.0) + float(row["amount"])

    if amount_any_missing and amount_total == 0.0:
        amount_total = None
        flags.add("market_amount_missing")
    elif amount_any_missing:
        flags.add("market_amount_partial")

    halted = 0
    if prev_map:
        for symbol in prev_map:
            if symbol not in today:
                halted += 1
    sample = len(returns)
    factors: dict[str, Any] = {
        "traded_count": traded,
        "breadth_sample_count": sample,
        "halted_or_missing_count": halted if prev_map else None,
        "excluded_st_count": excluded_st,
        "excluded_new_count": excluded_new,
        "up_count": up if sample else None,
        "down_count": down if sample else None,
        "flat_count": flat if sample else None,
        "up_ratio": _ratio(up, sample or None),
        "adv_dec_ratio": _ratio(up, down if down else None),
        "mean_return": _mean(returns),
        "median_return": _median(returns),
        "strong_count": strong if sample else None,
        "weak_count": weak if sample else None,
        "market_amount": _round6(amount_total) if amount_total else None,
    }
    if dataset.kline_by_date.get(trade_date) is None:
        flags.add("kline_day_missing")
    return factors


def _prev_calendar_date(dataset: _Dataset, trade_date: str) -> str | None:
    """前一交易日。``trade_date`` 本身不在日历时也成立（盘中当日尚未
    有日线路，只有分钟线 —— 此时取严格早于它的最大日历日）。"""
    idx = dataset.calendar_index.get(trade_date)
    if idx is not None:
        return dataset.calendar[idx - 1] if idx > 0 else None
    pos = bisect_left(dataset.calendar, trade_date)
    return dataset.calendar[pos - 1] if pos > 0 else None


def _limit_count_factors(
    dataset: _Dataset,
    filters: _Filters,
    trade_date: str,
    flags: set[str],
) -> dict[str, Any]:
    factors: dict[str, Any] = {}
    up_pool = _cohort(dataset, filters, trade_date, "U")
    down_pool = _cohort(dataset, filters, trade_date, "D")
    broken_pool = _cohort(dataset, filters, trade_date, "Z")
    if up_pool is None:
        flags.add("limit_board_missing")
        for key in (
            "limit_up_count",
            "limit_down_count",
            "limit_up_down_ratio",
            "broken_board_count",
            "broken_board_rate",
        ):
            factors[key] = None
        return factors
    factors["limit_up_count"] = len(up_pool)
    factors["limit_down_count"] = len(down_pool) if down_pool is not None else None
    if factors["limit_down_count"]:
        factors["limit_up_down_ratio"] = _ratio(
            len(up_pool), float(factors["limit_down_count"])
        )
    else:
        factors["limit_up_down_ratio"] = None
        if factors["limit_down_count"] is not None:
            flags.add("limit_down_zero")

    if broken_pool is None:
        factors["broken_board_count"] = None
        factors["broken_board_rate"] = None
        flags.add("broken_pool_missing")
    else:
        touched = len(broken_pool) + len(up_pool)
        factors["broken_board_count"] = len(broken_pool)
        factors["broken_board_rate"] = _ratio(len(broken_pool), float(touched) or None)
    return factors


def _limit_times_value(row: Mapping[str, Any]) -> int | None:
    value = row.get("limit_times")
    if value is None or _is_dirty(value):
        return None
    return int(float(value))


def _ladder_factors(
    dataset: _Dataset,
    filters: _Filters,
    trade_date: str,
    flags: set[str],
) -> dict[str, Any]:
    factors: dict[str, Any] = {
        key: None
        for key in (
            "max_consecutive_boards",
            "board_count_2",
            "board_count_3",
            "board_count_4_plus",
            "first_board_count",
            "continuation_count",
            "promotion_rate_1_to_2",
            "promotion_rate_2_to_3",
            "promotion_rate_3_to_4",
            "first_board_promotion_rate",
            "ladder_integrity",
            "ladder_concentration",
            "limit_theme_count",
        )
    }
    today_pool = _cohort(dataset, filters, trade_date, "U")
    if today_pool is None:
        return factors
    boards = []
    for row in today_pool.values():
        value = _limit_times_value(row)
        if value is None:
            flags.add("limit_times_missing")
            continue
        boards.append(value)
    if not today_pool:
        flags.add("no_limit_up_today")
    if boards:
        factors["max_consecutive_boards"] = max(boards)
        factors["board_count_2"] = sum(1 for b in boards if b == 2)
        factors["board_count_3"] = sum(1 for b in boards if b == 3)
        factors["board_count_4_plus"] = sum(1 for b in boards if b >= 4)
        factors["first_board_count"] = sum(1 for b in boards if b == 1)
        factors["continuation_count"] = sum(1 for b in boards if b >= 2)
        if len(boards) < len(today_pool):
            flags.add("board_counts_partial")
        bc2 = factors["board_count_2"]
        bc3 = factors["board_count_3"]
        bc4 = factors["board_count_4_plus"]
        factors["ladder_integrity"] = int(
            (bc2 or 0) > 0 and (bc3 or 0) > 0 and (bc4 or 0) > 0
        )
        consec = [b for b in boards if b >= 2]
        if consec:
            counts = {k: consec.count(k) for k in set(consec)}
            total = len(consec)
            factors["ladder_concentration"] = _round6(
                sum((c / total) ** 2 for c in counts.values())
            )
    elif not today_pool:
        # 真实"零涨停"日：高度与梯队计数为 0（不是缺失）。
        factors["max_consecutive_boards"] = 0
        factors["board_count_2"] = 0
        factors["board_count_3"] = 0
        factors["board_count_4_plus"] = 0
        factors["first_board_count"] = 0
        factors["continuation_count"] = 0
        factors["ladder_integrity"] = 0
    themes = {
        str(row["theme"]).strip()
        for row in today_pool.values()
        if row.get("theme") not in (None, "")
    }
    if themes:
        factors["limit_theme_count"] = len(themes)
    else:
        flags.add("theme_missing")

    prev_day = _prev_calendar_date(dataset, trade_date)
    prev_pool = _cohort(dataset, filters, prev_day, "U") if prev_day else None
    if prev_pool is None:
        flags.add("prev_limit_pool_missing")
        return factors
    today_by_symbol = today_pool
    for n, key in (
        (1, "promotion_rate_1_to_2"),
        (2, "promotion_rate_2_to_3"),
        (3, "promotion_rate_3_to_4"),
    ):
        cohort_n = {
            symbol for symbol, row in prev_pool.items() if _limit_times_value(row) == n
        }
        if not cohort_n:
            flags.add(f"promotion_denom_zero_{n}")
            continue
        promoted = 0
        for symbol in cohort_n:
            row = today_by_symbol.get(symbol)
            if row is not None and _limit_times_value(row) == n + 1:
                promoted += 1
        factors[key] = _ratio(promoted, float(len(cohort_n)))
    factors["first_board_promotion_rate"] = factors["promotion_rate_1_to_2"]
    return factors


def _effect_factors(
    dataset: _Dataset,
    filters: _Filters,
    trade_date: str,
    flags: set[str],
    big_loss_threshold: float,
) -> dict[str, Any]:
    """昨日涨停池在今日（观察日 D）的溢价/赚钱/亏钱效应。

    PIT：cohort 来自 D-1 收盘涨停池，价格取 D 日行情 —— D 收盘即可
    完整计算，本行永不依赖 D+1 数据（未来函数防护点）。
    """
    factors: dict[str, Any] = {
        key: None
        for key in (
            "big_loss_count",
            "big_loss_rate",
            "yesterday_limit_up_premium_open_mean",
            "yesterday_limit_up_premium_open_median",
            "yesterday_limit_up_premium_close_mean",
            "yesterday_limit_up_premium_close_median",
            "yesterday_limit_up_premium_high_mean",
            "yesterday_limit_up_premium_high_median",
            "yesterday_continuation_premium_open_mean",
            "yesterday_continuation_premium_close_mean",
            "yesterday_continuation_premium_close_median",
            "money_making_effect",
            "loss_effect",
        )
    }
    prev_day = _prev_calendar_date(dataset, trade_date)
    cohort = _cohort(dataset, filters, prev_day, "U") if prev_day else None
    if cohort is None:
        flags.add("prev_limit_pool_missing_effect")
        return factors
    if not cohort:
        flags.add("prev_limit_pool_empty")
        return factors

    today_map = dataset.kline_by_date.get(trade_date, {})
    opens: list[float] = []
    closes: list[float] = []
    highs: list[float] = []
    cont_opens: list[float] = []
    cont_closes: list[float] = []
    suspended = 0
    missing_price = 0
    for symbol, row in sorted(cohort.items()):
        base_close = row.get("close")
        if base_close is None or _is_dirty(base_close):
            base_close = (
                dataset.kline_by_date.get(prev_day, {}).get(symbol, {}).get("close")
            )
        day_row = today_map.get(symbol)
        if day_row is None:
            suspended += 1
            continue
        if base_close is None or _is_dirty(base_close) or float(base_close) <= 0:
            missing_price += 1
            continue
        base = float(base_close)
        open_p = _pct_from(day_row["open"], base)
        close_p = _pct_from(day_row["close"], base)
        high_p = _pct_from(day_row["high"], base)
        if open_p is not None:
            opens.append(open_p)
        if close_p is not None:
            closes.append(close_p)
        if high_p is not None:
            highs.append(high_p)
        if (_limit_times_value(row) or 1) >= 2:
            if open_p is not None:
                cont_opens.append(open_p)
            if close_p is not None:
                cont_closes.append(close_p)
    if suspended:
        flags.add(f"cohort_halted:{suspended}")
    if missing_price:
        flags.add(f"cohort_base_close_missing:{missing_price}")
    if not closes and not opens and not highs:
        flags.add("premium_no_observations")
        return factors

    factors["yesterday_limit_up_premium_open_mean"] = _mean(opens)
    factors["yesterday_limit_up_premium_open_median"] = _median(opens)
    factors["yesterday_limit_up_premium_close_mean"] = _mean(closes)
    factors["yesterday_limit_up_premium_close_median"] = _median(closes)
    factors["yesterday_limit_up_premium_high_mean"] = _mean(highs)
    factors["yesterday_limit_up_premium_high_median"] = _median(highs)
    factors["yesterday_continuation_premium_open_mean"] = _mean(cont_opens)
    factors["yesterday_continuation_premium_close_mean"] = _mean(cont_closes)
    factors["yesterday_continuation_premium_close_median"] = _median(cont_closes)

    denominator = len(closes) or None
    if denominator:
        gains = sum(1 for v in closes if v > 0)
        losses = sum(1 for v in closes if v < 0)
        big_loss = sum(1 for v in closes if v <= big_loss_threshold)
        factors["money_making_effect"] = _ratio(gains, float(denominator))
        factors["loss_effect"] = _ratio(losses, float(denominator))
        factors["big_loss_count"] = big_loss
        factors["big_loss_rate"] = _ratio(big_loss, float(denominator))
    return factors


def _index_flow_factors(
    dataset: _Dataset,
    trade_date: str,
    flags: set[str],
) -> dict[str, Any]:
    factors: dict[str, Any] = {
        "index_return_1d": None,
        "index_return_5d": None,
        "index_return_20d": None,
        "index_volatility_20d": None,
        "northbound_net_flow": None,
        "main_theme_count": None,
        "theme_diffusion": None,
        "margin_balance_change": None,
    }
    rows = [r for r in dataset.index_rows if str(r["trade_date"]) <= trade_date]
    if not rows or str(rows[-1]["trade_date"]) != trade_date:
        flags.add("index_day_missing")
    else:
        latest = rows[-1]
        r1 = num_field(latest, "pct_chg")
        if r1 is not None:
            factors["index_return_1d"] = _round6(r1 / 100.0)
        else:
            pre = num_field(latest, "pre_close")
            close = num_field(latest, "close")
            if pre and close and pre > 0:
                factors["index_return_1d"] = _round6(close / pre - 1.0)
            else:
                flags.add("index_return_1d_missing")
        close_series = [num_field(r, "close") for r in rows]
        for lag, key in ((5, "index_return_5d"), (20, "index_return_20d")):
            if len(rows) > lag:
                base = close_series[-1 - lag]
                head = close_series[-1]
                if base and head and base > 0:
                    factors[key] = _round6(head / base - 1.0)
            if factors[key] is None:
                flags.add(f"{key}_insufficient_history")
        returns_20 = [
            _pct_from(close_series[i], close_series[i - 1])
            for i in range(max(1, len(close_series) - 20), len(close_series))
        ]
        clean = [v for v in returns_20 if v is not None]
        if len(clean) >= 20:
            factors["index_volatility_20d"] = _round6(statistics.stdev(clean))
        else:
            flags.add("index_volatility_20d_insufficient_history")

    flow = dataset.flow_by_date.get(trade_date)
    if flow is not None:
        factors["northbound_net_flow"] = num_field(flow, "north_money")
    if factors["northbound_net_flow"] is None:
        flags.add("northbound_missing")
    flags.add("theme_engine_pending_#8")
    flags.add("margin_balance_pending_#1")
    return factors


def _amount_ratio_5d(
    history_amounts: list[float | None], flags: set[str]
) -> float | None:
    """当日成交额 / 之前 5 个有数据交易日的均值（不含当日）。"""
    prior = [v for v in history_amounts[:-1] if v is not None]
    if len(prior) < 5:
        flags.add("amount_ratio_5d_insufficient_history")
        return None
    window = prior[-5:]
    mean_prev = statistics.fmean(window)
    if mean_prev <= 0:
        return None
    return _ratio(history_amounts[-1], mean_prev)


# ─── 主入口：收盘帧 ───


def compute_sentiment_frame(
    db_path: str | Path,
    start_date: str,
    end_date: str,
    *,
    market_scope: str = "all",
    index_ts_code: str = "000001.SH",
    include_st: bool = False,
    exclude_new_days: int = 5,
    big_loss_threshold: float = -0.05,
    lookback_days: int = 75,
    score_buckets: Mapping[str, Sequence[Any]] | None = None,
    score_bucket_weights: Mapping[str, float] | None = None,
    regime_rules: Sequence[RegimeRule] | None = None,
    regime_params: Mapping[str, float] | None = None,
) -> pd.DataFrame:
    """回测友好的日频情绪因子表（幂等：同输入同输出）。

    * 只读取 ``[start_date - lookback_days, end_date]`` 的原始行；
      ``D`` 行只依赖 ``<= D`` 的数据，窗口扩大不会改变既有行
      （增量与全量回填结果逐行一致）。
    * 不负责建表/写库；输出 ``dtype=object`` 的 DataFrame，缺失值保持
      ``None``（写库即 NULL，不会填 0）。
    """
    _validate_scope(market_scope)
    if start_date > end_date:
        raise ValueError("start_date must be <= end_date")
    window_start = _shift_date(start_date, -abs(lookback_days))
    with closing(_connect_ro(db_path)) as conn:
        dataset = _load_dataset(
            conn,
            window_start=window_start,
            window_end=end_date,
            market_scope=market_scope,
            index_ts_code=index_ts_code,
        )
    filters = _make_filters(dataset, include_st, exclude_new_days)

    rows: list[dict[str, Any]] = []
    amount_history: list[float | None] = []
    prev_factors: dict[str, Any] | None = None
    prev_score: float | None = None
    prev_regime: str | None = None

    # 遍历整个回看窗口（含 buffer 日）：保证任一行 D 的计算与
    # start_date/end_date 的取值无关（增量与全量回填逐行一致）。
    for trade_date in dataset.calendar:
        if trade_date > end_date:
            break
        flags: set[str] = {f"table_missing:{name}" for name in dataset.missing_tables}
        factors: dict[str, Any] = {
            "trade_date": trade_date,
            "market_scope": market_scope,
        }
        factors.update(_breadth_factors(dataset, filters, trade_date, flags))
        factors.update(_limit_count_factors(dataset, filters, trade_date, flags))
        factors.update(_ladder_factors(dataset, filters, trade_date, flags))
        factors.update(
            _effect_factors(dataset, filters, trade_date, flags, big_loss_threshold)
        )
        factors.update(_index_flow_factors(dataset, trade_date, flags))

        amount_history.append(factors.get("market_amount"))
        factors["market_amount_ratio_5d"] = _amount_ratio_5d(amount_history, flags)

        score_result = compute_sentiment_score(
            factors, buckets=score_buckets, bucket_weights=score_bucket_weights
        )
        factors["sentiment_score"] = score_result.score
        factors["sentiment_score_breadth"] = score_result.components.get("breadth")
        factors["sentiment_score_ladder"] = score_result.components.get("ladder")
        factors["sentiment_score_effect"] = score_result.components.get("effect")
        factors["prev_sentiment_score"] = prev_score
        regime = classify_regime(
            factors,
            score=score_result.score,
            prev_factors=prev_factors,
            prev_score=prev_score,
            regime_rules=regime_rules,
            regime_params=regime_params,
        )
        factors["regime"] = regime
        factors["prev_regime"] = prev_regime
        factors["missing_flags"] = json.dumps(sorted(flags), ensure_ascii=False)

        if trade_date >= start_date:
            rows.append({col: factors.get(col) for col in OUTPUT_COLUMNS})
        prev_factors = factors
        prev_score = score_result.score
        prev_regime = regime

    frame = pd.DataFrame(rows, columns=list(OUTPUT_COLUMNS), dtype=object)
    return frame


def compute_close_factors(
    db_path: str | Path,
    trade_date: str,
    **kwargs: Any,
) -> dict[str, Any] | None:
    """单日收盘因子（与 ``compute_sentiment_frame`` 同一套公式）。"""
    frame = compute_sentiment_frame(db_path, trade_date, trade_date, **kwargs)
    if frame.empty:
        return None
    return dict(frame.iloc[-1])


# ─── 盘中估算（intraday_ 前缀，绝不回写收盘口径） ───


def _normalize_as_of(trade_date: str, as_of_time: str) -> str:
    text = str(as_of_time).strip()
    parts = text.split(":")
    if len(parts) == 2:
        return f"{trade_date} {parts[0]}:{parts[1]}:59"
    if len(parts) == 3:
        return f"{trade_date} {text}"
    raise ValueError(f"invalid as_of_time: {as_of_time!r} (expect HH:MM[:SS])")


def compute_intraday_estimates(
    db_path: str | Path,
    trade_date: str,
    as_of_time: str,
    *,
    market_scope: str = "all",
    index_ts_code: str = "000001.SH",
    include_st: bool = False,
    exclude_new_days: int = 5,
    big_loss_threshold: float = -0.05,
    lookback_days: int = 75,
    regime_rules: Sequence[RegimeRule] | None = None,
    regime_params: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    """基于 ``kline_minute``（rt_min 落地）的盘中**估算**。

    输出只包含 ``intraday_`` 前缀键 + ``trade_date``/``market_scope``/
    ``intraday_as_of_time`` 元数据；不落任何收盘列。盘中的"涨停数"随时段
    变化，属于估算量，与收盘口径物理隔离。

    估算口径：
    * 封板 = 截止 ``as_of_time`` 最后一根分钟线收盘价达到涨停容忍阈值；
    * 炸板 = 盘中最高价触及涨停价但当前未封住；
    * 连板高度 = 昨日涨停池标的今日再封 → 昨 ``limit_times`` + 1；
      非昨日池标的今日封板 → 记为首板（估算）。
    """
    _validate_scope(market_scope)
    cutoff = _normalize_as_of(trade_date, as_of_time)
    window_start = _shift_date(trade_date, -abs(lookback_days))
    with closing(_connect_ro(db_path)) as conn:
        dataset = _load_dataset(
            conn,
            window_start=window_start,
            window_end=trade_date,
            market_scope=market_scope,
            index_ts_code=index_ts_code,
            trade_date_for_minutes=trade_date,
            minute_cutoff=cutoff,
        )
    filters = _make_filters(dataset, include_st, exclude_new_days)
    flags: set[str] = {f"table_missing:{n}" for n in dataset.missing_tables}
    out: dict[str, Any] = {"trade_date": trade_date, "market_scope": market_scope}
    out[f"{INTRADAY_PREFIX}as_of_time"] = as_of_time

    minutes = dataset.minute_by_symbol
    prev_day = _prev_calendar_date(dataset, trade_date)
    prev_map = dataset.kline_by_date.get(prev_day, {}) if prev_day else {}
    if not minutes:
        flags.add("minute_data_missing")
        dataset_missing = True
    else:
        dataset_missing = False
    if prev_day is None:
        flags.add("intraday_prev_day_missing")

    sealed: set[str] = set()
    broken: set[str] = set()
    down_sealed: set[str] = set()
    returns: list[float] = []
    amounts: float | None = 0.0
    for symbol, bar in sorted(minutes.items()):
        # 广度样本与收盘口径一致：剔除 ST（除非 include_st）与新股。
        if filters.is_st(symbol, trade_date) or filters.is_new(symbol, trade_date):
            continue
        prev_row = prev_map.get(symbol)
        prev_close = prev_row["close"] if prev_row else None
        if prev_close is None or bar["last_close"] is None:
            continue
        ret = _pct_from(bar["last_close"], prev_close)
        if ret is None:
            continue
        returns.append(ret)
        if bar["amount_seen"]:
            amounts = (amounts or 0.0) + float(bar["amount"])
        # 封板判定用标的自身涨跌停幅度（ST 5% 容忍也要正确识别）。
        limit_pct = limit_pct_for(symbol, _st_by_name(dataset, symbol, trade_date))
        if ret >= limit_pct - _LIMIT_TOLERANCE:
            sealed.add(symbol)
        high_ret = _pct_from(bar.get("high"), prev_close)
        if (
            high_ret is not None
            and high_ret >= limit_pct - _LIMIT_TOLERANCE
            and symbol not in sealed
        ):
            broken.add(symbol)
        if ret <= -(limit_pct - _LIMIT_TOLERANCE):
            down_sealed.add(symbol)

    cohort_prev = (_cohort(dataset, filters, prev_day, "U") if prev_day else None) or {}
    boards: list[int] = []
    for symbol in sealed:
        prev_times = _limit_times_value(cohort_prev.get(symbol, {}))
        boards.append(prev_times + 1 if prev_times else 1)

    sample = len(returns)
    facts: dict[str, Any] = {
        "traded_count": sample,
        "up_count": sum(1 for v in returns if v > 0) if sample else None,
        "down_count": sum(1 for v in returns if v < 0) if sample else None,
        "up_ratio": _ratio(sum(1 for v in returns if v > 0), sample or None),
        "adv_dec_ratio": _ratio(
            sum(1 for v in returns if v > 0),
            sum(1 for v in returns if v < 0) or None,
        ),
        "mean_return": _mean(returns),
        "median_return": _median(returns),
        "strong_count": sum(1 for v in returns if v >= 0.05) if sample else None,
        "weak_count": sum(1 for v in returns if v <= -0.05) if sample else None,
        "market_amount": _round6(amounts) if amounts else None,
        "limit_up_count": len(sealed) if not dataset_missing else None,
        "limit_down_count": len(down_sealed) if not dataset_missing else None,
        "limit_up_down_ratio": _ratio(len(sealed), len(down_sealed) or None),
        "broken_board_count": len(broken) if not dataset_missing else None,
        "broken_board_rate": _ratio(
            len(broken), float(len(broken) + len(sealed)) or None
        ),
        "max_consecutive_boards": max(boards) if boards else None,
        "board_count_2": sum(1 for b in boards if b == 2) if boards else None,
        "board_count_3": sum(1 for b in boards if b == 3) if boards else None,
        "board_count_4_plus": sum(1 for b in boards if b >= 4) if boards else None,
        "first_board_count": sum(1 for b in boards if b == 1) if boards else None,
        "continuation_count": sum(1 for b in boards if b >= 2) if boards else None,
        "ladder_integrity": int(
            boards
            and any(b == 2 for b in boards)
            and any(b == 3 for b in boards)
            and any(b >= 4 for b in boards)
        )
        if boards
        else None,
    }
    if boards:
        consec = [b for b in boards if b >= 2]
        if consec:
            counts = {k: consec.count(k) for k in set(consec)}
            facts["ladder_concentration"] = _round6(
                sum((c / len(consec)) ** 2 for c in counts.values())
            )
    first_cohort = {s for s, row in cohort_prev.items() if _limit_times_value(row) == 1}
    if first_cohort and not dataset_missing:
        promoted = sum(1 for s in first_cohort if s in sealed)
        facts["promotion_rate_1_to_2"] = _ratio(promoted, float(len(first_cohort)))

    premium_closes: list[float] = []
    gains = losses = big = 0
    for symbol, row in sorted(cohort_prev.items()):
        base = row.get("close")
        bar = minutes.get(symbol)
        if bar is None or bar["last_close"] is None:
            continue
        if base is None or _is_dirty(base) or float(base) <= 0:
            continue
        prem = _pct_from(bar["last_close"], base)
        if prem is None:
            continue
        premium_closes.append(prem)
        gains += int(prem > 0)
        losses += int(prem < 0)
        big += int(prem <= big_loss_threshold)
    if premium_closes:
        facts["yesterday_limit_up_premium_close_mean"] = _mean(premium_closes)
        denom = float(len(premium_closes))
        facts["money_making_effect"] = _ratio(gains, denom)
        facts["loss_effect"] = _ratio(losses, denom)
        facts["big_loss_rate"] = _ratio(big, denom)
    if dataset_missing:
        flags.add("intraday_estimates_unavailable")
    if not cohort_prev:
        flags.add("prev_limit_pool_missing_intraday")

    score_result = compute_sentiment_score(facts)
    facts["sentiment_score"] = score_result.score
    regime = classify_regime(
        facts,
        score=score_result.score,
        prev_factors=None,
        prev_score=None,
        regime_rules=regime_rules,
        regime_params=regime_params,
    )
    facts["regime"] = regime

    out[f"{INTRADAY_PREFIX}sentiment_score"] = score_result.score
    for bucket in ("breadth", "ladder", "effect"):
        out[f"{INTRADAY_PREFIX}sentiment_score_{bucket}"] = score_result.components.get(
            bucket
        )
    out[f"{INTRADAY_PREFIX}regime"] = regime
    for key, value in facts.items():
        if key in ("sentiment_score", "regime"):
            continue
        out[f"{INTRADAY_PREFIX}{key}"] = value
    out[f"{INTRADAY_PREFIX}missing_flags"] = json.dumps(
        sorted(flags), ensure_ascii=False
    )
    return {
        k: v
        for k, v in out.items()
        if k in INTRADAY_OUTPUT_COLUMNS
        or k in ("trade_date", "market_scope")
        or k.startswith(INTRADAY_PREFIX)
    }
