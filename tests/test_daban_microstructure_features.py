"""#24 涨停微观结构 + 股性因子单元测试（deterministic fixture，不访问网络）。

覆盖：一字板、T字板、炸板、无封单、停牌、除权、新股、缺失率、PIT 抽查。

Fixture 日历：60 个交易日（bdate），``dates[59]`` 为 T 日。所有价格手算可核对。
"""

from __future__ import annotations

import random
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app.services.kline_store import KlineSQLiteStore
from strategy.first_limit_alpha.microstructure_features import (
    FACTOR_SPECS,
    PROXY_FACTORS,
    build_microstructure_features,
    microstructure_factor_specs,
    microstructure_output_columns,
    microstructure_quality_report,
    session_minutes,
)

SYNC_AT = "2026-04-01T17:00:00+08:00"
N_DAYS = 60
UNIVERSE = [
    "600111",  # 一字板四连板（idx56..59）
    "000222",  # T字板首板（idx59），历史首板 idx29
    "300333",  # 尾盘炸板（Z，idx59），历史首板 idx54
    "600444",  # 涨停但无封单额/无首封时间（idx59）
    "000888",  # 日线一字板但 limit 表整行缺失（权限降级，日线代理）
    "600555",  # idx19 首板、idx20 除权、idx48 起停牌
    "688777",  # 新股（只有 idx58/59 两天 K 线，idx59 一字板）
    "600999",  # 噪声股：idx39 涨停、idx59 再涨停（与 000222 同题材）
    "000999",  # 噪声股：全程无涨停
]


def _dates() -> list[str]:
    return [d.date().isoformat() for d in pd.bdate_range("2026-01-05", periods=N_DAYS)]


def _kline(date: str, o: float, h: float, lo: float, c: float, volume: float = 1_000_000.0) -> dict:
    return {"trade_date": date, "open": o, "high": h, "low": lo, "close": c, "volume": volume, "amount": volume * c}


def _drift_row(date: str, close: float = 10.0) -> dict:
    return _kline(date, round(close * 1.02, 4), round(close * 1.05, 4), round(close * 0.99, 4), close)


def _flat_row(date: str, price: float) -> dict:
    return _kline(date, price, price, price, price)


def _board(
    symbol: str,
    date: str,
    *,
    flag: str = "U",
    first_time: str | None = None,
    last_time: str | None = None,
    open_times: int | None = 0,
    limit_times: int | None = 1,
    up_stat: str | None = None,
    fd_amount: float | None = None,
    amount: float | None = None,
    limit_amount: float | None = None,
    float_mv: float | None = None,
    total_mv: float | None = None,
    close: float | None = None,
    pct_chg: float | None = None,
    turnover_ratio: float | None = None,
) -> dict:
    return {
        "symbol": symbol,
        "trade_date": date,
        "limit_flag": flag,
        "first_time": first_time,
        "last_time": last_time,
        "open_times": open_times,
        "limit_times": limit_times,
        "up_stat": up_stat,
        "fd_amount": fd_amount,
        "amount": amount,
        "limit_amount": limit_amount,
        "float_mv": float_mv,
        "total_mv": total_mv,
        "close": close,
        "pct_chg": pct_chg,
        "turnover_ratio": turnover_ratio,
    }


def build_fixture_db(tmp_path: Path) -> tuple[str, list[str]]:
    """构造确定性打板数据库。返回 (db_path, dates)。"""
    dates = _dates()
    store = KlineSQLiteStore(str(tmp_path / "market_kline.db"))

    klines: dict[str, list[dict]] = {s: [_drift_row(d) for d in dates] for s in UNIVERSE}

    def _seal_row(date: str, price: float, board_amount: float) -> dict:
        # 板上日：K 线成交额与 limit 表 amount 对齐（元），volume 反推
        return _kline(date, price, price, price, price, volume=board_amount / price)

    # 600111：一字板四连板 idx56..59（11.0 / 12.1 / 13.31 / 14.64）
    for idx, price, amt in [(56, 11.0, 5.0e8), (57, 12.1, 6.0e8), (58, 13.31, 7.0e8), (59, 14.64, 6.0e8)]:
        klines["600111"][idx] = _seal_row(dates[idx], price, amt)

    # 000222：idx29 历史首板（T字），idx59 再首板（T字板，题材 AI 算力）
    klines["000222"][29] = _kline(dates[29], 11.0, 11.0, 10.2, 11.0, volume=5.0e8 / 11.0)
    klines["000222"][59] = _kline(dates[59], 11.0, 11.0, 10.45, 11.0, volume=4.0e8 / 11.0)

    # 300333：idx54 首板（20cm 一字），idx59 尾盘炸板（触 12.0 收 11.5）
    klines["300333"][54] = _seal_row(dates[54], 12.0, 2.0e8)
    klines["300333"][59] = _kline(dates[59], 11.0, 12.0, 10.8, 11.5, volume=3.0e8 / 11.5)

    # 600444：涨停（收盘封死）但 fd/first/last 全 NULL
    klines["600444"][59] = _kline(dates[59], 10.2, 11.0, 10.0, 11.0, volume=2.0e8 / 11.0)

    # 000888：日线一字板，但 limit_board_daily 完全缺行（模拟无权限降级）
    klines["000888"][59] = _flat_row(dates[59], 11.0)

    # 600555：idx19 首板一字（11.0），idx20 除权（次日 close 5.0），idx48 起停牌
    klines["600555"][19] = _seal_row(dates[19], 11.0, 1.0e8)
    klines["600555"][20] = _drift_row(dates[20], 5.0)
    klines["600555"] = klines["600555"][:48]

    # 688777：新股，只有 idx58/59 两天，idx59 一字板（20cm）
    klines["688777"] = [_flat_row(dates[58], 20.0), _seal_row(dates[59], 24.0, 1.0e9)]

    # 600999：idx39 普通涨停，idx59 再涨停（尾盘封板、题材 AI 算力）
    klines["600999"][39] = _kline(dates[39], 10.2, 11.0, 10.0, 11.0, volume=2.0e8 / 11.0)
    klines["600999"][59] = _kline(dates[59], 10.1, 11.0, 10.0, 11.0, volume=2.0e8 / 11.0)

    boards: list[dict] = [
        # 600111 四连板
        _board("600111", dates[56], limit_times=1, up_stat="1/1", fd_amount=2.5e8, amount=5.0e8,
               limit_amount=2.5e8, float_mv=8e9, total_mv=1e10, close=11.0, pct_chg=10.0,
               first_time="09:25:00", last_time="09:25:00"),
        _board("600111", dates[57], limit_times=2, up_stat="2/2", fd_amount=2.6e8, amount=6.0e8,
               limit_amount=3.0e8, float_mv=8e9, total_mv=1e10, close=12.1, pct_chg=10.0,
               first_time="09:25:00", last_time="09:25:00"),
        _board("600111", dates[58], limit_times=3, up_stat="3/3", fd_amount=2.7e8, amount=7.0e8,
               limit_amount=3.5e8, float_mv=8e9, total_mv=1e10, close=13.31, pct_chg=10.0,
               first_time="09:25:00", last_time="09:25:00"),
        _board("600111", dates[59], limit_times=4, up_stat="4/4", fd_amount=2.0e8, amount=6.0e8,
               limit_amount=3.0e8, float_mv=8e9, total_mv=1e10, close=14.64, pct_chg=10.0,
               first_time="09:25:00", last_time="09:25:00"),
        # 000222
        _board("000222", dates[29], limit_times=1, up_stat="1/1", fd_amount=1.0e8, amount=5.0e8,
               limit_amount=1.0e8, float_mv=3e9, total_mv=5e9, close=11.0, pct_chg=10.0,
               first_time="10:30:00", last_time="13:30:00", open_times=1),
        _board("000222", dates[59], limit_times=1, up_stat="2/2", fd_amount=1.5e8, amount=4.0e8,
               limit_amount=3.0e8, float_mv=3e9, total_mv=5e9, close=11.0, pct_chg=10.0,
               first_time="09:30:00", last_time="14:00:00", open_times=2),
        # 300333
        _board("300333", dates[54], limit_times=1, up_stat="1/1", fd_amount=2.0e8, amount=2.0e8,
               limit_amount=1.0e8, float_mv=4e9, total_mv=6e9, close=12.0, pct_chg=20.0,
               first_time="09:40:00", last_time="09:40:00"),
        _board("300333", dates[59], flag="Z", limit_times=None, up_stat="1/0", fd_amount=None,
               amount=3.0e8, limit_amount=None, float_mv=4e9, total_mv=6e9, close=11.5, pct_chg=15.0,
               first_time="10:00:00", last_time="14:40:00", open_times=3),
        # 600444：无封单额、无首末封板时间
        _board("600444", dates[59], limit_times=1, up_stat="1/1", fd_amount=None, amount=2.0e8,
               limit_amount=1.0e8, float_mv=2e9, total_mv=4e9, close=11.0, pct_chg=10.0,
               first_time=None, last_time=None, open_times=0),
        # 600555：历史首板（除权前）
        _board("600555", dates[19], limit_times=1, up_stat="1/1", fd_amount=1.0e8, amount=1.0e8,
               limit_amount=0.5e8, float_mv=3e9, total_mv=3.5e9, close=11.0, pct_chg=10.0,
               first_time="09:35:00", last_time="09:35:00"),
        # 688777：新股一字板
        _board("688777", dates[59], limit_times=1, up_stat="1/1", fd_amount=3.0e8, amount=1.0e9,
               limit_amount=4.0e8, float_mv=6e9, total_mv=9e9, close=24.0, pct_chg=20.0,
               first_time="09:25:00", last_time="09:25:00"),
        # 600999
        _board("600999", dates[39], limit_times=1, up_stat="1/1", fd_amount=1.2e8, amount=2.0e8,
               limit_amount=0.8e8, float_mv=9e9, total_mv=12e9, close=11.0, pct_chg=10.0,
               first_time="10:10:00", last_time="10:10:00"),
        _board("600999", dates[59], limit_times=1, up_stat="2/2", fd_amount=1.0e8, amount=2.0e8,
               limit_amount=0.6e8, float_mv=9e9, total_mv=12e9, close=11.0, pct_chg=10.0,
               first_time="13:40:00", last_time="13:40:00", open_times=0),
    ]

    for symbol, rows in klines.items():
        store.upsert_symbol_klines(symbol, rows, SYNC_AT)
    assert store.upsert_limit_board_daily(boards, SYNC_AT) == len(boards)
    assert store.update_limit_board_theme(
        [
            {"symbol": "000222", "trade_date": dates[59], "theme": "AI算力", "board_type": "T字板",
             "limit_reason": "算力租赁"},
            {"symbol": "600999", "trade_date": dates[59], "theme": "AI算力", "board_type": None,
             "limit_reason": "IDC"},
        ],
        SYNC_AT,
    )
    return str(store.db_path), dates


def _row(frame: pd.DataFrame, symbol: str) -> pd.Series:
    hit = frame[frame["symbol"] == symbol]
    assert len(hit) == 1, f"missing symbol row {symbol}"
    return hit.iloc[0]


# ─── 基础解析 ────────────────────────────────────────────────────────


def test_session_minutes_parsing():
    assert session_minutes("09:25:00") == 0.0
    assert session_minutes("09:30:00") == 0.0
    assert session_minutes("10:00:00") == 30.0
    assert session_minutes("11:29:00") == 119.0
    assert session_minutes("12:30:00") == 120.0  # 午休钳制
    assert session_minutes("13:00:00") == 120.0
    assert session_minutes("14:40:00") == 220.0
    assert session_minutes("15:00:00") == 240.0
    assert session_minutes("144000") == 220.0  # HHMMSS 紧凑格式
    assert session_minutes(None) is None
    assert session_minutes("") is None
    assert session_minutes("ab:cd:ef") is None


# ─── 封板强度 / 板型 ─────────────────────────────────────────────────


def test_one_word_consecutive_board(tmp_path: Path):
    db, dates = build_fixture_db(tmp_path)
    frame = build_microstructure_features(db, UNIVERSE, dates[59], lookback=250)
    row = _row(frame, "600111")
    assert row["has_limit_row"] == 1
    assert row["limit_flag"] == "U"
    assert row["is_one_word"] == 1
    assert row["is_one_word_is_proxy"] == 0  # 真实字段口径
    assert row["first_seal_minute"] == 0.0
    assert row["last_seal_minute"] == 0.0
    assert row["open_times"] == 0
    assert row["limit_times"] == 4
    assert row["seal_duration_min"] == 240.0
    assert row["seal_duration_min_is_proxy"] == 1  # 上界近似（未扣开板区间）
    assert row["fd_amount"] == pytest.approx(2.0e8)
    assert row["fd_ratio_float_mv"] == pytest.approx(2.0e8 / 8e9)
    assert row["fd_ratio_amount"] == pytest.approx(2.0e8 / 6.0e8)
    assert row["fd_ratio_volume"] == pytest.approx(2.0e8 / 6.0e8)  # volume=amount/close 时退化为封成比
    assert row["on_board_amount"] == pytest.approx(3.0e8)
    assert row["on_board_amount_ratio"] == pytest.approx(0.5)
    assert row["board_position_code"] == 4
    assert row["board_position_label"] == "3进4"
    assert row["prev_limit_times"] == 3
    assert row["is_acceleration_board"] == 1
    assert row["is_tail_board"] == 0
    assert row["is_turnover_board"] == 0  # open_times=0，非换手板
    assert row["up_stat_days"] == 4
    assert row["up_stat_limits"] == 4
    assert row["up_stat_limit_density"] == pytest.approx(1.0)
    assert row["seal_quality_proxy"] == 2
    assert row["seal_quality_proxy_is_proxy"] == 0  # 有真实 limit 行佐证
    # 股性（严格 < T）
    assert row["limit_up_count_250d"] == 3
    assert row["first_board_count_250d"] == 1
    assert row["first_board_next_day_premium_avg"] == pytest.approx(12.1 / 11.0 - 1.0)
    assert row["first_board_next_day_win_rate"] == 1.0
    assert row["continuation_success_rate"] == pytest.approx(1.0)  # idx56/57 两次晋级成功；idx58->T 事件被 shift(1) 排除
    assert row["max_consecutive_boards_250d"] == 3
    assert row["limit_up_amount_avg"] == pytest.approx(6.0e8)
    assert row["float_mv_bucket"] == 2
    assert row["small_cap_limit_up_rate"] == pytest.approx(1.0)
    assert row["small_cap_limit_up_rate_is_proxy"] == 1
    assert row["broken_board_rate"] == pytest.approx(0.0)
    # 交互项
    assert row["open_times_x_continuation_rate"] == pytest.approx(0.0)
    assert row["seal_strength_x_float_mv"] == pytest.approx((2.0e8 / 8e9) * np.log10(8e9 / 1e8 + 1.0))
    assert row["first_seal_time_x_market_regime"] == pytest.approx(3.0, abs=0.01)  # regime clip 上限
    assert row["first_seal_time_x_market_regime_is_proxy"] == 1


def test_t_word_board_and_theme_interaction(tmp_path: Path):
    db, dates = build_fixture_db(tmp_path)
    frame = build_microstructure_features(db, UNIVERSE, dates[59], lookback=250)
    row = _row(frame, "000222")
    assert row["limit_flag"] == "U"
    assert row["is_t_word"] == 1
    assert row["is_t_word_is_proxy"] == 1  # 无分时，日线近似
    assert row["is_one_word"] == 0
    assert row["first_seal_minute"] == 0.0  # 09:30
    assert row["last_seal_minute"] == 180.0  # 14:00
    assert row["open_times"] == 2
    assert row["reseal_speed_min"] == pytest.approx(90.0)  # (180-0)/2
    assert row["reseal_speed_min_is_proxy"] == 1
    assert row["is_turnover_board"] == 1  # open_times>=1 且板上占比 0.75>=0.5
    assert row["board_position_code"] == 1
    assert row["board_position_label"] == "首板"
    assert row["board_type_text"] == "T字板"  # kpl 回填仅透传，不参与口径计算
    # 题材交互：T 日同 theme 涨停数 2（000222+600999）/10 * 封成比 1.5/4
    assert row["fd_ratio_x_theme_heat"] == pytest.approx((1.5e8 / 4.0e8) * 0.2)
    assert row["fd_ratio_x_theme_heat_is_proxy"] == 1
    # 股性：历史首板 idx29（次日 drift 10.0）
    assert row["limit_up_count_250d"] == 1
    assert row["first_board_next_day_premium_avg"] == pytest.approx(10.0 / 11.0 - 1.0)
    assert row["first_board_next_day_win_rate"] == 0.0
    assert row["continuation_success_rate"] == pytest.approx(0.0)
    assert row["open_times_x_continuation_rate"] == pytest.approx(0.0)


def test_broken_board(tmp_path: Path):
    db, dates = build_fixture_db(tmp_path)
    frame = build_microstructure_features(db, UNIVERSE, dates[59], lookback=250)
    row = _row(frame, "300333")
    assert row["limit_flag"] == "Z"
    assert row["first_seal_minute"] == 30.0  # 10:00
    assert row["last_seal_minute"] == 220.0  # 14:40
    assert row["broken_duration_min"] == pytest.approx(20.0)
    assert row["broken_duration_min_is_proxy"] == 1
    assert row["seal_duration_min"] == pytest.approx(190.0)
    assert row["reseal_speed_min"] == pytest.approx((220.0 - 30.0) / 3)
    assert row["is_tail_board"] == 0
    assert row["fd_amount_is_missing"] == 1  # 炸板收盘无封单
    assert np.isnan(row["fd_amount"])
    assert row["is_one_word"] == 0  # 收盘炸板，按定义非一字/T字（真实口径判定，非代理）
    assert row["is_one_word_is_proxy"] == 0
    assert row["is_t_word"] == 0
    assert row["board_position_code_is_missing"] == 1  # Z 行 limit_times NULL
    assert row["seal_quality_proxy"] == -1  # 盘中触板未封死
    assert row["seal_quality_proxy_is_proxy"] == 0  # 有 limit 行（Z），日线仅辅助
    assert row["limit_up_count_250d"] == 1  # idx54 历史首板
    assert row["broken_board_rate"] == pytest.approx(0.0)  # 窗口内 <T 无炸板记录


def test_no_seal_order_data(tmp_path: Path):
    db, dates = build_fixture_db(tmp_path)
    frame = build_microstructure_features(db, UNIVERSE, dates[59], lookback=250)
    row = _row(frame, "600444")
    # 无封单额/无首封时间：真实字段缺失，指示列=1，绝不填 0
    assert np.isnan(row["fd_amount"]) and row["fd_amount_is_missing"] == 1
    assert np.isnan(row["fd_ratio_float_mv"]) and row["fd_ratio_float_mv_is_missing"] == 1
    assert np.isnan(row["first_seal_minute"]) and row["first_seal_minute_is_missing"] == 1
    assert np.isnan(row["seal_duration_min"]) and row["seal_duration_min_is_missing"] == 1
    assert np.isnan(row["is_tail_board"]) and row["is_tail_board_is_missing"] == 1
    assert np.isnan(row["reseal_speed_min"]) and row["reseal_speed_min_is_missing"] == 1
    assert np.isnan(row["broken_duration_min"]) and row["broken_duration_min_is_missing"] == 1
    # 真实字段仍可用
    assert row["on_board_amount"] == pytest.approx(1.0e8)
    assert row["on_board_amount_ratio"] == pytest.approx(0.5)
    assert row["is_turnover_board"] == 0
    assert row["is_one_word_is_missing"] == 1  # 有板行但首封时间缺失，无法确认一字
    assert row["seal_quality_proxy"] == 0  # 收盘封死（日线，非一字非T）
    assert row["seal_quality_proxy_is_proxy"] == 0
    assert row["board_position_code"] == 1


def test_missing_limit_row_daily_proxy(tmp_path: Path):
    db, dates = build_fixture_db(tmp_path)
    frame = build_microstructure_features(db, UNIVERSE, dates[59], lookback=250)
    row = _row(frame, "000888")
    assert row["has_limit_row"] == 0
    assert row["is_one_word"] == 1
    assert row["is_one_word_is_proxy"] == 1  # 降级日线代理
    assert row["seal_quality_proxy"] == 2
    assert row["seal_quality_proxy_is_proxy"] == 1
    for name in ("fd_amount", "first_seal_minute", "limit_times", "open_times", "up_stat_days"):
        assert row[f"{name}_is_missing"] == 1, name
    # 股性正常：窗口内确实没有涨停记录（真实 0，不是缺失）
    assert row["limit_up_count_250d"] == 0
    assert row["limit_up_count_250d_is_missing"] == 0
    assert row["limit_up_freq_250d"] == pytest.approx(0.0)
    assert row["hist_days_observed"] == 59


def test_suspended_stock(tmp_path: Path):
    db, dates = build_fixture_db(tmp_path)
    frame = build_microstructure_features(db, UNIVERSE, dates[59], lookback=250)
    row = _row(frame, "600555")
    assert row["has_limit_row"] == 0
    assert row["has_kline_row"] == 0
    assert row["fd_amount_is_missing"] == 1
    assert row["is_one_word_is_missing"] == 1
    assert row["seal_quality_proxy_is_missing"] == 1
    # 停牌前历史股性仍可算；idx19 首板 -> idx20 次日（除权日）纳入溢价统计
    assert row["limit_up_count_250d"] == 1
    assert row["first_board_next_day_premium_avg"] == pytest.approx(5.0 / 11.0 - 1.0)
    assert row["first_board_next_day_win_rate"] == 0.0
    # 晋级事件：idx19 -> idx20 可观察且未涨停 -> 失败（分母含 idx20；停牌日不入分母）
    assert row["continuation_success_rate"] == pytest.approx(0.0)
    assert row["hist_days_observed"] == 48
    assert not np.isnan(row["price_bucket"])  # 停牌股用最近可得收盘分桶
    assert row["up_stat_raw_is_missing"] == 1


def test_new_stock_no_fake_history(tmp_path: Path):
    db, dates = build_fixture_db(tmp_path)
    frame = build_microstructure_features(db, UNIVERSE, dates[59], lookback=250)
    row = _row(frame, "688777")
    # 上市 2 天：封板强度用真实字段，股性一律缺失（禁止用极短历史造数）
    assert row["is_one_word"] == 1
    assert row["fd_amount"] == pytest.approx(3.0e8)
    assert row["fd_ratio_float_mv"] == pytest.approx(3.0e8 / 6e9)
    assert row["hist_days_observed"] == 1
    for name in (
        "limit_up_count_250d",
        "first_board_count_250d",
        "first_board_next_day_premium_avg",
        "first_board_next_day_win_rate",
        "continuation_success_rate",
        "broken_board_rate",
        "max_consecutive_boards_250d",
        "limit_up_amount_avg",
        "limit_up_freq_250d",
    ):
        assert np.isnan(row[name]) and row[f"{name}_is_missing"] == 1, name


def test_exright_premium_is_finite_and_flagged_as_limitation(tmp_path: Path):
    """除权场景：次日溢价用原始收盘价比值（idx19 首板 11.0 -> idx20 除权 5.0），
    因子须有限、不崩溃；精确复权口径限制已写入 FACTOR_CATALOG（依赖复权因子表）。"""
    db, dates = build_fixture_db(tmp_path)
    frame = build_microstructure_features(db, UNIVERSE, dates[59], lookback=250)
    row = _row(frame, "600555")
    premium = row["first_board_next_day_premium_avg"]
    assert np.isfinite(premium)
    assert premium == pytest.approx(5.0 / 11.0 - 1.0)  # 含除权跳变，口径限制已标注


# ─── 输出 schema / 缺失指示 ──────────────────────────────────────────


def test_output_schema_and_indicators(tmp_path: Path):
    db, dates = build_fixture_db(tmp_path)
    frame = build_microstructure_features(db, UNIVERSE, dates[59])
    assert list(frame.columns) == microstructure_output_columns()
    assert len(frame) == len(UNIVERSE)
    assert len(microstructure_factor_specs()) == len(FACTOR_SPECS)
    for spec in FACTOR_SPECS:
        assert f"{spec.name}_is_missing" in frame.columns, spec.name
        if spec.name in ("board_position_label", "up_stat_raw", "board_type_text"):
            continue
        numeric = pd.to_numeric(frame[spec.name], errors="coerce")
        missing_flag = frame[f"{spec.name}_is_missing"]
        assert (numeric.isna() == (missing_flag == 1)).all(), spec.name
    for name in PROXY_FACTORS:
        assert f"{name}_is_proxy" in frame.columns, name


def test_missing_never_zero_filled(tmp_path: Path):
    db, dates = build_fixture_db(tmp_path)
    frame = build_microstructure_features(db, UNIVERSE, dates[59])
    row = _row(frame, "600444")
    for name in ("fd_amount", "fd_ratio_float_mv", "fd_ratio_amount", "fd_ratio_volume"):
        assert np.isnan(row[name]), name
        assert row[f"{name}_is_missing"] == 1, name


def test_empty_symbols_returns_empty_frame(tmp_path: Path):
    db, dates = build_fixture_db(tmp_path)
    frame = build_microstructure_features(db, [], dates[59])
    assert frame.empty
    assert list(frame.columns) == microstructure_output_columns()


# ─── PIT：无未来函数 ─────────────────────────────────────────────────

CHARACTER_COLUMNS = [s.name for s in FACTOR_SPECS if s.group == "stock_character"]


def test_pit_future_rows_cannot_change_features(tmp_path: Path):
    """在 T 日之前任一时点 M 计算特征后，改动 M 之后的所有数据，特征必须不变。"""
    db, dates = build_fixture_db(tmp_path)
    mid = dates[49]  # T-10
    before = build_microstructure_features(db, UNIVERSE, mid, lookback=250)

    store = KlineSQLiteStore(db)
    tampered = [
        _board("000999", dates[51], limit_times=1, fd_amount=9.9e8, amount=1e9, limit_amount=1e9,
               float_mv=1e10, close=11.0, pct_chg=10.0, first_time="09:30:00", last_time="09:30:00"),
        _board("600555", dates[55], limit_times=2, fd_amount=8.8e8, amount=1e9, limit_amount=1e9,
               float_mv=3e9, close=11.0, pct_chg=10.0, first_time="09:25:00", last_time="09:25:00"),
    ]
    store.upsert_limit_board_daily(tampered, SYNC_AT)
    store.upsert_symbol_klines("000999", [_flat_row(dates[i], 99.0) for i in range(50, 60)], SYNC_AT)

    after = build_microstructure_features(db, UNIVERSE, mid, lookback=250)
    pd.testing.assert_frame_equal(
        before.reset_index(drop=True),
        after.reset_index(drop=True),
        check_dtype=False,
    )


def test_pit_random_spot_check(tmp_path: Path):
    """随机抽查（固定 seed）：对每个 (symbol, as_of) 样本，用截断到 as_of 的
    副本数据库重算，股性列（严格 <T）必须与全量库一致 -> 无 T+1 泄漏。"""
    db, dates = build_fixture_db(tmp_path)
    rng = random.Random(20261024)
    samples = []
    for _ in range(30):
        idx = rng.randrange(20, 60)
        symbol = rng.choice(UNIVERSE)
        samples.append((symbol, dates[idx]))

    truncated_dir = tmp_path / "truncated"
    truncated_dir.mkdir()
    cache: dict[str, pd.DataFrame] = {}
    checked = 0
    for symbol, as_of in samples:
        if as_of not in cache:
            cache[as_of] = build_microstructure_features(db, UNIVERSE, as_of, lookback=250)
        expected = cache[as_of]

        cut_db = str(truncated_dir / f"cut_{as_of}.db")
        if not Path(cut_db).exists():
            # 用 backup API 拿一致快照（含 WAL 中未落盘页），再物理截断
            src = sqlite3.connect(db)
            dst = sqlite3.connect(cut_db)
            src.backup(dst)
            src.close()
            dst.close()
            conn = sqlite3.connect(cut_db)
            conn.execute("DELETE FROM limit_board_daily WHERE trade_date > ?", (as_of,))
            conn.execute("DELETE FROM kline_daily WHERE trade_date > ?", (as_of,))
            conn.commit()
            conn.close()

        actual = build_microstructure_features(cut_db, [symbol], as_of, lookback=250)
        for col in CHARACTER_COLUMNS:
            left = _row(expected, symbol)[col]
            right = _row(actual, symbol)[col]
            if isinstance(left, float) and np.isnan(left):
                assert isinstance(right, float) and np.isnan(right), (symbol, as_of, col)
            elif isinstance(left, str) or isinstance(right, str):
                assert left == right, (symbol, as_of, col)
            else:
                assert float(left) == pytest.approx(float(right)), (symbol, as_of, col)
        checked += 1
    assert checked == len(samples)


# ─── 质量报告 ────────────────────────────────────────────────────────


def test_quality_report(tmp_path: Path):
    db, dates = build_fixture_db(tmp_path)
    report = microstructure_quality_report(db, UNIVERSE, dates[59])
    assert report["as_of_date"] == dates[59]
    assert report["symbol_count"] == len(UNIVERSE)
    assert set(report["missing_rate"]) == {s.name for s in FACTOR_SPECS}
    # T 日有封单额：600111/000222/600999/688777；缺：300333(Z NULL)/600444(NULL)/
    # 000888/600555/000999（无板行）
    assert report["missing_rate"]["fd_amount"] == pytest.approx(5 / 9)
    assert report["missing_rate"]["limit_up_count_250d"] == pytest.approx(1 / 9)  # 仅新股 688777
    # 缺首封时间/无板行：600444、000888、600555、000999
    assert report["missing_rate"]["first_seal_time_x_market_regime"] == pytest.approx(4 / 9)
    assert report["limit_row_coverage"] == pytest.approx(6 / 9)
    assert report["ic_report"].startswith("PARTIAL")
    assert report["model_gain_report"].startswith("PARTIAL")
    assert "board_position_code" in report["distribution"]
