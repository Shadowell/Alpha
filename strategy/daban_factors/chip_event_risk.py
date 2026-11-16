"""#31 筹码、解禁与事件风险因子族（纯计算 + PIT 公告日截断 + 负面过滤）。

覆盖 4 组因子：
A. 筹码 / 股本：``float_mv``、``total_mv``、``free_share``、``turnover_rate``、
   ``volume_ratio``、``holder_count``、``holder_count_change_1q``、``holder_count_change_1y``、
   ``avg_holding_value``、``chip_concentration``、``profit_ratio``、``avg_cost``、
   ``cost_90_low``、``cost_90_high``、``chip_peak_distance``
B. 解禁 / 减持 / 质押：``unlock_days``、``unlock_amount_ratio``、``unlock_mv_ratio``、
   ``is_unlock_30d``、``is_unlock_90d``、``reduction_plan_flag``、``reduction_amount_ratio``、
   ``pledge_ratio``、``pledge_risk_flag``、``block_trade_discount``、``block_trade_amount_ratio``
C. 监管 / 财务风险：``regulatory_letter_flag``、``st_flag``、``delisting_risk_flag``、
   ``goodwill_ratio``、``goodwill_impairment_flag``、``earnings_warning_flag``、
   ``audit_opinion_flag``、``litigation_flag``、``penalty_flag``
D. 新股 / 次新：``listed_days``、``is_sub_new``、``ipo_limit_up_count``
"""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping

import numpy as np
import pandas as pd

from strategy.daban_factors._base import (
    FactorSpec,
    attach_indicators,
)

CHIP_EVENT_RISK_SPECS: tuple[FactorSpec, ...] = (
    FactorSpec("float_mv", "筹码/股本", "流通市值(元)", "元", "daily_basic", "缺失记NaN", "T日收盘可见"),
    FactorSpec("total_mv", "筹码/股本", "总市值(元)", "元", "daily_basic", "缺失记NaN", "T日收盘可见"),
    FactorSpec("free_share", "筹码/股本", "自由流通股本(股)", "股", "daily_basic", "缺失记NaN", "T日收盘可见"),
    FactorSpec("turnover_rate", "筹码/股本", "换手率(小数)", "无量纲", "daily_basic", "缺失记NaN", "T日收盘可见"),
    FactorSpec("volume_ratio", "筹码/股本", "量比", "无量纲", "daily_basic", "缺失记NaN", "T日收盘可见"),
    FactorSpec("holder_count", "筹码/股本", "最新已披露股东户数", "次", "stk_holdernumber", "未披露记NaN", "仅用ann_date<=trade_date"),
    FactorSpec("holder_count_change_1q", "筹码/股本", "股东户数环比上季变化率", "无量纲", "stk_holdernumber", "不足两期记NaN", "仅用ann_date<=trade_date"),
    FactorSpec("holder_count_change_1y", "筹码/股本", "股东户数同比去年变化率", "无量纲", "stk_holdernumber", "不足一年记NaN", "仅用ann_date<=trade_date"),
    FactorSpec("avg_holding_value", "筹码/股本", "户均持股市值=float_mv/holder_count", "元", "derived", "缺失记NaN", "仅用<=trade_date已披露数据"),
    FactorSpec("chip_concentration", "筹码/股本", "(cost_90_high-cost_90_low)/(2*avg_cost)", "无量纲", "cyq_perf", "缺失记NaN", "T日收盘可见"),
    FactorSpec("profit_ratio", "筹码/股本", "获利盘比例(0~1)", "无量纲", "cyq_perf", "缺失记NaN", "T日收盘可见"),
    FactorSpec("avg_cost", "筹码/股本", "加权平均持仓成本", "无量纲", "cyq_perf", "缺失记NaN", "T日收盘可见"),
    FactorSpec("cost_90_low", "筹码/股本", "90%筹码成本下沿", "无量纲", "cyq_perf", "缺失记NaN", "T日收盘可见"),
    FactorSpec("cost_90_high", "筹码/股本", "90%筹码成本上沿", "无量纲", "cyq_perf", "缺失记NaN", "T日收盘可见"),
    FactorSpec("chip_peak_distance", "筹码/股本", "(close-avg_cost)/avg_cost", "无量纲", "derived", "缺失记NaN", "T日收盘可见"),
    FactorSpec("unlock_days", "解禁/减持/质押", "距最近未来解禁日自然日数", "天", "share_float", "无解禁记NaN", "仅用ann_date<=trade_date"),
    FactorSpec("unlock_amount_ratio", "解禁/减持/质押", "解禁股数/总股本", "无量纲", "share_float", "无解禁记NaN", "仅用ann_date<=trade_date"),
    FactorSpec("unlock_mv_ratio", "解禁/减持/质押", "解禁市值/流通市值", "无量纲", "share_float", "无解禁记NaN", "仅用ann_date<=trade_date"),
    FactorSpec("is_unlock_30d", "解禁/减持/质押", "未来30天内存在解禁(0/1)", "0/1", "share_float", "无解禁记0", "仅用ann_date<=trade_date"),
    FactorSpec("is_unlock_90d", "解禁/减持/质押", "未来90天内存在解禁(0/1)", "0/1", "share_float", "无解禁记0", "仅用ann_date<=trade_date"),
    FactorSpec("reduction_plan_flag", "解禁/减持/质押", "近90日披露减持计划(0/1)", "0/1", "events", "无事件记0", "仅用ann_date<=trade_date"),
    FactorSpec("reduction_amount_ratio", "解禁/减持/质押", "拟减持比例", "无量纲", "events", "无事件记NaN", "仅用ann_date<=trade_date"),
    FactorSpec("pledge_ratio", "解禁/减持/质押", "大股东质押比例(0~1)", "无量纲", "pledge_stat", "缺失记NaN", "仅用ann_date<=trade_date"),
    FactorSpec("pledge_risk_flag", "解禁/减持/质押", "pledge_ratio>=0.5(0/1)", "0/1", "pledge_stat", "缺失记NaN", "仅用ann_date<=trade_date"),
    FactorSpec("block_trade_discount", "解禁/减持/质押", "最近大宗交易折价率", "无量纲", "block_trade", "缺失记NaN", "仅用trade_date<=as_of"),
    FactorSpec("block_trade_amount_ratio", "解禁/减持/质押", "近20日大宗交易成交额/流通市值", "无量纲", "block_trade", "缺失记NaN", "仅用<=trade_date"),
    FactorSpec("regulatory_letter_flag", "监管/财务风险", "近60日监管函/问询函/关注函(0/1)", "0/1", "events", "缺失记0", "仅用ann_date<=trade_date"),
    FactorSpec("st_flag", "监管/财务风险", "ST/*ST标志(0/1)", "0/1", "stock_basic", "缺失记NaN", "T日可见"),
    FactorSpec("delisting_risk_flag", "监管/财务风险", "退市风险警示标志(0/1)", "0/1", "stock_basic", "缺失记0", "T日可见"),
    FactorSpec("goodwill_ratio", "监管/财务风险", "商誉/净资产", "无量纲", "balancesheet", "缺失记NaN", "仅用f_ann_date<=trade_date"),
    FactorSpec("goodwill_impairment_flag", "监管/财务风险", "商誉减值风险(goodwill_ratio>=0.3)(0/1)", "0/1", "balancesheet", "缺失记NaN", "仅用f_ann_date<=trade_date"),
    FactorSpec("earnings_warning_flag", "监管/财务风险", "近60日业绩预亏/预减/暴雷(0/1)", "0/1", "forecast", "缺失记0", "仅用ann_date<=trade_date"),
    FactorSpec("audit_opinion_flag", "监管/财务风险", "非标审计意见标志(0/1)", "0/1", "fina_audit", "缺失记0", "仅用ann_date<=trade_date"),
    FactorSpec("litigation_flag", "监管/财务风险", "近90日重大诉讼标志(0/1)", "0/1", "events", "缺失记0", "仅用ann_date<=trade_date"),
    FactorSpec("penalty_flag", "监管/财务风险", "近90日行政处罚标志(0/1)", "0/1", "events", "缺失记0", "仅用ann_date<=trade_date"),
    FactorSpec("listed_days", "新股/次新", "上市自然日数", "天", "stock_basic", "缺list_date记NaN", "T日可见"),
    FactorSpec("is_sub_new", "新股/次新", "上市不满365天(0/1)", "0/1", "stock_basic", "缺list_date记NaN", "T日可见"),
    FactorSpec("ipo_limit_up_count", "新股/次新", "上市初期连续涨停次数", "次", "limit_board_daily", "缺失记NaN", "仅用<=trade_date历史"),
)

CHIP_EVENT_FACTOR_NAMES: tuple[str, ...] = tuple(
    spec.name for spec in CHIP_EVENT_RISK_SPECS
)


def chip_event_risk_factor_specs() -> tuple[FactorSpec, ...]:
    return CHIP_EVENT_RISK_SPECS


def chip_event_risk_capability() -> dict[str, Any]:
    return {
        "issue": 31,
        "status": "READY",
        "factor_count": len(CHIP_EVENT_RISK_SPECS),
        "pit_guaranteed": True,
        "notes": "支持筹码/股东户数/解禁减持质押/监管财务风险 PIT 计算与负面风险过滤",
    }


def _parse_date(val: Any) -> date | None:
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return None
    s = str(val).strip()[:10]
    if not s or s.lower() == "nan":
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        if len(s) == 8 and s.isdigit():
            try:
                return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
            except ValueError:
                return None
        return None


def compute_chip_event_risk_factors(
    base_df: pd.DataFrame,
    *,
    holder_df: pd.DataFrame | None = None,
    unlock_df: pd.DataFrame | None = None,
    risk_events_df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """计算 #31 筹码、解禁与事件风险因子族（严格按 ``ann_date <= trade_date`` 截断）。"""
    if base_df is None or base_df.empty:
        return pd.DataFrame(columns=["symbol", "trade_date", *CHIP_EVENT_FACTOR_NAMES])

    df = base_df.copy()
    df["symbol"] = df["symbol"].astype(str)
    df["trade_date"] = df["trade_date"].astype(str)

    holders = holder_df.copy() if holder_df is not None and not holder_df.empty else pd.DataFrame()
    if not holders.empty:
        holders["symbol"] = holders["symbol"].astype(str)
        holders["ann_date"] = holders["ann_date"].astype(str)

    unlocks = unlock_df.copy() if unlock_df is not None and not unlock_df.empty else pd.DataFrame()
    if not unlocks.empty:
        unlocks["symbol"] = unlocks["symbol"].astype(str)
        if "ann_date" in unlocks.columns:
            unlocks["ann_date"] = unlocks["ann_date"].astype(str)
        unlocks["unlock_date"] = unlocks["unlock_date"].astype(str)

    events = risk_events_df.copy() if risk_events_df is not None and not risk_events_df.empty else pd.DataFrame()
    if not events.empty:
        events["symbol"] = events["symbol"].astype(str)
        events["ann_date"] = events["ann_date"].astype(str)

    rows: list[dict[str, Any]] = []
    for _, r in df.iterrows():
        sym = str(r["symbol"])
        dt_str = str(r["trade_date"])
        dt_obj = _parse_date(dt_str)

        rec: dict[str, Any] = {"symbol": sym, "trade_date": dt_str}
        for col in CHIP_EVENT_FACTOR_NAMES:
            rec[col] = np.nan

        # A. 筹码 / 股本直接字段
        for col in (
            "float_mv",
            "total_mv",
            "free_share",
            "turnover_rate",
            "volume_ratio",
            "profit_ratio",
            "avg_cost",
            "cost_90_low",
            "cost_90_high",
            "pledge_ratio",
            "block_trade_discount",
            "block_trade_amount_ratio",
            "goodwill_ratio",
            "ipo_limit_up_count",
        ):
            val = pd.to_numeric(r.get(col, np.nan), errors="coerce")
            if pd.notna(val):
                rec[col] = float(val)

        if pd.notna(rec["cost_90_high"]) and pd.notna(rec["cost_90_low"]) and pd.notna(rec["avg_cost"]) and rec["avg_cost"] > 0:
            rec["chip_concentration"] = (rec["cost_90_high"] - rec["cost_90_low"]) / (2.0 * rec["avg_cost"])
        close_p = pd.to_numeric(r.get("close", np.nan), errors="coerce")
        if pd.notna(close_p) and pd.notna(rec["avg_cost"]) and rec["avg_cost"] > 0:
            rec["chip_peak_distance"] = (float(close_p) - rec["avg_cost"]) / rec["avg_cost"]

        # 股东户数（严格 ann_date <= dt_str）
        if not holders.empty:
            h_sub = holders[(holders["symbol"] == sym) & (holders["ann_date"] <= dt_str)].sort_values("ann_date")
            if not h_sub.empty:
                hc = pd.to_numeric(h_sub["holder_count"], errors="coerce").dropna()
                if not hc.empty:
                    latest_hc = float(hc.iloc[-1])
                    rec["holder_count"] = latest_hc
                    if pd.notna(rec["float_mv"]) and latest_hc > 0:
                        rec["avg_holding_value"] = rec["float_mv"] / latest_hc
                    if len(hc) >= 2 and hc.iloc[-2] > 0:
                        rec["holder_count_change_1q"] = (latest_hc - float(hc.iloc[-2])) / float(hc.iloc[-2])
                    if len(hc) >= 5 and hc.iloc[-5] > 0:
                        rec["holder_count_change_1y"] = (latest_hc - float(hc.iloc[-5])) / float(hc.iloc[-5])

        # B. 解禁（严格 ann_date <= dt_str 且 unlock_date >= dt_str）
        rec["is_unlock_30d"] = 0.0
        rec["is_unlock_90d"] = 0.0
        if not unlocks.empty and dt_obj is not None:
            u_sub = unlocks[(unlocks["symbol"] == sym) & (unlocks["unlock_date"] >= dt_str)]
            if "ann_date" in u_sub.columns:
                u_sub = u_sub[u_sub["ann_date"] <= dt_str]
            if not u_sub.empty:
                u_sub = u_sub.sort_values("unlock_date")
                nearest = u_sub.iloc[0]
                u_date = _parse_date(nearest["unlock_date"])
                if u_date is not None:
                    days = float((u_date - dt_obj).days)
                    rec["unlock_days"] = days
                    rec["is_unlock_30d"] = 1.0 if days <= 30 else 0.0
                    rec["is_unlock_90d"] = 1.0 if days <= 90 else 0.0
                u_ratio = pd.to_numeric(nearest.get("unlock_amount_ratio", np.nan), errors="coerce")
                u_mv_ratio = pd.to_numeric(nearest.get("unlock_mv_ratio", np.nan), errors="coerce")
                if pd.notna(u_ratio):
                    rec["unlock_amount_ratio"] = float(u_ratio)
                if pd.notna(u_mv_ratio):
                    rec["unlock_mv_ratio"] = float(u_mv_ratio)

        if pd.notna(rec["pledge_ratio"]):
            rec["pledge_risk_flag"] = 1.0 if rec["pledge_ratio"] >= 0.5 else 0.0
        if pd.notna(rec["goodwill_ratio"]):
            rec["goodwill_impairment_flag"] = 1.0 if rec["goodwill_ratio"] >= 0.3 else 0.0

        # C. ST / 退市 / 风险事件（严格 ann_date <= dt_str）
        name_val = str(r.get("name", ""))
        if "st_flag" in df.columns and pd.notna(r.get("st_flag")):
            rec["st_flag"] = 1.0 if bool(r.get("st_flag")) else 0.0
        elif name_val:
            rec["st_flag"] = 1.0 if "ST" in name_val.upper() else 0.0

        rec["delisting_risk_flag"] = 1.0 if ("退" in name_val or bool(r.get("delisting_risk_flag", False))) else 0.0
        rec["reduction_plan_flag"] = 0.0
        rec["regulatory_letter_flag"] = 0.0
        rec["earnings_warning_flag"] = 0.0
        rec["audit_opinion_flag"] = 0.0
        rec["litigation_flag"] = 0.0
        rec["penalty_flag"] = 0.0

        if not events.empty and dt_obj is not None:
            e_sub = events[(events["symbol"] == sym) & (events["ann_date"] <= dt_str)]
            for _, ev in e_sub.iterrows():
                ev_date = _parse_date(ev["ann_date"])
                if ev_date is None:
                    continue
                age_d = (dt_obj - ev_date).days
                etype = str(ev.get("event_type", "")).lower()
                if etype == "reduction" and age_d <= 90:
                    rec["reduction_plan_flag"] = 1.0
                    amt_r = pd.to_numeric(ev.get("amount_ratio", np.nan), errors="coerce")
                    if pd.notna(amt_r):
                        rec["reduction_amount_ratio"] = float(amt_r)
                elif etype in ("regulatory_letter", "inquiry") and age_d <= 60:
                    rec["regulatory_letter_flag"] = 1.0
                elif etype in ("earnings_warning", "loss_forecast") and age_d <= 60:
                    rec["earnings_warning_flag"] = 1.0
                elif etype == "audit_qualified" and age_d <= 365:
                    rec["audit_opinion_flag"] = 1.0
                elif etype == "litigation" and age_d <= 90:
                    rec["litigation_flag"] = 1.0
                elif etype == "penalty" and age_d <= 90:
                    rec["penalty_flag"] = 1.0

        # D. 上市天数 / 次新
        list_d = _parse_date(r.get("list_date"))
        if list_d is not None and dt_obj is not None:
            ldays = float(max(0, (dt_obj - list_d).days))
            rec["listed_days"] = ldays
            rec["is_sub_new"] = 1.0 if ldays < 365.0 else 0.0

        rows.append(attach_indicators(rec, CHIP_EVENT_RISK_SPECS))

    return pd.DataFrame(rows)


def evaluate_risk_filter(row: Mapping[str, Any]) -> tuple[bool, list[str]]:
    """负面风险过滤：返回 ``(passed, reasons)``。"""
    reasons: list[str] = []
    if row.get("st_flag") == 1.0:
        reasons.append("st_flag")
    if row.get("delisting_risk_flag") == 1.0:
        reasons.append("delisting_risk")
    if row.get("is_unlock_30d") == 1.0 and (row.get("unlock_amount_ratio") or 0.0) >= 0.05:
        reasons.append("major_unlock_within_30d")
    if row.get("pledge_risk_flag") == 1.0:
        reasons.append("high_pledge_risk")
    if row.get("regulatory_letter_flag") == 1.0:
        reasons.append("regulatory_letter")
    if row.get("earnings_warning_flag") == 1.0:
        reasons.append("earnings_warning")
    if row.get("audit_opinion_flag") == 1.0:
        reasons.append("audit_opinion_qualified")
    if row.get("penalty_flag") == 1.0:
        reasons.append("administrative_penalty")
    return len(reasons) == 0, reasons


__all__ = [
    "CHIP_EVENT_FACTOR_NAMES",
    "CHIP_EVENT_RISK_SPECS",
    "chip_event_risk_capability",
    "chip_event_risk_factor_specs",
    "compute_chip_event_risk_factors",
    "evaluate_risk_filter",
]
