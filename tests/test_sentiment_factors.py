"""#25 日频情绪因子表（app.services.sentiment_factors）离线单元测试。

全部使用 tmp_path 下的 SQLite fixture（schema 来自 #22 迁移），
不访问网络、不依赖真实行情库。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.services.sentiment_factors import (
    INTRADAY_OUTPUT_COLUMNS,
    OUTPUT_COLUMNS,
    VALID_MARKET_SCOPES,
    compute_close_factors,
    compute_intraday_estimates,
    compute_sentiment_frame,
    limit_pct_for,
    normalize_symbol,
)
from app.services.sqlite_migrations import (
    MARKET_KLINE_DB_MIGRATIONS,
    apply_migrations,
)

UPDATED_AT = "2026-06-01T16:00:00+08:00"
DATES = [
    "2026-06-01",
    "2026-06-02",
    "2026-06-03",
    "2026-06-04",
    "2026-06-05",
    "2026-06-08",
    "2026-06-09",
    "2026-06-10",
]
# 小样本 fixture 统一关闭"新股剔除"（默认 5 日会把整段样本当成新股）；
# 新股规则本身由 test_new_stock_and_delisting_exclusion 单独覆盖。
MATURE = {"exclude_new_days": 0}


@pytest.fixture()
def db(tmp_path: Path):
    path = tmp_path / "market_kline.db"
    conn = sqlite3.connect(path)
    apply_migrations(conn, MARKET_KLINE_DB_MIGRATIONS)
    conn.commit()
    yield path, conn
    conn.close()


def _kline(conn, symbol, trade_date, open_, high, low, close, amount=1_000.0):
    conn.execute(
        """
        INSERT OR REPLACE INTO kline_daily(
            symbol, trade_date, open, high, low, close, volume, amount,
            updated_at)
        VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (symbol, trade_date, open_, high, low, close, 100.0, amount, UPDATED_AT),
    )


def _limit(
    conn,
    symbol,
    trade_date,
    flag,
    times=None,
    close=None,
    name=None,
    theme=None,
    pct_chg=None,
):
    conn.execute(
        """
        INSERT OR REPLACE INTO limit_board_daily(
            symbol, trade_date, limit_flag, limit_times, close, name, theme,
            pct_chg, synced_at)
        VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (symbol, trade_date, flag, times, close, name, theme, pct_chg, UPDATED_AT),
    )


def _index(conn, trade_date, close, pct_chg=None, ts_code="000001.SH"):
    conn.execute(
        """
        INSERT OR REPLACE INTO index_daily(
            ts_code, trade_date, close, pre_close, pct_chg, updated_at)
        VALUES(?,?,?,?,?,?)
        """,
        (ts_code, trade_date, close, None, pct_chg, UPDATED_AT),
    )


def _flow(conn, trade_date, north_money=None):
    conn.execute(
        """
        INSERT OR REPLACE INTO market_capital_flow(
            trade_date, north_money, updated_at) VALUES(?,?,?)
        """,
        (trade_date, north_money, UPDATED_AT),
    )


def _name(conn, symbol, name):
    conn.execute(
        "INSERT OR REPLACE INTO symbol_names(symbol, name, updated_at) VALUES(?,?,?)",
        (symbol, name, UPDATED_AT),
    )


def _minute(
    conn, symbol, trade_date, hhmm, close, high=None, low=None, open_=None, amount=10.0
):
    conn.execute(
        """
        INSERT OR REPLACE INTO kline_minute(
            symbol, bar_time, trade_date, open, high, low, close, volume,
            amount, updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        (
            symbol,
            f"{trade_date} {hhmm}:00",
            trade_date,
            open_ if open_ is not None else close,
            high if high is not None else close,
            low if low is not None else close,
            close,
            5.0,
            amount,
            UPDATED_AT,
        ),
    )


def _rows(frame):
    return frame.to_dict("records")


def _row_for(frame, trade_date):
    for row in _rows(frame):
        if row["trade_date"] == trade_date:
            return row
    raise AssertionError(f"row {trade_date} not found")


# ─── 基础工具 ───


def test_normalize_symbol_and_limit_pct():
    assert normalize_symbol("600519.SH") == "600519"
    assert normalize_symbol("sh.600519") == "600519"
    assert normalize_symbol("300750") == "300750"
    assert limit_pct_for("600000", is_st=False) == 0.10
    assert limit_pct_for("600000", is_st=True) == 0.05
    assert limit_pct_for("300001", is_st=False) == 0.20
    assert limit_pct_for("688001", is_st=False) == 0.20
    assert limit_pct_for("830799", is_st=False) == 0.30


def test_argument_validation(db):
    path, _ = db
    with pytest.raises(ValueError):
        compute_sentiment_frame(
            path, "2026-06-01", "2026-06-02", market_scope="unknown"
        )
    with pytest.raises(ValueError):
        compute_sentiment_frame(path, "2026-06-05", "2026-06-01")
    with pytest.raises(FileNotFoundError):
        compute_sentiment_frame(path.parent / "missing.db", "2026-06-01", "2026-06-02")
    assert "main" in VALID_MARKET_SCOPES


# ─── 广度 ───


def test_breadth_up_down_halted_and_st_exclusion(db):
    path, conn = db
    d1, d2 = DATES[0], DATES[1]
    _kline(conn, "600001", d1, 10, 10, 10, 10.0)
    _kline(conn, "600001", d2, 10.2, 10.8, 10.2, 10.6)  # +6% 强势
    _kline(conn, "000002", d1, 10, 10, 10, 10.0)
    _kline(conn, "000002", d2, 9.9, 9.9, 9.4, 9.5)  # -5% 弱势
    _kline(conn, "000003", d1, 10, 10, 10, 10.0)
    _kline(conn, "000003", d2, 10, 10.05, 9.98, 10.01)  # +0.1%
    _kline(conn, "600004", d1, 10, 10, 10, 10.0)  # d2 停牌
    _kline(conn, "600005", d1, 10, 10, 10, 10.0)
    _kline(conn, "600005", d2, 10.1, 10.5, 10.1, 10.5)
    _name(conn, "600005", "ST海润")  # ST 默认剔除
    conn.commit()

    row = _row_for(compute_sentiment_frame(path, d1, d2, **MATURE), d2)
    assert row["traded_count"] == 4
    assert row["excluded_st_count"] == 1
    assert row["breadth_sample_count"] == 3
    assert row["up_count"] == 2
    assert row["down_count"] == 1
    assert row["strong_count"] == 1
    assert row["weak_count"] == 1
    assert row["halted_or_missing_count"] == 1  # 600004 d1 有 d2 无
    assert abs(row["up_ratio"] - 2 / 3) < 1e-6
    assert abs(row["adv_dec_ratio"] - 2.0) < 1e-9
    assert row["mean_return"] is not None
    # include_st=True 时 600005 计入样本
    row2 = _row_for(
        compute_sentiment_frame(path, d1, d2, include_st=True, **MATURE), d2
    )
    assert row2["breadth_sample_count"] == 4
    assert row2["excluded_st_count"] == 0


def test_new_stock_and_delisting_exclusion(db):
    path, conn = db
    d_hist_1, d_hist_2 = DATES[0], DATES[1]
    d_first, d_today = DATES[6], DATES[7]
    # 600010：上市早已超过 5 个交易日
    for d in (
        d_hist_1,
        d_hist_2,
        DATES[2],
        DATES[3],
        DATES[4],
        DATES[5],
        d_first,
        d_today,
    ):
        _kline(conn, "600010", d, 10, 10, 10, 10.0)
    _kline(conn, "000011", d_first, 10, 11, 10, 11.0)  # 新上市
    _kline(conn, "000011", d_today, 11, 12, 11, 12.0)
    _kline(conn, "000012", d_first, 10, 10, 10, 10.0)  # 疑似退市尾部
    conn.commit()
    row = _row_for(compute_sentiment_frame(path, d_first, d_today), d_today)
    assert row["excluded_new_count"] == 1  # 000011 上市 <5 交易日
    assert row["breadth_sample_count"] == 1  # 只剩 600010
    assert row["flat_count"] == 1
    assert row["halted_or_missing_count"] == 1  # 000012 停止披露
    row_off = _row_for(
        compute_sentiment_frame(path, d_first, d_today, exclude_new_days=0),
        d_today,
    )
    assert row_off["excluded_new_count"] == 0
    assert row_off["breadth_sample_count"] == 2


# ─── 涨停/跌停 / 连板梯队 ───


def _ladder_fixture(conn):
    d1, d2 = DATES[0], DATES[1]
    # d1：A/B 首板涨停，C 炸板，另有 2 板 D
    _kline(conn, "600001", d1, 10, 11, 10, 11.0)
    _kline(conn, "600002", d1, 10, 11, 10, 11.0)
    _kline(conn, "600003", d1, 10, 11, 10.2, 10.2)
    _kline(conn, "600004", d1, 10, 11, 10, 11.0)
    _limit(conn, "600001", d1, "U", times=1, close=11.0)
    _limit(conn, "600002", d1, "U", times=1, close=11.0)
    _limit(conn, "600004", d1, "U", times=2, close=11.0)
    _limit(conn, "600003", d1, "Z", times=None)
    # d2：A 晋级 2 板，D 3 板，E 4 板，B 跌停，F 炸板
    _kline(conn, "600001", d2, 11, 12.1, 11, 12.1)
    _kline(conn, "600002", d2, 11, 11, 9.9, 9.9)
    _kline(conn, "600005", d2, 10, 11, 10, 11.0)
    _kline(conn, "600006", d2, 10, 11, 10, 11.0)
    _kline(conn, "600004", d2, 11, 12.1, 11, 12.1)
    _kline(conn, "600006", d1, 10, 10, 10, 10.0)
    _kline(conn, "600005", d1, 10, 10, 10, 10.0)
    _limit(conn, "600001", d2, "U", times=2, close=12.1)
    _limit(conn, "600004", d2, "U", times=3, close=12.1)
    _limit(conn, "600005", d2, "U", times=4, close=11.0)
    _limit(conn, "600002", d2, "D", times=None, close=9.9)
    _limit(conn, "600006", d2, "Z")
    return d1, d2


def test_ladder_and_promotion_rates(db):
    path, conn = db
    d1, d2 = _ladder_fixture(conn)
    conn.commit()
    frame = compute_sentiment_frame(path, d1, d2, **MATURE)
    r1 = _row_for(frame, d1)
    assert r1["limit_up_count"] == 3
    assert r1["limit_down_count"] == 0
    assert r1["limit_up_down_ratio"] is None  # 跌停 0 → 缺失而非 99
    assert r1["broken_board_count"] == 1
    assert abs(r1["broken_board_rate"] - 0.25) < 1e-9
    assert r1["max_consecutive_boards"] == 2
    assert r1["first_board_count"] == 2
    assert r1["continuation_count"] == 1
    # d1 无更早涨停池 → 晋级率缺失
    assert r1["promotion_rate_1_to_2"] is None

    r2 = _row_for(frame, d2)
    assert r2["limit_up_count"] == 3
    assert r2["limit_down_count"] == 1
    assert r2["limit_up_down_ratio"] == 3.0
    assert r2["max_consecutive_boards"] == 4
    assert r2["board_count_2"] == 1
    assert r2["board_count_3"] == 1
    assert r2["board_count_4_plus"] == 1
    assert r2["ladder_integrity"] == 1
    # 1进2：d1 首板 {600001,600002}，d2 中 600001 变 2 板 → 1/2
    assert abs(r2["promotion_rate_1_to_2"] - 0.5) < 1e-9
    assert r2["first_board_promotion_rate"] == r2["promotion_rate_1_to_2"]
    # 2进3：d1 的 2 板 {600004} → d2 3 板 → 1/1
    assert r2["promotion_rate_2_to_3"] == 1.0
    assert r2["promotion_rate_3_to_4"] is None  # d1 无 3 板 → 分母 0
    assert abs(r2["ladder_concentration"] - 1 / 3) < 1e-6  # 2/3/4 各一
    assert r2["broken_board_count"] == 1


def test_limit_times_missing_returns_none(db):
    path, conn = db
    d1 = DATES[0]
    _kline(conn, "600020", d1, 10, 11, 10, 11.0)
    _limit(conn, "600020", d1, "U", times=None, close=11.0)
    conn.commit()
    row = _row_for(compute_sentiment_frame(path, d1, d1, **MATURE), d1)
    assert row["limit_up_count"] == 1
    assert row["max_consecutive_boards"] is None
    assert row["first_board_count"] is None
    flags = json.loads(row["missing_flags"])
    assert "limit_times_missing" in flags


def test_limit_board_missing_day_not_zero(db):
    path, conn = db
    d1, d2 = DATES[0], DATES[1]
    _kline(conn, "600021", d1, 10, 10, 10, 10.0)
    _kline(conn, "600021", d2, 10, 11, 10, 11.0)
    _limit(conn, "600021", d1, "U", times=1, close=11.0)
    conn.commit()
    row = _row_for(compute_sentiment_frame(path, d1, d2, **MATURE), d2)
    # d2 无 limit_board 行 → 涨停族 None（不填 0），广度照常
    assert row["limit_up_count"] is None
    assert row["max_consecutive_boards"] is None
    assert row["broken_board_rate"] is None
    assert row["up_count"] == 1
    flags = json.loads(row["missing_flags"])
    assert "limit_board_missing" in flags


def test_real_zero_limit_day(db):
    path, conn = db
    d1 = DATES[0]
    _kline(conn, "600022", d1, 10, 10, 9.5, 9.5)
    _limit(conn, "600022", d1, "D", close=9.5)
    conn.commit()
    row = _row_for(compute_sentiment_frame(path, d1, d1, **MATURE), d1)
    assert row["limit_up_count"] == 0
    assert row["limit_down_count"] == 1
    assert row["limit_up_down_ratio"] == 0.0
    assert row["max_consecutive_boards"] == 0
    assert row["first_board_count"] == 0
    assert row["ladder_integrity"] == 0
    assert row["broken_board_count"] == 0
    assert row["broken_board_rate"] is None  # 0/0 → 缺失
    flags = json.loads(row["missing_flags"])
    assert "no_limit_up_today" in flags


# ─── 溢价 / 赚钱效应（观察日记账，PIT） ───


def test_premium_recorded_on_observation_day(db):
    path, conn = db
    d1, d2 = DATES[0], DATES[1]
    _kline(conn, "600030", d1, 10, 11, 10, 11.0)
    _kline(conn, "600030", d2, 11.5, 12.0, 11.2, 11.0)  # open +4.5% high +9%
    _kline(conn, "600031", d1, 10, 11, 10, 11.0)
    _kline(conn, "600031", d2, 10.4, 10.6, 10.0, 10.2)  # close -7.3% 大面
    _kline(conn, "600032", d1, 10, 11, 10, 11.0)
    # 600032 d2 停牌 → 溢价样本剔除该标的（分母不含停牌）
    _limit(conn, "600030", d1, "U", times=1, close=11.0)
    _limit(conn, "600031", d1, "U", times=2, close=11.0)
    _limit(conn, "600032", d1, "U", times=1, close=11.0)
    conn.commit()

    frame = compute_sentiment_frame(path, d1, d2, **MATURE)
    r1 = _row_for(frame, d1)
    # d1 行没有未来数据：溢价族缺失（尚未到观察日），不是 0
    assert r1["yesterday_limit_up_premium_close_mean"] is None
    assert "prev_limit_pool_missing_effect" in json.loads(r1["missing_flags"])

    r2 = _row_for(frame, d2)
    o = r2["yesterday_limit_up_premium_open_mean"]
    c = r2["yesterday_limit_up_premium_close_mean"]
    h = r2["yesterday_limit_up_premium_high_mean"]
    assert abs(o - (0.5 / 11 + -0.6 / 11) / 2) < 1e-6
    assert abs(c - (0.0 / 11 + -0.8 / 11) / 2) < 1e-6
    assert abs(h - (1.0 / 11 + -0.4 / 11) / 2) < 1e-6
    assert r2["money_making_effect"] == 0.0
    assert abs(r2["loss_effect"] - 0.5) < 1e-9
    assert r2["big_loss_count"] == 1
    assert abs(r2["big_loss_rate"] - 0.5) < 1e-9
    assert abs(r2["yesterday_continuation_premium_close_mean"] - (-0.8 / 11)) < 1e-6
    assert "cohort_halted:1" in json.loads(r2["missing_flags"])


def test_premium_no_lookahead_beyond_observation_day(db):
    path, conn = db
    d1, d2, d3 = DATES[0], DATES[1], DATES[2]
    _kline(conn, "600040", d1, 10, 11, 10, 11.0)
    _kline(conn, "600040", d2, 11, 12, 11, 12.0)
    _kline(conn, "600040", d3, 12, 13, 12, 13.0)
    _limit(conn, "600040", d1, "U", times=1, close=11.0)
    conn.commit()
    full = _row_for(compute_sentiment_frame(path, d1, d3, **MATURE), d2)
    # d2 行的溢价只用 d1/d2 数据；加入 d3 不改变 d2 行任何值
    short = _row_for(compute_sentiment_frame(path, d1, d2, **MATURE), d2)
    assert full == short


def test_st_cohort_excluded_from_premium(db):
    path, conn = db
    d1, d2 = DATES[0], DATES[1]
    _kline(conn, "600050", d1, 10, 11, 10, 11.0)
    _kline(conn, "600050", d2, 12, 12, 12, 12.0)
    _kline(conn, "000051", d1, 10, 11, 10, 11.0)
    _kline(conn, "000051", d2, 9.9, 9.9, 9.9, 9.9)
    _limit(conn, "600050", d1, "U", times=1, close=11.0, name="ST中1")
    _limit(conn, "000051", d1, "U", times=1, close=11.0, name="正常股")
    conn.commit()
    row = _row_for(compute_sentiment_frame(path, d1, d2, **MATURE), d2)
    # 默认剔除 ST：溢价样本只含 000051 → -10%
    assert abs(row["yesterday_limit_up_premium_close_mean"] + 0.1) < 1e-9
    row_incl = _row_for(
        compute_sentiment_frame(path, d1, d2, include_st=True, **MATURE), d2
    )
    assert (
        abs(row_incl["yesterday_limit_up_premium_close_mean"] - (1 / 11 - 0.1) / 2)
        < 1e-6
    )


# ─── 指数 / 资金 / 成交额比 ───


def test_index_flow_and_amount_ratio(db):
    path, conn = db
    for i, d in enumerate(DATES):
        _kline(conn, "600060", d, 10, 10, 10, 10.0, amount=1000.0)
        _index(conn, d, close=100 * (1.01**i), pct_chg=1.0)
    _flow(conn, DATES[-1], north_money=12345.6)
    conn.commit()
    last = _row_for(
        compute_sentiment_frame(path, DATES[0], DATES[-1], **MATURE),
        DATES[-1],
    )
    assert abs(last["index_return_1d"] - 0.01) < 1e-12
    assert last["index_return_5d"] is not None
    assert last["index_return_20d"] is None  # 8 天历史不足 20 → 缺失
    assert last["index_volatility_20d"] is None
    flags = json.loads(last["missing_flags"])
    assert "index_return_20d_insufficient_history" in flags
    assert last["northbound_net_flow"] == 12345.6
    assert abs(last["market_amount_ratio_5d"] - 1.0) < 1e-9
    # 依赖 #8/#1 的列先占位为 None
    assert last["main_theme_count"] is None
    assert last["theme_diffusion"] is None
    assert last["margin_balance_change"] is None
    first = _row_for(
        compute_sentiment_frame(path, DATES[0], DATES[-1], **MATURE),
        DATES[0],
    )
    assert first["northbound_net_flow"] is None
    assert "northbound_missing" in json.loads(first["missing_flags"])


# ─── 口径参数化 ───


def test_market_scope_filters(db):
    path, conn = db
    d1 = DATES[0]
    for symbol in ("600070", "000070", "300070", "688070", "830070"):
        _kline(conn, symbol, d1, 10, 10, 10, 10.0)
        _kline(conn, symbol, DATES[1], 10, 11, 10, 11.0)
        _limit(conn, symbol, d1, "U", times=1, close=11.0)
    conn.commit()
    main = _row_for(
        compute_sentiment_frame(path, d1, DATES[1], market_scope="main", **MATURE),
        d1,
    )
    allm = _row_for(
        compute_sentiment_frame(path, d1, DATES[1], market_scope="all", **MATURE),
        d1,
    )
    shsz = _row_for(
        compute_sentiment_frame(path, d1, DATES[1], market_scope="sh_sz", **MATURE),
        d1,
    )
    assert main["limit_up_count"] == 2  # 60/00
    assert allm["limit_up_count"] == 5  # 含创业/科创/北交所
    assert shsz["limit_up_count"] == 4  # 沪深全板不含北交所
    assert main["market_scope"] == "main"


# ─── 幂等 / 窗口无关 / 一致性 ───


def _rich_fixture(conn):
    """8 个交易日、含涨停池/炸板/跌停/指数的连续数据。"""
    for i, d in enumerate(DATES):
        _kline(conn, "600100", d, 10, 11, 10, 11.0, amount=2000.0 + i)
        _kline(conn, "000101", d, 10, 10.2, 9.8, 9.9, amount=1000.0 + i)
        _kline(conn, "300102", d, 10, 12, 10, 12.0, amount=1500.0 + i)
        _index(conn, d, close=100 + i, pct_chg=0.5)
        _limit(conn, "600100", d, "U", times=(i % 3) + 1, close=11.0, theme="电")
        _limit(conn, "000101", d, "D", close=9.9)
        if i >= 1:
            _limit(conn, "300102", d, "Z")
    conn.commit()


def test_frame_idempotent_and_window_independent(db):
    path, conn = db
    _rich_fixture(conn)
    a = _rows(compute_sentiment_frame(path, DATES[3], DATES[6], **MATURE))
    b = _rows(compute_sentiment_frame(path, DATES[0], DATES[6], **MATURE))
    a_again = _rows(compute_sentiment_frame(path, DATES[3], DATES[6], **MATURE))
    assert a == a_again  # 幂等
    assert [r["trade_date"] for r in a] == DATES[3:7]
    overlap = [r for r in b if DATES[3] <= r["trade_date"] <= DATES[6]]
    assert a == overlap  # 增量窗口与全量回填逐行一致
    # compute_close_factors 与帧内行一致
    single = compute_close_factors(path, DATES[6], **MATURE)
    assert single == overlap[-1]


def test_appending_later_data_never_rewrites_earlier_rows(db):
    path, conn = db
    _rich_fixture(conn)
    before = _row_for(
        compute_sentiment_frame(path, DATES[0], DATES[5], **MATURE),
        DATES[5],
    )
    _kline(conn, "600100", DATES[6], 11, 12, 11, 12.0, amount=9999.0)
    _kline(conn, "000101", DATES[6], 11, 12, 11, 12.0, amount=8888.0)
    conn.commit()
    after = _row_for(
        compute_sentiment_frame(path, DATES[0], DATES[6], **MATURE),
        DATES[5],
    )
    assert before == after  # 未来数据不改变已收盘行（无未来函数）


def test_regime_and_score_present(db):
    path, conn = db
    _rich_fixture(conn)
    frame = compute_sentiment_frame(path, DATES[2], DATES[7])
    for row in _rows(frame):
        assert row["sentiment_score"] is None or (
            0.0 <= row["sentiment_score"] <= 100.0
        )
        assert row["regime"] is None or row["regime"] in (
            "冰点",
            "修复",
            "发酵",
            "高潮",
            "分歧",
            "退潮",
        )
        assert row["market_scope"] == "all"


def test_intraday_only_day_without_kline(db):
    path, conn = db
    d1 = DATES[0]
    _limit(conn, "600110", d1, "U", times=1)
    conn.commit()
    row = _row_for(compute_sentiment_frame(path, d1, d1), d1)
    assert row["limit_up_count"] == 1
    assert row["up_count"] is None
    flags = json.loads(row["missing_flags"])
    assert "kline_day_missing" in flags


# ─── 盘中估算（intraday_ 前缀，隔离） ───


def test_intraday_estimates_sealed_broken_and_cutoff(db):
    path, conn = db
    d1, d2 = DATES[0], DATES[1]
    _kline(conn, "600200", d1, 10, 11, 10, 11.0)
    _kline(conn, "600201", d1, 10, 11, 10, 11.0)
    _kline(conn, "600202", d1, 10, 10, 10, 10.0)
    _kline(conn, "000203", d1, 10, 10, 10, 10.0)
    _limit(conn, "600200", d1, "U", times=1, close=11.0)
    _limit(conn, "600201", d1, "U", times=2, close=11.0)
    # d2 盘中：600200 09:31 起封板（12.1 = 11*1.1）；
    # 600201 14:00 触及涨停后回落（炸板）；000203 跌停。
    _minute(conn, "600200", d2, "09:31", close=12.1, high=12.1)
    _minute(conn, "600201", d2, "09:31", close=11.3, high=11.3)
    _minute(conn, "600201", d2, "14:00", close=11.35, high=12.1)
    _minute(conn, "600201", d2, "14:31", close=11.2, high=11.25)
    _minute(conn, "600202", d2, "09:31", close=10.0, high=10.0)
    _minute(conn, "000203", d2, "09:31", close=9.0, high=10.0)
    conn.commit()

    mid = compute_intraday_estimates(path, d2, "10:00", **MATURE)
    assert mid["intraday_limit_up_count"] == 1
    assert mid["intraday_broken_board_count"] == 0  # 14:00 的触碰未到
    assert mid["intraday_max_consecutive_boards"] == 2
    assert mid["intraday_promotion_rate_1_to_2"] == 1.0  # 600200 首板再封
    assert mid["intraday_down_count"] == 1
    assert mid["intraday_as_of_time"] == "10:00"

    late = compute_intraday_estimates(path, d2, "14:30", **MATURE)
    assert late["intraday_limit_up_count"] == 1
    assert late["intraday_broken_board_count"] == 1  # 600201 触板回落
    assert late["intraday_broken_board_rate"] == 0.5
    assert late["intraday_yesterday_limit_up_premium_close_mean"] is not None
    score = late["intraday_sentiment_score"]
    assert score is None or 0 <= score <= 100


def test_intraday_output_is_prefixed_and_isolated(db):
    path, conn = db
    d1, d2 = DATES[0], DATES[1]
    _kline(conn, "600300", d1, 10, 10, 10, 10.0)
    _minute(conn, "600300", d2, "09:31", close=11.0, high=11.0)
    conn.commit()
    snap = compute_intraday_estimates(path, d2, "10:00", **MATURE)
    for key in snap:
        assert key in ("trade_date", "market_scope") or key.startswith("intraday_"), key
    for col in OUTPUT_COLUMNS:
        if col in ("trade_date", "market_scope"):
            continue
        assert col not in snap
    flags = json.loads(snap["intraday_missing_flags"])
    assert "prev_limit_pool_missing_intraday" in flags
    # 盘中快照不影响收盘帧：同一库先后计算收盘行一致
    frame_a = _row_for(compute_sentiment_frame(path, d1, d1, **MATURE), d1)
    frame_b = _row_for(compute_sentiment_frame(path, d1, d1, **MATURE), d1)
    assert frame_a == frame_b


def test_intraday_without_minute_data(db):
    path, conn = db
    d1, d2 = DATES[0], DATES[1]
    _kline(conn, "600400", d1, 10, 10, 10, 10.0)
    _kline(conn, "600400", d2, 10, 10, 10, 10.0)
    conn.commit()
    snap = compute_intraday_estimates(path, d2, "11:30", **MATURE)
    flags = json.loads(snap["intraday_missing_flags"])
    assert "minute_data_missing" in flags
    assert snap["intraday_limit_up_count"] is None
    assert snap["intraday_sentiment_score"] is None
    assert snap["intraday_regime"] is None


def test_intraday_as_of_time_validation(db):
    path, _ = db
    with pytest.raises(ValueError):
        compute_intraday_estimates(path, DATES[0], "上午")


def test_intraday_output_columns_documented_subset():
    # INTRADAY_OUTPUT_COLUMNS 只是文档化子集；确保全部带前缀。
    assert all(
        c.startswith("intraday_") or c in ("trade_date", "market_scope")
        for c in INTRADAY_OUTPUT_COLUMNS
    )
