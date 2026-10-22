"""涨停微观结构与个股股性因子族（issue #24）。

本模块是 **离线因子库**：只读取 ``market_kline.db``（#22 v3 底座），
不注册任何 API、不触发同步、不改动现有规则引擎。#23 数据集可通过
``build_microstructure_features(db_path, symbols, as_of_date, lookback)``
按 ``(symbol, trade_date=as_of_date)`` 主键 join 本因子表。

数据口径
--------
* 金额单位统一为 **元**（#22 映射层已换算），缺失值保持 ``NULL``，
  本模块绝不把缺失填成 0；每个因子配 ``*_is_missing`` 指示列。
* 封板质量字段（``fd_amount`` / ``first_time`` 等）只来自 Tushare
  ``limit_list_d``（``limit_board_daily.source = 'tushare.limit_list_d'``）；
  ``board_type`` / ``theme`` 由 kpl 回填，仅用于交叉验证与标注
  ``is_proxy`` 的题材交互项，**不与 Tushare 口径混用**。
* 股性（Group C）全部为 PIT：窗口内只用 ``trade_date < as_of_date`` 的
  记录，且"次日溢价"类事件要求溢价观察日也 ``< as_of_date``
  （等价于对日频序列严格 ``shift(1)``），禁止 T 日标签 / T+1 数据泄漏。

降级与代理
----------
日线只能近似一字板 / T字板 / 尾盘板等结构（无分时数据，#9 未接入），
对应因子以 ``*_is_proxy = 1`` 标注；真实字段可用时优先真实字段
（``is_proxy = 0``）。完整公式、单位、缺失处理见 FACTOR_CATALOG.md。
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

# ─── 常量 ────────────────────────────────────────────────────────────

#: A 股连续竞价时长（分钟）：09:30-11:30 + 13:00-15:00。
SESSION_MINUTES = 240.0

#: 尾盘板阈值：首次封板在 14:30（第 210 分钟）之后。
TAIL_BOARD_MINUTE = 210.0

#: 加速板阈值：开盘 30 分钟内封板。
ACCELERATION_SEAL_MINUTE = 30.0

#: 换手板阈值：板上成交占全天成交额比例。
TURNOVER_BOARD_RATIO = 0.5

DEFAULT_LOOKBACK_DAYS = 250

# 市值分桶（元）：<20亿 / 20-50亿 / 50-100亿 / 100-300亿 / >300亿
MV_BUCKET_EDGES = (20e8, 50e8, 100e8, 300e8)

# 价格分桶（元）：<5 / 5-10 / 10-30 / 30-60 / >60
PRICE_BUCKET_EDGES = (5.0, 10.0, 30.0, 60.0)


@dataclass(frozen=True, slots=True)
class FactorSpec:
    """因子注册表条目：名称、分组、公式、单位、数据源、缺失处理、PIT、代理。"""

    name: str
    group: str
    formula: str
    unit: str
    source: str
    missing_rule: str
    pit_note: str
    is_proxy_capable: bool = False


def board_limit_pct(symbol: str) -> float:
    """按代码前缀推断涨跌停幅度（与 data_builder.board_limit_pct 口径一致，
    额外覆盖北交所 30%）。新股上市首日不适用，相关样本会标记缺失。"""
    code = str(symbol)
    if code.startswith(("30", "68")):
        return 0.20
    if code.startswith(("4", "8", "92")):
        return 0.30
    return 0.10


# ─── 时间解析 ────────────────────────────────────────────────────────


def session_minutes(time_text: Any) -> float | None:
    """把封板时间文本转成"连续竞价开始(09:30)起的交易分钟数"。

    支持 ``HH:MM:SS`` 与 ``HHMMSS``。集合竞价（<09:30）记 0；午休
    （11:30-13:00）按 120 钳制；15:00 记 240。解析失败返回 None。
    """
    if time_text is None:
        return None
    text = str(time_text).strip()
    if not text:
        return None
    parts = text.split(":")
    try:
        if len(parts) >= 2:
            hour, minute = int(parts[0]), int(parts[1])
        elif len(text) >= 4 and text.isdigit():
            hour, minute = int(text[:2]), int(text[2:4])
        else:
            return None
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    absolute = hour * 60 + minute
    if absolute <= 9 * 60 + 30:
        return 0.0
    if absolute <= 11 * 60 + 30:
        return float(absolute - (9 * 60 + 30))
    if absolute < 13 * 60:
        return 120.0
    return float(min(SESSION_MINUTES, 120 + absolute - 13 * 60))


# ─── 数据装载 ────────────────────────────────────────────────────────

_BOARD_COLUMNS = (
    "symbol, trade_date, limit_flag, first_time, last_time, open_times, "
    "limit_times, up_stat, fd_amount, amount, limit_amount, turnover_ratio, "
    "float_mv, total_mv, close, pct_chg, board_type, theme, source"
)
_BOARD_COL_LIST = [part.strip() for part in _BOARD_COLUMNS.replace("\n", " ").split(",")]


def _connect_ro(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _chunks(items: list[str], size: int = 400) -> Iterable[list[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _load_trading_calendar(conn: sqlite3.Connection, as_of_date: str) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT trade_date FROM kline_daily WHERE trade_date <= ? ORDER BY trade_date ASC",
        (as_of_date,),
    ).fetchall()
    return [str(r["trade_date"]) for r in rows]


def _load_board_frame(
    conn: sqlite3.Connection, symbols: list[str], start_date: str, end_date: str
) -> pd.DataFrame:
    frames = []
    for chunk in _chunks(symbols):
        marks = ",".join("?" for _ in chunk)
        frames.append(
            pd.read_sql_query(
                f"SELECT {_BOARD_COLUMNS} FROM limit_board_daily "
                f"WHERE symbol IN ({marks}) AND trade_date >= ? AND trade_date <= ?",
                conn,
                params=[*chunk, start_date, end_date],
            )
        )
    if not frames:
        return pd.DataFrame(columns=_BOARD_COL_LIST)
    return pd.concat(frames, ignore_index=True)


def _load_kline_frame(
    conn: sqlite3.Connection, symbols: list[str], start_date: str, end_date: str
) -> pd.DataFrame:
    frames = []
    for chunk in _chunks(symbols):
        marks = ",".join("?" for _ in chunk)
        frames.append(
            pd.read_sql_query(
                "SELECT symbol, trade_date, open, high, low, close, volume, amount "
                f"FROM kline_daily WHERE symbol IN ({marks}) AND trade_date >= ? AND trade_date <= ?",
                conn,
                params=[*chunk, start_date, end_date],
            )
        )
    if not frames:
        return pd.DataFrame(
            columns=["symbol", "trade_date", "open", "high", "low", "close", "volume", "amount"]
        )
    frame = pd.concat(frames, ignore_index=True)
    for col in ("open", "high", "low", "close", "volume", "amount"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame["trade_date"] = frame["trade_date"].astype(str)
    return frame


# ─── 数值工具 ────────────────────────────────────────────────────────


def _safe_div(numer: float | None, denom: float | None) -> float | None:
    if numer is None or denom is None:
        return None
    try:
        numer_f, denom_f = float(numer), float(denom)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numer_f) or not math.isfinite(denom_f) or denom_f <= 0:
        return None
    return numer_f / denom_f


def _fnum(value: Any) -> float | None:
    """把 sqlite/pandas 值安全转 float；NaN/None/Inf -> None。"""
    if value is None:
        return None
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def _bnum(value: Any) -> int | None:
    out = _fnum(value)
    return None if out is None else int(round(out))


def _text_or_none(value: Any) -> str | None:
    """sqlite/pandas 文本列的缺失值可能是 None 或 NaN，统一成 None。"""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return None
    return str(value)


def _cell(value: float | int | str | None) -> Any:
    return np.nan if value is None else value


# ─── 因子注册表 ──────────────────────────────────────────────────────


def _spec(
    name: str,
    group: str,
    formula: str,
    unit: str,
    source: str,
    missing_rule: str,
    pit_note: str,
    is_proxy_capable: bool = False,
) -> FactorSpec:
    return FactorSpec(name, group, formula, unit, source, missing_rule, pit_note, is_proxy_capable)


FACTOR_SPECS: tuple[FactorSpec, ...] = (
    # ---- A. 封板强度 ----
    _spec("fd_amount", "seal_strength", "limit_board_daily.fd_amount 原值", "元",
          "tushare.limit_list_d", "NULL→缺失（is_missing=1，不填 0）", "T 日盘后数据，允许"),
    _spec("fd_ratio_float_mv", "seal_strength", "fd_amount / float_mv", "无量纲",
          "tushare.limit_list_d", "任一为空/分母<=0→缺失", "T 日盘后数据，允许"),
    _spec("fd_ratio_amount", "seal_strength", "fd_amount / amount（封成比）", "无量纲",
          "tushare.limit_list_d", "任一为空/分母<=0→缺失", "T 日盘后数据，允许"),
    _spec("fd_ratio_volume", "seal_strength", "(fd_amount/close)/(成交量)；成交量取 kline_daily.volume，缺失用 amount/close",
          "无量纲", "tushare.limit_list_d + kline_daily", "任一为空/分母<=0→缺失", "T 日盘后数据，允许"),
    _spec("first_seal_minute", "seal_strength", "session_minutes(first_time)，09:30=0、15:00=240", "分钟",
          "tushare.limit_list_d", "first_time 缺失/无法解析→缺失", "T 日盘后数据，允许"),
    _spec("last_seal_minute", "seal_strength", "session_minutes(last_time)", "分钟",
          "tushare.limit_list_d", "last_time 缺失/无法解析→缺失", "T 日盘后数据，允许"),
    _spec("seal_duration_min", "seal_strength",
          "U: 240-first_seal_minute；Z: last_seal_minute-first_seal_minute（未扣除盘中开板区间，上界近似）", "分钟",
          "tushare.limit_list_d", "依赖的时间任一缺失→缺失", "T 日盘后数据，允许", True),
    _spec("open_times", "seal_strength", "limit_board_daily.open_times 原值（开板次数）", "次",
          "tushare.limit_list_d", "NULL→缺失", "T 日盘后数据，允许"),
    _spec("broken_duration_min", "seal_strength",
          "仅 Z（收盘炸板）：240-last_seal_minute；U 行需分钟线才能精确，保持缺失（#9）", "分钟",
          "tushare.limit_list_d", "非 Z 行或时间缺失→缺失", "T 日盘后数据，允许", True),
    _spec("reseal_speed_min", "seal_strength",
          "open_times>=1 且有首末封板时间：(last_seal_minute-first_seal_minute)/open_times，均匀节奏假设", "分钟",
          "tushare.limit_list_d", "条件不满足→缺失", "T 日盘后数据，允许", True),
    _spec("on_board_amount", "seal_strength", "limit_board_daily.limit_amount（板上成交额）", "元",
          "tushare.limit_list_d", "NULL→缺失", "T 日盘后数据，允许"),
    _spec("on_board_amount_ratio", "seal_strength", "limit_amount / amount（板上成交占比）", "无量纲",
          "tushare.limit_list_d", "任一为空/分母<=0→缺失", "T 日盘后数据，允许"),
    _spec("seal_quality_proxy", "seal_strength",
          "日线近似：2=一字(开=高=低=收)、1=T字/加速(开=高=收>低)、0=普通收盘封死(高=收)、-1=盘中触板未封死(高>收)", "序数",
          "kline_daily（代理）", "无 T 日 K 线→缺失", "T 日盘后数据，允许", True),
    # ---- B. 板型 ----
    _spec("is_one_word", "board_type", "一字板：真实=limit 行存在且首封<=09:30 且 open_times=0 且 O=H=L=C；Z 行按定义=0；无 limit 行时降级日线代理 O=H=L=C 且涨幅达板", "0/1",
          "limit_list_d + kline_daily", "两者都缺→缺失", "T 日盘后数据，允许", True),
    _spec("is_t_word", "board_type", "T字板：O=H=C>L 且收盘封板（日线近似；炸板日按定义=0）", "0/1",
          "kline_daily（近似）", "无 K 线→缺失", "T 日盘后数据，允许", True),
    _spec("is_turnover_board", "board_type", "换手板：open_times>=1 且 on_board_amount_ratio>=0.5", "0/1",
          "tushare.limit_list_d", "无 limit 行或关键字段缺失→缺失", "T 日盘后数据，允许"),
    _spec("is_tail_board", "board_type", "尾盘板：first_seal_minute>=210（14:30 后首封）", "0/1",
          "tushare.limit_list_d", "first_time 缺失→缺失（日线无法近似时间）", "T 日盘后数据，允许"),
    _spec("is_acceleration_board", "board_type", "加速板：limit_times>=2 且 open_times=0 且 (一字板 或 first_seal_minute<=30)", "0/1",
          "limit_list_d + kline_daily", "关键字段缺失→缺失", "T 日盘后数据，允许", True),
    _spec("is_reversal_board", "board_type", "反转板：收盘封死(高=收) 且 低开(open<prev_close)", "0/1",
          "kline_daily", "缺前收/当日 K 线→缺失", "T 日盘后数据，允许", True),
    _spec("limit_times", "board_type", "limit_board_daily.limit_times（连板数）原值", "板",
          "tushare.limit_list_d", "NULL→缺失", "T 日盘后数据，允许"),
    _spec("board_position_code", "board_type", "板位编码：1=首板,2=1进2,3=2进3,4=3进4,5=高位(≥5 连)；由 T 日 limit_times 分档", "序数",
          "limit_list_d（T 行）", "无 limit 行或 limit_times 缺失→缺失", "只用 T 日行，盘后合法"),
    _spec("board_position_label", "board_type", "board_position_code 的中文标签（首板/1进2/2进3/3进4/高位）", "文本",
          "limit_list_d（T 行）", "同上", "同上"),
    _spec("prev_limit_times", "board_type", "T-1 交易日的 limit_times（供下游晋级交叉校验；T-1 无板行时为 NULL）", "板",
          "limit_list_d", "日历缺失/T-1 无行→缺失", "T-1 数据"),
    _spec("up_stat_days", "board_type", "up_stat 'X/Y' 解析：X=自最近首个涨停日起天数", "天",
          "tushare.limit_list_d", "解析失败→缺失", "T 日盘后数据，允许"),
    _spec("up_stat_limits", "board_type", "up_stat 'X/Y' 解析：Y=期间涨停次数", "次",
          "tushare.limit_list_d", "解析失败→缺失", "T 日盘后数据，允许"),
    _spec("up_stat_limit_density", "board_type", "up_stat_limits / up_stat_days", "无量纲",
          "tushare.limit_list_d", "解析失败/分母<=0→缺失", "T 日盘后数据，允许"),
    _spec("up_stat_raw", "board_type", "up_stat 原文透传", "文本",
          "tushare.limit_list_d", "NULL→缺失", "T 日盘后数据，允许"),
    _spec("board_type_text", "board_type", "kpl 回填的 board_type 原文（仅交叉验证，不参与口径计算，禁止与 Tushare 混用）", "文本",
          "kpl（回填列）", "NULL→缺失", "T 日数据，仅标注"),
    # ---- C. 股性（全部 shift(1)：只用 trade_date < T 且观察日 < T 的记录）----
    _spec("limit_up_count_250d", "stock_character", "窗口 [T-lookback, T-1] 内 limit_flag='U' 天数", "次",
          "limit_list_d", "新股/停牌致观察不足→缺失", "严格 < T"),
    _spec("first_board_count_250d", "stock_character", "窗口内 limit_times=1 的 U 天数", "次",
          "limit_list_d", "同上", "严格 < T"),
    _spec("first_board_next_day_premium_avg", "stock_character",
          "历史首板日 d（d<T 且次日 n(d)<T，n(d) 为全市场次一交易日且该股本日有 K 线）：mean(close[n]/close[d]-1)", "比率",
          "limit_list_d + kline_daily", "无事件→缺失", "d<T 且 n(d)<T（shift(1)）"),
    _spec("first_board_next_day_win_rate", "stock_character", "上述事件中溢价>0 的占比", "比率",
          "limit_list_d + kline_daily", "无事件→缺失", "同上"),
    _spec("continuation_success_rate", "stock_character",
          "历史 U 日 d（n(d)<T 且该股本日可观察）中，n(d) 仍为 U 的占比（连板晋级率）", "比率",
          "limit_list_d + kline_daily", "无样本→缺失", "同上"),
    _spec("broken_board_rate", "stock_character", "窗口内 limit 行中 limit_flag='Z' 占比（U/Z 口径）", "比率",
          "limit_list_d", "无 U/Z 行→缺失", "严格 < T"),
    _spec("max_consecutive_boards_250d", "stock_character", "窗口内按市场日历连续的 U 日最大连板长度", "板",
          "limit_list_d", "窗口无 U 日→缺失", "严格 < T"),
    _spec("limit_up_amount_avg", "stock_character", "窗口内 U 日 amount 均值", "元",
          "limit_list_d", "无 U 日或 amount 全空→缺失", "严格 < T"),
    _spec("limit_up_freq_250d", "stock_character", "窗口内 U 天数 / 该股有 K 线的观察天数", "比率",
          "limit_list_d + kline_daily", "观察天数不足 min_obs→缺失", "严格 < T"),
    _spec("float_mv_bucket", "stock_character", "T-1 及以前最近一条 limit 行的 float_mv 分桶：0..4（<20/20-50/50-100/100-300/>300 亿）", "序数",
          "limit_list_d", "无市值记录→缺失", "严格 < T"),
    _spec("small_cap_limit_up_rate", "stock_character",
          "同市值分桶内（桶成员=窗口内出现过于 limit 表的股票）个股涨停频率的均值；全市场日频市值表接入前为板内代理", "比率",
          "limit_list_d（板内宇宙）", "桶内无股票→缺失", "严格 < T", True),
    _spec("price_bucket", "stock_character", "T-1 收盘价分桶：0..4（<5/5-10/10-30/30-60/>60 元）", "序数",
          "kline_daily", "无 T-1 K 线→缺失", "T-1 数据"),
    _spec("price_bucket_limit_up_rate", "stock_character",
          "T-1 同价格分桶股票中，窗口内至少 1 次涨停的股票占比（全 K 线宇宙）", "比率",
          "kline_daily + limit_list_d", "桶内无股票→缺失", "窗口严格 < T"),
    _spec("hist_days_observed", "stock_character", "窗口内该股有 K 线的交易日数", "天",
          "kline_daily", "恒有值（新股=0）", "严格 < T"),
    # ---- D. 交互项 ----
    _spec("seal_strength_x_float_mv", "interaction", "fd_ratio_float_mv * log10(float_mv/1e8+1)", "无量纲",
          "tushare.limit_list_d", "任一成分缺失→缺失", "T 日盘后数据，允许"),
    _spec("first_seal_time_x_market_regime", "interaction",
          "(1 - first_seal_minute/240) * regime，regime=clip(T-1 全市场涨停数 / 之前 20 日均值, 0, 3)", "无量纲",
          "limit_list_d（截面聚合）", "任一成分缺失→缺失", "regime 用 T-1 及以前", True),
    _spec("open_times_x_continuation_rate", "interaction", "open_times * continuation_success_rate", "无量纲",
          "limit_list_d（两表）", "任一成分缺失→缺失", "股性成分严格 < T"),
    _spec("fd_ratio_x_theme_heat", "interaction",
          "fd_ratio_amount * min(T 日同 theme 涨停数/10, 1)；theme 依赖 #8 回填，未回填时缺失", "无量纲",
          "limit_list_d + kpl 回填 theme（仅题材计数交叉口径）", "theme/fd_ratio 缺失→缺失", "T 日截面，盘后合法", True),
)

INDICATED_FACTORS = tuple(
    s.name for s in FACTOR_SPECS if s.group in {"seal_strength", "board_type", "stock_character", "interaction"}
)

#: 需要同时输出 ``*_is_proxy`` 的因子
PROXY_FACTORS = (
    "seal_duration_min",
    "broken_duration_min",
    "reseal_speed_min",
    "seal_quality_proxy",
    "is_one_word",
    "is_t_word",
    "is_acceleration_board",
    "is_reversal_board",
    "small_cap_limit_up_rate",
    "first_seal_time_x_market_regime",
    "fd_ratio_x_theme_heat",
)

TEXT_FACTORS = ("board_position_label", "up_stat_raw", "board_type_text")
RAW_FACTORS = ("symbol", "as_of_date", "limit_flag", "has_limit_row", "has_kline_row")

MICROSTRUCTURE_FACTOR_COLUMNS: tuple[str, ...] = RAW_FACTORS + tuple(s.name for s in FACTOR_SPECS)


def microstructure_factor_specs() -> list[dict[str, Any]]:
    """因子注册表（供 #23/#27 与文档校验）。"""
    return [
        {
            "name": s.name,
            "group": s.group,
            "formula": s.formula,
            "unit": s.unit,
            "source": s.source,
            "missing_rule": s.missing_rule,
            "pit_note": s.pit_note,
            "is_proxy_capable": s.is_proxy_capable,
        }
        for s in FACTOR_SPECS
    ]


# ─── 股性辅助结构 ────────────────────────────────────────────────────


def _day_lookup(frame: pd.DataFrame) -> dict[str, dict[str, Any]]:
    if frame.empty:
        return {}
    return {
        str(row["trade_date"]): row
        for row in frame.to_dict("records")
    }


def _parse_up_stat(text: Any) -> tuple[int | None, int | None]:
    """Tushare ``up_stat`` 形如 '5/3'（自最近首个涨停日起 5 天 3 涨停）。"""
    if text is None:
        return None, None
    parts = str(text).split("/")
    if len(parts) != 2:
        return None, None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None, None


def _bucket(value: float | None, edges: tuple[float, ...]) -> int | None:
    if value is None:
        return None
    for idx, edge in enumerate(edges):
        if value < edge:
            return idx
    return len(edges)


# ─── 市场截面（regime / 题材热度）────────────────────────────────────


def _market_regime_map(conn: sqlite3.Connection, as_of_date: str) -> dict[str, float]:
    """trade_date -> 全市场涨停数 / "之前 20 个交易日"均值（clip 0..3）。

    对任意 d<=as_of_date，均值窗口 [d-20, d-1] 严格早于 d，PIT 安全。
    """
    rows = conn.execute(
        "SELECT trade_date, COUNT(*) AS cnt FROM limit_board_daily "
        "WHERE limit_flag = 'U' AND trade_date <= ? GROUP BY trade_date",
        (as_of_date,),
    ).fetchall()
    counts = {str(r["trade_date"]): float(r["cnt"]) for r in rows}
    if not counts:
        return {}
    calendar = _load_trading_calendar(conn, as_of_date)
    regime: dict[str, float] = {}
    for idx, day in enumerate(calendar):
        today = counts.get(day, 0.0)
        base_window = calendar[max(0, idx - 20) : idx]
        base = [counts.get(d, 0.0) for d in base_window]
        if not base:
            continue
        avg = float(np.mean(base))
        if avg <= 0:
            continue
        regime[day] = float(min(3.0, max(0.0, today / avg)))
    return regime


def _theme_heat_map(conn: sqlite3.Connection, trade_date: str) -> dict[str, int]:
    rows = conn.execute(
        "SELECT theme, COUNT(*) AS cnt FROM limit_board_daily "
        "WHERE trade_date = ? AND limit_flag = 'U' AND theme IS NOT NULL AND TRIM(theme) <> '' "
        "GROUP BY theme",
        (trade_date,),
    ).fetchall()
    return {str(r["theme"]): int(r["cnt"]) for r in rows}


def _bucket_cross_section(
    conn: sqlite3.Connection,
    start_date: str,
    prev_date: str,
) -> tuple[dict[int, float], dict[int, float]]:
    """返回 (市值桶->板内涨停频率均值, 价格桶->桶内涨停股票占比)。

    窗口 = [start_date, prev_date]（即严格早于 T 日）。

    * 市值桶：宇宙 = 窗口内在 limit_board_daily 出现过的股票（全市场日频
      市值表未接入，标记 is_proxy）。个股频率 = 该股窗口内 U 天数 / 板行数。
    * 价格桶：宇宙 = T-1 日有 K 线的全市场股票；桶率 = 窗口内 >=1 次涨停
      的股票占比。
    """
    if not prev_date:
        return {}, {}
    limit_hist = pd.read_sql_query(
        "SELECT symbol, trade_date, limit_flag, float_mv FROM limit_board_daily "
        "WHERE trade_date >= ? AND trade_date <= ?",
        conn,
        params=(start_date, prev_date),
    )
    mv_rate: dict[int, float] = {}
    if not limit_hist.empty:
        grouped = limit_hist.groupby("symbol")
        rows_count = grouped.size()
        up_count = limit_hist.assign(is_up=(limit_hist["limit_flag"] == "U").astype(int)).groupby("symbol")["is_up"].sum()
        last_mv = (
            limit_hist.sort_values("trade_date").groupby("symbol").tail(1).set_index("symbol")["float_mv"]
        )
        per_symbol = pd.DataFrame({"rows": rows_count, "ups": up_count, "mv": last_mv})
        per_symbol["freq"] = per_symbol["ups"] / per_symbol["rows"].replace(0, np.nan)
        per_symbol = per_symbol.replace([np.inf, -np.inf], np.nan)
        per_symbol["bucket"] = [
            _bucket(_fnum(v), MV_BUCKET_EDGES) for v in per_symbol["mv"]
        ]
        valid = per_symbol.dropna(subset=["freq", "bucket"])
        for bucket, part in valid.groupby("bucket"):
            mv_rate[int(bucket)] = float(part["freq"].mean())

    prev_kline = pd.read_sql_query(
        "SELECT symbol, close FROM kline_daily WHERE trade_date = ?",
        conn,
        params=(prev_date or "",),
    )
    price_rate: dict[int, float] = {}
    if not prev_kline.empty:
        up_symbols = pd.read_sql_query(
            "SELECT DISTINCT symbol FROM limit_board_daily WHERE limit_flag = 'U' AND trade_date >= ? AND trade_date <= ?",
            conn,
            params=(start_date, prev_date),
        )
        up_set = set(up_symbols["symbol"].astype(str)) if not up_symbols.empty else set()
        prev_kline["close"] = pd.to_numeric(prev_kline["close"], errors="coerce")
        prev_kline = prev_kline.dropna(subset=["close"])
        prev_kline["bucket"] = [_bucket(_fnum(c), PRICE_BUCKET_EDGES) for c in prev_kline["close"]]
        prev_kline["had_limit"] = prev_kline["symbol"].astype(str).isin(up_set).astype(int)
        valid = prev_kline.dropna(subset=["bucket"])
        for bucket, part in valid.groupby("bucket"):
            price_rate[int(bucket)] = float(part["had_limit"].mean())
    return mv_rate, price_rate


# ─── 主构建函数 ──────────────────────────────────────────────────────

MIN_HISTORY_OBS = 5  # 股性因子最少观察天数，低于此视为新股/数据不足 -> 缺失


def build_microstructure_features(
    db_path: str | Path,
    symbols: Iterable[str],
    as_of_date: str,
    lookback: int = DEFAULT_LOOKBACK_DAYS,
) -> pd.DataFrame:
    """为 ``symbols`` 在 ``as_of_date``（T 日）构建微观结构 + 股性因子。

    返回 DataFrame 主键 ``symbol``（附 ``as_of_date`` 列），可直接与 #23
    dataset 的 ``(symbol, trade_date)`` join。所有缺失保持 NaN 并配
    ``*_is_missing`` 指示列；代理口径配 ``*_is_proxy`` 指示列。
    """
    symbol_list = sorted({str(s).strip() for s in symbols if str(s).strip()})
    as_of_date = str(as_of_date)
    lookback = max(5, int(lookback))
    if not symbol_list:
        return pd.DataFrame(columns=_all_output_columns())

    conn = _connect_ro(db_path)
    try:
        calendar = _load_trading_calendar(conn, as_of_date)
        if not calendar:
            return pd.DataFrame(columns=_all_output_columns())
        if calendar[-1] != as_of_date:
            # as_of_date 非交易日：T 日无行情，全部封板强度缺失，股性仍按 <=T 历史计算。
            prev_date = calendar[-1] if calendar else ""
        else:
            prev_date = calendar[-2] if len(calendar) >= 2 else ""
        window_days = calendar[: max(0, len(calendar) - 1)]  # 严格 < T
        history_days = window_days[-lookback:] if window_days else []
        start_date = history_days[0] if history_days else as_of_date

        board = _load_board_frame(conn, symbol_list, start_date if history_days else as_of_date, as_of_date)
        board["trade_date"] = board["trade_date"].astype(str)
        kline = _load_kline_frame(conn, symbol_list, start_date if history_days else as_of_date, as_of_date)

        calendar_index = {day: idx for idx, day in enumerate(calendar)}
        regime_map = _market_regime_map(conn, as_of_date)
        theme_counts = _theme_heat_map(conn, as_of_date)
        mv_bucket_rate, price_bucket_rate = _bucket_cross_section(conn, start_date, prev_date)

        records = []
        for symbol in symbol_list:
            records.append(
                _build_symbol_row(
                    symbol=symbol,
                    as_of_date=as_of_date,
                    prev_date=prev_date,
                    board=board,
                    kline=kline,
                    calendar=calendar,
                    calendar_index=calendar_index,
                    history_days=history_days,
                    regime_map=regime_map,
                    theme_counts=theme_counts,
                    mv_bucket_rate=mv_bucket_rate,
                    price_bucket_rate=price_bucket_rate,
                )
            )
        frame = pd.DataFrame(records)
    finally:
        conn.close()
    for column in _all_output_columns():
        if column not in frame.columns:
            frame[column] = np.nan
    return frame[_all_output_columns()]


def _build_symbol_row(
    *,
    symbol: str,
    as_of_date: str,
    prev_date: str,
    board: pd.DataFrame,
    kline: pd.DataFrame,
    calendar: list[str],
    calendar_index: dict[str, int],
    history_days: list[str],
    regime_map: dict[str, float],
    theme_counts: dict[str, int],
    mv_bucket_rate: dict[int, float],
    price_bucket_rate: dict[int, float],
) -> dict[str, Any]:
    row: dict[str, Any] = {"symbol": symbol, "as_of_date": as_of_date}
    proxy_row: dict[str, int] = {}

    sym_board = board[board["symbol"] == symbol]
    sym_board_by_date = _day_lookup(sym_board)
    t_board = sym_board_by_date.get(as_of_date)
    t_kline = kline[(kline["symbol"] == symbol) & (kline["trade_date"] == as_of_date)]
    k_row = t_kline.iloc[0].to_dict() if not t_kline.empty else None

    row["limit_flag"] = (t_board or {}).get("limit_flag") if t_board is not None else None
    if row["limit_flag"] is None:
        row["limit_flag"] = np.nan
    row["has_limit_row"] = 1 if t_board is not None else 0
    row["has_kline_row"] = 1 if k_row is not None else 0

    # ---------- A. 封板强度 ----------
    fd_amount = _fnum((t_board or {}).get("fd_amount")) if t_board is not None else None
    board_amount = _fnum((t_board or {}).get("amount")) if t_board is not None else None
    limit_amount = _fnum((t_board or {}).get("limit_amount")) if t_board is not None else None
    float_mv = _fnum((t_board or {}).get("float_mv")) if t_board is not None else None
    t_close = _fnum((t_board or {}).get("close")) if t_board is not None else None
    if t_close is None and k_row is not None:
        t_close = _fnum(k_row.get("close"))
    first_min = session_minutes((t_board or {}).get("first_time")) if t_board is not None else None
    last_min = session_minutes((t_board or {}).get("last_time")) if t_board is not None else None
    open_times = _bnum((t_board or {}).get("open_times")) if t_board is not None else None
    limit_times = _bnum((t_board or {}).get("limit_times")) if t_board is not None else None
    flag = ((t_board or {}).get("limit_flag") if t_board is not None else None)

    day_volume: float | None = None
    if k_row is not None:
        day_volume = _fnum(k_row.get("volume"))
        if (day_volume is None or day_volume <= 0) and board_amount is not None and t_close:
            day_volume = board_amount / t_close
    if day_volume is None and board_amount is not None and t_close:
        day_volume = board_amount / t_close

    row["fd_amount"] = _cell(fd_amount)
    row["fd_ratio_float_mv"] = _cell(_safe_div(fd_amount, float_mv))
    row["fd_ratio_amount"] = _cell(_safe_div(fd_amount, board_amount))
    seal_volume = _safe_div(fd_amount, t_close)
    row["fd_ratio_volume"] = _cell(_safe_div(seal_volume, day_volume))
    row["first_seal_minute"] = _cell(first_min)
    row["last_seal_minute"] = _cell(last_min)
    row["open_times"] = _cell(open_times)

    seal_duration: float | None = None
    if flag == "U" and first_min is not None:
        seal_duration = SESSION_MINUTES - first_min
        proxy_row["seal_duration_min"] = 1  # 未扣除开板区间，上界近似
    elif flag == "Z" and first_min is not None and last_min is not None:
        seal_duration = max(0.0, last_min - first_min)
        proxy_row["seal_duration_min"] = 1
    row["seal_duration_min"] = _cell(seal_duration)

    broken_duration: float | None = None
    if flag == "Z" and last_min is not None:
        broken_duration = max(0.0, SESSION_MINUTES - last_min)
        proxy_row["broken_duration_min"] = 1
    row["broken_duration_min"] = _cell(broken_duration)

    reseal: float | None = None
    if open_times is not None and open_times >= 1 and first_min is not None and last_min is not None and last_min >= first_min:
        reseal = (last_min - first_min) / open_times
        proxy_row["reseal_speed_min"] = 1
    row["reseal_speed_min"] = _cell(reseal)

    row["on_board_amount"] = _cell(limit_amount)
    row["on_board_amount_ratio"] = _cell(_safe_div(limit_amount, board_amount))

    # 日线代理（seal_quality_proxy / 一字、T字 判定用）
    prev_close: float | None = None
    if k_row is not None:
        sym_kline = kline[kline["symbol"] == symbol].sort_values("trade_date")
        before = sym_kline[sym_kline["trade_date"] < as_of_date]
        if not before.empty:
            prev_close = _fnum(before.iloc[-1]["close"])
    seal_proxy: int | None = None
    is_proxy_quality = 0
    if k_row is not None and prev_close is not None and prev_close > 0:
        o, h, lo, c = (_fnum(k_row[col]) for col in ("open", "high", "low", "close"))
        if None not in (o, h, lo, c):
            limit_pct = board_limit_pct(symbol)
            sealed = c is not None and h is not None and abs(c - h) < 1e-9 and (c / prev_close - 1.0) >= limit_pct - 0.0025
            if sealed and o == h == lo == c:
                seal_proxy = 2
            elif sealed and o == h == c and lo is not None and lo < c:
                seal_proxy = 1
            elif sealed:
                seal_proxy = 0
            elif h is not None and c is not None and h > c and (h / prev_close - 1.0) >= limit_pct - 0.0025:
                seal_proxy = -1
            is_proxy_quality = 1 if t_board is None else 0
    row["seal_quality_proxy"] = _cell(seal_proxy)
    if seal_proxy is not None:
        proxy_row["seal_quality_proxy"] = is_proxy_quality

    # ---------- B. 板型 ----------
    o = _fnum(k_row.get("open")) if k_row is not None else None
    h = _fnum(k_row.get("high")) if k_row is not None else None
    lo = _fnum(k_row.get("low")) if k_row is not None else None
    c = _fnum(k_row.get("close")) if k_row is not None else (t_close if t_board is not None else None)

    one_word: int | None = None
    one_word_proxy = 0
    if t_board is not None and flag == "U":
        if first_min is not None and open_times is not None and o is not None and h is not None and lo is not None and c is not None:
            one_word = int(first_min <= 0.0 and open_times == 0 and o == h == lo == c)
    elif t_board is not None and flag == "Z":
        one_word = 0  # 收盘炸板，按定义不是板型（真实口径，非代理）
    elif k_row is not None and prev_close is not None and None not in (o, h, lo, c):
        limit_pct = board_limit_pct(symbol)
        one_word = int(o == h == lo == c and (c / prev_close - 1.0) >= limit_pct - 0.0025)
        one_word_proxy = 1
    row["is_one_word"] = _cell(one_word)
    if one_word is not None:
        proxy_row["is_one_word"] = one_word_proxy

    t_word: int | None = None
    if k_row is not None and None not in (o, h, lo, c) and flag in ("U", "Z", None) and (t_board is not None or seal_proxy is not None):
        t_word = int(o == h == c and lo is not None and lo < c)
    row["is_t_word"] = _cell(t_word)
    if t_word is not None:
        proxy_row["is_t_word"] = 1  # 无分时数据，板型只能日线近似

    turnover_board: int | None = None
    if open_times is not None:
        ratio = _safe_div(limit_amount, board_amount)
        if ratio is not None:
            turnover_board = int(open_times >= 1 and ratio >= TURNOVER_BOARD_RATIO)
    row["is_turnover_board"] = _cell(turnover_board)

    tail_board: int | None = None
    if first_min is not None and flag in ("U", "Z"):
        tail_board = int(first_min >= TAIL_BOARD_MINUTE)
    row["is_tail_board"] = _cell(tail_board)

    accel: int | None = None
    if limit_times is not None and limit_times >= 2 and open_times is not None and open_times == 0:
        if one_word == 1:
            accel = 1
        elif first_min is not None:
            accel = int(first_min <= ACCELERATION_SEAL_MINUTE)
    elif limit_times is not None and open_times is not None:
        accel = 0
    row["is_acceleration_board"] = _cell(accel)
    if accel is not None:
        proxy_row["is_acceleration_board"] = 1  # "加速板"依赖日线近似

    reversal: int | None = None
    if k_row is not None and None not in (o, h, lo, c) and prev_close is not None:
        sealed_close = c == h and (t_board is None or flag == "U")
        reversal = int(bool(sealed_close) and o < prev_close)
    row["is_reversal_board"] = _cell(reversal)
    if reversal is not None:
        proxy_row["is_reversal_board"] = 1

    row["limit_times"] = _cell(limit_times)

    prev_row = sym_board_by_date.get(prev_date) if prev_date else None
    prev_limit_times = _bnum((prev_row or {}).get("limit_times")) if prev_row is not None else None
    # 板位按 T 日 limit_times（连板数）直接分档；prev_limit_times 单独成列，
    # 供下游做"晋级"交叉校验（口径不混用，见 FACTOR_CATALOG）。
    pos_code: int | None = None
    pos_label: str | None = None
    if limit_times is not None and limit_times >= 1:
        pos_code = 5 if limit_times >= 5 else int(limit_times)
        pos_label = {1: "首板", 2: "1进2", 3: "2进3", 4: "3进4", 5: "高位"}[pos_code]
    row["board_position_code"] = _cell(pos_code)
    row["board_position_label"] = pos_label if pos_label is not None else np.nan
    row["prev_limit_times"] = _cell(prev_limit_times)

    stat_days, stat_limits = _parse_up_stat(_text_or_none((t_board or {}).get("up_stat")) if t_board is not None else None)
    row["up_stat_days"] = _cell(stat_days)
    row["up_stat_limits"] = _cell(stat_limits)
    row["up_stat_limit_density"] = _cell(_safe_div(_fnum(stat_limits), _fnum(stat_days)))
    raw_stat = _text_or_none((t_board or {}).get("up_stat")) if t_board is not None else None
    row["up_stat_raw"] = raw_stat if raw_stat is not None else np.nan
    bt = _text_or_none((t_board or {}).get("board_type")) if t_board is not None else None
    row["board_type_text"] = bt if bt is not None else np.nan

    # ---------- C. 股性（严格 < T）----------
    hist = sym_board[sym_board["trade_date"] < as_of_date]
    if history_days:
        hist = hist[hist["trade_date"] >= history_days[0]]
    sym_kline_full = kline[kline["symbol"] == symbol].sort_values("trade_date")
    hist_kline = sym_kline_full[
        (sym_kline_full["trade_date"] < as_of_date)
        & (sym_kline_full["trade_date"] >= (history_days[0] if history_days else as_of_date))
    ] if not sym_kline_full.empty else sym_kline_full
    kline_dates = set(hist_kline["trade_date"]) if not hist_kline.empty else set()
    observed_days = len(kline_dates)

    up_hist = hist[hist["limit_flag"] == "U"]
    up_days = sorted(set(up_hist["trade_date"]))
    uz_hist = hist[hist["limit_flag"].isin(["U", "Z"])]

    too_new = observed_days < MIN_HISTORY_OBS
    row["hist_days_observed"] = observed_days
    row["limit_up_count_250d"] = _cell(None if too_new else len(up_days))
    first_boards = up_hist[up_hist["limit_times"].map(lambda v: _bnum(v)) == 1]
    row["first_board_count_250d"] = _cell(None if too_new else len(set(first_boards["trade_date"])))

    # 事件类股性：d < T 且 n(d) < T（shift(1)）
    kline_by_date = {
        str(r["trade_date"]): _fnum(r["close"])
        for r in hist_kline.to_dict("records")
    } if not hist_kline.empty else {}
    premiums: list[float] = []
    first_board_dates = sorted(set(first_boards["trade_date"]))
    for d in first_board_dates:
        idx = calendar_index.get(d)
        if idx is None or idx + 1 >= len(calendar):
            continue
        n_day = calendar[idx + 1]
        if n_day >= as_of_date:
            continue  # 溢价观察日必须严格早于 T
        close_d = kline_by_date.get(d)
        close_n = None
        if n_day < as_of_date:
            close_n = kline_by_date.get(n_day)
        if close_d is None or close_n is None or close_d <= 0:
            continue  # 次日停牌 / 缺数据：剔除而非填 0
        premiums.append(close_n / close_d - 1.0)
    if premiums and not too_new:
        row["first_board_next_day_premium_avg"] = float(np.mean(premiums))
        row["first_board_next_day_win_rate"] = float(np.mean([1.0 if p > 0 else 0.0 for p in premiums]))
    else:
        row["first_board_next_day_premium_avg"] = np.nan
        row["first_board_next_day_win_rate"] = np.nan

    successes, attempts = 0, 0
    for d in up_days:
        idx = calendar_index.get(d)
        if idx is None or idx + 1 >= len(calendar):
            continue
        n_day = calendar[idx + 1]
        if n_day >= as_of_date:
            continue
        if n_day not in kline_dates:
            continue  # 次日停牌不可观察，剔除分母
        attempts += 1
        n_row = sym_board_by_date.get(n_day)
        if n_row is not None and n_row.get("limit_flag") == "U":
            successes += 1
    row["continuation_success_rate"] = float(successes / attempts) if attempts >= 1 and not too_new else np.nan

    if not uz_hist.empty and not too_new:
        uz_days = uz_hist.drop_duplicates("trade_date")
        z_day_count = int((uz_days["limit_flag"] == "Z").sum())
        row["broken_board_rate"] = z_day_count / float(len(uz_days))
    else:
        row["broken_board_rate"] = np.nan

    max_run = 0
    run = 0
    up_set = set(up_days)
    for day in history_days:
        if day in up_set:
            run += 1
            max_run = max(max_run, run)
        elif day not in kline_dates:
            continue  # 停牌不打断连板定义？保守：停牌不中断
        else:
            run = 0
    row["max_consecutive_boards_250d"] = _cell(None if (too_new or not up_days) else max_run)

    up_amounts = [_fnum(v) for v in up_hist["amount"].tolist()] if not up_hist.empty else []
    up_amounts = [v for v in up_amounts if v is not None and v > 0]
    row["limit_up_amount_avg"] = float(np.mean(up_amounts)) if up_amounts and not too_new else np.nan

    row["limit_up_freq_250d"] = float(len(up_days) / observed_days) if observed_days >= MIN_HISTORY_OBS and not too_new else np.nan

    # 市值桶：用 <T 最近一条 limit 行的 float_mv
    hist_mv = hist[hist["float_mv"].map(_fnum).notna()].sort_values("trade_date") if not hist.empty else hist
    mv_prev = _fnum(hist_mv.iloc[-1]["float_mv"]) if not hist_mv.empty else None
    mv_bucket = _bucket(mv_prev, MV_BUCKET_EDGES)
    row["float_mv_bucket"] = _cell(mv_bucket)
    row["small_cap_limit_up_rate"] = _cell(mv_bucket_rate.get(mv_bucket) if mv_bucket is not None else None)
    if row["small_cap_limit_up_rate"] == row["small_cap_limit_up_rate"]:  # 非 NaN
        proxy_row["small_cap_limit_up_rate"] = 1  # 板内宇宙代理

    prev_close_price = prev_close if prev_close is not None else (_fnum(hist_kline.iloc[-1]["close"]) if not hist_kline.empty else None)
    price_bucket = _bucket(prev_close_price, PRICE_BUCKET_EDGES)
    row["price_bucket"] = _cell(price_bucket)
    row["price_bucket_limit_up_rate"] = _cell(price_bucket_rate.get(price_bucket) if price_bucket is not None else None)

    # ---------- D. 交互项 ----------
    fd_ratio_fmv = _safe_div(fd_amount, float_mv)
    log_mv = math.log10(float_mv / 1e8 + 1.0) if float_mv is not None and float_mv > 0 else None
    row["seal_strength_x_float_mv"] = _cell(
        fd_ratio_fmv * log_mv if fd_ratio_fmv is not None and log_mv is not None else None
    )

    regime = regime_map.get(prev_date) if prev_date else None
    if first_min is not None and regime is not None:
        row["first_seal_time_x_market_regime"] = (1.0 - first_min / SESSION_MINUTES) * regime
        proxy_row["first_seal_time_x_market_regime"] = 1
    else:
        row["first_seal_time_x_market_regime"] = np.nan

    cont = row["continuation_success_rate"]
    row["open_times_x_continuation_rate"] = _cell(
        open_times * cont if open_times is not None and isinstance(cont, float) and not math.isnan(cont) else None
    )

    fd_ratio_amt = _safe_div(fd_amount, board_amount)
    theme_val = _text_or_none((t_board or {}).get("theme")) if t_board is not None else None
    theme_heat = None
    if theme_val is not None and fd_ratio_amt is not None:
        theme_heat = fd_ratio_amt * min(theme_counts.get(str(theme_val), 0) / 10.0, 1.0)
        proxy_row["fd_ratio_x_theme_heat"] = 1  # 依赖 #8 主题回填，先标代理
    row["fd_ratio_x_theme_heat"] = _cell(theme_heat)

    # ---------- 缺失 / 代理指示列 ----------
    for spec in FACTOR_SPECS:
        value = row.get(spec.name)
        if spec.name in TEXT_FACTORS:
            missing = value is None or (isinstance(value, float) and math.isnan(value)) or value == ""
        else:
            missing = value is None or (isinstance(value, float) and math.isnan(value))
        row[f"{spec.name}_is_missing"] = int(bool(missing))
    for name in PROXY_FACTORS:
        row[f"{name}_is_proxy"] = int(proxy_row.get(name, 0))
    return row


def _all_output_columns() -> list[str]:
    columns = list(RAW_FACTORS)
    for spec in FACTOR_SPECS:
        columns.append(spec.name)
    for spec in FACTOR_SPECS:
        columns.append(f"{spec.name}_is_missing")
    columns.extend(f"{name}_is_proxy" for name in PROXY_FACTORS)
    return columns


def microstructure_output_columns() -> list[str]:
    """输出列清单（含指示列），供 #23 join 前的 schema 校验。"""
    return _all_output_columns()


# ─── 质量报告（样本分布 / 缺失率；IC 待 #23/#27）─────────────────────


def microstructure_quality_report(
    db_path: str | Path,
    symbols: Iterable[str],
    as_of_date: str,
    lookback: int = DEFAULT_LOOKBACK_DAYS,
) -> dict[str, Any]:
    """输出因子缺失率与样本分布；IC / 分层收益 / 模型增量留待 #23、#27（PARTIAL）。"""
    frame = build_microstructure_features(db_path, symbols, as_of_date, lookback)
    total = int(len(frame))
    missing_rates: dict[str, float] = {}
    for spec in FACTOR_SPECS:
        missing_rates[spec.name] = (
            float(frame[f"{spec.name}_is_missing"].mean()) if total else 1.0
        )
    proxy_rates: dict[str, float] = {}
    for name in PROXY_FACTORS:
        proxy_rates[name] = float(frame[f"{name}_is_proxy"].mean()) if total else 0.0
    distribution: dict[str, dict[str, float]] = {}
    for spec in FACTOR_SPECS:
        col = frame.get(spec.name)
        if col is None or spec.name in TEXT_FACTORS:
            continue
        numeric = pd.to_numeric(col, errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
        if numeric.empty:
            continue
        distribution[spec.name] = {
            "count": int(numeric.shape[0]),
            "mean": float(numeric.mean()),
            "median": float(numeric.median()),
            "p90": float(numeric.quantile(0.9)),
            "min": float(numeric.min()),
            "max": float(numeric.max()),
        }
    return {
        "as_of_date": str(as_of_date),
        "symbol_count": total,
        "limit_row_coverage": float(frame["has_limit_row"].mean()) if total else 0.0,
        "missing_rate": missing_rates,
        "proxy_rate": proxy_rates,
        "distribution": distribution,
        "ic_report": "PARTIAL：依赖 #23 标签与 #27 评估基线，禁止提前填数",
        "model_gain_report": "PARTIAL：同上（AUC/PR-AUC/Precision@TopK 增量待 #6/#27）",
    }
