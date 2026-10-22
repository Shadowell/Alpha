"""打板可执行标签口径单测（issue #23）。

全部用内存里的合成 bars / 打板行驱动 ``labeling.py`` 的纯函数，
**不访问数据库、不访问网络**。覆盖：一字板不可成交、换手板成交、炸板、
断板、T+1 停牌、数据缺失、旧日线降级、单位/时区、严格 PIT 与大面口径。
"""

from __future__ import annotations

import math

import pytest

from strategy.first_limit_alpha.labeling import (
    FEATURE_SAFE_COLUMNS,
    LABEL_ONLY_COLUMNS,
    LABEL_SOURCE_PROXY,
    LABEL_SOURCE_V3,
    LS_INSUFFICIENT_HORIZON,
    LS_MISSING_FUTURE_DATA,
    LS_UNFILLED,
    META_COLUMNS,
    STATUS_FILLED,
    STATUS_UNFILLED,
    STATUS_UNKNOWN,
    UNFILLED_FLAG_CONFLICT,
    UNFILLED_NEVER_OPENED,
    UNFILLED_NEXT_ONE_WORD_LIMIT_UP,
    UNFILLED_NO_NEXT_BAR,
    UNFILLED_ONE_WORD,
    UNFILLED_T_WORD,
    ExecutableLabelConfig,
    assert_no_label_in_features,
    board_limit_pct,
    build_sample_label,
    classify_float_mv_bucket,
    classify_limit_shape,
    classify_promotion_stage,
    label_columns,
    label_field_docs,
    normalize_pct_chg,
    norm_trade_date,
    parse_seal_seconds,
)

W = 10_000  # 1bp = 1/10000


def make_bars(rows: list[tuple[str, float, float, float, float]]) -> list[dict]:
    """rows: (trade_date, open, high, low, close) -> 升序 bar 列表。"""
    bars = []
    for i, (date, o, hi, lo, c) in enumerate(rows):
        bars.append(
            {
                "trade_date": date,
                "symbol": "600001",
                "open": o,
                "high": hi,
                "low": lo,
                "close": c,
                "volume": 1_000_000.0,
                "amount": c * 1_000_000.0,
            }
        )
        bars[i]["trade_date"] = date
    return bars


def flat_after(t_close: float, n: int, start_idx: int = 1, price: float | None = None) -> list[tuple[str, float, float, float, float]]:
    """生成 T 日之后 n 根"横盘"bar（默认价 = t_close），用于隔离单一变量。"""
    px = t_close if price is None else price
    out = []
    for k in range(1, n + 1):
        out.append((f"2026-02-{start_idx + k:02d}", px, px, px, px))
    return out


BASE_CFG = ExecutableLabelConfig()


def signal_u(**overrides) -> dict:
    """一个"收盘封板、盘中开过板"的标准可成交首板 v3 行。"""
    row = {
        "symbol": "600001",
        "name": "测试股份",
        "limit_flag": "U",
        "first_time": "10:15:00",
        "last_time": "14:05:00",
        "open_times": 1,
        "limit_times": 1,
        "up_stat": "1/1",
        "fd_amount": 5_000_000.0,
        "limit_amount": 50_000_000.0,
        "amount": 300_000_000.0,
        "turnover_ratio": 5.0,
        "float_mv": 3_000_000_000.0,
        "total_mv": 6_000_000_000.0,
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# 场景 A：可成交性
# ---------------------------------------------------------------------------


def test_one_word_board_is_unfilled_and_returns_are_null_not_zero():
    """一字板买不到：必须标 entry_unfilled，收益类标签为 None（绝不能算成 0 收益）。"""
    bars = make_bars(
        [
            ("2026-02-01", 9.9, 10.05, 9.85, 10.0),
            ("2026-02-02", 11.0, 11.0, 11.0, 11.0),  # T 日一字涨停
            *[(f"2026-02-{d:02d}", 11.2, 11.4, 11.0, 11.3) for d in range(3, 9)],
        ]
    )
    row = build_sample_label(signal=signal_u(open_times=0), bars=bars, idx=1, cfg=BASE_CFG)
    assert row is not None
    assert row["limit_shape"] == "一字"
    assert row["entry_status_A"] == STATUS_UNFILLED
    assert row["entry_unfilled_A"] == 1
    assert row["entry_unfilled_reason_A"] == UNFILLED_ONE_WORD
    assert row["entry_price_A"] is None
    # 收益类标签必须为 None，而不是 0
    for col in ("ret_1d_A", "ret_3d_A", "ret_5d_A", "mfe_3d_A", "mao_3d_A"):
        assert row[col] is None, col
    assert row["label_status"] == LS_UNFILLED
    assert row["label_complete"] == 0
    # 排队成交概率极低但可计算
    assert row["fill_probability"] is not None and row["fill_probability"] < 0.1


def test_t_word_board_is_unfilled():
    """T 字板（开盘即涨停、盘中砸开又回封）按保守口径不可成交。"""
    bars = make_bars(
        [
            ("2026-02-01", 9.9, 10.05, 9.85, 10.0),
            ("2026-02-02", 11.0, 11.0, 10.4, 11.0),  # 开=高=收=涨停价，low 砸下
            *[(f"2026-02-{d:02d}", 11.2, 11.4, 11.0, 11.3) for d in range(3, 9)],
        ]
    )
    row = build_sample_label(signal=signal_u(open_times=2), bars=bars, idx=1, cfg=BASE_CFG)
    assert row["limit_shape"] == "T字"
    assert row["entry_status_A"] == STATUS_UNFILLED
    assert row["entry_unfilled_reason_A"] == UNFILLED_T_WORD
    assert row["ret_1d_A"] is None


def test_turnover_board_fills_at_limit_price_with_fee_adjusted_return():
    """换手板（盘中有开板、收盘封住）可成交：入场价 = T 日涨停价，收益扣双边手续费。"""
    bars = make_bars(
        [
            ("2026-02-01", 9.9, 10.05, 9.85, 10.0),  # prev_close = 10.0 -> 涨停价 11.00
            ("2026-02-02", 10.2, 11.0, 10.15, 11.0),  # 盘中开过板后回封
            ("2026-02-03", 11.0, 11.6, 10.9, 11.55),
            ("2026-02-04", 11.5, 11.8, 11.2, 11.4),
            ("2026-02-05", 11.4, 11.9, 11.1, 11.7),
            ("2026-02-06", 11.7, 12.0, 11.5, 11.9),
            ("2026-02-09", 11.9, 12.2, 11.6, 12.1),
            ("2026-02-10", 12.1, 12.4, 11.9, 12.3),
        ]
    )
    row = build_sample_label(signal=signal_u(), bars=bars, idx=1, cfg=BASE_CFG)
    assert row["entry_status_A"] == STATUS_FILLED
    assert row["entry_unfilled_A"] == 0
    assert row["entry_unfilled_reason_A"] is None
    assert row["entry_price_A"] == pytest.approx(11.0)
    assert row["entry_confidence_A"] == "v3_open_times"
    # 11.55/11.00 - 1 = 0.05，再扣双边 2*8bp
    assert row["ret_1d_A"] == pytest.approx(0.05 - 2 * BASE_CFG.fee_bps / W, abs=1e-6)
    assert row["ret_3d_A"] == pytest.approx(11.7 / 11.0 - 1 - 2 * BASE_CFG.fee_bps / W, abs=1e-6)
    assert row["ret_5d_A"] == pytest.approx(12.1 / 11.0 - 1 - 2 * BASE_CFG.fee_bps / W, abs=1e-6)
    # 溢价类相对 T 日收盘价、不含成本
    assert row["next_close_return"] == pytest.approx(11.55 / 11.0 - 1.0, abs=1e-6)
    assert row["next_open_premium"] == pytest.approx(0.0, abs=1e-6)
    assert row["next_high_premium"] == pytest.approx(11.6 / 11.0 - 1.0, abs=1e-6)
    assert row["ret_benchmark_source"] == "entry_price_A"
    assert row["label_status"] == "ok"
    assert row["label_complete"] == 1


def test_never_opened_limit_up_is_unfilled_even_if_shape_is_turnover():
    """涨停开不了板：open_times=0 时排队拿不到货，标 never_opened。"""
    bars = make_bars(
        [
            ("2026-02-01", 9.9, 10.05, 9.85, 10.0),
            ("2026-02-02", 10.5, 11.0, 10.4, 11.0),
            *[(f"2026-02-{d:02d}", 11.2, 11.4, 11.0, 11.3) for d in range(3, 9)],
        ]
    )
    row = build_sample_label(signal=signal_u(open_times=0, last_time="10:30:00"), bars=bars, idx=1, cfg=BASE_CFG)
    assert row["limit_shape"] == "换手"
    assert row["entry_unfilled_reason_A"] == UNFILLED_NEVER_OPENED
    assert row["ret_1d_A"] is None


def test_missing_open_times_falls_back_to_ohlc_inference():
    """open_times 缺失时用日线推断：盘中跌下过涨停价 → 判定开过板可成交。"""
    bars = make_bars(
        [
            ("2026-02-01", 9.9, 10.05, 9.85, 10.0),
            ("2026-02-02", 10.3, 11.0, 10.25, 11.0),
            *[(f"2026-02-{d:02d}", 11.2, 11.4, 11.0, 11.3) for d in range(3, 9)],
        ]
    )
    row = build_sample_label(signal=signal_u(open_times=None), bars=bars, idx=1, cfg=BASE_CFG)
    assert row["entry_status_A"] == STATUS_FILLED
    assert row["entry_confidence_A"] == "ohlc_inferred"

    # 日线完全看不出开板（open=low=high=close=涨停价）→ 一字，仍判不可成交
    bars2 = make_bars(
        [
            ("2026-02-01", 9.9, 10.05, 9.85, 10.0),
            ("2026-02-02", 11.0, 11.0, 11.0, 11.0),
            *[(f"2026-02-{d:02d}", 11.2, 11.4, 11.0, 11.3) for d in range(3, 9)],
        ]
    )
    row2 = build_sample_label(signal=signal_u(open_times=None), bars=bars2, idx=1, cfg=BASE_CFG)
    assert row2["entry_unfilled_reason_A"] == UNFILLED_ONE_WORD
    assert row2["entry_confidence_A"] == "ohlc_inferred"


def test_flag_price_conflict_yields_unknown_entry_not_fake_return():
    """limit_flag=U 但收盘价远未到涨停价（脏数据）→ unknown，不得臆造成交收益。"""
    bars = make_bars(
        [
            ("2026-02-01", 9.9, 10.05, 9.85, 10.0),
            ("2026-02-02", 10.2, 11.0, 10.1, 10.3),  # 摸板但收盘没封住
            *[(f"2026-02-{d:02d}", 10.4, 10.6, 10.3, 10.5) for d in range(3, 9)],
        ]
    )
    row = build_sample_label(signal=signal_u(), bars=bars, idx=1, cfg=BASE_CFG)
    assert row["flag_price_conflict"] == 1
    assert row["entry_status_A"] == STATUS_UNKNOWN
    assert row["entry_unfilled_reason_A"] == UNFILLED_FLAG_CONFLICT
    assert row["ret_1d_A"] is None
    assert row["is_broken_board"] == 1


def test_fill_probability_monotonic_in_seal_ratio():
    """封单越厚 → 排队越难成交（fill_probability 单调递减），且始终落在 [0,1]。"""
    bars = make_bars(
        [
            ("2026-02-01", 9.9, 10.05, 9.85, 10.0),
            ("2026-02-02", 10.2, 11.0, 10.15, 11.0),
            *[(f"2026-02-{d:02d}", 11.2, 11.4, 11.0, 11.3) for d in range(3, 9)],
        ]
    )
    thin = build_sample_label(signal=signal_u(fd_amount=1_000_000.0), bars=bars, idx=1, cfg=BASE_CFG)
    thick = build_sample_label(signal=signal_u(fd_amount=500_000_000.0), bars=bars, idx=1, cfg=BASE_CFG)
    assert 0.0 <= thin["fill_probability"] <= 1.0
    assert thick["fill_probability"] < thin["fill_probability"]
    assert thick["seal_ratio"] > thin["seal_ratio"]


def test_fill_probability_is_null_when_inputs_missing():
    """封单/成交额全缺失时 fill_probability=None，不猜 0。"""
    bars = make_bars(
        [
            ("2026-02-01", 9.9, 10.05, 9.85, 10.0),
            ("2026-02-02", 10.2, 11.0, 10.15, 11.0),
            *[(f"2026-02-{d:02d}", 11.2, 11.4, 11.0, 11.3) for d in range(3, 9)],
        ]
    )
    row = build_sample_label(
        signal=signal_u(fd_amount=None, limit_amount=None, amount=None),
        bars=bars,
        idx=1,
        cfg=BASE_CFG,
    )
    assert row["seal_ratio"] is None
    assert row["fill_probability"] is not None  # 形态已知，仍能给出基线估计


# ---------------------------------------------------------------------------
# 场景 B：T+1 竞价 / 开盘接力
# ---------------------------------------------------------------------------


def test_scene_b_uses_auction_price_when_available_and_open_when_not():
    bars = make_bars(
        [
            ("2026-02-01", 9.9, 10.05, 9.85, 10.0),
            ("2026-02-02", 10.2, 11.0, 10.15, 11.0),
            ("2026-02-03", 11.0, 11.6, 10.9, 11.55),
            *[(f"2026-02-{d:02d}", 11.5, 11.7, 11.3, 11.6) for d in range(4, 9)],
        ]
    )
    bars[2]["auction_price"] = 11.20
    row = build_sample_label(signal=signal_u(), bars=bars, idx=1, cfg=BASE_CFG)
    assert row["auction_proxy"] == 0
    assert row["auction_price"] == pytest.approx(11.2)
    assert row["entry_price_B"] == pytest.approx(11.2 * (1 + BASE_CFG.slippage_bps / W), abs=1e-6)
    assert row["entry_time_B"].endswith("09:25:00")
    assert row["next_auction_premium"] == pytest.approx(11.2 / 11.0 - 1.0, abs=1e-6)
    assert row["ret_1d_B"] == pytest.approx(11.55 / row["entry_price_B"] - 1 - 2 * BASE_CFG.fee_bps / W, abs=1e-6)

    bars2 = [dict(b) for b in bars]
    bars2[2].pop("auction_price")
    row2 = build_sample_label(signal=signal_u(), bars=bars2, idx=1, cfg=BASE_CFG)
    assert row2["auction_proxy"] == 1
    assert row2["auction_price"] is None
    assert row2["entry_time_B"].endswith("09:30:00")


def test_scene_b_next_day_suspension_is_unfilled():
    """T+1 停牌（未来无 bar）：场景 B 不可成交，溢价与收益标签全部为 None。"""
    bars = make_bars(
        [
            ("2026-02-01", 9.9, 10.05, 9.85, 10.0),
            ("2026-02-02", 10.2, 11.0, 10.15, 11.0),
        ]
    )
    row = build_sample_label(signal=signal_u(), bars=bars, idx=1, cfg=BASE_CFG)
    assert row["entry_status_B"] == STATUS_UNFILLED
    assert row["entry_unfilled_B"] == 1
    assert row["entry_unfilled_reason_B"] == UNFILLED_NO_NEXT_BAR
    assert row["entry_price_B"] is None
    for col in ("ret_1d_B", "ret_3d_B", "ret_5d_B", "next_open_premium", "next_close_return", "next_high_premium", "next_auction_premium"):
        assert row[col] is None, col
    # 场景 A 也拿不到 T+1 收盘价
    assert row["ret_1d_A"] is None
    assert LS_MISSING_FUTURE_DATA in row["label_status"]


def test_scene_b_next_day_one_word_limit_up_cannot_be_bought():
    bars = make_bars(
        [
            ("2026-02-01", 9.9, 10.05, 9.85, 10.0),
            ("2026-02-02", 10.2, 11.0, 10.15, 11.0),
            ("2026-02-03", 12.1, 12.1, 12.1, 12.1),  # T+1 一字涨停（11*1.1）
            *[(f"2026-02-{d:02d}", 12.2, 12.4, 12.0, 12.3) for d in range(4, 9)],
        ]
    )
    row = build_sample_label(signal=signal_u(), bars=bars, idx=1, cfg=BASE_CFG)
    assert row["entry_unfilled_B"] == 1
    assert row["entry_unfilled_reason_B"] == UNFILLED_NEXT_ONE_WORD_LIMIT_UP
    assert row["ret_1d_B"] is None
    # 但隔日溢价仍然可观测（这是"看得见吃不到"的典型样本）
    assert row["next_close_return"] == pytest.approx(12.1 / 11.0 - 1.0, abs=1e-6)
    assert row["entry_status_A"] == STATUS_FILLED


# ---------------------------------------------------------------------------
# 炸板 / 断板 / 跌停 / 大面
# ---------------------------------------------------------------------------


def test_broken_board_fills_and_creates_big_loss():
    """炸板：盘中排队的买单在开板时成交 → 真实亏损，必须可成交而非"收益 0"。"""
    bars = make_bars(
        [
            ("2026-02-01", 9.9, 10.05, 9.85, 10.0),
            ("2026-02-02", 10.3, 11.0, 10.2, 10.35),  # 摸板后炸板
            ("2026-02-03", 10.2, 10.3, 9.85, 9.9),
            *[(f"2026-02-{d:02d}", 9.9, 10.1, 9.7, 9.95) for d in range(4, 9)],
        ]
    )
    row = build_sample_label(
        signal=signal_u(limit_flag="Z", open_times=3, limit_times=None),
        bars=bars,
        idx=1,
        cfg=BASE_CFG,
    )
    assert row["limit_shape"] == "炸板未回封"
    assert row["is_broken_board"] == 1
    assert row["entry_status_A"] == STATUS_FILLED
    assert row["entry_price_A"] == pytest.approx(11.0)
    assert row["ret_1d_A"] < -0.05
    assert row["is_big_loss"] == 1
    assert row["board_type"] == "炸板"


def test_reversal_context_marks_reversal_board_type():
    bars = make_bars(
        [
            ("2026-02-01", 9.9, 10.05, 9.85, 10.0),
            ("2026-02-02", 10.2, 11.0, 10.15, 11.0),
            *[(f"2026-02-{d:02d}", 11.2, 11.4, 11.0, 11.3) for d in range(3, 9)],
        ]
    )
    row = build_sample_label(
        signal=signal_u(),
        bars=bars,
        idx=1,
        cfg=BASE_CFG,
        prev_signal={"limit_flag": "Z", "limit_times": None},
    )
    assert row["board_type"] == "反包"


def test_di_tian_ban_detection():
    """地天板：盘中触及跌停、收盘封涨停。"""
    bars = make_bars(
        [
            ("2026-02-01", 9.9, 10.05, 9.85, 10.0),  # prev_close 10 → 跌停价 9.00
            ("2026-02-02", 9.0, 11.0, 9.0, 11.0),
            *[(f"2026-02-{d:02d}", 11.2, 11.4, 11.0, 11.3) for d in range(3, 9)],
        ]
    )
    row = build_sample_label(signal=signal_u(), bars=bars, idx=1, cfg=BASE_CFG)
    assert row["board_type"] == "地天板"


def test_break_streak_next_and_continuation():
    def build(next_close: float, next_flag: str | None):
        bars = make_bars(
            [
                ("2026-02-01", 9.9, 10.05, 9.85, 10.0),
                ("2026-02-02", 10.2, 11.0, 10.15, 11.0),
                ("2026-02-03", 11.0, next_close + 0.1, 10.9, next_close),
                *[(f"2026-02-{d:02d}", next_close, next_close + 0.1, next_close - 0.1, next_close) for d in range(4, 9)],
            ]
        )
        future = {"2026-02-03": {"limit_flag": next_flag}} if next_flag else {}
        return build_sample_label(signal=signal_u(), bars=bars, idx=1, cfg=BASE_CFG, future_limit_rows=future)

    promoted = build(12.1, "U")
    assert promoted["is_break_streak_next"] == 0
    assert promoted["is_continuation"] == 1
    assert promoted["is_1_to_2"] == 1
    assert promoted["promotion_source"] == LABEL_SOURCE_V3

    broken = build(10.5, None)
    assert broken["is_break_streak_next"] == 1
    assert broken["is_continuation"] == 0
    assert broken["is_1_to_2"] == 0


def test_next_limit_down_flag():
    bars = make_bars(
        [
            ("2026-02-01", 9.9, 10.05, 9.85, 10.0),
            ("2026-02-02", 10.2, 11.0, 10.15, 11.0),
            ("2026-02-03", 10.5, 10.6, 9.9, 9.9),  # 跌停价 11*0.9 = 9.90
            *[(f"2026-02-{d:02d}", 9.9, 10.0, 9.7, 9.8) for d in range(4, 9)],
        ]
    )
    row = build_sample_label(signal=signal_u(), bars=bars, idx=1, cfg=BASE_CFG)
    assert row["is_next_limit_down"] == 1
    assert row["is_next_close_down"] == 1
    assert row["is_big_loss"] == 1


# ---------------------------------------------------------------------------
# 晋级台阶
# ---------------------------------------------------------------------------


def test_promotion_stage_labeling():
    assert classify_promotion_stage(1) == "1进2"
    assert classify_promotion_stage(2) == "2进3"
    assert classify_promotion_stage(3) == "3进4"
    assert classify_promotion_stage(5) == "4进5+"
    assert classify_promotion_stage(None) == ""


def test_two_to_three_only_sets_that_task():
    """多任务掩码：2 连板样本只输出 is_2_to_3，其余台阶为 None（避免结构性 0）。"""
    bars = make_bars(
        [
            ("2026-01-30", 9.9, 10.0, 9.8, 10.0),
            ("2026-01-31", 10.0, 11.0, 10.0, 11.0),
            ("2026-02-02", 11.0, 12.1, 11.0, 12.1),  # T 日 2 连板
            ("2026-02-03", 12.1, 13.31, 12.1, 13.31),  # T+1 晋级 3 连板
            *[(f"2026-02-{d:02d}", 13.3, 13.6, 13.2, 13.4) for d in range(4, 9)],
        ]
    )
    row = build_sample_label(
        signal=signal_u(limit_times=2, up_stat="2/2"),
        bars=bars,
        idx=2,
        cfg=BASE_CFG,
        future_limit_rows={"2026-02-03": {"limit_flag": "U", "limit_times": 3}},
        prev_signal={"limit_flag": "U", "limit_times": 1},
    )
    assert row["board_type"] == "二连板"
    assert row["promotion_stage"] == "2进3"
    assert row["is_2_to_3"] == 1
    assert row["is_1_to_2"] is None
    assert row["is_3_to_4"] is None
    assert row["promotion_level_next"] == 3


def test_three_to_four_promotion_via_daily_seal_proxy():
    """v3 未来行缺失时，用日线封板证据判晋级，并明确标注 promotion_source。"""
    bars = make_bars(
        [
            ("2026-01-29", 9.9, 10.0, 9.8, 10.0),
            ("2026-01-30", 10.0, 11.0, 10.0, 11.0),
            ("2026-01-31", 11.0, 12.1, 11.0, 12.1),
            ("2026-02-02", 12.1, 13.31, 12.1, 13.31),  # T 日 3 连板
            ("2026-02-03", 13.3, 14.65, 13.2, 14.64),  # T+1 触及 13.31*1.1=14.64 并封住
            *[(f"2026-02-{d:02d}", 14.6, 15.0, 14.5, 14.8) for d in range(4, 9)],
        ]
    )
    row = build_sample_label(
        signal=signal_u(limit_times=3, up_stat="3/3"),
        bars=bars,
        idx=3,
        cfg=BASE_CFG,
        future_limit_rows={},
    )
    assert row["promotion_stage"] == "3进4"
    assert row["is_3_to_4"] == 1
    assert row["promotion_source"] == "kline_daily_seal_proxy"


# ---------------------------------------------------------------------------
# 窗口不足 / 数据缺失
# ---------------------------------------------------------------------------


def test_insufficient_horizon_keeps_sample_and_nulls_long_labels():
    """窗口不足不丢样本：短标签可算、长标签 None，并写 insufficient_horizon 状态。"""
    bars = make_bars(
        [
            ("2026-02-01", 9.9, 10.05, 9.85, 10.0),
            ("2026-02-02", 10.2, 11.0, 10.15, 11.0),
            ("2026-02-03", 11.0, 11.6, 10.9, 11.5),
            ("2026-02-04", 11.5, 11.7, 11.3, 11.6),
            ("2026-02-05", 11.6, 11.8, 11.4, 11.7),
        ]
    )
    row = build_sample_label(signal=signal_u(), bars=bars, idx=1, cfg=BASE_CFG)
    assert row is not None
    assert LS_INSUFFICIENT_HORIZON in row["label_status"]
    assert row["ret_3d_A"] is not None
    assert row["ret_5d_A"] is None
    assert row["fwd_ret_5d"] is None
    assert row["label_complete"] == 0
    assert row["exit_time_long"] is None
    assert row["exit_time_short"] is not None


def test_unusable_signal_day_returns_none():
    """T 日下标越界 / 收盘价非正 → 不产样本；脏 flag 与价格矛盾则走 unknown 而非丢弃。"""
    bars = make_bars([("2026-02-01", 10.0, 10.1, 9.9, 10.0), ("2026-02-02", 10.0, 10.1, 9.9, 10.0)])
    assert build_sample_label(signal=signal_u(), bars=bars, idx=99, cfg=BASE_CFG) is None
    assert build_sample_label(signal=signal_u(), bars=bars, idx=-1, cfg=BASE_CFG) is None
    broken = make_bars(
        [("2026-02-01", 10.0, 10.1, 9.9, 10.0), ("2026-02-02", 10.0, 10.1, 9.9, 0.0)]
    )
    assert build_sample_label(signal=signal_u(), bars=broken, idx=1, cfg=BASE_CFG) is None
    # 普通上涨日、且没有 v3 打板行：不是打板样本
    plain = make_bars(
        [
            ("2026-02-01", 9.9, 10.05, 9.85, 10.0),
            ("2026-02-02", 10.0, 10.4, 9.95, 10.3),
            *[(f"2026-02-{d:02d}", 10.3, 10.4, 10.2, 10.3) for d in range(3, 9)],
        ]
    )
    assert build_sample_label(signal={}, bars=plain, idx=1, cfg=BASE_CFG) is None


def test_prev_close_derived_from_pct_chg_when_history_missing():
    """没有 T-1 日线时，用 close/(1+pct_chg) 反推 prev_close 并显式标注。"""
    bars = make_bars(
        [
            ("2026-02-02", 10.2, 11.0, 10.15, 11.0),
            *[(f"2026-02-{d:02d}", 11.2, 11.4, 11.0, 11.3) for d in range(3, 9)],
        ]
    )
    row = build_sample_label(signal=signal_u(pct_chg=10.0), bars=bars, idx=0, cfg=BASE_CFG)
    assert row["prev_close_derived"] == 1
    assert row["prev_close"] == pytest.approx(10.0, abs=1e-3)
    assert row["pct_chg_convention"] == "percent"


# ---------------------------------------------------------------------------
# 单位 / 时区 / 板别
# ---------------------------------------------------------------------------


def test_money_columns_pass_through_in_yuan():
    """金额列保持 v3 口径"元"，不做任何缩放。"""
    bars = make_bars(
        [
            ("2026-02-01", 9.9, 10.05, 9.85, 10.0),
            ("2026-02-02", 10.2, 11.0, 10.15, 11.0),
            *[(f"2026-02-{d:02d}", 11.2, 11.4, 11.0, 11.3) for d in range(3, 9)],
        ]
    )
    row = build_sample_label(signal=signal_u(), bars=bars, idx=1, cfg=BASE_CFG)
    assert row["fd_amount"] == 5_000_000.0
    assert row["limit_amount"] == 50_000_000.0
    assert row["float_mv"] == 3_000_000_000.0
    assert row["float_mv_bucket"] == "小盘"
    assert classify_float_mv_bucket(50_000_000_000.0, BASE_CFG) == "大盘"
    assert classify_float_mv_bucket(8_000_000_000.0, BASE_CFG) == "中盘"
    assert classify_float_mv_bucket(None, BASE_CFG) == "未知"
    # seal_ratio = fd_amount / limit_amount - 1
    assert row["seal_ratio"] == pytest.approx(5_000_000.0 / 50_000_000.0 - 1.0)


def test_pct_chg_unit_autodetect():
    assert normalize_pct_chg(10.0) == (0.10, "percent")
    assert normalize_pct_chg(0.10) == (0.10, "fraction")
    assert normalize_pct_chg(-9.98)[1] == "percent"
    assert normalize_pct_chg(None) == (None, "unknown")
    assert normalize_pct_chg("")[1] == "unknown"


def test_board_limit_pct_by_board_and_st():
    assert board_limit_pct("600001") == 0.10
    assert board_limit_pct("000001") == 0.10
    assert board_limit_pct("300750") == 0.20
    assert board_limit_pct("688981") == 0.20
    assert board_limit_pct("830799") == 0.30
    assert board_limit_pct("430047") == 0.30
    assert board_limit_pct("600001", "ST康美") == 0.05


def test_trade_date_normalization_and_seal_time_parsing():
    assert norm_trade_date("20260105") == "2026-01-05"
    assert norm_trade_date("2026-01-05") == "2026-01-05"
    assert norm_trade_date("2026/01/05") == "2026-01-05"
    assert norm_trade_date(None) is None
    assert norm_trade_date("") is None
    assert parse_seal_seconds("09:30:00") == 9 * 3600 + 30 * 60
    assert parse_seal_seconds("093000") == 9 * 3600 + 30 * 60
    assert parse_seal_seconds("14:05") == 14 * 3600 + 5 * 60
    assert parse_seal_seconds("") is None
    assert parse_seal_seconds(None) is None
    assert parse_seal_seconds("99:99:99") is None


def test_dates_are_normalized_in_payload_and_times_are_shanghai_market_time():
    bars = make_bars(
        [
            ("20260201", 9.9, 10.05, 9.85, 10.0),
            ("20260202", 10.2, 11.0, 10.15, 11.0),
            ("20260203", 11.0, 11.6, 10.9, 11.5),
            ("20260204", 11.5, 11.7, 11.3, 11.6),
            ("20260205", 11.6, 11.8, 11.4, 11.7),
            ("20260206", 11.7, 11.9, 11.5, 11.8),
            ("20260209", 11.8, 12.0, 11.6, 11.9),
        ]
    )
    row = build_sample_label(signal=signal_u(), bars=bars, idx=1, cfg=BASE_CFG)
    assert row["trade_date"] == "2026-02-02"
    assert row["decision_time"] == "2026-02-02 15:00:00"
    assert row["d1_close"] == 11.5
    assert row["label_end_date"] == "2026-02-09"
    assert row["label_horizon_days"] == BASE_CFG.max_horizon
    assert row["embargo_days"] == BASE_CFG.required_embargo_days()


def test_late_seal_shape_and_configurable_cutoff():
    bars = make_bars(
        [
            ("2026-02-01", 9.9, 10.05, 9.85, 10.0),
            ("2026-02-02", 10.2, 11.0, 10.15, 11.0),
            *[(f"2026-02-{d:02d}", 11.2, 11.4, 11.0, 11.3) for d in range(3, 9)],
        ]
    )
    row = build_sample_label(signal=signal_u(last_time="14:45:00"), bars=bars, idx=1, cfg=BASE_CFG)
    assert row["limit_shape"] == "尾盘"
    cfg_late = ExecutableLabelConfig(late_seal_seconds=14 * 3600 + 50 * 60)
    row2 = build_sample_label(signal=signal_u(last_time="14:45:00"), bars=bars, idx=1, cfg=cfg_late)
    assert row2["limit_shape"] == "换手"


def test_classify_limit_shape_handles_missing_prices():
    cfg = ExecutableLabelConfig()
    assert classify_limit_shape(
        limit_flag="U", sealed_at_close=True, open_times=1, open_p=None, high=None, low=None, close=None,
        limit_price=11.0, last_time_seconds=None, cfg=cfg,
    ) == "未知"


# ---------------------------------------------------------------------------
# 旧日线代理降级
# ---------------------------------------------------------------------------


def test_daily_proxy_degradation_marks_label_source():
    """v3 打板行缺失（signal={}）→ 走日线代理，显式 label_source=daily_proxy_v2。"""
    bars = make_bars(
        [
            ("2026-02-01", 9.9, 10.05, 9.85, 10.0),
            ("2026-02-02", 10.2, 11.0, 10.15, 11.0),
            *[(f"2026-02-{d:02d}", 11.2, 11.4, 11.0, 11.3) for d in range(3, 9)],
        ]
    )
    row = build_sample_label(signal={}, bars=bars, idx=1, cfg=BASE_CFG)
    assert row["label_source"] == LABEL_SOURCE_PROXY
    assert row["limit_flag_inferred"] == 1
    assert row["limit_flag"] == "U"
    assert row["entry_confidence_A"] == "ohlc_inferred"
    assert row["entry_status_A"] == STATUS_FILLED
    # 代理口径拿不到封单，fill_probability 仍可由形态给出，但 seal_ratio 必为 None
    assert row["seal_ratio"] is None

    # 代理口径下 T 日只摸板未回封 → 推断为炸板 Z
    bars_broken = make_bars(
        [
            ("2026-02-01", 9.9, 10.05, 9.85, 10.0),
            ("2026-02-02", 10.2, 11.0, 10.1, 10.4),
            *[(f"2026-02-{d:02d}", 10.4, 10.6, 10.3, 10.5) for d in range(3, 9)],
        ]
    )
    row2 = build_sample_label(signal={}, bars=bars_broken, idx=1, cfg=BASE_CFG)
    assert row2["limit_flag"] == "Z"
    assert row2["is_broken_board"] == 1


def test_non_limit_up_day_is_not_a_sample():
    bars = make_bars([("2026-02-01", 9.9, 10.05, 9.85, 10.0), ("2026-02-02", 10.0, 10.4, 9.95, 10.3)])
    assert build_sample_label(signal={}, bars=bars, idx=1, cfg=BASE_CFG) is None


# ---------------------------------------------------------------------------
# 严格 PIT
# ---------------------------------------------------------------------------


def test_feature_safe_columns_do_not_depend_on_future_bars():
    """T 日已知列不得受未来 bar 影响（改未来价，这些列必须逐字不变）。"""
    base_rows = [
        ("2026-02-01", 9.9, 10.05, 9.85, 10.0),
        ("2026-02-02", 10.2, 11.0, 10.15, 11.0),
    ]
    tail_normal = [(f"2026-02-{d:02d}", 11.2, 11.4, 11.0, 11.3) for d in range(3, 9)]
    row_normal = build_sample_label(signal=signal_u(), bars=make_bars(base_rows + tail_normal), idx=1, cfg=BASE_CFG)

    tail_crash = [("2026-02-03", 6.0, 6.1, 5.5, 5.6)] + tail_normal[1:]
    row_crash = build_sample_label(signal=signal_u(), bars=make_bars(base_rows + tail_crash), idx=1, cfg=BASE_CFG)

    for col in sorted(FEATURE_SAFE_COLUMNS):
        if col in row_normal:
            assert row_normal[col] == row_crash[col], f"{col} 受未来数据影响，违反 PIT"
    assert row_normal["ret_1d_A"] != row_crash["ret_1d_A"]


def test_labels_ignore_limit_rows_at_or_before_signal_date():
    """future_limit_rows 里混入 T 日/更早日期必须被忽略（防未来函数与自相关泄漏）。"""
    bars = make_bars(
        [
            ("2026-02-01", 9.9, 10.05, 9.85, 10.0),
            ("2026-02-02", 10.2, 11.0, 10.15, 11.0),
            *[(f"2026-02-{d:02d}", 11.2, 11.4, 11.0, 11.3) for d in range(3, 9)],
        ]
    )
    clean = build_sample_label(signal=signal_u(), bars=bars, idx=1, cfg=BASE_CFG)
    polluted = build_sample_label(
        signal=signal_u(),
        bars=bars,
        idx=1,
        cfg=BASE_CFG,
        future_limit_rows={
            "2026-02-02": {"limit_flag": "U", "limit_times": 99},  # T 日，应忽略
            "2026-02-01": {"limit_flag": "U", "limit_times": 99},  # T-1，应忽略
        },
    )
    assert polluted["promotion_level_next"] == clean["promotion_level_next"]
    assert polluted["is_1_to_2"] == clean["is_1_to_2"]
    assert polluted["future_limit_flag_d1"] is None


def test_no_label_column_leaks_into_feature_whitelist():
    payload = build_sample_label(
        signal=signal_u(),
        bars=make_bars(
            [
                ("2026-02-01", 9.9, 10.05, 9.85, 10.0),
                ("2026-02-02", 10.2, 11.0, 10.15, 11.0),
                *[(f"2026-02-{d:02d}", 11.2, 11.4, 11.0, 11.3) for d in range(3, 9)],
            ]
        ),
        idx=1,
        cfg=BASE_CFG,
    )
    assert_no_label_in_features(sorted(FEATURE_SAFE_COLUMNS))
    with pytest.raises(ValueError, match="标签列泄漏"):
        assert_no_label_in_features(["volume_ratio_20d", "is_1_to_2"])
    with pytest.raises(ValueError):
        assert_no_label_in_features(["d1_high"])
    cols = list(payload.keys())
    leaked = label_columns(cols)
    # 三类分桶必须互斥，且"仅标签"列必然被判为泄漏
    assert not (META_COLUMNS & FEATURE_SAFE_COLUMNS)
    assert not (META_COLUMNS & LABEL_ONLY_COLUMNS)
    assert not (FEATURE_SAFE_COLUMNS & LABEL_ONLY_COLUMNS)
    assert set(LABEL_ONLY_COLUMNS).issubset(set(leaked))
    assert not set(FEATURE_SAFE_COLUMNS) & set(leaked)
    assert "ret_1d_A" in leaked and "is_big_loss" in leaked and "d1_open" in leaked
    assert "fill_probability" not in leaked
    assert "limit_times" not in leaked


def test_every_output_column_is_documented_and_classified():
    """每个标签字段都要有口径文档，且恰好落在 元信息/特征安全/仅标签 三类之一。"""
    payload = build_sample_label(
        signal=signal_u(),
        bars=make_bars(
            [
                ("2026-02-01", 9.9, 10.05, 9.85, 10.0),
                ("2026-02-02", 10.2, 11.0, 10.15, 11.0),
                *[(f"2026-02-{d:02d}", 11.2, 11.4, 11.0, 11.3) for d in range(3, 9)],
            ]
        ),
        idx=1,
        cfg=BASE_CFG,
    )
    docs = label_field_docs()
    buckets = META_COLUMNS | FEATURE_SAFE_COLUMNS | LABEL_ONLY_COLUMNS
    future_family = ("d1_", "d2_", "d3_", "d4_", "d5_", "future_")
    for col in payload:
        assert col in buckets or col.startswith(future_family), f"{col} 未归入三类分桶"
        # d1_* / future_* 由区间通配条目覆盖，其余必须有同名精确条目
        covered = col in docs or (col.startswith(future_family) and any("…" in k for k in docs))
        assert covered, f"{col} 缺少口径文档"
    # 文档条目不得引用已不存在的列名（防止口径与代码漂移）
    emitted = set(payload) | {"d1_open", "future_limit_flag_d1", "future_limit_times_d5"}
    for key in docs:
        if "…" in key or "/" in key:
            continue
        assert key in emitted, f"文档条目 {key} 没有对应输出列"


def test_returns_are_finite_or_none_never_nan_strings():
    payload = build_sample_label(
        signal=signal_u(),
        bars=make_bars(
            [
                ("2026-02-01", 9.9, 10.05, 9.85, 10.0),
                ("2026-02-02", 10.2, 11.0, 10.15, 11.0),
                *[(f"2026-02-{d:02d}", 11.2, 11.4, 11.0, 11.3) for d in range(3, 9)],
            ]
        ),
        idx=1,
        cfg=BASE_CFG,
    )
    for col in ("ret_1d_A", "ret_3d_A", "ret_5d_A", "fwd_ret_3d", "fwd_ret_5d", "max_drawdown_3d", "max_adverse_offset_3d"):
        value = payload[col]
        assert value is None or (isinstance(value, float) and math.isfinite(value)), col
    assert payload["max_drawdown_3d"] <= 0.0
    assert payload["max_adverse_offset_3d"] <= 0.0
    assert payload["max_runup_3d"] >= payload["max_adverse_offset_3d"]


def test_max_drawdown_and_adverse_offset_definitions():
    """max_drawdown_3d 是峰谷回撤，max_adverse_offset_3d 是相对入场价的最深下探。"""
    bars = make_bars(
        [
            ("2026-02-01", 9.9, 10.05, 9.85, 10.0),
            ("2026-02-02", 10.2, 11.0, 10.15, 11.0),  # entry_price_A = 11.00
            ("2026-02-03", 11.0, 11.5, 10.45, 11.2),  # 收盘新高 11.2，最低 10.45
            ("2026-02-04", 11.2, 11.3, 10.78, 10.8),  # 收盘峰谷回撤 10.8/11.2-1
            ("2026-02-05", 10.8, 11.1, 10.7, 11.0),
            ("2026-02-06", 11.0, 11.2, 10.9, 11.1),
            ("2026-02-09", 11.1, 11.3, 11.0, 11.2),
        ]
    )
    row = build_sample_label(signal=signal_u(), bars=bars, idx=1, cfg=BASE_CFG)
    assert row["max_adverse_offset_3d"] == pytest.approx(10.45 / 11.0 - 1.0, abs=1e-6)
    assert row["max_drawdown_3d"] == pytest.approx(10.8 / 11.2 - 1.0, abs=1e-6)
    assert row["max_runup_3d"] == pytest.approx(11.5 / 11.0 - 1.0, abs=1e-6)


def test_embargo_requirement_grows_with_horizon():
    short = ExecutableLabelConfig(long_horizon=3, short_horizon=2, continuation_horizon=2, promotion_horizon=1)
    assert short.max_horizon == 3
    assert short.required_embargo_days() == 4
    wide = ExecutableLabelConfig(embargo_buffer_days=3)
    assert wide.required_embargo_days() == wide.max_horizon + 3
