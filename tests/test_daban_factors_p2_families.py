"""#29 / #30 / #31 打板因子族（题材生命周期、竞价分时、筹码解禁事件风险）单元测试。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from strategy.daban_factors import FACTOR_FAMILY_MODULES, family_capability
from strategy.daban_factors.auction_intraday import (
    AUCTION_INTRADAY_FACTOR_NAMES,
    compute_auction_intraday_factors,
)
from strategy.daban_factors.chip_event_risk import (
    CHIP_EVENT_FACTOR_NAMES,
    compute_chip_event_risk_factors,
    evaluate_risk_filter,
)
from strategy.daban_factors.theme_lifecycle import (
    THEME_FACTOR_NAMES,
    compute_theme_lifecycle_factors,
)


def test_family_capability_lists_all_four_issues():
    assert set(FACTOR_FAMILY_MODULES.keys()) == {28, 29, 30, 31}
    cap = family_capability()
    assert set(cap.keys()) == {"28", "29", "30", "31"}
    for key in ("29", "30", "31"):
        assert cap[key]["factor_count"] > 20


def test_theme_lifecycle_pit_and_ranks():
    board = pd.DataFrame(
        [
            {"symbol": "000001", "trade_date": "2026-10-20", "is_limit_up": True, "limit_times": 3, "amount": 5e8, "pct_chg": 10.0, "fd_amount_ratio": 0.2},
            {"symbol": "000002", "trade_date": "2026-10-20", "is_limit_up": True, "limit_times": 1, "amount": 3e8, "pct_chg": 10.0, "fd_amount_ratio": 0.1},
            {"symbol": "000003", "trade_date": "2026-10-21", "is_limit_up": True, "limit_times": 1, "amount": 2e8, "pct_chg": 10.0},
        ]
    )
    cm = pd.DataFrame(
        [
            {"symbol": "000001", "concept": "AI算力", "effective_from": "2026-10-01", "effective_to": ""},
            {"symbol": "000002", "concept": "AI算力", "effective_from": "2026-10-01", "effective_to": ""},
            {"symbol": "000001", "concept": "未来概念", "effective_from": "2026-10-25", "effective_to": ""},
        ]
    )
    hist = pd.DataFrame(
        [
            {"concept": "AI算力", "trade_date": "2026-10-19", "limit_up_count": 1, "pct_chg": 3.0, "is_active": True},
            {"concept": "AI算力", "trade_date": "2026-10-20", "limit_up_count": 2, "pct_chg": 5.0, "is_active": True},
            {"concept": "AI算力", "trade_date": "2026-10-21", "limit_up_count": 9, "pct_chg": 8.0, "is_active": True},
        ]
    )
    out = compute_theme_lifecycle_factors(
        board, concept_members=cm, concept_history=hist, trade_date="2026-10-20", regime_score=65.0
    )
    assert len(out) == 2
    for col in THEME_FACTOR_NAMES:
        assert col in out.columns
    r1 = out[out["symbol"] == "000001"].iloc[0]
    r2 = out[out["symbol"] == "000002"].iloc[0]
    # 未来概念不应出现在 2026-10-20
    assert r1["concept_count"] == 1.0
    assert r1["is_concept_leader"] == 1.0
    assert r2["board_position_in_concept"] >= 2.0
    assert r1["concept_age_days"] == 2.0


def test_auction_intraday_proxy_and_pit_cutoff():
    snap = pd.DataFrame(
        [
            {
                "symbol": "000001",
                "trade_date": "2026-10-20",
                "pre_close": 10.0,
                "open": 10.3,
                "prev_vol": 1e6,
                "prev_amount": 1e7,
                "prev_high": 10.2,
                "float_share": 1e8,
                "limit_up_price": 11.0,
                "prev_weak_board": True,
                "prev_strong_board": False,
            }
        ]
    )
    mins = pd.DataFrame(
        [
            {"symbol": "000001", "trade_date": "2026-10-20", "bar_time": "2026-10-20 09:31", "close": 10.4, "high": 10.5, "low": 10.3, "volume": 1e5, "amount": 1.04e6},
            {"symbol": "000001", "trade_date": "2026-10-20", "bar_time": "2026-10-20 09:35", "close": 10.6, "high": 10.6, "low": 10.4, "volume": 1e5, "amount": 1.06e6},
            {"symbol": "000001", "trade_date": "2026-10-20", "bar_time": "2026-10-20 09:45", "close": 11.0, "high": 11.0, "low": 10.6, "volume": 2e5, "amount": 2.2e6},
            {"symbol": "000001", "trade_date": "2026-10-20", "bar_time": "2026-10-20 14:55", "close": 11.0, "high": 11.0, "low": 11.0, "volume": 1e4, "amount": 1.1e5},
        ]
    )
    mid = compute_auction_intraday_factors(snap, minute_df=mins, as_of_hhmm="09:35")
    assert len(mid) == 1
    for col in AUCTION_INTRADAY_FACTOR_NAMES:
        assert col in mid.columns
    row = mid.iloc[0]
    assert row["auction_gap_pct_is_proxy"] == 1
    assert np.isclose(row["auction_gap_pct"], 0.03)
    assert row["auction_weak_to_strong"] == 1.0
    assert np.isclose(row["open_5m_return"], 0.06)
    # 09:35 截断时不应看到 09:45 的封板或 14:55 的尾盘
    assert np.isnan(row["first_seal_time"])
    assert np.isnan(row["tail_30m_return"])


def test_chip_event_risk_pit_and_filter():
    base = pd.DataFrame(
        [
            {
                "symbol": "000001",
                "trade_date": "2026-10-20",
                "name": "测试股",
                "close": 12.0,
                "float_mv": 5e9,
                "total_mv": 8e9,
                "free_share": 4e8,
                "turnover_rate": 0.08,
                "volume_ratio": 2.1,
                "avg_cost": 10.0,
                "cost_90_low": 9.0,
                "cost_90_high": 11.0,
                "pledge_ratio": 0.55,
                "list_date": "2026-01-10",
            }
        ]
    )
    holders = pd.DataFrame(
        [
            {"symbol": "000001", "ann_date": "2026-07-20", "holder_count": 50000},
            {"symbol": "000001", "ann_date": "2026-10-15", "holder_count": 40000},
            {"symbol": "000001", "ann_date": "2026-10-25", "holder_count": 10000},  # 未来披露
        ]
    )
    unlocks = pd.DataFrame(
        [
            {"symbol": "000001", "ann_date": "2026-09-01", "unlock_date": "2026-11-05", "unlock_amount_ratio": 0.12, "unlock_mv_ratio": 0.15}
        ]
    )
    events = pd.DataFrame(
        [
            {"symbol": "000001", "ann_date": "2026-10-10", "event_type": "regulatory_letter"},
            {"symbol": "000001", "ann_date": "2026-10-25", "event_type": "penalty"},  # 未来披露
        ]
    )
    out = compute_chip_event_risk_factors(
        base, holder_df=holders, unlock_df=unlocks, risk_events_df=events
    )
    assert len(out) == 1
    for col in CHIP_EVENT_FACTOR_NAMES:
        assert col in out.columns
    row = out.iloc[0]
    assert row["holder_count"] == 40000.0
    assert np.isclose(row["holder_count_change_1q"], -0.2)
    assert row["is_unlock_30d"] == 1.0
    assert row["pledge_risk_flag"] == 1.0
    assert row["regulatory_letter_flag"] == 1.0
    assert row["penalty_flag"] == 0.0
    assert row["is_sub_new"] == 1.0

    passed, reasons = evaluate_risk_filter(row.to_dict())
    assert passed is False
    assert "major_unlock_within_30d" in reasons
    assert "high_pledge_risk" in reasons
    assert "regulatory_letter" in reasons
