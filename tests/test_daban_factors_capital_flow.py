"""#28 资金流向与龙虎榜因子族单测（确定性 fixture；不联网、不读写仓库 ``data/``）。

覆盖 issue 验收项：缺失处理、龙虎榜披露延迟、机构 / 游资席位分类、北向无数据，
外加单位口径锁定（元 / 手 / 百分数）、PIT 时点审计（无 T+1 泄漏）、
覆盖率 / rank IC / 共线性报告的 PARTIAL 状态判定。
"""

from __future__ import annotations

import math
import sqlite3

import numpy as np
import pandas as pd
import pytest

from strategy.daban_factors import capital_flow as cf
from strategy.daban_factors._base import parse_ts

T_DAY = "2026-05-08"
CALENDAR = ["2026-05-04", "2026-05-05", "2026-05-06", "2026-05-07", T_DAY]
POST_CLOSE = parse_ts(f"{T_DAY} 20:00:00")
SYMBOLS = ["600111", "000222", "300333"]


def _mf(
    symbol: str,
    day: str,
    *,
    lg_buy=0.0,
    lg_sell=0.0,
    elg_buy=0.0,
    elg_sell=0.0,
    net=None,
):
    if net is None:
        net = (lg_buy + elg_buy) - (lg_sell + elg_sell)
    return {
        "symbol": symbol,
        "trade_date": day,
        "buy_sm_amount": 0.0,
        "sell_sm_amount": 0.0,
        "buy_md_amount": 0.0,
        "sell_md_amount": 0.0,
        "buy_lg_amount": lg_buy,
        "sell_lg_amount": lg_sell,
        "buy_elg_amount": elg_buy,
        "sell_elg_amount": elg_sell,
        "net_mf_amount": net,
        "updated_at": f"{day} 16:00:00",
    }


def _board(symbol: str, day: str, **overrides):
    row = {
        "symbol": symbol,
        "trade_date": day,
        "limit_flag": "U",
        "limit_times": 1,
        "first_time": "09:35:00",
        "fd_amount": 5.0e7,
        "amount": 5.0e8,
        "float_mv": 2.0e9,
        "theme": "AI应用",
        "synced_at": f"{day} 16:30:00",
    }
    row.update(overrides)
    return row


def _base_inputs(**overrides) -> cf.CapitalFlowInputs:
    """默认输入：600111 每日主力净买 100 万，T 日龙虎榜 + 北向在榜。"""
    moneyflow = [
        _mf("600111", day, lg_buy=2.0e6, lg_sell=1.0e6, elg_buy=1.0e6, elg_sell=1.0e6)
        for day in CALENDAR
    ] + [_mf("000222", day, net=-5.0e5) for day in CALENDAR]
    lhb = [
        {
            "symbol": "600111",
            "trade_date": T_DAY,
            "reason": "日涨幅偏离值达7%",
            "amount": 4.0e8,
            "l_buy": 1.2e8,
            "l_sell": 6.0e7,
            "net_amount": 6.0e7,
            "synced_at": f"{T_DAY} 19:30:00",
        },
        {
            # 同股第二条上榜原因（成交额更小）→ 应被"取成交额最大行"规则排除。
            "symbol": "600111",
            "trade_date": T_DAY,
            "reason": "连续三个交易日涨幅偏离",
            "amount": 1.0e8,
            "l_buy": 3.0e7,
            "l_sell": 2.0e7,
            "net_amount": 1.0e7,
            "synced_at": f"{T_DAY} 19:30:00",
        },
        {
            "symbol": "000222",
            "trade_date": "2026-05-07",
            "reason": "日换手率达20%",
            "amount": 2.0e8,
            "l_buy": 5.0e7,
            "l_sell": 1.0e7,
            "net_amount": 4.0e7,
            "synced_at": "2026-05-07 19:30:00",
        },
    ]
    seats = [
        {
            "trade_date": T_DAY,
            "symbol": "600111",
            "seat_name": "机构专用",
            "side": "buy",
            "buy_amount": 5.0e7,
            "sell_amount": 0.0,
            "net_amount": 5.0e7,
            "seat_count": 2,
            "synced_at": f"{T_DAY} 19:30:00",
        },
        {
            "trade_date": T_DAY,
            "symbol": "600111",
            "seat_name": "常洲路营业部",
            "side": "buy",
            "buy_amount": 6.0e7,
            "sell_amount": 1.0e7,
            "net_amount": 5.0e7,
            "seat_count": 1,
            "synced_at": f"{T_DAY} 19:30:00",
        },
        {
            "trade_date": T_DAY,
            "symbol": "600111",
            "seat_name": "华南大道营业部",
            "side": "sell",
            "buy_amount": 0.0,
            "sell_amount": 4.0e7,
            "net_amount": -4.0e7,
            "seat_count": 1,
            "synced_at": f"{T_DAY} 19:30:00",
        },
    ]
    hot = [
        {"seat_name": "常洲路营业部", "org_code": "HZ01", "category": "游资"},
        {"seat_name": "华南大道营业部", "org_code": "SZ02", "category": "游资"},
    ]
    hsgt = [
        {
            "symbol": "600111",
            "trade_date": T_DAY,
            "market_type": "1",
            "amount": 8.0e7,
            "net_amount": 3.0e7,
            "buy_amount": 5.5e7,
            "sell_amount": 2.5e7,
            "synced_at": f"{T_DAY} 18:30:00",
        }
    ]
    defaults: dict[str, pd.DataFrame] = {
        "moneyflow": pd.DataFrame(moneyflow),
        "board": pd.DataFrame(
            [
                _board("600111", T_DAY),
                _board("000222", T_DAY, limit_flag="Z", limit_times=None, theme=None),
            ]
        ),
        "kline": pd.DataFrame(
            [
                {
                    "symbol": s,
                    "trade_date": d,
                    "close": 10.0,
                    "volume": 1.0e5,
                    "amount": 5.0e8,
                }
                for s in SYMBOLS
                for d in CALENDAR
            ]
        ),
        "calendar": pd.DataFrame({"trade_date": CALENDAR}),
        "lhb": pd.DataFrame(lhb),
        "lhb_seats": pd.DataFrame(seats),
        "hot_money_seats": pd.DataFrame(hot),
        "hsgt_top10": pd.DataFrame(hsgt),
        "extras": pd.DataFrame(
            [
                {
                    "symbol": "600111",
                    "trade_date": T_DAY,
                    "turnover_rate": 12.5,
                    "volume_ratio": 2.0,
                }
            ]
        ),
    }
    defaults.update(overrides)
    return cf.CapitalFlowInputs(**defaults)


def _build(inputs: cf.CapitalFlowInputs, **kwargs) -> pd.DataFrame:
    kwargs.setdefault("as_of_time", POST_CLOSE)
    return cf.compute_capital_flow_factors(inputs, SYMBOLS, T_DAY, **kwargs)


def _row(frame: pd.DataFrame, symbol: str) -> dict:
    hit = frame[frame["symbol"] == symbol]
    assert len(hit) == 1, f"symbol {symbol} 应有且仅有一行，实际 {len(hit)}"
    return hit.iloc[0].to_dict()


# ─── 单位口径（集中换算 + 锁定）──────────────────────────────────


def test_unit_scale_converts_wan_and_hand_to_canonical():
    frame = pd.DataFrame(
        [{"symbol": "600111", "trade_date": T_DAY, "net_mf_amount": 250.0}]
    )
    converted = cf.normalize_input_units(frame, ["net_mf_amount"], "wan_yuan")
    assert float(converted["net_mf_amount"].iloc[0]) == pytest.approx(2.5e6)
    hand = cf.normalize_input_units(pd.DataFrame([{"volume": 3.0}]), ["volume"], "hand")
    assert float(hand["volume"].iloc[0]) == pytest.approx(300.0)
    with pytest.raises(ValueError, match="未登记"):
        cf.normalize_input_units(frame, ["net_mf_amount"], "usd")


def test_as_ratio_locks_percent_to_decimal():
    assert cf.as_ratio(12.5) == pytest.approx(0.125)
    assert cf.as_ratio(0.125, unit="ratio") == pytest.approx(0.125)
    assert cf.as_ratio(None) is None
    with pytest.raises(ValueError):
        cf.as_ratio(1.0, unit="bp")


def test_context_columns_expose_canonical_units_for_collinearity():
    row = _row(_build(_base_inputs()), "600111")
    assert row["context_amount"] == pytest.approx(5.0e8)  # 元
    assert row["context_turnover_rate"] == pytest.approx(0.125)  # 百分数 → 小数
    assert row["context_volume_ratio"] == pytest.approx(2.0)


# ─── 个股资金流 ────────────────────────────────────────────────────


def test_main_net_inflow_uses_tushare_major_order_definition():
    row = _row(_build(_base_inputs()), "600111")
    assert row["main_net_inflow"] == pytest.approx(1.0e6)  # (200-100)+(100-100) 万
    assert row["main_net_inflow_ratio"] == pytest.approx(1.0e6 / 5.0e8)
    assert row["main_net_inflow_to_float_mv"] == pytest.approx(1.0e6 / 2.0e9)
    assert row["large_net_inflow"] == pytest.approx(1.0e6)
    assert row["super_large_net_inflow"] == pytest.approx(0.0)
    assert row["small_medium_net_inflow"] == pytest.approx(0.0)
    assert row["inflow_ratio_vs_amount"] == pytest.approx(1.0e6 / 5.0e8)


def test_net_inflow_windows_are_calendar_driven():
    row = _row(_build(_base_inputs()), "600111")
    assert row["net_inflow_3d"] == pytest.approx(3.0e6)
    assert row["net_inflow_5d"] == pytest.approx(5.0e6)
    # 仅 5 个交易日 → 10 日窗口不足，显式缺失而非部分求和
    assert np.isnan(row["net_inflow_10d"])
    assert row["net_inflow_10d_is_missing"] == 1
    assert row["consecutive_inflow_days"] == 5
    assert row["sell_pressure_days"] == 0


def test_net_inflow_hole_in_moneyflow_marks_missing_not_partial_sum():
    inputs = _base_inputs()
    keep = ~(
        (inputs.moneyflow["symbol"] == "600111")
        & (inputs.moneyflow["trade_date"] == "2026-05-06")
    )
    inputs.moneyflow = inputs.moneyflow[keep].reset_index(drop=True)
    row = _row(_build(inputs), "600111")
    assert np.isnan(row["net_inflow_3d"])
    assert np.isnan(row["net_inflow_5d"])
    assert np.isnan(row["consecutive_inflow_days"])
    assert row["consecutive_inflow_days_is_missing"] == 1


def test_sell_pressure_days_counts_negative_streak():
    row = _row(_build(_base_inputs()), "000222")
    assert row["sell_pressure_days"] == 5
    assert row["consecutive_inflow_days"] == 0  # T 日为负 → 真实 0，不是缺失
    assert row["consecutive_inflow_days_is_missing"] == 0


def test_inflow_before_and_on_limit_up_day():
    row = _row(_build(_base_inputs()), "600111")
    assert row["inflow_before_limit_up"] == pytest.approx(3.0e6)  # T 前 3 个交易日
    assert row["inflow_on_limit_up_day"] == pytest.approx(1.0e6)
    broken = _row(_build(_base_inputs()), "000222")  # T 日炸板（Z）
    assert np.isnan(broken["inflow_before_limit_up"])
    assert np.isnan(broken["inflow_on_limit_up_day"])


def test_symbol_without_any_rows_stays_nan():
    row = _row(_build(_base_inputs()), "300333")
    for name in (
        "main_net_inflow",
        "net_inflow_3d",
        "lhb_net_buy",
        "northbound_net_buy",
    ):
        assert np.isnan(row[name]), name
        assert row[f"{name}_is_missing"] == 1, name
    assert row["has_moneyflow_row"] == 0
    # 当日榜单已由 600111 证明披露 → 300333 不在榜是真实零事件
    assert row["lhb_on_list"] == 0
    assert row["lhb_net_buy_is_missing"] == 1


# ─── 龙虎榜 ────────────────────────────────────────────────────────


def test_dragon_tiger_canonical_row_and_ratios():
    row = _row(_build(_base_inputs()), "600111")
    assert row["lhb_on_list"] == 1
    assert row["lhb_net_buy"] == pytest.approx(6.0e7)  # 取成交额最大行
    assert row["lhb_buy_amount"] == pytest.approx(1.2e8)
    assert row["lhb_sell_amount"] == pytest.approx(6.0e7)
    assert row["lhb_net_buy_ratio"] == pytest.approx(6.0e7 / 4.0e8)
    assert row["lhb_net_buy_to_float_mv"] == pytest.approx(6.0e7 / 2.0e9)
    assert row["lhb_reason_count"] == 2
    assert row["lhb_is_first_board"] == 1
    assert row["lhb_is_continuation"] == 0
    assert row["lhb_on_list_5d_days"] == 1
    assert row["lhb_net_buy_5d"] == pytest.approx(0.0)  # T 之前无该股榜单
    assert row["lhb_net_buy_5d_is_proxy"] == 1


def test_institution_and_hot_money_seat_classification():
    row = _row(_build(_base_inputs()), "600111")
    assert row["lhb_institution_net_buy"] == pytest.approx(5.0e7)
    assert row["lhb_hot_money_net_buy"] == pytest.approx(5.0e7 - 4.0e7)
    assert row["lhb_famous_seat_count"] == 2
    assert row["lhb_buy_seat_count"] == 3  # seat_count 2 + 1
    assert row["lhb_sell_seat_count"] == 1
    assert row["lhb_seat_concentration"] == pytest.approx(1.0)

    no_profile = _build(_base_inputs(hot_money_seats=pd.DataFrame()))
    row2 = _row(no_profile, "600111")
    assert np.isnan(row2["lhb_hot_money_net_buy"])  # 不伪造游资
    assert np.isnan(row2["lhb_famous_seat_count"])
    assert "hot_money_profile_missing" in row2["degradation_reasons"]
    assert row2["lhb_institution_net_buy"] == pytest.approx(5.0e7)
    # 机构口径跨日代理同样只统计已披露记录
    assert row2["lhb_institution_net_buy_5d"] == pytest.approx(5.0e7)


def test_dragon_tiger_history_proxy_survives_same_day_absence():
    row = _row(_build(_base_inputs()), "000222")
    assert np.isnan(row["lhb_net_buy"])  # T 日不在榜
    assert row["lhb_on_list"] == 0
    assert row["lhb_net_buy_5d"] == pytest.approx(4.0e7)  # T-1 已披露
    assert row["lhb_net_buy_5d_is_proxy"] == 1


# ─── 北向 ──────────────────────────────────────────────────────────


def test_northbound_net_buy_and_proxy_holding_change():
    row = _row(_build(_base_inputs()), "600111")
    assert row["northbound_net_buy"] == pytest.approx(3.0e7)
    assert row["northbound_net_buy_ratio"] == pytest.approx(3.0e7 / 8.0e7)
    assert row["northbound_holding_change"] == pytest.approx(3.0e7)
    assert row["northbound_holding_change_is_proxy"] == 1  # hk_hold 未接入
    assert np.isnan(row["northbound_holding_ratio"])
    assert row["northbound_consecutive_buy_days"] == 1
    assert "hk_hold_not_wired" in row["degradation_reasons"]


def test_northbound_absent_symbol_is_missing_not_zero():
    row = _row(_build(_base_inputs()), "000222")
    for name in (
        "northbound_net_buy",
        "northbound_net_buy_ratio",
        "northbound_holding_change",
        "northbound_consecutive_buy_days",
    ):
        assert np.isnan(row[name]), name
        assert row[f"{name}_is_missing"] == 1, name


def test_northbound_holding_uses_real_snapshot_when_available():
    inputs = _base_inputs(
        north_holding=pd.DataFrame(
            [
                {"symbol": "600111", "trade_date": "2026-05-07", "holding_mv": 1.0e8},
                {"symbol": "600111", "trade_date": T_DAY, "holding_mv": 1.4e8},
            ]
        )
    )
    row = _row(_build(inputs), "600111")
    assert row["northbound_holding_change"] == pytest.approx(4.0e7)
    assert row["northbound_holding_change_is_proxy"] == 0
    assert row["northbound_holding_ratio"] == pytest.approx(1.4e8 / 2.0e9)


# ─── 披露时点与 PIT ────────────────────────────────────────────────


def test_lhb_disclosure_delayed_until_next_day():
    delayed = _base_inputs()
    late = delayed.lhb.copy()
    late["synced_at"] = "2026-05-09 09:00:00"
    delayed.lhb = late

    intraday = _row(_build(delayed, as_of_time=parse_ts(f"{T_DAY} 20:00:00")), "600111")
    assert np.isnan(intraday["lhb_net_buy"])
    assert np.isnan(intraday["lhb_on_list"])  # 无法确认"未上榜"，不填 0
    assert intraday["lhb_disclosure_lagged"] == 1
    assert intraday["lhb_day_disclosed"] == 0
    assert "lhb_disclose_delayed" in intraday["degradation_reasons"]
    # 保守历史代理（只用 T 之前已披露记录）仍可计算
    assert intraday["lhb_net_buy_10d"] == pytest.approx(0.0)

    next_morning = _row(
        _build(delayed, as_of_time=parse_ts("2026-05-09 09:30:00")), "600111"
    )
    assert next_morning["lhb_net_buy"] == pytest.approx(6.0e7)
    assert next_morning["lhb_day_disclosed"] == 1
    # lagged 标记描述"记录本身延迟披露 1 天"这一事实，与当前时点无关
    assert next_morning["lhb_disclosure_lagged"] == 1


def test_disclosure_lag_days_measures_calendar_delay():
    inputs = _base_inputs()
    late = inputs.lhb.copy()
    late["synced_at"] = "2026-05-09 09:00:00"
    assert cf.disclosure_lag_days(late, trade_date=T_DAY) == 1
    assert cf.disclosure_lag_days(inputs.lhb, trade_date=T_DAY) == 0
    assert cf.disclosure_lag_days(inputs.lhb, trade_date="2026-01-01") is None


def test_row_available_at_respects_stAMP_and_default_clock():
    assert cf.row_available_at(
        T_DAY, f"{T_DAY} 16:00:00", cf.LHB_DISCLOSE_TIME
    ) == parse_ts(f"{T_DAY} 19:00:00")
    assert cf.row_available_at(
        T_DAY, "2026-05-09 08:00:00", cf.LHB_DISCLOSE_TIME
    ) == parse_ts("2026-05-09 08:00:00")
    assert cf.row_available_at("", f"{T_DAY} 16:00:00", cf.LHB_DISCLOSE_TIME) is None


def test_default_decision_time_is_prev_close_and_blocks_post_close_factors():
    frame = cf.compute_capital_flow_factors(
        _base_inputs(), SYMBOLS, T_DAY, as_of_time_provided=False
    )
    assert frame["as_of_time_source"].iloc[0] == cf.SOURCE_DEFAULT_PREV_CLOSE
    row = _row(frame, "600111")
    for name in cf.capital_flow_spec_names("stock_moneyflow"):
        if name == "inflow_before_limit_up":
            continue  # 只用 T 之前记录，前一收盘姿态下合法
        assert np.isnan(row[name]), f"{name} 在默认前一收盘时点不得可见（T+1 泄漏）"
        assert row[f"{name}_is_missing"] == 1, name
    for name in cf.capital_flow_spec_names("dragon_tiger"):
        if name in cf.HISTORY_ONLY_FACTORS:
            continue
        assert np.isnan(row[name]), name
    audit = cf.pit_audit(frame)
    assert audit["status"] == "pass", audit
    assert audit["post_close_all_missing"] is True
    assert audit["violations"] == {}


def test_date_only_as_of_time_is_treated_as_prev_close():
    computed = _build(
        _base_inputs(), as_of_time=parse_ts(T_DAY), source_label=cf.SOURCE_DATE_ONLY
    )
    row = _row(computed, "600111")
    assert row["as_of_time_source"] == cf.SOURCE_DATE_ONLY
    assert np.isnan(row["main_net_inflow"])
    assert row["lhb_net_buy_5d"] == pytest.approx(0.0)
    assert cf.pit_audit(computed)["status"] == "pass"


def test_future_records_never_leak_into_T_day_factors():
    inputs = _base_inputs()
    future = inputs.moneyflow.copy()
    future["trade_date"] = "2026-05-09"
    future["updated_at"] = "2026-05-09 16:00:00"
    future["net_mf_amount"] = 9.9e9
    future["buy_lg_amount"] = 9.9e9
    inputs.moneyflow = pd.concat([inputs.moneyflow, future], ignore_index=True)
    row = _row(_build(inputs), "600111")
    assert row["net_inflow_3d"] == pytest.approx(3.0e6)
    assert row["main_net_inflow"] == pytest.approx(1.0e6)
    assert row["consecutive_inflow_days"] == 5


# ─── 交互项 / 融资大宗降级 ─────────────────────────────────────────


def test_interaction_terms_and_proxy_flags():
    row = _row(_build(_base_inputs()), "600111")
    assert row["fd_ratio_x_main_inflow"] == pytest.approx(
        (5.0e7 / 5.0e8) * (1.0e6 / 5.0e8)
    )
    assert row["lhb_net_buy_x_theme_heat"] == pytest.approx((6.0e7 / 4.0e8) * 0.1)
    assert row["lhb_net_buy_x_theme_heat_is_proxy"] == 1  # 题材为 kpl 回填口径
    assert row["northbound_change_x_float_mv"] == pytest.approx(
        (3.0e7 / 2.0e9) * math.log10(2.0e9 / 1e8 + 1.0)
    )
    assert row["inflow_3d_x_first_seal_time"] == pytest.approx(
        (3.0e6 / 2.0e9) * (1.0 - 5.0 / 240.0)
    )


def test_margin_and_block_trade_groups_stay_explicitly_missing():
    row = _row(_build(_base_inputs()), "600111")
    for name in (
        "margin_balance_change",
        "margin_buy_ratio",
        "block_trade_premium",
        "block_trade_amount_ratio",
    ):
        assert np.isnan(row[name]), name
        assert row[f"{name}_is_missing"] == 1, name
    assert "margin_detail_not_wired" in row["degradation_reasons"]
    assert "block_trade_not_wired" in row["degradation_reasons"]


def test_margin_and_block_trade_compute_when_frames_injected():
    inputs = _base_inputs(
        margin=pd.DataFrame(
            [
                {
                    "symbol": "600111",
                    "trade_date": "2026-05-07",
                    "margin_balance": 1.0e8,
                    "margin_buy_amount": 1.0e7,
                    "updated_at": "2026-05-08 21:00:00",
                },
                {
                    "symbol": "600111",
                    "trade_date": T_DAY,
                    "margin_balance": 1.2e8,
                    "margin_buy_amount": 2.0e7,
                    "updated_at": f"{T_DAY} 21:00:00",
                },
            ]
        ),
        block_trades=pd.DataFrame(
            [
                {
                    "symbol": "600111",
                    "trade_date": T_DAY,
                    "deal_amount": 1.0e7,
                    "price": 9.0,
                    "close": 10.0,
                    "updated_at": f"{T_DAY} 19:30:00",
                }
            ]
        ),
    )
    # 融资融券余额次一交易日才披露 → 用 T+1 早盘时点验证可计算
    row = _row(_build(inputs, as_of_time=parse_ts("2026-05-09 10:00:00")), "600111")
    assert row["margin_balance_change"] == pytest.approx(2.0e7)
    assert row["margin_buy_ratio"] == pytest.approx(2.0e7 / 5.0e8)
    assert row["block_trade_premium"] == pytest.approx(-0.1)
    assert row["block_trade_amount_ratio"] == pytest.approx(1.0e7 / 5.0e8)


# ─── 注册表 / schema / 禁 fillna(0) ───────────────────────────────


def test_factor_registry_covers_issue_checklist():
    names = set(cf.capital_flow_spec_names())
    for expected in (
        "main_net_inflow",
        "main_net_inflow_ratio",
        "super_large_net_inflow",
        "large_net_inflow",
        "net_inflow_3d",
        "net_inflow_5d",
        "net_inflow_10d",
        "consecutive_inflow_days",
        "inflow_ratio_vs_amount",
        "inflow_before_limit_up",
        "inflow_on_limit_up_day",
        "northbound_net_buy",
        "northbound_holding_change",
        "northbound_holding_ratio",
        "northbound_consecutive_buy_days",
        "lhb_net_buy",
        "lhb_buy_amount",
        "lhb_sell_amount",
        "lhb_net_buy_ratio",
        "lhb_institution_net_buy",
        "lhb_hot_money_net_buy",
        "lhb_seat_concentration",
        "lhb_buy_seat_count",
        "lhb_sell_seat_count",
        "lhb_is_first_board",
        "lhb_is_continuation",
        "lhb_famous_seat_count",
        "margin_balance_change",
        "margin_buy_ratio",
        "block_trade_premium",
        "block_trade_amount_ratio",
        "fd_ratio_x_main_inflow",
        "lhb_net_buy_x_theme_heat",
        "northbound_change_x_float_mv",
        "inflow_3d_x_first_seal_time",
    ):
        assert expected in names, expected
    for spec in cf.FACTOR_SPECS:
        assert spec.unit in ("元", "无量纲", "次", "天", "0/1"), spec.name
        assert spec.missing_rule and spec.pit_note and spec.source, spec.name


def test_output_schema_is_complete_and_stable():
    frame = _build(_base_inputs())
    columns = cf.capital_flow_output_columns()
    assert list(frame.columns) == columns
    for spec in cf.FACTOR_SPECS:
        assert f"{spec.name}_is_missing" in columns
    for name in cf.PROXY_FACTORS:
        assert f"{name}_is_proxy" in columns


def test_no_missing_factor_is_zero_filled():
    frame = _build(_base_inputs())
    for spec in cf.FACTOR_SPECS:
        if spec.name in cf.TEXT_COLUMNS:
            continue
        values = frame.loc[frame[f"{spec.name}_is_missing"] == 1, spec.name]
        assert values.isna().all(), f"{spec.name} 标记缺失却仍有非 NaN 值"


# ─── 验收报告与能力标记 ────────────────────────────────────────────


def test_quality_report_flags_partial_without_labels_or_samples():
    report = cf.capital_flow_report_from_frame(_build(_base_inputs()))
    assert report["issue"] == 28
    assert report["symbol_count"] == 3
    assert report["as_of_time_source"] == cf.SOURCE_EXPLICIT
    assert report["pit_audit"]["status"] == "skipped_explicit_time"
    assert "PARTIAL" in report["ic_acceptance"]
    assert report["collinearity"]["status"] == "insufficient_samples"
    assert 0.0 <= report["row_coverage"]["moneyflow"] <= 1.0
    assert "PARTIAL" in report["model_gain_acceptance"]
    assert "hot_money_profile_missing" not in report["degradation_reasons"]


def test_quality_report_reports_insufficient_ic_instead_of_fabricating():
    frame = _build(_base_inputs())
    labels = pd.DataFrame(
        {
            "symbol": SYMBOLS,
            "label_next_day_premium": [0.03, -0.02, 0.01],
            "label_promotion": [1, 0, 1],
            "label_break_board": [0, 1, 0],
        }
    )
    report = cf.capital_flow_report_from_frame(frame, labels=labels)
    ic = report["single_factor_ic"]["label_next_day_premium"]
    assert ic["status"] == "insufficient_samples"  # 3 个样本 < MIN_SAMPLE_COUNT
    assert ic["sample_count"] == 3
    assert all(value is None for value in ic["ic"].values())
    assert "PARTIAL" in report["ic_acceptance"]


def test_empty_database_report_is_partial_not_zero():
    report = cf.capital_flow_report_from_frame(
        cf.compute_capital_flow_factors(cf.CapitalFlowInputs(), ["600111"], T_DAY)
    )
    assert report["symbol_count"] == 1
    assert report["row_coverage"]["moneyflow"] == 0.0
    assert "PARTIAL" in report["coverage_acceptance"]


def test_capability_marks_unwired_sources():
    capability = cf.capital_flow_capability()
    assert capability["issue"] == 28
    assert "hk_hold_not_wired" in capability["degraded_groups"]["northbound_holding"]
    assert capability["acceptance"]["coverage_ge_90pct"].startswith("PARTIAL")
    assert capability["acceptance"]["lhb_disclosure_alignment"].startswith("DONE")


def test_inputs_container_defaults_to_empty_frames():
    inputs = cf.CapitalFlowInputs()
    assert inputs.moneyflow.empty and inputs.lhb.empty and inputs.calendar.empty
    frame = cf.compute_capital_flow_factors(
        inputs, ["600111"], T_DAY, as_of_time=POST_CLOSE
    )
    assert list(frame.columns) == cf.capital_flow_output_columns()
    assert int(frame["lhb_on_list_is_missing"].iloc[0]) == 1
    assert int(frame["lhb_day_disclosed"].iloc[0]) == 0


def test_build_with_no_symbols_returns_empty_schema():
    frame = cf.compute_capital_flow_factors(
        _base_inputs(), [], T_DAY, as_of_time=POST_CLOSE
    )
    assert frame.empty
    assert list(frame.columns) == cf.capital_flow_output_columns()


# ─── 只读取数（tmp_path 独立库；绝不碰仓库 data/*.db）─────────────


def _write_fixture_db(tmp_path):
    from app.services.kline_store import KlineSQLiteStore

    store = KlineSQLiteStore(str(tmp_path / "market_kline.db"))
    inputs = _base_inputs()
    assert store.upsert_moneyflow(
        inputs.moneyflow.to_dict("records"), f"{T_DAY} 16:00:00"
    )
    assert store.upsert_limit_board_daily(
        inputs.board.to_dict("records"), f"{T_DAY} 16:30:00"
    )
    assert store.update_limit_board_theme(
        [
            {"symbol": "600111", "trade_date": T_DAY, "theme": "AI应用"},
            {"symbol": "000222", "trade_date": T_DAY, "theme": None},
        ],
        f"{T_DAY} 16:40:00",
    )
    assert store.upsert_dragon_tiger(inputs.lhb.to_dict("records"), f"{T_DAY} 19:30:00")
    assert store.upsert_dragon_tiger_seats(
        inputs.lhb_seats.to_dict("records"), f"{T_DAY} 19:30:00"
    )
    assert store.upsert_hot_money_seats(
        inputs.hot_money_seats.to_dict("records"), f"{T_DAY} 19:30:00"
    )
    assert store.upsert_hsgt_top10(
        inputs.hsgt_top10.to_dict("records"), f"{T_DAY} 18:30:00"
    )
    assert store.upsert_daily_extras(
        inputs.extras.to_dict("records"), f"{T_DAY} 17:00:00"
    )
    for symbol in SYMBOLS:
        rows = [
            {
                "trade_date": day,
                "open": 10.0,
                "high": 10.0,
                "low": 10.0,
                "close": 10.0,
                "volume": 1.0e5,
                "amount": 5.0e8,
                "source": "unit_test",
            }
            for day in CALENDAR
        ]
        store.upsert_symbol_klines(symbol, rows, f"{T_DAY} 17:00:00")
    return store, inputs


def test_build_from_db_matches_injected_frame_result(tmp_path):
    store, inputs = _write_fixture_db(tmp_path)
    db_frame = cf.build_capital_flow_features(
        store.db_path, SYMBOLS, T_DAY, as_of_time=f"{T_DAY} 20:00:00"
    )
    injected = _build(inputs)
    pd.testing.assert_frame_equal(
        db_frame.reset_index(drop=True), injected.reset_index(drop=True)
    )
    row = _row(db_frame, "600111")
    assert row["main_net_inflow"] == pytest.approx(1.0e6)
    assert row["lhb_net_buy"] == pytest.approx(6.0e7)
    assert row["context_amount"] == pytest.approx(5.0e8)


def test_build_from_db_with_date_only_decision_time_has_no_leak(tmp_path):
    store, _inputs = _write_fixture_db(tmp_path)
    frame = cf.build_capital_flow_features(
        store.db_path, ["600111"], T_DAY, as_of_time=T_DAY
    )
    assert frame["as_of_time_source"].iloc[0] == cf.SOURCE_DATE_ONLY
    assert np.isnan(frame["main_net_inflow"].iloc[0])
    assert np.isnan(frame["lhb_net_buy"].iloc[0])
    assert cf.pit_audit(frame)["status"] == "pass"


def test_build_from_empty_db_reports_partially_missing(tmp_path):
    from app.services.kline_store import KlineSQLiteStore

    store = KlineSQLiteStore(str(tmp_path / "empty_market_kline.db"))
    frame = cf.build_capital_flow_features(store.db_path, ["600111"], T_DAY)
    assert list(frame.columns) == cf.capital_flow_output_columns()
    report = cf.capital_flow_report_from_frame(frame)
    assert report["row_coverage"]["moneyflow"] == 0.0
    assert "PARTIAL" in report["coverage_acceptance"]


def test_ro_connection_is_read_only(tmp_path):
    store, _inputs = _write_fixture_db(tmp_path)
    conn = cf.ro_connection(store.db_path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE moneyflow_daily SET net_mf_amount = 0")
    finally:
        conn.close()
