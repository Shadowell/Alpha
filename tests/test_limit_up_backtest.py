"""#26 打板可成交性回测引擎单元测试（全部离线，tmp_path 合成数据库，不请求网络）。

覆盖：一字板成功/失败、炸板、停牌、跌停顺延、T+1、费用（佣金最低5/印花税/过户费）、
滑点、容量（缩量/跳过/容量曲线）、除权、strict/loose、净值 vs 单笔相加、
分层（regime/板型/市值/高度）、基准缺失 skip、与旧 BacktestLab 差异报告、持久化与可复现。
"""

from __future__ import annotations

import hashlib
import json
import math
import random

import pandas as pd
import pytest

from app.services import limit_up_backtest as lb
from app.services.kline_store import KlineSQLiteStore

D0, D1, D2, D3, D4 = (
    "2026-01-05",
    "2026-01-06",
    "2026-01-07",
    "2026-01-08",
    "2026-01-09",
)
CAL = [D0, D1, D2, D3, D4]


def bar(date, o, h, lo, c, amount=2e8):
    return {
        "trade_date": date,
        "open": o,
        "high": h,
        "low": lo,
        "close": c,
        "volume": amount / 10.0,
        "amount": amount,
    }


def board(
    symbol,
    date,
    flag,
    *,
    first="10:30:00",
    ot=0,
    lt=1,
    fd=5e7,
    amount=2e8,
    close=11.0,
    fmv=3e8,
):
    return {
        "symbol": symbol,
        "trade_date": date,
        "limit_flag": flag,
        "first_time": first,
        "open_times": ot,
        "limit_times": lt,
        "fd_amount": fd,
        "amount": amount,
        "close": close,
        "float_mv": fmv,
    }


@pytest.fixture()
def market_db(tmp_path):
    """构造离线合成行情库（金额单位元；数据均为合成，不复用真实标的）。"""
    db = tmp_path / "market_kline.db"
    store = KlineSQLiteStore(str(db))
    lu_d1 = [bar(D0, 10, 10, 10, 10, 1e8), bar(D1, 10.5, 11.0, 10.4, 11.0, 2e8)]

    # 001000 填充标的：保证 5 天交易日历完整
    store.upsert_symbol_klines(
        "001000", [bar(d, 10, 10.1, 10, 10, 1e9) for d in CAL], "u"
    )

    # 001001 正常打板：T+1 开盘买入、次日收盘卖出
    store.upsert_symbol_klines(
        "001001",
        lu_d1
        + [
            bar(D2, 11.2, 12.0, 10.9, 11.5, 3e8),
            bar(D3, 11.5, 11.8, 11.0, 11.3, 2.5e8),
            bar(D4, 11.3, 11.5, 11.2, 11.4),
        ],
        "u",
    )
    store.upsert_daily_extras(
        [
            {
                "symbol": "001001",
                "trade_date": D1,
                "up_limit": 11.0,
                "down_limit": 9.0,
                "adj_factor": 1.0,
                "circ_mv": 3e8,
            },
            {
                "symbol": "001001",
                "trade_date": D2,
                "up_limit": 12.1,
                "down_limit": 9.9,
                "adj_factor": 1.0,
                "circ_mv": 3e8,
            },
            {
                "symbol": "001001",
                "trade_date": D3,
                "up_limit": 12.65,
                "down_limit": 10.35,
                "adj_factor": 1.0,
            },
            {
                "symbol": "001001",
                "trade_date": D4,
                "up_limit": 12.43,
                "down_limit": 10.17,
                "adj_factor": 1.0,
            },
        ],
        "u",
    )
    store.upsert_limit_board_daily([board("001001", D1, "U", fd=5e7, amount=2e8)], "u")

    # 001002：T+1 一字涨停（买不到）
    store.upsert_symbol_klines(
        "001002",
        lu_d1
        + [
            bar(D2, 12.1, 12.1, 12.1, 12.1, 5e7),
            bar(D3, 12.0, 13.0, 11.9, 12.5),
            bar(D4, 12.5, 12.7, 12.3, 12.6),
        ],
        "u",
    )
    store.upsert_daily_extras(
        [
            {
                "symbol": "001002",
                "trade_date": D1,
                "up_limit": 11.0,
                "down_limit": 9.0,
                "adj_factor": 1.0,
            },
            {
                "symbol": "001002",
                "trade_date": D2,
                "up_limit": 12.1,
                "down_limit": 9.9,
                "adj_factor": 1.0,
            },
            {
                "symbol": "001002",
                "trade_date": D3,
                "up_limit": 13.31,
                "down_limit": 10.89,
                "adj_factor": 1.0,
            },
        ],
        "u",
    )
    store.upsert_limit_board_daily([board("001002", D1, "U", fd=8e7, amount=2e8)], "u")

    # 001003：T+1 停牌（无 bar）
    store.upsert_symbol_klines(
        "001003",
        lu_d1 + [bar(D3, 11.0, 11.2, 10.9, 11.1), bar(D4, 11.1, 11.3, 11.0, 11.2)],
        "u",
    )
    store.upsert_limit_board_daily([board("001003", D1, "U")], "u")

    # 001004：T 日炸板（涨停价排队确定性成交）
    store.upsert_symbol_klines(
        "001004",
        [
            bar(D0, 10, 10, 10, 10, 1e8),
            bar(D1, 10.5, 11.0, 10.3, 10.7, 2e8),
            bar(D2, 10.8, 11.77, 10.6, 11.3, 2e8),
            bar(D3, 11.3, 11.5, 11.0, 11.2),
            bar(D4, 11.2, 11.3, 11.0, 11.1),
        ],
        "u",
    )
    store.upsert_daily_extras(
        [
            {
                "symbol": "001004",
                "trade_date": D1,
                "up_limit": 11.0,
                "down_limit": 9.0,
                "adj_factor": 1.0,
            },
            {
                "symbol": "001004",
                "trade_date": D2,
                "up_limit": 11.77,
                "down_limit": 9.63,
                "adj_factor": 1.0,
            },
            {
                "symbol": "001004",
                "trade_date": D3,
                "up_limit": 12.43,
                "down_limit": 10.17,
                "adj_factor": 1.0,
            },
        ],
        "u",
    )
    store.upsert_limit_board_daily(
        [board("001004", D1, "Z", ot=1, fd=None, amount=2e8, close=10.7)], "u"
    )

    # 001005：T 日一字封死（strict 不成交；loose 概率成交）
    store.upsert_symbol_klines(
        "001005",
        [
            bar(D0, 10, 10, 10, 10, 1e8),
            bar(D1, 11.0, 11.0, 11.0, 11.0, 2.5e8),
            bar(D2, 11.2, 11.4, 11.1, 11.3),
            bar(D3, 11.3, 11.5, 11.2, 11.4),
            bar(D4, 11.4, 11.5, 11.1, 11.2),
        ],
        "u",
    )
    store.upsert_daily_extras(
        [
            {
                "symbol": "001005",
                "trade_date": D1,
                "up_limit": 11.0,
                "down_limit": 9.0,
                "adj_factor": 1.0,
            }
        ],
        "u",
    )
    store.upsert_limit_board_daily(
        [board("001005", D1, "U", first="09:31:00", fd=2.4e8, amount=2.5e8)], "u"
    )

    # 001006：T+1 收盘封跌停（收盘离场顺延）
    store.upsert_symbol_klines(
        "001006",
        lu_d1
        + [
            bar(D2, 10.9, 10.9, 9.9, 9.9, 3e8),
            bar(D3, 10.2, 10.6, 10.0, 10.5),
            bar(D4, 10.5, 10.6, 10.3, 10.4),
        ],
        "u",
    )
    store.upsert_daily_extras(
        [
            {
                "symbol": "001006",
                "trade_date": D1,
                "up_limit": 11.0,
                "down_limit": 9.0,
                "adj_factor": 1.0,
            },
            {
                "symbol": "001006",
                "trade_date": D2,
                "up_limit": 12.1,
                "down_limit": 9.9,
                "adj_factor": 1.0,
            },
            {
                "symbol": "001006",
                "trade_date": D3,
                "up_limit": 10.89,
                "down_limit": 8.91,
                "adj_factor": 1.0,
            },
        ],
        "u",
    )
    store.upsert_limit_board_daily(
        [board("001006", D1, "U"), board("001006", D2, "D")], "u"
    )

    # 001008：卖出日停牌（顺延）
    store.upsert_symbol_klines(
        "001008",
        lu_d1 + [bar(D2, 11.2, 11.4, 11.0, 11.3), bar(D4, 11.6, 11.8, 11.5, 11.7)],
        "u",
    )
    store.upsert_daily_extras(
        [
            {
                "symbol": "001008",
                "trade_date": D1,
                "up_limit": 11.0,
                "down_limit": 9.0,
                "adj_factor": 1.0,
            },
            {
                "symbol": "001008",
                "trade_date": D2,
                "up_limit": 12.1,
                "down_limit": 9.9,
                "adj_factor": 1.0,
            },
        ],
        "u",
    )
    store.upsert_limit_board_daily([board("001008", D1, "U")], "u")

    # 001009：T+1 开盘价即涨停价（strict 不确认成交）
    store.upsert_symbol_klines(
        "001009",
        lu_d1
        + [
            bar(D2, 12.1, 12.1, 11.5, 11.8),
            bar(D3, 11.9, 12.0, 11.7, 11.9),
            bar(D4, 11.9, 12.0, 11.8, 11.85),
        ],
        "u",
    )
    store.upsert_daily_extras(
        [
            {
                "symbol": "001009",
                "trade_date": D1,
                "up_limit": 11.0,
                "down_limit": 9.0,
                "adj_factor": 1.0,
            },
            {
                "symbol": "001009",
                "trade_date": D2,
                "up_limit": 12.1,
                "down_limit": 9.9,
                "adj_factor": 1.0,
            },
        ],
        "u",
    )
    store.upsert_limit_board_daily([board("001009", D1, "U")], "u")

    # 001007：持有期内除权（adj_factor 1.0 → 1.25，价格 11.2 → 8.96）
    store.upsert_symbol_klines(
        "001007",
        lu_d1
        + [
            bar(D2, 11.2, 11.4, 11.0, 11.2),
            bar(D3, 8.96, 8.96, 8.96, 8.96),
            bar(D4, 9.0, 9.1, 8.95, 9.0),
        ],
        "u",
    )
    store.upsert_daily_extras(
        [
            {
                "symbol": "001007",
                "trade_date": D1,
                "up_limit": 11.0,
                "down_limit": 9.0,
                "adj_factor": 1.0,
            },
            {
                "symbol": "001007",
                "trade_date": D2,
                "up_limit": 12.1,
                "down_limit": 9.9,
                "adj_factor": 1.0,
            },
            {
                "symbol": "001007",
                "trade_date": D3,
                "up_limit": 9.86,
                "down_limit": 8.06,
                "adj_factor": 1.25,
            },
            {
                "symbol": "001007",
                "trade_date": D4,
                "up_limit": 9.86,
                "down_limit": 8.06,
                "adj_factor": 1.25,
            },
        ],
        "u",
    )
    store.upsert_limit_board_daily([board("001007", D1, "U")], "u")

    # 001010：T 日炸板（queue_t 可买）+ T+1 一字跌停（卖不出：strict 顺延 / loose 跌停价模型成交）
    store.upsert_symbol_klines(
        "001010",
        [
            bar(D0, 10, 10, 10, 10, 1e8),
            bar(D1, 10.5, 11.0, 10.3, 10.7, 2e8),
            bar(D2, 9.63, 9.63, 9.63, 9.63, 1e7),
            bar(D3, 10.0, 10.2, 9.95, 10.1),
            bar(D4, 10.1, 10.3, 10.05, 10.2),
        ],
        "u",
    )
    store.upsert_daily_extras(
        [
            {
                "symbol": "001010",
                "trade_date": D1,
                "up_limit": 11.0,
                "down_limit": 9.0,
                "adj_factor": 1.0,
            },
            {
                "symbol": "001010",
                "trade_date": D2,
                "up_limit": 11.77,
                "down_limit": 9.63,
                "adj_factor": 1.0,
            },
        ],
        "u",
    )
    store.upsert_limit_board_daily(
        [
            board("001010", D1, "Z", ot=1, fd=None, amount=2e8, close=10.7),
            board("001010", D2, "D", fd=None, amount=1e7, close=9.63),
        ],
        "u",
    )

    # 001012：T 日炸板 + T+1 收盘封死跌停（非一字）→ 收盘离场场景顺延
    store.upsert_symbol_klines(
        "001012",
        [
            bar(D0, 10, 10, 10, 10, 1e8),
            bar(D1, 10.5, 11.0, 10.3, 10.7, 2e8),
            bar(D2, 10.6, 10.65, 9.63, 9.63, 3e8),
            bar(D3, 9.9, 10.1, 9.8, 10.0),
            bar(D4, 10.0, 10.2, 9.95, 10.05),
        ],
        "u",
    )
    store.upsert_daily_extras(
        [
            {
                "symbol": "001012",
                "trade_date": D1,
                "up_limit": 11.0,
                "down_limit": 9.0,
                "adj_factor": 1.0,
            },
            {
                "symbol": "001012",
                "trade_date": D2,
                "up_limit": 11.77,
                "down_limit": 9.63,
                "adj_factor": 1.0,
            },
        ],
        "u",
    )
    store.upsert_limit_board_daily(
        [
            board("001012", D1, "Z", ot=1, fd=None, amount=2e8, close=10.7),
            board("001012", D2, "D", fd=None, amount=3e8, close=9.63),
        ],
        "u",
    )

    # 001011：有涨停 K 线但涨停表/涨跌停价全缺 → 排队概率不可评估（不造数）
    store.upsert_symbol_klines(
        "001011",
        [
            bar(D0, 10, 10, 10, 10, 1e8),
            bar(D1, 11.0, 11.0, 11.0, 11.0, 2e8),
            bar(D2, 11.2, 11.3, 11.1, 11.3),
            bar(D3, 11.3, 11.4, 11.2, 11.35),
            bar(D4, 11.35, 11.4, 11.2, 11.3),
        ],
        "u",
    )

    # 基准指数（000300.SH 有数；999999.SH 无数 → skip）
    store.upsert_index_daily(
        [
            {"ts_code": "000300.SH", "trade_date": D0, "close": 4000.0},
            {"ts_code": "000300.SH", "trade_date": D1, "close": 4020.0},
            {"ts_code": "000300.SH", "trade_date": D2, "close": 4050.0},
            {"ts_code": "000300.SH", "trade_date": D3, "close": 4030.0},
            {"ts_code": "000300.SH", "trade_date": D4, "close": 4060.0},
        ],
        "u",
    )
    store.upsert_daily_extras(
        [
            {
                "symbol": "001004",
                "trade_date": D1,
                "up_limit": 11.0,
                "down_limit": 9.0,
                "adj_factor": 1.0,
            }
        ],
        "u",
    )
    return str(db)


def sig(symbol, date=D1):
    return {"symbol": symbol, "signal_date": date, "name": "合成"}


def run(db, signals, **kw):
    kw.setdefault("generated_at", "2026-01-09T16:00:00+08:00")
    return lb.run_limit_up_backtest(signals, db_path=db, **kw)


def only_closed(rep):
    closed = [t for t in rep["trades"] if t["status"] == "filled"]
    assert len(closed) == 1, rep["trades"]
    return closed[0]


# ─────────────────── 买入可成交性 ───────────────────


def test_next_open_entry_next_close_exit(market_db):
    rep = run(
        market_db, [sig("001001")], entry_scene="next_open", exit_scene="next_close"
    )
    t = only_closed(rep)
    assert t["entry_date"] == D2 and t["entry_price_gross"] == 11.2
    assert t["exit_date"] == D3 and t["exit_price_gross"] == 11.3
    assert t["hold_days"] == 1
    assert rep["nav_series"] and rep["metrics"]["total_return_pct"] is not None


def test_t1_yizhi_unbuyable_both_modes(market_db):
    for mode in ("strict", "loose"):
        rep = run(market_db, [sig("001002")], mode=mode)
        det = rep["signal_details"][0]
        assert det["filled"] is False
        assert det["fail_reason"] == "t1_yizhi_unbuyable"
    # 虚假成交防线：不存在任何一字涨停买入
    assert rep["trades"] == []


def test_t1_suspended_buy_fail(market_db):
    rep = run(market_db, [sig("001003")])
    assert rep["signal_details"][0]["fail_reason"] == "t1_suspended"
    assert rep["fill_stats"]["entry_filled"] == 0


def test_queue_t_zhaban_deterministic_fill(market_db):
    rep = run(
        market_db,
        [sig("001004")],
        entry_scene="queue_t",
        exit_scene="hold_days",
        hold_days=1,
    )
    t = only_closed(rep)
    assert t["entry_date"] == D1 and t["entry_price_gross"] == 11.0
    # T+1 制度：炸板排队买在 T 日，最早 T+1 卖出
    assert t["exit_date"] == D2 and t["hold_days"] == 1
    assert any("炸板" in a for a in t["assumptions"])


def test_queue_t_yizhi_strict_excluded(market_db):
    rep = run(market_db, [sig("001005")], entry_scene="queue_t", mode="strict")
    det = rep["signal_details"][0]
    assert det["fail_reason"] == "sealed_board_strict"
    assert 0 < (det["queue_fill_prob"] or 0) <= lb.QueueFillConfig().yizhi_prob_cap


def test_queue_t_yizhi_loose_probabilistic_and_reproducible(market_db):
    rep = run(market_db, [sig("001005")], entry_scene="queue_t", mode="loose", seed=7)
    det = rep["signal_details"][0]
    assert det["filled"] == (det["queue_fill_draw"] <= det["queue_fill_prob"])
    rep2 = run(market_db, [sig("001005")], entry_scene="queue_t", mode="loose", seed=7)
    assert rep2["signal_details"] == rep["signal_details"]
    # 多 seed 扫描：概率在 (0,1) 开区间 → 两种结果都应出现（模型确实影响结果）
    outcomes = set()
    for s in range(60):
        r = run(market_db, [sig("001005")], entry_scene="queue_t", mode="loose", seed=s)
        outcomes.add(r["signal_details"][0]["filled"])
    assert outcomes == {True, False}


def test_next_open_at_limit_price(market_db):
    strict = run(market_db, [sig("001009")], mode="strict")
    assert strict["signal_details"][0]["fail_reason"] == "t1_open_at_limit_strict"
    loose = run(market_db, [sig("001009")], mode="loose", seed=3)
    det = loose["signal_details"][0]
    assert (det["filled"] and det["entry_price_gross"] == 12.1) or det[
        "fail_reason"
    ] == "t1_auction_at_limit_not_filled"


def test_missing_board_row_not_assessable_no_fabrication(market_db):
    rep = run(market_db, [sig("001011")], entry_scene="queue_t")
    assert rep["signal_details"][0]["fail_reason"].startswith("queue_fill_unassessable")


def test_signal_symbol_without_klines(market_db):
    rep = run(market_db, [sig("009999")])
    assert rep["signal_details"][0]["fail_reason"] == "t1_suspended"


# ─────────────────── 卖出约束 ───────────────────


def test_sell_defer_down_limit_close(market_db):
    # queue_t 买在 T 日炸板（11.0），T+1 收盘封死跌停 → 收盘离场顺延到 T+2
    rep = run(
        market_db, [sig("001012")], entry_scene="queue_t", exit_scene="next_close"
    )
    t = only_closed(rep)
    assert t["entry_date"] == D1
    assert t["exit_date"] == D3  # D2 收盘封跌停 → 顺延到 D3
    assert t["defer_days"] == 1
    assert "down_limit_close_defer" in rep["fill_stats"]["sell_defer_reasons"]


def test_sell_defer_suspension(market_db):
    rep = run(market_db, [sig("001008")], exit_scene="next_close")
    t = only_closed(rep)
    assert t["exit_date"] == D4  # D3 停牌顺延
    assert t["defer_reasons"][0].endswith("suspended")


def test_sl_gap_down_limit_strict_vs_loose(market_db):
    # queue_t 炸板买入 11.0，止损 -10% = 9.9；T+1 一字跌停 9.63（跳空穿止损且卖不出）
    kw = dict(entry_scene="queue_t", exit_scene="hold_days", hold_days=1, sl_pct=-10.0)
    strict = run(market_db, [sig("001010")], mode="strict", **kw)
    t = only_closed(strict)
    assert (
        t["exit_date"] == D3 and t["defer_days"] == 1
    )  # D2 一字跌停卖不出，strict 顺延
    loose = run(market_db, [sig("001010")], mode="loose", **kw)
    tl = only_closed(loose)
    assert tl["exit_date"] == D2 and tl["exit_price_gross"] == 9.63
    assert tl["exit_reason"] == "sl_gap_down_limit_loose"


def test_sl_intraday_trigger(market_db):
    # 001006 next_open 买入 10.9，止损 -7% = 10.137；T+2（D3）盘中 low 10.0 触发但开盘 10.2 未跳空 → 按止损价成交
    rep = run(
        market_db, [sig("001006")], exit_scene="hold_days", hold_days=3, sl_pct=-7.0
    )
    t = only_closed(rep)
    assert t["exit_date"] == D3 and t["exit_reason"] == "sl"
    assert math.isclose(t["exit_price_gross"], 10.9 * 0.93, abs_tol=1e-3)


def test_sl_gap_open_intraday_fill_at_open(market_db):
    # 001012: queue_t 买入 11.0，止损 -3% = 10.67；D2 开盘 10.6 跳空低于止损价但未封死 → 按开盘价成交（不按止损价）
    rep = run(
        market_db,
        [sig("001012")],
        entry_scene="queue_t",
        exit_scene="hold_days",
        hold_days=3,
        sl_pct=-3.0,
    )
    t = only_closed(rep)
    assert t["exit_date"] == D2 and t["exit_reason"] == "sl_gap_open"
    assert t["exit_price_gross"] == 10.6


def test_tp_trigger_and_cap_at_limit_up(market_db):
    # 正常止盈：entry 11.2, tp 5% = 11.76 ≤ D3 high 11.8 → D3 盘中触发
    rep = run(
        market_db, [sig("001001")], exit_scene="hold_days", hold_days=3, tp_pct=5.0
    )
    t = only_closed(rep)
    assert t["exit_reason"] == "tp" and t["exit_date"] == D3
    assert math.isclose(t["exit_price_gross"], 11.2 * 1.05, abs_tol=1e-3)
    # 止盈价高于涨停价 → 按涨停价封顶成交（炸板排队 11.0 买入，tp 10% = 12.1 > D2 涨停 11.77）
    rep2 = run(
        market_db,
        [sig("001004")],
        entry_scene="queue_t",
        exit_scene="hold_days",
        hold_days=3,
        tp_pct=10.0,
    )
    t2 = only_closed(rep2)
    assert t2["exit_reason"] == "tp" and t2["exit_date"] == D2
    assert t2["exit_price_gross"] == 11.77
    assert any("涨停价卖出" in a for a in t2["assumptions"])


def test_open_exit_no_intraday_tp_no_lookahead(market_db):
    # next_open 场景：T+1 买入、首个可卖日开盘离场，不检查当日盘中 high（避免未来函数）
    rep = run(market_db, [sig("001001")], exit_scene="next_open", tp_pct=1.0)
    t = only_closed(rep)
    assert t["exit_date"] == D3 and t["exit_price_gross"] == 11.5
    assert t["exit_reason"] == "planned_open"


def test_open_at_end_when_as_of_blocks_exit(market_db):
    rep = run(market_db, [sig("001001")], exit_scene="next_close", as_of=D2)
    assert rep["signal_details"][0]["exit_date"] is None
    assert rep["signal_details"][0]["exit_reason"] == "open_at_end"
    assert rep["open_positions"] and rep["open_positions"][0]["symbol"] == "001001"


# ─────────────────── 费用 / 滑点 ───────────────────


def test_fees_min_commission_stamp_tax_transfer(market_db):
    rep = run(
        market_db, [sig("001001")], initial_capital=100_000.0, position_size_pct=0.2
    )
    t = only_closed(rep)
    assert (
        t["buy_costs"]["commission"] == 5.0
    )  # 1.9 万买入，万2.5=4.76 < 5 → 最低佣金生效
    assert t["buy_costs"]["stamp_tax"] == 0.0  # 印花税只收卖出
    assert t["buy_costs"]["transfer_fee"] > 0
    assert t["sell_costs"]["stamp_tax"] == pytest.approx(
        t["sell_gross"] * 5e-4, abs=0.01
    )
    assert t["sell_costs"]["commission"] >= 5.0
    assert rep["trade_stats"]["total_fees"] > 0
    assert rep["trade_stats"]["fee_ratio_of_buy_pct"] > 0


def test_fee_config_changes_result(market_db):
    base = run(market_db, [sig("001001")])
    high_fee = run(
        market_db,
        [sig("001001")],
        fees=lb.FeeConfig(commission_rate=5e-3, stamp_tax_rate=2e-3),
    )
    assert only_closed(high_fee)["net_pnl"] < only_closed(base)["net_pnl"]
    no_tf = run(
        market_db, [sig("001001")], fees=lb.FeeConfig(transfer_fee_enabled=False)
    )
    assert only_closed(no_tf)["buy_costs"]["transfer_fee"] == 0.0
    # 高滑点更差
    slip = run(market_db, [sig("001001")], slippage=lb.SlippageConfig(base_bps=300.0))
    assert only_closed(slip)["net_pnl"] < only_closed(base)["net_pnl"]


# ─────────────────── 容量 ───────────────────


def test_capacity_scale(market_db):
    rep = run(market_db, [sig("001001")], initial_capital=1e8, position_size_pct=0.2)
    t = only_closed(rep)
    cap = 3e8 * 0.05  # D2 成交额 3 亿 × 5% 参与率
    assert t["buy_gross"] <= cap + 12.1 * 100  # 整手取整内不超过容量
    assert t["capacity_capped"] is True
    assert rep["capacity"]["capacity_hit_count"] == 1


def test_capacity_skip_and_curve(market_db):
    rep = run(
        market_db,
        [sig("001001")],
        initial_capital=1e8,
        position_size_pct=0.2,
        capacity=lb.CapacityConfig(action="skip"),
    )
    assert rep["trades"][0]["status"] == "skipped"
    assert rep["trades"][0]["fail_reason"] == "capacity_skip"
    assert rep["nav_series"][-1]["nav"] == pytest.approx(1e8)
    curve = lb.capacity_curve(
        [sig("001001")], db_path=market_db, capitals=[1e5, 1e6, 1e8]
    )
    rows = curve["curve"]
    assert rows[0]["capacity_hit_count"] == 0 and rows[-1]["capacity_hit_count"] >= 1
    # 大资金容量受限后收益不高于小资金（模型一致性）
    assert rows[-1]["total_return_pct"] <= rows[0]["total_return_pct"] + 1e-9


def test_insufficient_cash_skip(market_db):
    rep = run(market_db, [sig("001001")], initial_capital=500.0)
    assert rep["trades"][0]["status"] == "skipped"
    assert rep["trades"][0]["fail_reason"] in (
        "insufficient_capacity_or_cash",
        "insufficient_cash",
    )


# ─────────────────── 除权 ───────────────────


def test_ex_rights_adj_factor_adjustment(market_db):
    kw = dict(entry_scene="next_open", exit_scene="hold_days", hold_days=2)
    adj = run(market_db, [sig("001007")], use_adj_factor=True, **kw)
    ta = only_closed(adj)
    raw = run(market_db, [sig("001007")], use_adj_factor=False, **kw)
    tr = only_closed(raw)
    # 除权把 11.2→8.96 的假跌还原：复权口径接近 0（+0.45% 毛收益-费用），未复权 ≈ -20%
    assert ta["gross_return_pct"] == pytest.approx(0.446, abs=0.1)
    assert ta["net_pnl_pct"] > -5.0
    assert tr["net_pnl_pct"] < -15.0
    assert any("ex_rights" in n for n in adj["data_quality_notes"])


# ─────────────────── strict/loose、净值口径 ───────────────────


def test_strict_loose_comparison_report(market_db):
    cmp_ = lb.run_strict_loose_comparison(
        [sig("001005")], db_path=market_db, entry_scene="queue_t", seed=7
    )
    assert cmp_["strict"]["fill_stats"]["entry_filled"] == 0
    if cmp_["loose"]["fill_stats"]["entry_filled"]:
        assert cmp_["delta"]["extra_trades_only_in_loose"] == ["001005@2026-01-06"]
    assert "概率模型" in cmp_["delta"]["explanation"]


def test_nav_curve_not_sum_of_trades(market_db):
    rep = run(
        market_db,
        [sig("001001"), sig("001006"), sig("001008")],
        exit_scene="next_close",
    )
    closed = [t for t in rep["trades"] if t["status"] == "filled"]
    assert len(closed) == 3
    assert "单笔" in rep["trade_stats"]["note"]
    assert rep["trade_stats"]["sum_single_trade_pnl_pct"] != pytest.approx(
        rep["metrics"]["total_return_pct"], abs=1e-6
    )


def test_metrics_present(market_db):
    rep = run(market_db, [sig("001001"), sig("001004")], entry_scene="queue_t")
    m = rep["metrics"]
    for k in (
        "total_return_pct",
        "annualized_return_pct",
        "max_drawdown_pct",
        "sharpe",
        "sortino",
        "calmar",
    ):
        assert k in m
    assert m["max_drawdown_pct"] <= 0


# ─────────────────── 分层 / 基准 / 溢价 ───────────────────


def test_regime_layering_and_degradation(market_db):
    rep = run(market_db, [sig("001001")])
    assert "skipped" in rep["layered_metrics"]["_regime_layer"]
    rep2 = run(market_db, [sig("001001")], regime_by_date={D1: "退潮期"})
    assert rep2["layered_metrics"]["regime"]["退潮期"]["trades"] == 1
    rep3 = run(market_db, [sig("001001")], regime_by_date={"2099-01-01": "别的"})
    assert rep3["layered_metrics"]["regime"]["missing"]["trades"] == 1


def test_board_type_mcap_height_layers(market_db):
    rep = run(market_db, [sig("001004")], entry_scene="queue_t")
    lay = only_closed(rep)["layers"]
    assert lay["board_type"] == "炸板"
    assert lay["height"] == 1
    assert lay["mcap"] == "2e+08-5e+08"  # float_mv 3e8 元合成值


def test_benchmarks(market_db):
    rep = run(market_db, [sig("001001")], benchmark_ts_codes=["000300.SH", "999999.SH"])
    names = {b["name"] for b in rep["benchmarks"]}
    assert "index:000300.SH" in names
    # D1..D3 净值窗口：4020 → 4030
    b300 = next(b for b in rep["benchmarks"] if b["name"] == "index:000300.SH")
    assert b300["total_return_pct"] == pytest.approx((4030 / 4020 - 1) * 100, abs=1e-3)
    assert any("999999.SH" in s and "skip" in s for s in rep["benchmark_skipped"])
    eq = next(b for b in rep["benchmarks"] if b["name"] == "equal_weight_all_market")
    assert eq["coverage"] == 1.0
    assert "strategy_excess_pct" in eq


def test_premium_metrics(market_db):
    rep = run(market_db, [sig("001001")])
    prem = rep["premium_metrics"]
    assert prem["next_open_premium"]["mean_pct"] == pytest.approx(
        (11.2 / 11.0 - 1) * 100, abs=1e-3
    )
    assert prem["ret_5d"]["count"] == 0 and "skip" in prem["ret_5d"]["note"]
    assert (
        "open_proxy" in json.dumps(prem, ensure_ascii=False)
        or "开盘价代理" in prem["note"]
    )


# ─────────────────── 与旧 BacktestLab 差异（纯函数） ───────────────────


def test_legacy_diff_report(market_db):
    rep = run(market_db, [sig("001001")], exit_scene="next_close")
    legacy = [
        {
            "symbol": "001001",
            "signal_date": D1,
            "pnl_pct": 8.0,
        },  # 旧口径无条件次日开盘买入
        {"symbol": "001005", "signal_date": D1, "pnl_pct": 9.0},  # 实际一字买不到
    ]
    diff = lb.legacy_lab_diff_report(legacy, rep)
    assert diff["legacy_only_signals"] == ["001005@2026-01-06"]
    assert diff["common_signals"] == 1
    assert diff["legacy_sum_single_trade_pct"] == 17.0
    assert diff["mean_pnl_diff_pct(new-legacy)"] is not None
    assert "系统性高估" in diff["legacy_only_note"]


# ─────────────────── 工程性：持久化 / 复现 / 参数校验 ───────────────────


def test_persist_and_repro(tmp_path, market_db):
    out = tmp_path / "reports" / "run1.json"
    rep = run(market_db, [sig("001001")], persist_path=str(out))
    loaded = json.loads(out.read_text(encoding="utf-8"))
    assert loaded["engine"] == rep["engine"] == "limit_up_backtest/v1"
    assert loaded["repro"]["seed"] == rep["params"]["seed"]
    assert loaded["data_watermark"]["kline_daily_rows"] > 0
    assert len(loaded["repro"]["signals_hash"]) == 16


def test_pandas_dataframe_input_and_normalization(market_db):
    df = pd.DataFrame([{"symbol": "1001", "signal_date": D1}])
    rep = run(market_db, df)
    assert rep["signal_details"][0]["symbol"] == "001001"  # zfill(6)


def test_assumptions_in_report(market_db):
    rep = run(market_db, [sig("001001")])
    a = rep["assumptions"]
    assert "万 2.50" in a["commission"] and "卖出万 5.00" in a["stamp_tax"]
    assert "5%" in a["capacity"]
    assert "模型假设" in a["queue_fill_model"]
    assert "开盘价代理" in a["auction"]
    assert "strict" in a["strict_mode"]


def test_validation_errors(market_db):
    with pytest.raises(ValueError):
        run(market_db, [sig("001001")], mode="aggressive")
    with pytest.raises(ValueError):
        run(market_db, [sig("001001")], entry_scene="t0")
    with pytest.raises(ValueError):
        run(market_db, [sig("001001")], exit_scene="hold_days", hold_days=0)
    with pytest.raises(ValueError):
        run(market_db, [{"signal_date": D1}])
    with pytest.raises(TypeError):
        run(market_db, 123)
    with pytest.raises(FileNotFoundError):
        lb.run_limit_up_backtest([sig("001001")], db_path="data/不存在的库.db")


def test_queue_fill_probability_model_monotonic(market_db):
    """封单越强/首封越早 → 成交概率越低（只用 T 日收盘前字段）。"""
    view = lb.MarketDataView(market_db)
    strong = lb.build_board_context(view, "001005", D1, "合成")  # 09:31 封死 seal 0.96
    weak = lb.build_board_context(view, "001001", D1, "合成")  # 10:30 seal 0.25
    p_strong = lb.queue_fill_probability(strong)
    p_weak = lb.queue_fill_probability(weak)
    assert p_strong is not None and p_weak is not None
    assert p_strong < p_weak  # 封单更强/首封更早 → 排队成交概率更低
    assert p_strong <= lb.QueueFillConfig().yizhi_prob_cap  # 一字板概率被封顶
    assert strong.yizhi and not weak.yizhi
    # 炸板：确定性成交概率 1
    zb = lb.build_board_context(view, "001004", D1, "合成")
    assert lb.queue_fill_probability(zb) == 1.0
    # 缺失封单数据：None（不造数）
    nb = lb.build_board_context(view, "001011", D1, "合成")
    assert lb.queue_fill_probability(nb) is None


def test_rng_draw_matches_recorded(market_db):
    """loose 成交判定与报告中记录的 seed/draw 一致（可复现审计）。"""
    rep = run(market_db, [sig("001005")], entry_scene="queue_t", mode="loose", seed=42)
    det = rep["signal_details"][0]
    digest = hashlib.sha256(f"42|001005|{D1}|queue".encode()).hexdigest()
    draw = random.Random(int(digest[:16], 16)).random()
    assert det["queue_fill_draw"] == draw
    assert det["filled"] == (draw <= det["queue_fill_prob"])
