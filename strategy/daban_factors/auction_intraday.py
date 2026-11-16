"""#30 集合竞价与盘中分时强度因子族（纯计算 + PIT 分时截断 + 代理降级标记）。

覆盖 3 组因子：
A. 集合竞价：``auction_gap_pct``、``auction_volume_ratio``、``auction_amount_ratio``、
   ``auction_turnover``、``auction_strength_rank``、``auction_weak_to_strong``、
   ``auction_strong_to_weak``
B. 开盘强度：``open_5m_return``、``open_15m_return``、``open_30m_return``、
   ``open_5m_amount_ratio``、``open_15m_amount_ratio``、``above_vwap``、``vwap_deviation``、
   ``first_touch_limit_time``、``first_seal_time``、``open_high_break``
C. 盘中结构：``intraday_high_time``、``intraday_low_time``、``tail_30m_return``、
   ``tail_30m_amount_ratio``、``seal_open_count``、``reseal_count``、
   ``on_board_amount_ratio``、``up_minute_ratio``、``intraday_max_drawdown``
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from strategy.daban_factors._base import (
    FactorSpec,
    attach_indicators,
)

AUCTION_INTRADAY_SPECS: tuple[FactorSpec, ...] = (
    FactorSpec("auction_gap_pct", "集合竞价", "(auction_price-pre_close)/pre_close", "无量纲", "stk_auction/daily_open", "缺失记NaN", "09:25可见", visible_from="09:25", is_proxy_capable=True),
    FactorSpec("auction_volume_ratio", "集合竞价", "auction_vol/prev_day_vol", "无量纲", "stk_auction+daily", "缺失记NaN", "09:25可见", visible_from="09:25", is_proxy_capable=True),
    FactorSpec("auction_amount_ratio", "集合竞价", "auction_amount/prev_day_amount", "无量纲", "stk_auction+daily", "缺失记NaN", "09:25可见", visible_from="09:25", is_proxy_capable=True),
    FactorSpec("auction_turnover", "集合竞价", "auction_vol/float_share", "无量纲", "stk_auction+daily_basic", "缺失记NaN", "09:25可见", visible_from="09:25", is_proxy_capable=True),
    FactorSpec("auction_strength_rank", "集合竞价", "全市场当日竞价涨幅降序排名", "序数", "stk_auction", "缺失记NaN", "09:25截面", visible_from="09:25", is_proxy_capable=True),
    FactorSpec("auction_weak_to_strong", "集合竞价", "昨日烂板/炸板且今日高开>=2%(0/1)", "0/1", "stk_auction+limit_board", "缺失记NaN", "09:25可见", visible_from="09:25"),
    FactorSpec("auction_strong_to_weak", "集合竞价", "昨日强板(一字/0炸板)且今日低开<0%(0/1)", "0/1", "stk_auction+limit_board", "缺失记NaN", "09:25可见", visible_from="09:25"),
    FactorSpec("open_5m_return", "开盘强度", "09:35收盘相对昨收涨幅", "无量纲", "kline_minute", "分钟线缺失记NaN", "09:35可见", visible_from="09:35"),
    FactorSpec("open_15m_return", "开盘强度", "09:45收盘相对昨收涨幅", "无量纲", "kline_minute", "分钟线缺失记NaN", "09:45可见", visible_from="09:45"),
    FactorSpec("open_30m_return", "开盘强度", "10:00收盘相对昨收涨幅", "无量纲", "kline_minute", "分钟线缺失记NaN", "10:00可见", visible_from="10:00"),
    FactorSpec("open_5m_amount_ratio", "开盘强度", "开盘5分钟成交额/昨日全天成交额", "无量纲", "kline_minute+daily", "缺失记NaN", "09:35可见", visible_from="09:35"),
    FactorSpec("open_15m_amount_ratio", "开盘强度", "开盘15分钟成交额/昨日全天成交额", "无量纲", "kline_minute+daily", "缺失记NaN", "09:45可见", visible_from="09:45"),
    FactorSpec("above_vwap", "开盘强度", "当前价>=分时均价VWAP(0/1)", "0/1", "kline_minute", "缺失记NaN", "盘中滚动可见", visible_from="09:31"),
    FactorSpec("vwap_deviation", "开盘强度", "(close-vwap)/vwap", "无量纲", "kline_minute", "缺失记NaN", "盘中滚动可见", visible_from="09:31"),
    FactorSpec("first_touch_limit_time", "开盘强度", "首次触及涨停距09:30分钟数", "分钟", "kline_minute", "未触板记NaN", "盘中事件时刻", visible_from="09:31"),
    FactorSpec("first_seal_time", "开盘强度", "首次封板(close>=limit_up)距09:30分钟数", "分钟", "kline_minute", "未封板记NaN", "盘中事件时刻", visible_from="09:31"),
    FactorSpec("open_high_break", "开盘强度", "开盘30分钟内最高价突破昨日最高价(0/1)", "0/1", "kline_minute+daily", "缺失记NaN", "10:00可见", visible_from="10:00"),
    FactorSpec("intraday_high_time", "盘中结构", "日内最高价出现距09:30交易分钟数", "分钟", "kline_minute", "缺失记NaN", "截至as_of_time", visible_from="post_close"),
    FactorSpec("intraday_low_time", "盘中结构", "日内最低价出现距09:30交易分钟数", "分钟", "kline_minute", "缺失记NaN", "截至as_of_time", visible_from="post_close"),
    FactorSpec("tail_30m_return", "盘中结构", "14:30至15:00涨跌幅", "无量纲", "kline_minute", "未到尾盘记NaN", "15:00可见", visible_from="15:00"),
    FactorSpec("tail_30m_amount_ratio", "盘中结构", "14:30至15:00成交额/全天成交额", "无量纲", "kline_minute", "未到尾盘记NaN", "15:00可见", visible_from="15:00"),
    FactorSpec("seal_open_count", "盘中结构", "盘中封板后开板次数", "次", "kline_minute", "缺失记NaN", "截至as_of_time", visible_from="post_close"),
    FactorSpec("reseal_count", "盘中结构", "盘中炸板后回封次数", "次", "kline_minute", "缺失记NaN", "截至as_of_time", visible_from="post_close"),
    FactorSpec("on_board_amount_ratio", "盘中结构", "涨停价成交额/全天成交额", "无量纲", "kline_minute", "缺失记NaN", "截至as_of_time", visible_from="post_close"),
    FactorSpec("up_minute_ratio", "盘中结构", "上涨分钟bar占比(0~1)", "无量纲", "kline_minute", "缺失记NaN", "截至as_of_time", visible_from="09:31"),
    FactorSpec("intraday_max_drawdown", "盘中结构", "日内从最高点回撤幅度(<=0)", "无量纲", "kline_minute", "缺失记NaN", "截至as_of_time", visible_from="09:31"),
)

AUCTION_INTRADAY_FACTOR_NAMES: tuple[str, ...] = tuple(
    spec.name for spec in AUCTION_INTRADAY_SPECS
)


def auction_intraday_factor_specs() -> tuple[FactorSpec, ...]:
    return AUCTION_INTRADAY_SPECS


def auction_intraday_capability() -> dict[str, Any]:
    return {
        "issue": 30,
        "status": "PARTIAL",
        "factor_count": len(AUCTION_INTRADAY_SPECS),
        "pit_guaranteed": True,
        "notes": "支持 stk_auction + 分钟线计算；无竞价直连数据时自动使用 open/首根分钟线代理并打 *_is_proxy=1",
    }


def _hhmm_to_trade_minutes(hhmm: str) -> float:
    parts = str(hhmm).strip().split(":")
    if len(parts) < 2:
        return np.nan
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        return np.nan
    total = h * 60 + m
    if total <= 11 * 60 + 30:
        return float(max(0, total - (9 * 60 + 30)))
    return float(120 + max(0, total - (13 * 60)))


def _extract_hhmm(bar_time: Any) -> str:
    s = str(bar_time or "").strip()
    if " " in s:
        s = s.split(" ", 1)[1]
    elif "T" in s:
        s = s.split("T", 1)[1]
    return s[:5]


def compute_auction_intraday_factors(
    snapshot_df: pd.DataFrame,
    *,
    minute_df: pd.DataFrame | None = None,
    as_of_hhmm: str | None = None,
) -> pd.DataFrame:
    """计算 #30 集合竞价与盘中分时强度因子（按 ``as_of_hhmm`` 严格截断未来分钟线）。"""
    if snapshot_df is None or snapshot_df.empty:
        return pd.DataFrame(columns=["symbol", "trade_date", *AUCTION_INTRADAY_FACTOR_NAMES])

    df = snapshot_df.copy()
    df["symbol"] = df["symbol"].astype(str)
    df["trade_date"] = df["trade_date"].astype(str)

    mins = minute_df.copy() if minute_df is not None and not minute_df.empty else pd.DataFrame()
    if not mins.empty:
        mins["symbol"] = mins["symbol"].astype(str)
        mins["trade_date"] = mins["trade_date"].astype(str)
        mins["_hhmm"] = mins["bar_time"].map(_extract_hhmm)
        if as_of_hhmm is not None:
            mins = mins[mins["_hhmm"] <= str(as_of_hhmm)].copy()
        mins = mins.sort_values(["symbol", "trade_date", "_hhmm"])

    rows: list[dict[str, Any]] = []
    used_proxy_flags: list[int] = []

    for _, r in df.iterrows():
        sym = str(r["symbol"])
        dt = str(r["trade_date"])
        pre_close = pd.to_numeric(r.get("pre_close", np.nan), errors="coerce")
        prev_vol = pd.to_numeric(r.get("prev_vol", np.nan), errors="coerce")
        prev_amt = pd.to_numeric(r.get("prev_amount", np.nan), errors="coerce")
        prev_high = pd.to_numeric(r.get("prev_high", np.nan), errors="coerce")
        float_share = pd.to_numeric(r.get("float_share", np.nan), errors="coerce")
        limit_up_price = pd.to_numeric(r.get("limit_up_price", np.nan), errors="coerce")
        prev_weak = r.get("prev_weak_board", np.nan)
        prev_strong = r.get("prev_strong_board", np.nan)

        rec: dict[str, Any] = {"symbol": sym, "trade_date": dt}
        for col in AUCTION_INTRADAY_FACTOR_NAMES:
            rec[col] = np.nan

        # 集合竞价（优先 auction_* 列，缺失则用 open 代理）
        auc_price = pd.to_numeric(r.get("auction_price", np.nan), errors="coerce")
        is_proxy = 0
        if pd.isna(auc_price):
            open_p = pd.to_numeric(r.get("open", np.nan), errors="coerce")
            if pd.notna(open_p):
                auc_price = open_p
                is_proxy = 1

        if pd.notna(auc_price) and pd.notna(pre_close) and pre_close > 0:
            gap = (float(auc_price) - float(pre_close)) / float(pre_close)
            rec["auction_gap_pct"] = gap
            if pd.notna(prev_weak):
                rec["auction_weak_to_strong"] = 1.0 if bool(prev_weak) and gap >= 0.02 else 0.0
            if pd.notna(prev_strong):
                rec["auction_strong_to_weak"] = 1.0 if bool(prev_strong) and gap < 0.0 else 0.0

        auc_vol = pd.to_numeric(r.get("auction_volume", np.nan), errors="coerce")
        auc_amt = pd.to_numeric(r.get("auction_amount", np.nan), errors="coerce")
        if pd.notna(auc_vol) and pd.notna(prev_vol) and prev_vol > 0:
            rec["auction_volume_ratio"] = float(auc_vol) / float(prev_vol)
        if pd.notna(auc_amt) and pd.notna(prev_amt) and prev_amt > 0:
            rec["auction_amount_ratio"] = float(auc_amt) / float(prev_amt)
        if pd.notna(auc_vol) and pd.notna(float_share) and float_share > 0:
            rec["auction_turnover"] = float(auc_vol) / float(float_share)

        # 分钟线盘中强度
        if not mins.empty:
            sub = mins[(mins["symbol"] == sym) & (mins["trade_date"] == dt)]
            if not sub.empty:
                close_s = pd.to_numeric(sub["close"], errors="coerce")
                high_s = pd.to_numeric(sub["high"], errors="coerce") if "high" in sub.columns else close_s
                low_s = pd.to_numeric(sub["low"], errors="coerce") if "low" in sub.columns else close_s
                vol_s = pd.to_numeric(sub["volume"], errors="coerce") if "volume" in sub.columns else pd.Series(np.nan, index=sub.index)
                amt_s = pd.to_numeric(sub["amount"], errors="coerce") if "amount" in sub.columns else pd.Series(np.nan, index=sub.index)
                hhmm_s = sub["_hhmm"]

                if pd.notna(pre_close) and pre_close > 0:
                    for cutoff, col in (("09:35", "open_5m_return"), ("09:45", "open_15m_return"), ("10:00", "open_30m_return")):
                        part = close_s[hhmm_s <= cutoff].dropna()
                        if not part.empty and hhmm_s[hhmm_s <= cutoff].max() >= cutoff:
                            rec[col] = (float(part.iloc[-1]) - float(pre_close)) / float(pre_close)

                if pd.notna(prev_amt) and prev_amt > 0:
                    for cutoff, col in (("09:35", "open_5m_amount_ratio"), ("09:45", "open_15m_amount_ratio")):
                        part_amt = amt_s[hhmm_s <= cutoff].dropna()
                        if not part_amt.empty and hhmm_s[hhmm_s <= cutoff].max() >= cutoff:
                            rec[col] = float(part_amt.sum()) / float(prev_amt)

                if pd.notna(prev_high) and (hhmm_s <= "10:00").any():
                    h30 = high_s[hhmm_s <= "10:00"].max()
                    if pd.notna(h30):
                        rec["open_high_break"] = 1.0 if float(h30) > float(prev_high) else 0.0

                # VWAP
                total_amt = amt_s.sum(min_count=1)
                total_vol = vol_s.sum(min_count=1)
                last_close = close_s.dropna().iloc[-1] if not close_s.dropna().empty else np.nan
                if pd.notna(total_amt) and pd.notna(total_vol) and total_vol > 0 and pd.notna(last_close):
                    vwap = float(total_amt) / float(total_vol)
                    if vwap > 0:
                        rec["above_vwap"] = 1.0 if float(last_close) >= vwap else 0.0
                        rec["vwap_deviation"] = (float(last_close) - vwap) / vwap

                # 触板与封板时间、炸板/回封次数
                if pd.notna(limit_up_price) and limit_up_price > 0:
                    eps = limit_up_price - 1e-4
                    touched = sub[high_s >= eps]
                    if not touched.empty:
                        rec["first_touch_limit_time"] = _hhmm_to_trade_minutes(touched["_hhmm"].iloc[0])
                    sealed = sub[close_s >= eps]
                    if not sealed.empty:
                        rec["first_seal_time"] = _hhmm_to_trade_minutes(sealed["_hhmm"].iloc[0])

                    is_sealed_arr = (close_s >= eps).fillna(False).to_numpy()
                    open_cnt = 0
                    reseal_cnt = 0
                    was_sealed = False
                    broken_before = False
                    for flag in is_sealed_arr:
                        if flag:
                            if not was_sealed and broken_before:
                                reseal_cnt += 1
                            was_sealed = True
                        else:
                            if was_sealed:
                                open_cnt += 1
                                broken_before = True
                            was_sealed = False
                    rec["seal_open_count"] = float(open_cnt)
                    rec["reseal_count"] = float(reseal_cnt)

                    if pd.notna(total_amt) and total_amt > 0:
                        board_amt = amt_s[close_s >= eps].sum(min_count=1)
                        rec["on_board_amount_ratio"] = float(board_amt) / float(total_amt) if pd.notna(board_amt) else 0.0

                # 日内高低点时间、上涨分钟占比、最大回撤
                if not high_s.dropna().empty:
                    hi_idx = high_s.idxmax()
                    rec["intraday_high_time"] = _hhmm_to_trade_minutes(sub.loc[hi_idx, "_hhmm"])
                if not low_s.dropna().empty:
                    lo_idx = low_s.idxmin()
                    rec["intraday_low_time"] = _hhmm_to_trade_minutes(sub.loc[lo_idx, "_hhmm"])

                diffs = close_s.diff().dropna()
                if not diffs.empty:
                    rec["up_minute_ratio"] = float((diffs > 0).mean())

                valid_c = close_s.dropna()
                if not valid_c.empty:
                    cum_max = valid_c.cummax()
                    dd = (valid_c - cum_max) / cum_max.replace(0, np.nan)
                    if not dd.dropna().empty:
                        rec["intraday_max_drawdown"] = float(dd.min())

                # 尾盘 30 分钟
                tail_sub = sub[hhmm_s >= "14:30"]
                if not tail_sub.empty and hhmm_s.max() >= "14:55":
                    tc = pd.to_numeric(tail_sub["close"], errors="coerce").dropna()
                    if len(tc) >= 2 and tc.iloc[0] > 0:
                        rec["tail_30m_return"] = (float(tc.iloc[-1]) - float(tc.iloc[0])) / float(tc.iloc[0])
                    if pd.notna(total_amt) and total_amt > 0:
                        t_amt = pd.to_numeric(tail_sub["amount"], errors="coerce").sum(min_count=1)
                        if pd.notna(t_amt):
                            rec["tail_30m_amount_ratio"] = float(t_amt) / float(total_amt)

        used_proxy_flags.append(is_proxy)
        rows.append(
            attach_indicators(
                rec,
                AUCTION_INTRADAY_SPECS,
                proxy_flags={"auction_gap_pct": is_proxy},
                proxy_factors=("auction_gap_pct",),
            )
        )

    out = pd.DataFrame(rows)
    # 按 trade_date 计算竞价涨幅排名
    if not out.empty and "auction_gap_pct" in out.columns:
        out["auction_strength_rank"] = out.groupby("trade_date")["auction_gap_pct"].rank(
            ascending=False, method="min"
        )
        out["auction_strength_rank_is_missing"] = (
            out["auction_strength_rank"].isna().astype(int)
        )
    return out


__all__ = [
    "AUCTION_INTRADAY_FACTOR_NAMES",
    "AUCTION_INTRADAY_SPECS",
    "auction_intraday_capability",
    "auction_intraday_factor_specs",
    "compute_auction_intraday_factors",
]
