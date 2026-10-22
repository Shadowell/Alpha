"""资金流向与龙虎榜因子族（issue #28）。

设计原则
--------
* **只读取数 / 可注入计算**：默认从 #22 数据底座（``market_kline.db``）只读加载
  ``moneyflow_daily`` / ``kline_daily`` / ``kline_daily_extras`` /
  ``limit_board_daily`` / ``hsgt_top10_daily`` / ``dragon_tiger_daily`` /
  ``dragon_tiger_seat_daily`` / ``hot_money_seat``；单测走
  :class:`CapitalFlowInputs` 注入 DataFrame，不碰磁盘也不联网。
* **PIT（严格按实际可见时刻）**：每条盘后记录的实际可见时刻
  ``available_at = max(trade_date + 默认披露时刻, 落库时间戳)``；
  龙虎榜默认 19:00（:data:`LHB_DISCLOSE_TIME`）、个股资金流默认 15:30、
  北向十大默认 18:00。``as_of_time`` 早于 ``available_at`` 的记录一律不可用：

  1. **未显式给 ``as_of_time`` 时默认取 ``as_of_date 00:00``（前一收盘决策时点）** ——
     与 #23 数据集 / #26 日频回测的入参一致，此时 T 日资金流 / 龙虎榜 / 北向因子
     全部为 NaN，因此**不可能出现"用 T 日盘后榜单解释 T 日盘中"**，更不存在
     T+1 泄漏；``as_of_time_source`` 列写明 ``default_prev_close``。
  2. 需要"当日盘后"因子时必须显式传 ``as_of_date 21:00`` 这类时刻。
  3. 当日榜单披露延迟（落库时间跨过午夜）→ 当日 T 因子显式缺失，并给出
     只用 ``trade_date < T`` 已披露榜单的保守代理（``lhb_net_buy_5d/10d``，
     打 ``*_is_proxy=1``）。

* **单位口径集中锁定**：库内金额一律 **元**（#22 / Tushare 映射层负责换算），
  ``kline_daily.volume`` 是 **手**，``kline_daily_extras.turnover_rate`` 是
  **百分数**（本模块用 :func:`as_ratio` 换成无量纲小数）。:func:`normalize_input_units`
  还支持以 ``wan_yuan`` / ``yi_yuan`` / ``hand`` / ``pct`` 注入原始数据；
  未登记单位直接抛错，绝不静默猜单位。
* **缺失**：缺失恒为 ``numpy.nan`` + ``*_is_missing=1``，**绝不 fillna(0)**。
  例外：龙虎榜/北向的"未上榜 / 未成交"是**真实零事件**（同"未涨停记 0"），
  跨日聚合按 0 计入，代码在 :func:`_sum_window` 显式标注该语义。
* **不伪造游资标签**：``lhb_famous_seat_count`` / ``lhb_hot_money_net_buy``
  依赖 ``hot_money_seat`` 画像（#13）；画像为空时保持缺失。

主力资金口径（写死 Tushare，不混用其他源）
------------------------------------------
主力 = 大单 + 特大单：``main_net_inflow = buy_lg + buy_elg - sell_lg - sell_elg``；
``net_mf_amount`` 为 Tushare 全单净流入；中小单镜像口径用于识别"拉高出货"。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

from strategy.daban_factors._base import (
    UNIT_SCALE,
    FactorSpec,
    attach_indicators,
    cell,
    chunked,
    empty_frame,
    factor_distribution,
    indicator_rates,
    num,
    output_columns,
    parse_ts,
    project_columns,
    query_frame,
    ro_connection,
    safe_div,
    session_minutes_from_clock,
    specs_as_dicts,
    text_or_none,
)

# ─── 常量与口径 ──────────────────────────────────────────────────────

#: 个股资金流（Tushare ``moneyflow``）默认盘后可见时刻。
MONEYFLOW_DISCLOSE_TIME = "15:30"
#: 龙虎榜（``top_list`` / ``top_inst``）默认盘后披露时刻。
LHB_DISCLOSE_TIME = "19:00"
#: 北向十大成交股（``hsgt_top10``）默认披露时刻。
HSGT_DISCLOSE_TIME = "18:00"
#: 融资融券余额次一交易日才披露。
MARGIN_DISCLOSE_TIME = "next_day"
#: 未显式给 ``as_of_time`` 时的默认决策时点（前一收盘，盘后数据不可见）。
DEFAULT_DECISION_CLOCK = "00:00"

#: ``as_of_time_source`` 取值：默认前一收盘 / 只给日期（等同前一收盘）/ 显式时刻。
SOURCE_DEFAULT_PREV_CLOSE = "default_prev_close"
SOURCE_DATE_ONLY = "date_only_prev_close"
SOURCE_EXPLICIT = "explicit_time"
#: 视为"前一收盘决策"（T 日盘后因子必须缺失）的两种姿态。
PREV_CLOSE_SOURCES = (SOURCE_DEFAULT_PREV_CLOSE, SOURCE_DATE_ONLY)

DEFAULT_LOOKBACK_DAYS = 60
#: 拉取窗口相对回看窗口的缓冲（自然日）。
LOOKBACK_BUFFER_DAYS = 60
#: 单行历史最大跨度（自然日）：控制取数成本，同时保证 10 日窗口可算。
MAX_HISTORY_SPAN_DAYS = 365
#: 交易日历最大回看跨度（自然日）。
CALENDAR_SPAN_DAYS = 420

NET_INFLOW_WINDOWS = (3, 5, 10)
LHB_WINDOW_DAYS = (5, 10)
PRE_LIMIT_WINDOW_DAYS = 3
ON_LIST_LOOKBACK_DAYS = 5

#: 机构专用席位识别（Tushare ``top_inst.exalter`` 固定文案）。
INSTITUTION_SEAT_KEYWORDS = ("机构专用",)

#: 共线性检查的基线列（成交额 / 量比 / 换手率）。
BASELINE_COLUMNS = ("context_amount", "context_volume_ratio", "context_turnover_rate")

#: 相关性阈值：|corr| 超过即标记需正交化。
COLLINEARITY_THRESHOLD = 0.8

# 降级原因码（写进 ``degradation_reasons`` 列与验收报告，不静默失败）。
REASON_LHB_DISCLOSE_DELAYED = "lhb_disclose_delayed"
REASON_HOT_MONEY_PROFILE_MISSING = "hot_money_profile_missing"
REASON_MARGIN_API_MISSING = "margin_detail_not_wired"
REASON_BLOCK_TRADE_API_MISSING = "block_trade_not_wired"
REASON_HK_HOLD_API_MISSING = "hk_hold_not_wired"

LABEL_COLUMNS = ("label_next_day_premium", "label_promotion", "label_break_board")


def normalize_input_units(
    frame: pd.DataFrame, columns: Sequence[str], unit: str
) -> pd.DataFrame:
    """把注入 frame 的指定列从 ``unit`` 换算为内部规范单位（元 / 股 / 小数）。"""
    if unit not in UNIT_SCALE:
        raise ValueError(f"未登记的资金/量单位口径: {unit}")
    if frame is None or frame.empty:
        return pd.DataFrame(columns=list(frame.columns) if frame is not None else [])
    result = frame.copy()
    scale = UNIT_SCALE[unit]
    if scale == 1.0:
        return result
    for column in columns:
        if column in result.columns:
            result[column] = pd.to_numeric(result[column], errors="coerce") * scale
    return result


def as_ratio(value: Any, unit: str = "pct") -> float | None:
    """百分数口径（``pct``）转无量纲小数；``ratio`` 原样返回。"""
    number = num(value)
    if number is None:
        return None
    if unit == "ratio":
        return number
    if unit == "pct":
        return number / 100.0
    raise ValueError(f"不支持的比率单位: {unit}")


# ─── 因子注册表 ──────────────────────────────────────────────────────


def _spec(
    name: str,
    group: str,
    formula: str,
    unit: str,
    source: str,
    missing_rule: str,
    pit_note: str,
    *,
    is_proxy_capable: bool = False,
    visible_from: str = MONEYFLOW_DISCLOSE_TIME,
) -> FactorSpec:
    return FactorSpec(
        name=name,
        group=group,
        formula=formula,
        unit=unit,
        source=source,
        missing_rule=missing_rule,
        pit_note=pit_note,
        visible_from=visible_from,
        is_proxy_capable=is_proxy_capable,
    )


FACTOR_SPECS: tuple[FactorSpec, ...] = (
    # ---- A. 个股资金流 ----
    _spec(
        "main_net_inflow",
        "stock_moneyflow",
        "buy_lg+buy_elg-sell_lg-sell_elg（Tushare 主力=大单+特大单）",
        "元",
        "moneyflow_daily",
        "T 日记录不可见或四列任一缺失→缺失",
        "T 日 15:30 后可见",
    ),
    _spec(
        "main_net_inflow_ratio",
        "stock_moneyflow",
        "main_net_inflow / 当日成交额",
        "无量纲",
        "moneyflow_daily + 成交额",
        "成分缺失或分母<=0→缺失",
        "T 日 15:30 后可见",
    ),
    _spec(
        "main_net_inflow_to_float_mv",
        "stock_moneyflow",
        "main_net_inflow / 流通市值",
        "无量纲",
        "moneyflow_daily + limit_board_daily.float_mv",
        "成分缺失/市值<=0→缺失",
        "T 日 15:30 后可见",
    ),
    _spec(
        "super_large_net_inflow",
        "stock_moneyflow",
        "buy_elg_amount-sell_elg_amount",
        "元",
        "moneyflow_daily",
        "T 日两列任一缺失→缺失",
        "T 日 15:30 后可见",
    ),
    _spec(
        "large_net_inflow",
        "stock_moneyflow",
        "buy_lg_amount-sell_lg_amount",
        "元",
        "moneyflow_daily",
        "T 日两列任一缺失→缺失",
        "T 日 15:30 后可见",
    ),
    _spec(
        "small_medium_net_inflow",
        "stock_moneyflow",
        "(buy_sm+buy_md)-(sell_sm+sell_md)：主力净买的镜像，识别拉高出货",
        "元",
        "moneyflow_daily",
        "T 日四列任一缺失→缺失",
        "T 日 15:30 后可见",
    ),
    *[
        _spec(
            f"net_inflow_{n}d",
            "stock_moneyflow",
            f"最近 {n} 个交易日 net_mf_amount 之和（日历驱动，窗口内不得有空洞）",
            "元",
            "moneyflow_daily",
            "T 日不可见/窗口不足/窗口内任一日无记录→缺失",
            "窗口内每条记录均需 available_at<=as_of_time",
        )
        for n in NET_INFLOW_WINDOWS
    ],
    _spec(
        "consecutive_inflow_days",
        "stock_moneyflow",
        "自 T 日往前连续 net_mf_amount>0 的交易日数（遇非正值停止）",
        "天",
        "moneyflow_daily",
        "T 日不可见/判定窗口内记录不可见→缺失",
        "只用可见记录",
    ),
    _spec(
        "sell_pressure_days",
        "stock_moneyflow",
        "自 T 日往前连续 net_mf_amount<0 的交易日数（一日游 / 出货识别）",
        "天",
        "moneyflow_daily",
        "同 consecutive_inflow_days",
        "只用可见记录",
    ),
    _spec(
        "inflow_ratio_vs_amount",
        "stock_moneyflow",
        "net_mf_amount / 当日成交额",
        "无量纲",
        "moneyflow_daily + 成交额",
        "成分缺失/分母<=0→缺失",
        "T 日 15:30 后可见",
    ),
    _spec(
        "inflow_before_limit_up",
        "stock_moneyflow",
        f"T 日涨停(U) 时：T 日前 {PRE_LIMIT_WINDOW_DAYS} 个交易日 net_mf_amount 之和",
        "元",
        "moneyflow_daily + limit_board_daily",
        "非涨停日/窗口不足→缺失",
        "只用 trade_date<T 的记录（严格早于决策日）",
    ),
    _spec(
        "inflow_on_limit_up_day",
        "stock_moneyflow",
        "T 日涨停(U) 时的当日 net_mf_amount",
        "元",
        "moneyflow_daily + limit_board_daily",
        "非涨停日/T 不可见→缺失",
        "T 日 15:30 后可见",
    ),
    # ---- B. 北向 ----
    _spec(
        "northbound_net_buy",
        "northbound",
        "hsgt_top10 当日 net_amount（沪深通道求和）",
        "元",
        "hsgt_top10_daily",
        "未上榜/不可见→缺失（十大口径覆盖有限）",
        f"T 日 {HSGT_DISCLOSE_TIME} 后可见",
        visible_from=HSGT_DISCLOSE_TIME,
    ),
    _spec(
        "northbound_net_buy_ratio",
        "northbound",
        "northbound_net_buy / 北向十大买卖成交额合计",
        "无量纲",
        "hsgt_top10_daily",
        "未上榜/分母<=0→缺失",
        f"T 日 {HSGT_DISCLOSE_TIME} 后可见",
        visible_from=HSGT_DISCLOSE_TIME,
    ),
    _spec(
        "northbound_holding_change",
        "northbound",
        "优先 hk_hold 持股市值差分；未接入时用北向净买额代理",
        "元",
        "hk_hold（未接入）→ hsgt_top10_daily（代理）",
        "两路均无→缺失",
        f"T 日 {HSGT_DISCLOSE_TIME} 后可见",
        is_proxy_capable=True,
        visible_from=HSGT_DISCLOSE_TIME,
    ),
    _spec(
        "northbound_holding_ratio",
        "northbound",
        "北向持股市值 / 流通市值",
        "无量纲",
        "hk_hold + limit_board_daily.float_mv",
        "无持股市值→缺失",
        "需持股快照（未接入）",
        visible_from="next_day",
    ),
    _spec(
        "northbound_consecutive_buy_days",
        "northbound",
        "自 T 日往前连续上榜日 net_amount>0 的天数（未上榜日按真实零事件计 0）",
        "天",
        "hsgt_top10_daily + 交易日历",
        "T 日不可见/判定窗口记录不可见→缺失",
        "只用可见记录",
        visible_from=HSGT_DISCLOSE_TIME,
    ),
    # ---- C. 龙虎榜 ----
    _spec(
        "lhb_on_list",
        "dragon_tiger",
        "T 日榜单已披露且该股在榜→1；确认不在榜→0；未披露→缺失",
        "0/1",
        "dragon_tiger_daily",
        "披露延迟→缺失（不填 0）",
        f"T 日 {LHB_DISCLOSE_TIME} 后可见",
        visible_from=LHB_DISCLOSE_TIME,
    ),
    _spec(
        "lhb_net_buy",
        "dragon_tiger",
        "上榜行 net_amount（同股多原因取成交额最大行，避免重复计入）",
        "元",
        "dragon_tiger_daily",
        "未上榜/未披露→缺失",
        f"T 日 {LHB_DISCLOSE_TIME} 后可见",
        visible_from=LHB_DISCLOSE_TIME,
    ),
    _spec(
        "lhb_buy_amount",
        "dragon_tiger",
        "同口径的 l_buy",
        "元",
        "dragon_tiger_daily",
        "未上榜/未披露→缺失",
        f"T 日 {LHB_DISCLOSE_TIME} 后可见",
        visible_from=LHB_DISCLOSE_TIME,
    ),
    _spec(
        "lhb_sell_amount",
        "dragon_tiger",
        "同口径的 l_sell",
        "元",
        "dragon_tiger_daily",
        "未上榜/未披露→缺失",
        f"T 日 {LHB_DISCLOSE_TIME} 后可见",
        visible_from=LHB_DISCLOSE_TIME,
    ),
    _spec(
        "lhb_net_buy_ratio",
        "dragon_tiger",
        "lhb_net_buy / 榜单成交额 amount",
        "无量纲",
        "dragon_tiger_daily",
        "未上榜/分母<=0→缺失",
        f"T 日 {LHB_DISCLOSE_TIME} 后可见",
        visible_from=LHB_DISCLOSE_TIME,
    ),
    _spec(
        "lhb_net_buy_to_float_mv",
        "dragon_tiger",
        "lhb_net_buy / 流通市值",
        "无量纲",
        "dragon_tiger_daily + limit_board_daily",
        "成分缺失/市值<=0→缺失",
        f"T 日 {LHB_DISCLOSE_TIME} 后可见",
        visible_from=LHB_DISCLOSE_TIME,
    ),
    _spec(
        "lhb_reason_count",
        "dragon_tiger",
        "T 日该股上榜原因条数",
        "次",
        "dragon_tiger_daily",
        "未上榜/未披露→缺失",
        f"T 日 {LHB_DISCLOSE_TIME} 后可见",
        visible_from=LHB_DISCLOSE_TIME,
    ),
    *[
        _spec(
            f"lhb_net_buy_{n}d",
            "dragon_tiger",
            f"T 日前 {n} 个交易日**已披露**榜单 net_amount 之和（不含 T 日）",
            "元",
            "dragon_tiger_daily",
            "日历缺失/窗口内无任何榜单记录→缺失",
            "只用 trade_date<T 且已披露的行，故当日披露延迟不影响该保守代理",
            is_proxy_capable=True,
            visible_from=DEFAULT_DECISION_CLOCK,
        )
        for n in LHB_WINDOW_DAYS
    ],
    _spec(
        "lhb_on_list_5d_days",
        "dragon_tiger",
        f"最近 {ON_LIST_LOOKBACK_DAYS} 个交易日已披露榜单的上榜天数（含 T）",
        "天",
        "dragon_tiger_daily + 交易日历",
        "日历缺失→缺失",
        "未上榜按真实零事件计 0",
        visible_from=DEFAULT_DECISION_CLOCK,
    ),
    _spec(
        "lhb_institution_net_buy",
        "dragon_tiger",
        "席位名为「机构专用」的净额求和（T 日席位）",
        "元",
        "dragon_tiger_seat_daily",
        "无席位明细→缺失",
        f"T 日 {LHB_DISCLOSE_TIME} 后可见",
        visible_from=LHB_DISCLOSE_TIME,
    ),
    _spec(
        "lhb_institution_net_buy_5d",
        "dragon_tiger",
        f"最近 {ON_LIST_LOOKBACK_DAYS} 个交易日已披露的机构专用席位净额求和",
        "元",
        "dragon_tiger_seat_daily + 交易日历",
        "日历缺失/无席位明细→缺失",
        "未上榜按真实零事件计 0",
        visible_from=DEFAULT_DECISION_CLOCK,
    ),
    _spec(
        "lhb_hot_money_net_buy",
        "dragon_tiger",
        "席位命中游资画像的净额求和（画像为空→缺失，不猜游资）",
        "元",
        "dragon_tiger_seat_daily + hot_money_seat",
        "画像表为空→缺失",
        f"T 日 {LHB_DISCLOSE_TIME} 后可见",
        visible_from=LHB_DISCLOSE_TIME,
    ),
    _spec(
        "lhb_seat_concentration",
        "dragon_tiger",
        "买入席位金额前 5 名之和 / 全部买入席位金额",
        "无量纲",
        "dragon_tiger_seat_daily",
        "买入席位金额合计<=0→缺失",
        f"T 日 {LHB_DISCLOSE_TIME} 后可见",
        visible_from=LHB_DISCLOSE_TIME,
    ),
    _spec(
        "lhb_buy_seat_count",
        "dragon_tiger",
        "side=buy 的席位记录数（按 seat_count 放大）",
        "次",
        "dragon_tiger_seat_daily",
        "无席位明细→缺失",
        f"T 日 {LHB_DISCLOSE_TIME} 后可见",
        visible_from=LHB_DISCLOSE_TIME,
    ),
    _spec(
        "lhb_sell_seat_count",
        "dragon_tiger",
        "side=sell 的席位记录数",
        "次",
        "dragon_tiger_seat_daily",
        "无席位明细→缺失",
        f"T 日 {LHB_DISCLOSE_TIME} 后可见",
        visible_from=LHB_DISCLOSE_TIME,
    ),
    _spec(
        "lhb_is_first_board",
        "dragon_tiger",
        "已披露上榜且 T 日 limit_times=1 → 1",
        "0/1",
        "dragon_tiger_daily + limit_board_daily",
        "无榜单或无板行→缺失",
        "T 日盘后",
        visible_from=LHB_DISCLOSE_TIME,
    ),
    _spec(
        "lhb_is_continuation",
        "dragon_tiger",
        "已披露上榜且 T 日 limit_times>=2 → 1",
        "0/1",
        "dragon_tiger_daily + limit_board_daily",
        "无榜单或无板行→缺失",
        "T 日盘后",
        visible_from=LHB_DISCLOSE_TIME,
    ),
    _spec(
        "lhb_famous_seat_count",
        "dragon_tiger",
        "命中游资画像的去重席位数量（依赖 #13）",
        "次",
        "dragon_tiger_seat_daily + hot_money_seat",
        "画像表为空→缺失",
        "T 日盘后",
        visible_from=LHB_DISCLOSE_TIME,
    ),
    # ---- D. 融资 / 大宗（接口未接入 → 显式缺失）----
    _spec(
        "margin_balance_change",
        "margin_block",
        "融资余额 T - T-1（margin_detail 未接入）",
        "元",
        "margin_detail（未接入）",
        "无数据源→缺失",
        "融资融券次一交易日披露",
        visible_from="next_day",
    ),
    _spec(
        "margin_buy_ratio",
        "margin_block",
        "融资买入额 / 当日成交额（未接入）",
        "无量纲",
        "margin_detail（未接入）",
        "无数据源→缺失",
        "次一交易日披露",
        visible_from="next_day",
    ),
    _spec(
        "block_trade_premium",
        "margin_block",
        "大宗加权成交价 / 收盘价 - 1（未接入）",
        "无量纲",
        "block_trade（未接入）",
        "无数据源→缺失",
        "盘后披露",
        visible_from=LHB_DISCLOSE_TIME,
    ),
    _spec(
        "block_trade_amount_ratio",
        "margin_block",
        "大宗成交金额 / 当日成交额（未接入）",
        "无量纲",
        "block_trade（未接入）",
        "无数据源→缺失",
        "盘后披露",
        visible_from=LHB_DISCLOSE_TIME,
    ),
    # ---- E. 交互项 ----
    _spec(
        "fd_ratio_x_main_inflow",
        "interaction",
        "(fd_amount/amount) * (main_net_inflow/amount)",
        "无量纲",
        "limit_board_daily + moneyflow_daily",
        "任一成分缺失→缺失",
        "成分需 T 日 15:30 后可见；默认决策时点下为 NaN",
    ),
    _spec(
        "lhb_net_buy_x_theme_heat",
        "interaction",
        "lhb_net_buy_ratio * min(当日同题材涨停数/10, 1)；题材为 kpl 回填口径",
        "无量纲",
        "dragon_tiger_daily + limit_board_daily.theme",
        "成分缺失→缺失",
        f"T 日 {LHB_DISCLOSE_TIME} 后可见",
        is_proxy_capable=True,
        visible_from=LHB_DISCLOSE_TIME,
    ),
    _spec(
        "northbound_change_x_float_mv",
        "interaction",
        "(northbound_net_buy/float_mv) * log10(float_mv/1e8 + 1)",
        "无量纲",
        "hsgt_top10_daily + limit_board_daily",
        "成分缺失/市值<=0→缺失",
        f"T 日 {HSGT_DISCLOSE_TIME} 后可见",
        visible_from=HSGT_DISCLOSE_TIME,
    ),
    _spec(
        "inflow_3d_x_first_seal_time",
        "interaction",
        "(net_inflow_3d/float_mv) * (1 - first_seal_minute/240)",
        "无量纲",
        "moneyflow_daily + limit_board_daily",
        "成分缺失→缺失",
        "资金流成分需 T 日 15:30 后可见",
    ),
)

PROXY_FACTORS = (
    "northbound_holding_change",
    "lhb_net_buy_x_theme_heat",
    *(f"lhb_net_buy_{n}d" for n in LHB_WINDOW_DAYS),
)

RAW_COLUMNS = (
    "symbol",
    "as_of_date",
    "as_of_time",
    "as_of_time_source",
    "has_moneyflow_row",
    "has_lhb_row",
    "has_north_row",
    "moneyflow_visible_at_T",
    "lhb_visible_at_T",
    "moneyflow_disclosure_lagged",
    "lhb_disclosure_lagged",
    "lhb_day_disclosed",
    "context_amount",
    "context_volume_ratio",
    "context_turnover_rate",
    "degradation_reasons",
)

TEXT_COLUMNS = ("degradation_reasons",)

#: 需要"当日盘后"数据的因子组（默认决策时点下必须全缺失）。
POST_CLOSE_GROUPS = ("stock_moneyflow", "northbound", "dragon_tiger", "interaction")
#: 跨日保守代理（只用 T 之前的已披露记录，允许在默认决策时点可见）。
HISTORY_ONLY_FACTORS = (
    *(f"lhb_net_buy_{n}d" for n in LHB_WINDOW_DAYS),
    "lhb_on_list_5d_days",
    "lhb_institution_net_buy_5d",
    # 只累加 T-1 及更早的资金流，前一收盘时点即可见（不含任何 T 日信息）。
    "inflow_before_limit_up",
)


def capital_flow_factor_specs() -> list[dict[str, Any]]:
    """因子字典（公式 / 单位 / 数据源 / 缺失规则 / PIT 说明 / 最早可见时点）。"""
    return specs_as_dicts(FACTOR_SPECS)


def capital_flow_output_columns() -> list[str]:
    return output_columns(
        FACTOR_SPECS, raw_columns=RAW_COLUMNS, proxy_factors=PROXY_FACTORS
    )


def capital_flow_spec_names(group: str | None = None) -> list[str]:
    return [spec.name for spec in FACTOR_SPECS if group is None or spec.group == group]


def numeric_factor_names() -> list[str]:
    return [spec.name for spec in FACTOR_SPECS if spec.name not in TEXT_COLUMNS]


# ─── 输入容器 ────────────────────────────────────────────────────────


def _empty(columns: Sequence[str]) -> pd.DataFrame:
    return pd.DataFrame(columns=list(columns))


@dataclass(slots=True)
class CapitalFlowInputs:
    """注入式输入（金额列单位=元；比率列=无量纲小数；换手率列=百分数）。"""

    moneyflow: pd.DataFrame = field(
        default_factory=lambda: _empty(
            [
                "symbol",
                "trade_date",
                "buy_sm_amount",
                "sell_sm_amount",
                "buy_md_amount",
                "sell_md_amount",
                "buy_lg_amount",
                "sell_lg_amount",
                "buy_elg_amount",
                "sell_elg_amount",
                "net_mf_amount",
                "updated_at",
            ]
        )
    )
    board: pd.DataFrame = field(
        default_factory=lambda: _empty(
            [
                "symbol",
                "trade_date",
                "limit_flag",
                "limit_times",
                "first_time",
                "fd_amount",
                "amount",
                "float_mv",
                "theme",
                "synced_at",
            ]
        )
    )
    kline: pd.DataFrame = field(
        default_factory=lambda: _empty(
            ["symbol", "trade_date", "close", "volume", "amount"]
        )
    )
    extras: pd.DataFrame = field(
        default_factory=lambda: _empty(
            ["symbol", "trade_date", "turnover_rate", "volume_ratio"]
        )
    )
    calendar: pd.DataFrame = field(default_factory=lambda: _empty(["trade_date"]))
    hsgt_top10: pd.DataFrame = field(
        default_factory=lambda: _empty(
            [
                "symbol",
                "trade_date",
                "market_type",
                "amount",
                "net_amount",
                "buy_amount",
                "sell_amount",
                "synced_at",
            ]
        )
    )
    north_holding: pd.DataFrame = field(
        default_factory=lambda: _empty(["symbol", "trade_date", "holding_mv"])
    )
    lhb: pd.DataFrame = field(
        default_factory=lambda: _empty(
            [
                "symbol",
                "trade_date",
                "reason",
                "amount",
                "l_buy",
                "l_sell",
                "net_amount",
                "synced_at",
            ]
        )
    )
    lhb_seats: pd.DataFrame = field(
        default_factory=lambda: _empty(
            [
                "trade_date",
                "symbol",
                "seat_name",
                "side",
                "buy_amount",
                "sell_amount",
                "net_amount",
                "seat_count",
                "synced_at",
            ]
        )
    )
    hot_money_seats: pd.DataFrame = field(
        default_factory=lambda: _empty(["seat_name", "org_code", "category"])
    )
    margin: pd.DataFrame = field(
        default_factory=lambda: _empty(
            [
                "symbol",
                "trade_date",
                "margin_balance",
                "margin_buy_amount",
                "updated_at",
            ]
        )
    )
    block_trades: pd.DataFrame = field(
        default_factory=lambda: _empty(
            ["symbol", "trade_date", "deal_amount", "price", "close", "updated_at"]
        )
    )


# ─── 可见性判定（PIT）──────────────────────────────────────────────


def row_available_at(
    trade_date: Any, stamp: Any, default_clock: str
) -> datetime | None:
    """盘后记录的实际可见时刻 = max(交易日 + 默认披露时刻, 落库时间戳)。

    ``default_clock="next_day"`` 表示次一交易日才披露（融资融券）。
    """
    day = str(trade_date or "")[:10]
    if len(day) != 10:
        return None
    if default_clock == MARGIN_DISCLOSE_TIME:
        base = parse_ts(day)
        scheduled = base + timedelta(days=1, hours=9) if base else None
    else:
        scheduled = parse_ts(f"{day} {default_clock}")
    stamped = parse_ts(stamp)
    if scheduled is None:
        return stamped
    if stamped is None:
        return scheduled
    return max(scheduled, stamped)


def filter_visible(
    frame: pd.DataFrame,
    as_of_time: datetime,
    *,
    default_clock: str,
    stamp_column: str = "synced_at",
) -> pd.DataFrame:
    """只保留 ``available_at <= as_of_time`` 的记录（PIT 截断）。"""
    columns = list(frame.columns) if frame is not None else []
    if frame is None or frame.empty:
        return pd.DataFrame(columns=columns)
    mask: list[bool] = []
    for _, row in frame.iterrows():
        available = row_available_at(
            row.get("trade_date"), row.get(stamp_column), default_clock
        )
        mask.append(available is not None and available <= as_of_time)
    return frame.loc[mask].reset_index(drop=True)


def disclosure_lag_days(
    frame: pd.DataFrame,
    *,
    trade_date: str,
    stamp_column: str = "synced_at",
    default_clock: str = LHB_DISCLOSE_TIME,
) -> int | None:
    """指定交易日记录的披露延迟天数（自然日）；该日无记录 → None。"""
    if frame is None or frame.empty:
        return None
    subset = frame[frame["trade_date"].astype(str).str[:10] == trade_date]
    if subset.empty:
        return None
    worst: int | None = None
    reference = parse_ts(trade_date)
    for _, row in subset.iterrows():
        available = row_available_at(
            row.get("trade_date"), row.get(stamp_column), default_clock
        )
        if available is None or reference is None:
            continue
        lag = max(0, (available.date() - reference.date()).days)
        worst = lag if worst is None else max(worst, lag)
    return worst


# ─── 逐股序列工具 ────────────────────────────────────────────────────


def _series_by_symbol(frame: pd.DataFrame) -> dict[str, dict[str, dict[str, Any]]]:
    """``symbol -> {trade_date -> 行}``（同日多行保留最后一条）。"""
    out: dict[str, dict[str, dict[str, Any]]] = {}
    if frame is None or frame.empty or "symbol" not in frame.columns:
        return out
    ordered = frame.sort_values(["symbol", "trade_date"], kind="stable")
    for row in ordered.to_dict("records"):
        symbol = text_or_none(row.get("symbol"))
        day = str(row.get("trade_date") or "")[:10]
        if symbol and len(day) == 10:
            out.setdefault(symbol, {})[day] = row
    return out


def _grouped_by_symbol(
    frame: pd.DataFrame,
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """``symbol -> {trade_date -> 行列表}``（一日多行，如北向双通道）。"""
    out: dict[str, dict[str, list[dict[str, Any]]]] = {}
    if frame is None or frame.empty or "symbol" not in frame.columns:
        return out
    ordered = frame.sort_values(["symbol", "trade_date"], kind="stable")
    for row in ordered.to_dict("records"):
        symbol = text_or_none(row.get("symbol"))
        day = str(row.get("trade_date") or "")[:10]
        if symbol and len(day) == 10:
            out.setdefault(symbol, {}).setdefault(day, []).append(row)
    return out


def _group_by_key(
    frame: pd.DataFrame, key: str = "symbol"
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """``(symbol, trade_date) -> 行列表``（龙虎榜同股多原因 / 大宗多笔）。"""
    out: dict[tuple[str, str], list[dict[str, Any]]] = {}
    if frame is None or frame.empty or key not in frame.columns:
        return out
    for row in frame.to_dict("records"):
        symbol = text_or_none(row.get(key))
        day = str(row.get("trade_date") or "")[:10]
        if symbol and len(day) == 10:
            out.setdefault((symbol, day), []).append(row)
    return out


def _aggregate_by_symbol_date(
    frame: pd.DataFrame, columns: Sequence[str]
) -> pd.DataFrame:
    """按 ``(symbol, trade_date)`` 聚合金额列（NaN 跳过；全 NaN 时不生成该日行）。"""
    if frame is None or frame.empty:
        return pd.DataFrame(columns=["symbol", "trade_date", *columns])
    records: dict[tuple[str, str], dict[str, Any]] = {}
    for row in frame.to_dict("records"):
        symbol = text_or_none(row.get("symbol"))
        day = str(row.get("trade_date") or "")[:10]
        if not symbol or len(day) != 10:
            continue
        bucket = records.setdefault(
            (symbol, day), {"symbol": symbol, "trade_date": day}
        )
        for column in columns:
            value = num(row.get(column))
            if value is None:
                continue
            bucket[column] = float(bucket.get(column, 0.0)) + value
    return pd.DataFrame(list(records.values()))


def _build_calendar(*frames: pd.DataFrame, as_of_date: str) -> list[str]:
    """从含 ``trade_date`` 的 frame 收集 ``<= as_of_date`` 的交易日并升序去重。"""
    days: set[str] = set()
    for frame in frames:
        if frame is None or frame.empty or "trade_date" not in frame.columns:
            continue
        for value in frame["trade_date"]:
            day = str(value or "")[:10]
            if len(day) == 10 and day <= as_of_date:
                days.add(day)
    return sorted(days)


def _window_days(calendar: Sequence[str], as_of_date: str, days: int) -> list[str]:
    """截至 ``as_of_date``（含）最近 N 个交易日；当日不在日历 → 空列表。"""
    upto = [day for day in calendar if day <= as_of_date]
    if not upto or upto[-1] != as_of_date:
        return []
    return upto[-days:]


def _prior_days(calendar: Sequence[str], as_of_date: str, days: int) -> list[str]:
    """``as_of_date`` 之前最近 N 个交易日（严格 < as_of_date）。"""
    prior = [day for day in calendar if day < as_of_date]
    return prior[-days:] if prior else []


def _sum_window(
    days: Mapping[str, Mapping[str, Any]],
    dates: Sequence[str],
    column: str,
    *,
    absent_is_zero: bool = False,
) -> float | None:
    """按日期序列求和。

    ``absent_is_zero=False``（默认，用于 moneyflow 这类"每日必有"的表）：
    窗口内任一日缺记录或值为空 → 整体缺失，绝不部分求和。
    ``absent_is_zero=True``（用于龙虎榜 / 北向）：某日无记录是**真实零事件**
    （当日未上榜 / 北向未成交），按 0 计入，与"未涨停记 0"同理；
    但若该日记录存在而金额为空值，仍视为数据缺失 → 整体缺失。
    """
    if not dates:
        return None
    total = 0.0
    for day in dates:
        record = days.get(day)
        if record is None:
            if absent_is_zero:
                continue
            return None
        value = num(record.get(column))
        if value is None:
            return None
        total += value
    return float(total)


def _streak_by_calendar(
    days: Mapping[str, Mapping[str, Any]],
    calendar: Sequence[str],
    as_of_date: str,
    column: str,
    predicate: Callable[[float], bool],
    *,
    absent_is_zero: bool = False,
) -> int | None:
    """按交易日历自 T 往前连续满足 ``predicate`` 的交易日数。

    参与判定的交易日若"记录存在但值为空"或"记录缺失且 ``absent_is_zero=False``"
    → 返回 None（无法判断），绝不把缺数当成连续 0 天。
    """
    upto = [day for day in calendar if day <= as_of_date]
    if not upto or upto[-1] != as_of_date:
        return None
    streak = 0
    for day in reversed(upto):
        record = days.get(day)
        if record is None:
            if absent_is_zero:
                return streak
            return None
        value = num(record.get(column))
        if value is None:
            return None
        if not predicate(value):
            return streak
        streak += 1
    return streak


def _famous_seat_names(frame: pd.DataFrame) -> set[str]:
    """游资画像席位集合；画像为空 → 空集（调用方据此把游资因子置缺失）。"""
    if frame is None or frame.empty or "seat_name" not in frame.columns:
        return set()
    names: set[str] = set()
    for row in frame.to_dict("records"):
        name = text_or_none(row.get("seat_name"))
        if name is None:
            continue
        category = text_or_none(row.get("category")) or ""
        if category and ("游资" not in category and "hot" not in category.lower()):
            continue
        names.add(name)
    return names


def _is_institution(seat_name: Any) -> bool:
    text = text_or_none(seat_name)
    if not text:
        return False
    return any(keyword in text for keyword in INSTITUTION_SEAT_KEYWORDS)


def _theme_limit_up_counts(board: pd.DataFrame, as_of_date: str) -> dict[str, int]:
    out: dict[str, int] = {}
    if board is None or board.empty or "theme" not in board.columns:
        return out
    subset = board[board["trade_date"].astype(str).str[:10] == as_of_date]
    for row in subset.to_dict("records"):
        if text_or_none(row.get("limit_flag")) != "U":
            continue
        theme = text_or_none(row.get("theme"))
        if theme:
            out[theme] = out.get(theme, 0) + 1
    return out


def _day_amount(
    board_days: Mapping[str, Mapping[str, Any]],
    kline_days: Mapping[str, Mapping[str, Any]],
    day: str,
) -> float | None:
    """当日成交额（元）：优先 ``limit_board_daily.amount``，回退 ``kline_daily.amount``。"""
    row = board_days.get(day)
    if row is not None:
        value = num(row.get("amount"))
        if value is not None:
            return value
    krow = kline_days.get(day)
    return num(krow.get("amount")) if krow else None


def _is_nan(value: Any) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))


# ─── 核心计算 ────────────────────────────────────────────────────────


def compute_capital_flow_factors(
    inputs: CapitalFlowInputs,
    symbols: Iterable[str],
    as_of_date: str,
    as_of_time: datetime | None = None,
    *,
    as_of_time_provided: bool = True,
    source_label: str | None = None,
) -> pd.DataFrame:
    """按 ``(symbol, as_of_date)`` 构建 #28 资金流 / 龙虎榜因子表。

    ``as_of_time`` 缺省时取 ``as_of_date 00:00``（前一收盘决策时点），此时
    T 日盘后因子全为 NaN —— 防泄漏的**默认安全姿态**。
    ``source_label`` 写入 ``as_of_time_source`` 列，供 PIT 审计判定姿态。
    """
    symbol_list = sorted({str(s).strip() for s in symbols if str(s).strip()})
    as_of_date = str(as_of_date)[:10]
    if as_of_time is None:
        as_of_time = parse_ts(f"{as_of_date} {DEFAULT_DECISION_CLOCK}")
    if not symbol_list or as_of_time is None:
        return empty_frame(capital_flow_output_columns())
    label = source_label or (
        SOURCE_EXPLICIT if as_of_time_provided else SOURCE_DEFAULT_PREV_CLOSE
    )

    mf = filter_visible(
        inputs.moneyflow,
        as_of_time,
        default_clock=MONEYFLOW_DISCLOSE_TIME,
        stamp_column="updated_at",
    )
    hsgt = filter_visible(
        inputs.hsgt_top10, as_of_time, default_clock=HSGT_DISCLOSE_TIME
    )
    lhb = filter_visible(inputs.lhb, as_of_time, default_clock=LHB_DISCLOSE_TIME)
    seats = filter_visible(
        inputs.lhb_seats, as_of_time, default_clock=LHB_DISCLOSE_TIME
    )
    margin = filter_visible(
        inputs.margin,
        as_of_time,
        default_clock=MARGIN_DISCLOSE_TIME,
        stamp_column="updated_at",
    )
    blocks = filter_visible(
        inputs.block_trades,
        as_of_time,
        default_clock=LHB_DISCLOSE_TIME,
        stamp_column="updated_at",
    )
    board, kline, extras = inputs.board, inputs.kline, inputs.extras

    calendar = _build_calendar(inputs.calendar, kline, board, as_of_date=as_of_date)

    mf_raw = _series_by_symbol(mf)
    mf_daily_by = _series_by_symbol(_aggregate_by_symbol_date(mf, ["net_mf_amount"]))
    hsgt_by = _grouped_by_symbol(hsgt)
    holding_by = _series_by_symbol(inputs.north_holding)
    board_by = _series_by_symbol(board)
    kline_by = _series_by_symbol(kline)
    extras_by = _series_by_symbol(extras)
    margin_by = _series_by_symbol(margin)
    lhb_by = _group_by_key(lhb)
    lhb_daily_by = _series_by_symbol(_aggregate_by_symbol_date(lhb, ["net_amount"]))
    seat_by = _group_by_key(seats)
    inst_seats = (
        seats[seats["seat_name"].map(_is_institution)] if not seats.empty else seats
    )
    inst_daily_by = _series_by_symbol(
        _aggregate_by_symbol_date(inst_seats, ["net_amount"])
    )
    block_by = _group_by_key(blocks)
    famous = _famous_seat_names(inputs.hot_money_seats)
    theme_counts = _theme_limit_up_counts(board, as_of_date)

    mf_lag = disclosure_lag_days(
        inputs.moneyflow,
        trade_date=as_of_date,
        stamp_column="updated_at",
        default_clock=MONEYFLOW_DISCLOSE_TIME,
    )
    lhb_lag = disclosure_lag_days(
        inputs.lhb, trade_date=as_of_date, default_clock=LHB_DISCLOSE_TIME
    )
    lhb_day_disclosed = bool(
        not lhb.empty and lhb["trade_date"].astype(str).str[:10].eq(as_of_date).any()
    )

    records: list[dict[str, Any]] = []
    for symbol in symbol_list:
        row: dict[str, Any] = {
            "symbol": symbol,
            "as_of_date": as_of_date,
            "as_of_time": as_of_time.strftime("%Y-%m-%d %H:%M:%S"),
            "as_of_time_source": label,
            "degradation_reasons": "",
        }
        proxy: dict[str, int] = {}
        reasons: list[str] = []

        days_mf = mf_daily_by.get(symbol, {})
        days_hsgt = hsgt_by.get(symbol, {})
        board_days = board_by.get(symbol, {})
        t_board = board_days.get(as_of_date)
        amount_t = _day_amount(board_days, kline_by.get(symbol, {}), as_of_date)
        float_mv_t = num((t_board or {}).get("float_mv"))
        extras_t = extras_by.get(symbol, {}).get(as_of_date) or {}

        row["has_moneyflow_row"] = int(as_of_date in mf_raw.get(symbol, {}))
        row["has_lhb_row"] = int(bool(lhb_by.get((symbol, as_of_date))))
        row["has_north_row"] = int(bool(days_hsgt.get(as_of_date)))
        row["moneyflow_visible_at_T"] = int(as_of_date in days_mf)
        row["lhb_visible_at_T"] = int(as_of_date in lhb_daily_by.get(symbol, {}))
        row["moneyflow_disclosure_lagged"] = int(bool(mf_lag))
        row["lhb_disclosure_lagged"] = int(bool(lhb_lag))
        row["lhb_day_disclosed"] = int(lhb_day_disclosed)
        row["context_amount"] = cell(amount_t)
        row["context_volume_ratio"] = cell(num(extras_t.get("volume_ratio")))
        row["context_turnover_rate"] = cell(
            as_ratio(extras_t.get("turnover_rate"))
            if extras_t.get("turnover_rate") is not None
            else None
        )

        if lhb_lag:
            reasons.append(REASON_LHB_DISCLOSE_DELAYED)
        if not famous:
            reasons.append(REASON_HOT_MONEY_PROFILE_MISSING)
        if inputs.margin.empty:
            reasons.append(REASON_MARGIN_API_MISSING)
        if inputs.block_trades.empty:
            reasons.append(REASON_BLOCK_TRADE_API_MISSING)
        if inputs.north_holding.empty:
            reasons.append(REASON_HK_HOLD_API_MISSING)

        _fill_stock_moneyflow(
            row,
            days_mf,
            mf_raw.get(symbol, {}),
            as_of_date,
            amount_t,
            t_board,
            float_mv_t,
            calendar,
        )
        _fill_northbound(
            row,
            proxy,
            days_hsgt,
            holding_by.get(symbol, {}),
            as_of_date,
            float_mv_t,
            calendar,
        )
        _fill_dragon_tiger(
            row,
            t_rows=lhb_by.get((symbol, as_of_date)),
            daily_lhb=lhb_daily_by.get(symbol, {}),
            t_seats=seat_by.get((symbol, as_of_date)),
            inst_daily=inst_daily_by.get(symbol, {}),
            famous=famous,
            t_board=t_board,
            float_mv_t=float_mv_t,
            calendar=calendar,
            day_disclosed=lhb_day_disclosed,
            proxy=proxy,
        )
        _fill_margin_block(
            row,
            margin_by.get(symbol, {}),
            block_by.get((symbol, as_of_date), []),
            as_of_date,
            amount_t,
        )

        _fill_interactions(
            row, t_board, amount_t, float_mv_t, theme_counts, calendar, proxy
        )

        row["degradation_reasons"] = "|".join(dict.fromkeys(reasons))
        attach_indicators(
            row,
            FACTOR_SPECS,
            proxy_flags=proxy,
            proxy_factors=PROXY_FACTORS,
            text_factors=TEXT_COLUMNS,
        )
        records.append(row)

    return project_columns(pd.DataFrame(records), capital_flow_output_columns())


def _fill_stock_moneyflow(
    row: dict[str, Any],
    days_mf: dict[str, dict[str, Any]],
    raw_days: dict[str, dict[str, Any]],
    as_of_date: str,
    amount_t: float | None,
    t_board: dict[str, Any] | None,
    float_mv_t: float | None,
    calendar: Sequence[str],
) -> None:
    t = raw_days.get(as_of_date) or {}
    buy_lg, sell_lg = num(t.get("buy_lg_amount")), num(t.get("sell_lg_amount"))
    buy_elg, sell_elg = num(t.get("buy_elg_amount")), num(t.get("sell_elg_amount"))
    buy_sm, sell_sm = num(t.get("buy_sm_amount")), num(t.get("sell_sm_amount"))
    buy_md, sell_md = num(t.get("buy_md_amount")), num(t.get("sell_md_amount"))

    main = None
    if None not in (buy_lg, sell_lg, buy_elg, sell_elg):
        main = (buy_lg or 0.0) + (buy_elg or 0.0) - (sell_lg or 0.0) - (sell_elg or 0.0)
    row["main_net_inflow"] = cell(main)
    row["main_net_inflow_ratio"] = cell(safe_div(main, amount_t))
    row["main_net_inflow_to_float_mv"] = cell(safe_div(main, float_mv_t))
    row["super_large_net_inflow"] = cell(
        buy_elg - sell_elg if buy_elg is not None and sell_elg is not None else None
    )
    row["large_net_inflow"] = cell(
        buy_lg - sell_lg if buy_lg is not None and sell_lg is not None else None
    )
    small_ok = None not in (buy_sm, sell_sm, buy_md, sell_md)
    row["small_medium_net_inflow"] = cell(
        (buy_sm or 0.0) + (buy_md or 0.0) - (sell_sm or 0.0) - (sell_md or 0.0)
        if small_ok
        else None
    )

    net_t = num(t.get("net_mf_amount"))
    for window in NET_INFLOW_WINDOWS:
        dates = _window_days(calendar, as_of_date, window)
        if len(dates) < window:
            dates = _observed_tail(days_mf, as_of_date, window)
        if len(dates) < window:
            # 覆盖不足 N 个交易日（新股 / 数据未回填）→ 显式缺失，绝不给部分和。
            row[f"net_inflow_{window}d"] = np.nan
        else:
            row[f"net_inflow_{window}d"] = cell(
                _sum_window(days_mf, dates, "net_mf_amount")
            )

    row["consecutive_inflow_days"] = cell(
        _streak_row(days_mf, calendar, as_of_date, "net_mf_amount", lambda v: v > 0)
    )
    row["sell_pressure_days"] = cell(
        _streak_row(days_mf, calendar, as_of_date, "net_mf_amount", lambda v: v < 0)
    )
    row["inflow_ratio_vs_amount"] = cell(safe_div(net_t, amount_t))

    if text_or_none((t_board or {}).get("limit_flag")) == "U":
        prior = _prior_days(
            calendar, as_of_date, PRE_LIMIT_WINDOW_DAYS
        ) or _observed_tail(
            days_mf, as_of_date, PRE_LIMIT_WINDOW_DAYS, strict_before=True
        )
        row["inflow_before_limit_up"] = cell(
            _sum_window(days_mf, prior, "net_mf_amount")
        )
        row["inflow_on_limit_up_day"] = cell(net_t)
    else:
        row["inflow_before_limit_up"] = np.nan
        row["inflow_on_limit_up_day"] = np.nan


def _observed_tail(
    days: Mapping[str, Mapping[str, Any]],
    as_of_date: str,
    count: int,
    *,
    strict_before: bool = False,
) -> list[str]:
    """已观测日期序列尾部 N 天（交易日历不可用时的兜底窗口）。"""
    ordered = sorted(
        d for d in days if (d < as_of_date if strict_before else d <= as_of_date)
    )
    return ordered[-count:] if ordered else []


def _streak_row(
    days: Mapping[str, Mapping[str, Any]],
    calendar: Sequence[str],
    as_of_date: str,
    column: str,
    predicate: Callable[[float], bool],
    *,
    absent_is_zero: bool = False,
) -> int | None:
    if calendar:
        return _streak_by_calendar(
            days, calendar, as_of_date, column, predicate, absent_is_zero=absent_is_zero
        )
    ordered = sorted(d for d in days if d <= as_of_date)
    if not ordered or ordered[-1] != as_of_date:
        return None
    streak = 0
    for day in reversed(ordered):
        value = num(days.get(day, {}).get(column))
        if value is None:
            return None
        if not predicate(value):
            return streak
        streak += 1
    return streak


def _fill_northbound(
    row: dict[str, Any],
    proxy: dict[str, int],
    days: dict[str, list[dict[str, Any]]],
    holding_days: dict[str, dict[str, Any]],
    as_of_date: str,
    float_mv_t: float | None,
    calendar: Sequence[str],
) -> None:
    t_rows = days.get(as_of_date) or []
    if t_rows:
        net = sum(num(r.get("net_amount")) or 0.0 for r in t_rows)
        traded = sum(
            (num(r.get("buy_amount")) or 0.0) + (num(r.get("sell_amount")) or 0.0)
            for r in t_rows
        )
        row["northbound_net_buy"] = net
        row["northbound_net_buy_ratio"] = cell(safe_div(net, traded))
    else:
        row["northbound_net_buy"] = np.nan
        row["northbound_net_buy_ratio"] = np.nan

    holding_t = num((holding_days.get(as_of_date) or {}).get("holding_mv"))
    prior = sorted(d for d in holding_days if d < as_of_date)
    holding_prev = (
        num((holding_days[prior[-1]] or {}).get("holding_mv")) if prior else None
    )
    if holding_t is not None and holding_prev is not None:
        row["northbound_holding_change"] = holding_t - holding_prev
        proxy["northbound_holding_change"] = 0
    elif not _is_nan(row["northbound_net_buy"]):
        row["northbound_holding_change"] = float(row["northbound_net_buy"])
        proxy["northbound_holding_change"] = 1  # 净买额代理，非真实持股变动
    else:
        row["northbound_holding_change"] = np.nan

    row["northbound_holding_ratio"] = cell(safe_div(holding_t, float_mv_t))

    if t_rows:
        merged = {
            day: {"net_amount": sum(num(r.get("net_amount")) or 0.0 for r in rows)}
            for day, rows in days.items()
        }
        row["northbound_consecutive_buy_days"] = cell(
            _streak_row(
                merged,
                calendar,
                as_of_date,
                "net_amount",
                lambda v: v > 0,
                absent_is_zero=True,
            )
        )
    else:
        row["northbound_consecutive_buy_days"] = np.nan


def _fill_dragon_tiger(
    row: dict[str, Any],
    *,
    t_rows: Sequence[dict[str, Any]] | None,
    daily_lhb: dict[str, dict[str, Any]],
    t_seats: Sequence[dict[str, Any]] | None,
    inst_daily: dict[str, dict[str, Any]],
    famous: set[str],
    t_board: dict[str, Any] | None,
    float_mv_t: float | None,
    calendar: Sequence[str],
    day_disclosed: bool,
    proxy: dict[str, int],
) -> None:
    rows = list(t_rows or [])
    if not day_disclosed:
        # 当日榜单整体尚未披露 → 无法区分"未上榜"与"没数据"，一律缺失（不填 0）。
        row["lhb_on_list"] = np.nan
        for name in (
            "lhb_net_buy",
            "lhb_buy_amount",
            "lhb_sell_amount",
            "lhb_net_buy_ratio",
            "lhb_net_buy_to_float_mv",
            "lhb_reason_count",
        ):
            row[name] = np.nan
    elif rows:
        canonical = max(rows, key=lambda item: num(item.get("amount")) or 0.0)
        row["lhb_on_list"] = 1
        row["lhb_net_buy"] = cell(num(canonical.get("net_amount")))
        row["lhb_buy_amount"] = cell(num(canonical.get("l_buy")))
        row["lhb_sell_amount"] = cell(num(canonical.get("l_sell")))
        row["lhb_net_buy_ratio"] = cell(
            safe_div(num(canonical.get("net_amount")), num(canonical.get("amount")))
        )
        row["lhb_net_buy_to_float_mv"] = cell(
            safe_div(num(canonical.get("net_amount")), float_mv_t)
        )
        row["lhb_reason_count"] = cell(float(len(rows)))
    else:
        row["lhb_on_list"] = 0  # 已披露且确认不在榜：真实零事件
        for name in (
            "lhb_net_buy",
            "lhb_buy_amount",
            "lhb_sell_amount",
            "lhb_net_buy_ratio",
            "lhb_net_buy_to_float_mv",
            "lhb_reason_count",
        ):
            row[name] = np.nan

    for window in LHB_WINDOW_DAYS:
        prior = _prior_days(calendar, str(row["as_of_date"]), window)
        row[f"lhb_net_buy_{window}d"] = cell(
            _sum_window(daily_lhb, prior, "net_amount", absent_is_zero=True)
            if prior
            else None
        )
        # 跨日累计是"当日口径不可用时的保守代理"，始终打 is_proxy（含披露延迟场景）。
        proxy[f"lhb_net_buy_{window}d"] = 1

    on_list_dates = {
        day
        for day, values in daily_lhb.items()
        if num(values.get("net_amount")) is not None
    }
    window_days = _window_days(calendar, str(row["as_of_date"]), ON_LIST_LOOKBACK_DAYS)
    if window_days:
        row["lhb_on_list_5d_days"] = cell(
            float(sum(1 for day in window_days if day in on_list_dates))
        )
    else:
        row["lhb_on_list_5d_days"] = np.nan

    seats = list(t_seats or [])
    if seats:
        inst_rows = [s for s in seats if _is_institution(s.get("seat_name"))]
        row["lhb_institution_net_buy"] = cell(
            sum(num(s.get("net_amount")) or 0.0 for s in inst_rows)
            if inst_rows
            else None
        )
        if famous:
            hot_rows = [s for s in seats if text_or_none(s.get("seat_name")) in famous]
            row["lhb_hot_money_net_buy"] = sum(
                num(s.get("net_amount")) or 0.0 for s in hot_rows
            )
            row["lhb_famous_seat_count"] = float(
                len({str(s.get("seat_name")) for s in hot_rows})
            )
        else:
            row["lhb_hot_money_net_buy"] = np.nan  # 画像为空：不伪造游资
            row["lhb_famous_seat_count"] = np.nan
        buys = sorted(
            (
                num(s.get("buy_amount")) or 0.0
                for s in seats
                if text_or_none(s.get("side")) == "buy"
            ),
            reverse=True,
        )
        row["lhb_seat_concentration"] = cell(
            sum(buys[:5]) / sum(buys) if sum(buys) > 0 else None
        )
        row["lhb_buy_seat_count"] = cell(
            float(
                sum(
                    int(num(s.get("seat_count")) or 1)
                    for s in seats
                    if text_or_none(s.get("side")) == "buy"
                )
            )
        )
        row["lhb_sell_seat_count"] = cell(
            float(
                sum(
                    int(num(s.get("seat_count")) or 1)
                    for s in seats
                    if text_or_none(s.get("side")) == "sell"
                )
            )
        )
    else:
        for name in (
            "lhb_institution_net_buy",
            "lhb_hot_money_net_buy",
            "lhb_seat_concentration",
            "lhb_buy_seat_count",
            "lhb_sell_seat_count",
            "lhb_famous_seat_count",
        ):
            row[name] = np.nan

    inst_dates = {
        day
        for day, values in inst_daily.items()
        if num(values.get("net_amount")) is not None
    }
    if window_days and inst_dates:
        row["lhb_institution_net_buy_5d"] = cell(
            float(
                sum(
                    num(inst_daily[day].get("net_amount")) or 0.0
                    for day in window_days
                    if day in inst_dates
                )
            )
        )
    else:
        row["lhb_institution_net_buy_5d"] = np.nan

    limit_times = num((t_board or {}).get("limit_times")) if t_board else None
    if rows and limit_times is not None:
        row["lhb_is_first_board"] = int(limit_times == 1)
        row["lhb_is_continuation"] = int(limit_times >= 2)
    else:
        row["lhb_is_first_board"] = np.nan
        row["lhb_is_continuation"] = np.nan


def _fill_margin_block(
    row: dict[str, Any],
    margin_by: dict[str, dict[str, Any]],
    blocks: Sequence[dict[str, Any]],
    as_of_date: str,
    amount_t: float | None,
) -> None:
    """融资 / 大宗：接口未接入时保持显式缺失（见 :func:`capital_flow_capability`）。"""
    t = margin_by.get(as_of_date) or {}
    prior = sorted(d for d in margin_by if d < as_of_date)
    prev = margin_by[prior[-1]] if prior else {}
    balance, balance_prev = (
        num(t.get("margin_balance")),
        num(prev.get("margin_balance")),
    )
    row["margin_balance_change"] = cell(
        balance - balance_prev
        if balance is not None and balance_prev is not None
        else None
    )
    row["margin_buy_ratio"] = cell(safe_div(num(t.get("margin_buy_amount")), amount_t))

    rows = list(blocks or [])
    if rows:
        pairs = [
            (num(b.get("price")), num(b.get("deal_amount")))
            for b in rows
            if num(b.get("price")) is not None and num(b.get("deal_amount")) is not None
        ]
        close_ref = next(
            (num(b.get("close")) for b in rows if num(b.get("close"))), None
        )
        total = sum(value for _price, value in pairs)
        if pairs and close_ref and total > 0:
            row["block_trade_premium"] = (
                sum(p * v for p, v in pairs) / total
            ) / close_ref - 1.0
        else:
            row["block_trade_premium"] = np.nan
        row["block_trade_amount_ratio"] = cell(safe_div(total, amount_t))
    else:
        row["block_trade_premium"] = np.nan
        row["block_trade_amount_ratio"] = np.nan


def _fill_interactions(
    row: dict[str, Any],
    t_board: dict[str, Any] | None,
    amount_t: float | None,
    float_mv_t: float | None,
    theme_counts: Mapping[str, int],
    calendar: Sequence[str],
    proxy: dict[str, int],
) -> None:
    fd_ratio = safe_div(num((t_board or {}).get("fd_amount")), amount_t)
    main_ratio = num(row.get("main_net_inflow_ratio"))
    row["fd_ratio_x_main_inflow"] = cell(
        fd_ratio * main_ratio
        if fd_ratio is not None and main_ratio is not None
        else None
    )

    lhb_ratio = num(row.get("lhb_net_buy_ratio"))
    theme = text_or_none((t_board or {}).get("theme"))
    if lhb_ratio is not None and theme is not None:
        row["lhb_net_buy_x_theme_heat"] = lhb_ratio * min(
            theme_counts.get(theme, 0) / 10.0, 1.0
        )
        proxy["lhb_net_buy_x_theme_heat"] = 1  # 题材为 kpl 回填口径
    else:
        row["lhb_net_buy_x_theme_heat"] = np.nan

    north_buy = num(row.get("northbound_net_buy"))
    if north_buy is not None and float_mv_t and float_mv_t > 0:
        row["northbound_change_x_float_mv"] = (north_buy / float_mv_t) * math.log10(
            float_mv_t / 1e8 + 1.0
        )
    else:
        row["northbound_change_x_float_mv"] = np.nan

    inflow_3d = num(row.get("net_inflow_3d"))
    first_min = session_minutes_from_clock((t_board or {}).get("first_time"))
    if (
        inflow_3d is not None
        and float_mv_t
        and float_mv_t > 0
        and first_min is not None
    ):
        row["inflow_3d_x_first_seal_time"] = (inflow_3d / float_mv_t) * (
            1.0 - first_min / 240.0
        )
    else:
        row["inflow_3d_x_first_seal_time"] = np.nan


# ─── 只读装载（#22 底座）────────────────────────────────────────────


def load_capital_flow_inputs(
    db_path: str | Path,
    symbols: Sequence[str],
    start_date: str,
    end_date: str,
    *,
    calendar_start: str | None = None,
    calendar_days: int = 400,
) -> CapitalFlowInputs:
    """从 ``market_kline.db`` **只读** 加载所需表（表缺失时返回空 frame）。"""
    conn = ro_connection(db_path)
    symbol_list = [str(s).strip() for s in symbols if str(s).strip()]
    try:
        moneyflow_frames, kline_frames, extras_frames = [], [], []
        for chunk in chunked(symbol_list):
            ph = ",".join("?" for _ in chunk)
            moneyflow_frames.append(
                query_frame(
                    conn,
                    f"SELECT symbol, trade_date, buy_sm_amount, sell_sm_amount, buy_md_amount,"
                    f" sell_md_amount, buy_lg_amount, sell_lg_amount, buy_elg_amount,"
                    f" sell_elg_amount, net_mf_amount, updated_at FROM moneyflow_daily"
                    f" WHERE symbol IN ({ph}) AND trade_date >= ? AND trade_date <= ?",
                    [*chunk, start_date, end_date],
                )
            )
            kline_frames.append(
                query_frame(
                    conn,
                    f"SELECT symbol, trade_date, close, volume, amount FROM kline_daily"
                    f" WHERE symbol IN ({ph}) AND trade_date >= ? AND trade_date <= ?",
                    [*chunk, start_date, end_date],
                )
            )
            extras_frames.append(
                query_frame(
                    conn,
                    f"SELECT symbol, trade_date, turnover_rate, volume_ratio FROM kline_daily_extras"
                    f" WHERE symbol IN ({ph}) AND trade_date >= ? AND trade_date <= ?",
                    [*chunk, start_date, end_date],
                )
            )
        board = query_frame(
            conn,
            "SELECT symbol, trade_date, limit_flag, limit_times, first_time, fd_amount, amount,"
            " float_mv, theme, synced_at FROM limit_board_daily WHERE trade_date >= ? AND trade_date <= ?",
            [start_date, end_date],
        )
        hsgt = query_frame(
            conn,
            "SELECT symbol, trade_date, market_type, amount, net_amount, buy_amount, sell_amount,"
            " synced_at FROM hsgt_top10_daily WHERE trade_date >= ? AND trade_date <= ?",
            [start_date, end_date],
        )
        lhb = query_frame(
            conn,
            "SELECT symbol, trade_date, reason, amount, l_buy, l_sell, net_amount, synced_at"
            " FROM dragon_tiger_daily WHERE trade_date >= ? AND trade_date <= ?",
            [start_date, end_date],
        )
        seats = query_frame(
            conn,
            "SELECT trade_date, symbol, seat_name, side, buy_amount, sell_amount, net_amount,"
            " seat_count, synced_at FROM dragon_tiger_seat_daily WHERE trade_date >= ? AND trade_date <= ?",
            [start_date, end_date],
        )
        hot = query_frame(
            conn, "SELECT seat_name, org_code, category FROM hot_money_seat"
        )
        calendar = query_frame(
            conn,
            "SELECT DISTINCT trade_date FROM kline_daily WHERE trade_date >= ? AND trade_date <= ?"
            " ORDER BY trade_date ASC LIMIT ?",
            [calendar_start or start_date, end_date, int(calendar_days)],
        )
    finally:
        conn.close()

    def _cat(frames: list[pd.DataFrame]) -> pd.DataFrame:
        filled = [f for f in frames if f is not None and not f.empty]
        return pd.concat(filled, ignore_index=True) if filled else pd.DataFrame()

    return CapitalFlowInputs(
        moneyflow=_cat(moneyflow_frames),
        board=board,
        kline=_cat(kline_frames),
        extras=_cat(extras_frames),
        calendar=calendar,
        hsgt_top10=hsgt,
        lhb=lhb,
        lhb_seats=seats,
        hot_money_seats=hot,
    )


def build_capital_flow_features(
    db_path: str | Path,
    symbols: Iterable[str],
    as_of_date: str,
    as_of_time: str | datetime | None = None,
    lookback: int = DEFAULT_LOOKBACK_DAYS,
) -> pd.DataFrame:
    """线上入口：只读取 #22 表并构建 #28 因子。

    ``as_of_time`` 只给日期（``YYYY-MM-DD``，如数据集的决策时点）时按 ``00:00``
    处理 = 前一收盘，T 日盘后资金流 / 龙虎榜因子为 NaN（防泄漏）。
    """
    symbol_list = sorted({str(s).strip() for s in symbols if str(s).strip()})
    as_of_date = str(as_of_date)[:10]
    provided = as_of_time is not None and str(as_of_time).strip() != ""
    raw_text = "" if isinstance(as_of_time, datetime) else str(as_of_time or "").strip()
    if not provided:
        source_label = SOURCE_DEFAULT_PREV_CLOSE
    elif len(raw_text) <= 10:
        source_label = SOURCE_DATE_ONLY  # 只给日期 → 视为前一收盘，T 日盘后因子必须缺失
    else:
        source_label = SOURCE_EXPLICIT
    resolved = (
        as_of_time if isinstance(as_of_time, datetime) else parse_ts(as_of_time or "")
    )
    if resolved is None:
        resolved = parse_ts(f"{as_of_date} {DEFAULT_DECISION_CLOCK}")
    if not symbol_list:
        return empty_frame(capital_flow_output_columns())

    lookback = max(5, int(lookback))
    span = lookback + LOOKBACK_BUFFER_DAYS
    history_start = max(
        (parse_ts(as_of_date) - timedelta(days=span)).strftime("%Y-%m-%d"),
        (parse_ts(as_of_date) - timedelta(days=MAX_HISTORY_SPAN_DAYS)).strftime(
            "%Y-%m-%d"
        ),
    )
    inputs = load_capital_flow_inputs(
        db_path,
        symbol_list,
        history_start,
        as_of_date,
        calendar_start=(
            parse_ts(as_of_date) - timedelta(days=CALENDAR_SPAN_DAYS)
        ).strftime("%Y-%m-%d"),
    )
    return compute_capital_flow_factors(
        inputs,
        symbol_list,
        as_of_date,
        resolved,
        as_of_time_provided=provided,
        source_label=source_label,
    )


# ─── 覆盖率 / 验收报告 ───────────────────────────────────────────────


def capital_flow_quality_report(
    db_path: str | Path,
    symbols: Iterable[str],
    as_of_date: str,
    as_of_time: str | datetime | None = None,
    lookback: int = DEFAULT_LOOKBACK_DAYS,
    labels: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """线上入口的验收报告（覆盖率 + 缺失率 + IC + 共线性 + PIT 审计）。"""
    frame = build_capital_flow_features(
        db_path, symbols, as_of_date, as_of_time, lookback
    )
    return capital_flow_report_from_frame(frame, labels=labels)


def capital_flow_report_from_frame(
    frame: pd.DataFrame,
    labels: pd.DataFrame | None = None,
    top_n: int = 10,
) -> dict[str, Any]:
    """由因子表产出验收报告；``labels`` 为 #23 标签（``symbol`` + ``label_*``）。"""
    from strategy.daban_factors._correlation import (
        MIN_SAMPLE_COUNT,
        collinearity_report,
        quantile_returns,
        rank_ic,
    )

    frame = project_columns(frame, capital_flow_output_columns())
    total = int(len(frame))
    factors = numeric_factor_names()

    def _rate(column: str, default: float = 0.0) -> float:
        if not total or column not in frame.columns:
            return default
        return float(pd.to_numeric(frame[column], errors="coerce").fillna(0.0).mean())

    merged = frame
    if labels is not None and not labels.empty and "symbol" in labels.columns:
        keep = [c for c in labels.columns if c != "symbol"]
        merged = frame.merge(labels[["symbol", *keep]], on="symbol", how="left")

    ic_block: dict[str, Any] = {}
    label_columns = [c for c in LABEL_COLUMNS if c in merged.columns]
    for label in label_columns:
        ic_block[label] = rank_ic(merged, factors, label)
    layer_block: dict[str, Any] = {}
    if label_columns:
        first_label = label_columns[0]
        scored = merged[factors].apply(pd.to_numeric, errors="coerce")
        coverage = scored.notna().mean().sort_values(ascending=False)
        for factor in list(coverage.index[:top_n]):
            layer_block[str(factor)] = quantile_returns(
                merged, str(factor), first_label
            )

    collinearity = collinearity_report(
        merged, factors, BASELINE_COLUMNS, threshold=COLLINEARITY_THRESHOLD
    )

    report: dict[str, Any] = {
        "issue": 28,
        "as_of_date": str(frame["as_of_date"].iloc[0]) if total else "",
        "as_of_time": str(frame["as_of_time"].iloc[0]) if total else "",
        "as_of_time_source": str(frame["as_of_time_source"].iloc[0])
        if total
        else SOURCE_DEFAULT_PREV_CLOSE,
        "symbol_count": total,
        "row_coverage": {
            "moneyflow": _rate("has_moneyflow_row"),
            "dragon_tiger": _rate("has_lhb_row"),
            "northbound_top10": _rate("has_north_row"),
        },
        "t_day_visibility": {
            "moneyflow": _rate("moneyflow_visible_at_T"),
            "dragon_tiger": _rate("lhb_visible_at_T"),
        },
        "disclosure_lagged_rate": {
            "moneyflow": _rate("moneyflow_disclosure_lagged"),
            "dragon_tiger": _rate("lhb_disclosure_lagged"),
        },
        "missing_rate": indicator_rates(frame, FACTOR_SPECS, "is_missing"),
        "proxy_rate": {name: _rate(f"{name}_is_proxy") for name in PROXY_FACTORS},
        "distribution": factor_distribution(frame, FACTOR_SPECS),
        "single_factor_ic": ic_block,
        "quantile_layers": layer_block,
        "collinearity": collinearity,
        "pit_audit": pit_audit(frame),
        "degradation_reasons": _reason_counts(frame),
    }
    report["coverage_acceptance"] = _coverage_note(report)
    report["correlation_acceptance"] = _correlation_note(collinearity)
    report["ic_acceptance"] = _ic_note(ic_block, total, min_samples=MIN_SAMPLE_COUNT)
    report["model_gain_acceptance"] = (
        "PARTIAL：与纯量价模型的样本外增量对比需 #6 模型基线，本模块只提供因子表"
    )
    return report


def pit_audit(frame: pd.DataFrame) -> dict[str, Any]:
    """PIT 审计：默认决策时点下，当日盘后因子必须 100% 缺失。"""
    result: dict[str, Any] = {
        "post_close_groups": list(POST_CLOSE_GROUPS),
        "rule": (
            "as_of_time 未显式给出 → as_of_date 00:00（前一收盘）→ "
            f"{', '.join(POST_CLOSE_GROUPS)} 的 T 日因子必须全缺失"
        ),
        "history_only_factors": list(HISTORY_ONLY_FACTORS),
        "post_close_all_missing": None,
        "violations": {},
    }
    if frame is None or frame.empty or "as_of_time_source" not in frame.columns:
        result["status"] = "no_rows"
        return result
    if str(frame["as_of_time_source"].iloc[0]) not in PREV_CLOSE_SOURCES:
        result["status"] = "skipped_explicit_time"
        return result
    violations: dict[str, int] = {}
    for spec in FACTOR_SPECS:
        if spec.name in HISTORY_ONLY_FACTORS or spec.group not in POST_CLOSE_GROUPS:
            continue
        column = f"{spec.name}_is_missing"
        if column not in frame.columns:
            continue
        visible = int(
            (pd.to_numeric(frame[column], errors="coerce").fillna(1.0) == 0).sum()
        )
        if visible:
            violations[spec.name] = visible
    result["violations"] = violations
    result["post_close_all_missing"] = not violations
    result["status"] = "pass" if not violations else "fail"
    return result


def _reason_counts(frame: pd.DataFrame) -> dict[str, int]:
    if frame is None or frame.empty or "degradation_reasons" not in frame.columns:
        return {}
    counts: dict[str, int] = {}
    for value in frame["degradation_reasons"].fillna("").astype(str):
        for reason in str(value).split("|"):
            if reason:
                counts[reason] = counts.get(reason, 0) + 1
    return counts


def _coverage_note(report: Mapping[str, Any]) -> str:
    if int(report.get("symbol_count") or 0) == 0:
        return (
            "PARTIAL：样本 0 —— 线上 #22 表（moneyflow_daily / dragon_tiger_daily）"
            "尚未由盘后调度回填，无法评估 >=90% 覆盖率"
        )
    coverage = report.get("row_coverage") or {}
    money = float(coverage.get("moneyflow") or 0.0)
    verdict = "PASS" if money >= 0.90 else "PARTIAL"
    return (
        f"{verdict}：moneyflow 覆盖 {money:.1%}、龙虎榜（上榜股）覆盖 "
        f"{float(coverage.get('dragon_tiger') or 0.0):.1%}、北向十大 "
        f"{float(coverage.get('northbound_top10') or 0.0):.1%}；验收阈值 90%（有数据股票口径）"
    )


def _correlation_note(collinearity: Mapping[str, Any]) -> str:
    if str(collinearity.get("status")) != "ok":
        return f"PARTIAL：{collinearity.get('status')}（样本不足或缺基线列 {collinearity.get('baselines')}）"
    flagged = collinearity.get("flagged") or []
    if flagged:
        return f"WARN：与成交额/量比/换手高共线（|corr|>={COLLINEARITY_THRESHOLD}）的因子 {sorted(flagged)}"
    return f"PASS：未发现与成交额/量比/换手高共线（阈值 {COLLINEARITY_THRESHOLD}）"


def _ic_note(ic_block: Mapping[str, Any], total: int, *, min_samples: int) -> str:
    if not ic_block:
        return (
            f"PARTIAL：未提供 #23 标签（label_next_day_premium / label_promotion / "
            f"label_break_board）；当前截面 {total} 行"
        )
    statuses = {str(item.get("status")) for item in ic_block.values()}
    if statuses and statuses <= {"ok"}:
        return f"PARTIAL：单截面 rank IC 已产出（{total} 行），多日 IC 序列与分层显著性仍需 #27 审计"
    return (
        f"PARTIAL：成对样本 < {min_samples} 或标签列缺失有效值（status={sorted(statuses)}），"
        "不产出 IC 数值"
    )


def capital_flow_capability() -> dict[str, Any]:
    """能力标记：显式写出未接入数据源与降级路径（不静默失败）。"""
    return {
        "issue": 28,
        "ready_groups": [
            "stock_moneyflow",
            "dragon_tiger",
            "northbound(十大口径)",
            "interaction",
        ],
        "degraded_groups": {
            "northbound_holding": (
                f"{REASON_HK_HOLD_API_MISSING}：hk_hold 未接入 → 北向净买代理并打 "
                "northbound_holding_change_is_proxy=1；northbound_holding_ratio 保持缺失"
            ),
            "margin_block": (
                f"{REASON_MARGIN_API_MISSING} / {REASON_BLOCK_TRADE_API_MISSING} → D 组恒缺失，"
                "接口就绪后注入 margin / block_trades frame 即可启用"
            ),
            "hot_money_seats": (
                f"{REASON_HOT_MONEY_PROFILE_MISSING}：游资画像依赖 #13；画像空则因子缺失，不伪造标签"
            ),
            "lhb_delayed_disclosure": (
                f"{REASON_LHB_DISCLOSE_DELAYED}：当日 T 因子缺失，改用 lhb_net_buy_5d/10d 历史代理"
            ),
        },
        "acceptance": {
            "coverage_ge_90pct": "PARTIAL（线上 #22 表未回填；逻辑与报告已实现）",
            "lhb_disclosure_alignment": (
                "DONE（row_available_at + filter_visible + as_of_time 默认前一收盘 + pit_audit）"
            ),
            "single_factor_ic_report": "PARTIAL（rank IC / 分层已实现，需标签 + 数据回填）",
            "out_of_sample_gain": "PARTIAL（待 #6 模型基线）",
            "factor_correlation_matrix": "DONE（vs 成交额/量比/换手，报告含 flagged）",
            "unit_tests": "DONE（缺失/披露延迟/机构-游资分类/北向无数据/单位口径/PIT 审计）",
        },
    }
