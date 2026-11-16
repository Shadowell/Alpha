"""#27 分层晋级概率模型与因子评估体系全链路单元测试。"""

from __future__ import annotations

import numpy as np
import pytest

from strategy.first_limit_alpha.tiered_model.calibration import (
    PlattCalibrator,
    reliability_curve,
)
from strategy.first_limit_alpha.tiered_model.dataset import (
    build_all_tier_datasets,
    to_validation_panel,
)
from strategy.first_limit_alpha.tiered_model.explain import explain_single_sample
from strategy.first_limit_alpha.tiered_model.factors import (
    compute_vif_table,
    evaluate_factor_ic,
)
from strategy.first_limit_alpha.tiered_model.pipeline import run_tiered_model_pipeline
from strategy.first_limit_alpha.tiered_model.pit import (
    LeakageError,
    assert_pit_features,
    find_leakage_columns,
)
from strategy.first_limit_alpha.tiered_model.synthetic import (
    SyntheticTierConfig,
    make_synthetic_labels,
)
from strategy.first_limit_alpha.tiered_model.tiers import (
    TIER_ORDER,
    assert_tiers_disjoint,
    assign_board_tiers,
)
from strategy.first_limit_alpha.tiered_model.walkforward import run_tier_walkforward


def test_synthetic_and_all_tier_datasets():
    labels, factors, meta = make_synthetic_labels(SyntheticTierConfig(n_dates=15, n_symbols=12))
    assert not labels.empty
    assert not factors.empty
    assert isinstance(meta, dict)

    tiered, _ = assign_board_tiers(labels)
    assert_tiers_disjoint(tiered)

    datasets = build_all_tier_datasets(labels, factors)
    assert set(datasets.keys()).issubset(set(TIER_ORDER))
    assert len(datasets) > 0

    first_tier = next(iter(datasets.values()))
    assert not first_tier.frame.empty
    panel = to_validation_panel(
        first_tier.frame,
        first_tier.tier,
        features=first_tier.features,
        primary_head=first_tier.primary_head,
    )
    assert panel.frame is not None
    assert not panel.frame.empty


def test_pit_leakage_guard():
    assert "auction_price" in find_leakage_columns(["seal_strength", "auction_price"])
    with pytest.raises(LeakageError):
        assert_pit_features(["seal_strength", "auction_price"])


def test_calibration_and_factor_evaluation():
    y = np.array([0, 0, 1, 1, 0, 1, 1, 0], dtype=float)
    p = np.array([0.2, 0.3, 0.7, 0.8, 0.4, 0.6, 0.9, 0.1], dtype=float)
    cal = PlattCalibrator().fit(p, y)
    p_cal = cal.transform(p)
    assert p_cal.shape == p.shape
    rel = reliability_curve(y, p_cal, n_bins=4)
    assert 0.0 <= rel["ece"] <= 1.0
    assert 0.0 <= rel["brier"] <= 1.0

    labels, factors, _ = make_synthetic_labels(SyntheticTierConfig(n_dates=25, n_symbols=12))
    datasets = build_all_tier_datasets(labels, factors)
    ds = datasets["first_board"]
    ret_col = "fwd_ret_3d" if "fwd_ret_3d" in ds.frame.columns else f"y_{ds.primary_head}"
    ic_df = evaluate_factor_ic(ds.frame, ds.features[:5], return_col=ret_col)
    assert not ic_df.empty
    assert "rank_ic_mean" in ic_df.columns
    vif_df = compute_vif_table(ds.frame, ds.features[:4])
    assert len(vif_df) == min(4, len(ds.features[:4]))


def test_end_to_end_tiered_pipeline_and_explain():
    labels, factors, _ = make_synthetic_labels(SyntheticTierConfig(n_dates=30, n_symbols=14))
    report = run_tiered_model_pipeline(
        labels,
        factors,
        tiers=("first_board", "one_to_two"),
        n_splits=2,
        embargo_dates=2,
        min_train_dates=8,
    )
    assert "first_board" in report["tier_reports"]
    fb = report["tier_reports"]["first_board"]
    assert fb["status"] == "OK"
    assert fb["n_rows"] > 0
    assert "auc" in fb["oos_metrics"]
    assert "first_board" in report["model_cards"]
    card = report["model_cards"]["first_board"]
    assert card["tier"] == "first_board"
    assert len(card["known_failure_modes"]) >= 1

    # 单样本解释验证
    ds = build_all_tier_datasets(labels, factors, tiers=("first_board",))["first_board"]
    wf = run_tier_walkforward(ds, n_splits=2, embargo_dates=2, min_train_dates=8)
    clf = wf.get("last_estimator")
    if clf is not None:
        row_x = ds.frame[list(ds.features)].iloc[0].to_numpy(dtype=float)
        ex = explain_single_sample(clf, row_x, ds.features, top_k=3)
        assert len(ex) <= 3
