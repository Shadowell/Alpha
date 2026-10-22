"""Metric primitives (IC / rank IC / PR-AUC / 分层收益 / 校准 / 换手) - issue #36."""

from __future__ import annotations

import numpy as np
import pytest

from strategy.model_validation.metrics import (
    ScoreMetrics,
    cross_sectional_ic,
    evaluate_scores,
    expected_calibration_error,
    hit_rate,
    information_coefficient,
    layered_returns,
    max_drawdown,
    mean_topk_return,
    one_sided_turnover,
    pd_unique,
    pr_auc,
    precision_at_topk,
    rank_average,
    roc_auc,
    spearman,
    topk_selection,
)


def test_pearson_and_spearman_on_hand_values() -> None:
    x = np.array([1.0, 2.0, 3.0, 4.0])
    assert information_coefficient(x, 2.0 * x + 1.0) == pytest.approx(1.0)
    assert information_coefficient(x, -x) == pytest.approx(-1.0)
    # monotone but non-linear -> Pearson < 1, Spearman == 1
    assert spearman(x, x**3) == pytest.approx(1.0)
    assert information_coefficient(x, x**3) < 1.0


def test_degenerate_scores_return_none_instead_of_a_fake_number() -> None:
    flat = np.zeros(8)
    varied = np.arange(8, dtype=float)
    assert information_coefficient(flat, varied) is None
    assert spearman(flat, varied) is None
    assert layered_returns(flat, varied, n_layers=4).long_short is None


def test_nan_rows_are_dropped_not_zero_filled_for_ic() -> None:
    x = np.array([1.0, 2.0, 3.0, 4.0, np.nan])
    y = np.array([2.0, 4.0, 6.0, 8.0, 99.0])
    assert information_coefficient(x, y) == pytest.approx(1.0)


def test_rank_average_handles_ties() -> None:
    ranks = rank_average(np.array([5.0, 5.0, 1.0, 9.0]))
    assert ranks.tolist() == [1.5, 1.5, 0.0, 3.0]


def test_roc_auc_known_values() -> None:
    assert roc_auc(
        np.array([1.0, 0.0, 1.0, 0.0]), np.array([0.9, 0.1, 0.8, 0.2])
    ) == pytest.approx(1.0)
    assert roc_auc(np.array([1.0, 0.0]), np.array([0.5, 0.5])) == pytest.approx(0.5)
    assert roc_auc(np.array([1.0, 1.0]), np.array([0.4, 0.9])) is None  # single class


def test_pr_auc_matches_hand_computed_average_precision() -> None:
    labels = np.array([1.0, 0.0, 1.0])
    scores = np.array([0.9, 0.1, 0.8])
    assert pr_auc(labels, scores) == pytest.approx(1.0)
    worst = pr_auc(labels, -scores)
    # ranking reversed: positives land last -> AP = (1/3 * 0.5) + (2/3... ) computed below
    assert 0.0 < worst < 0.6


def test_precision_and_hit_rate_and_mean_return_at_topk() -> None:
    labels = np.array([1.0, 0.0, 1.0, 0.0])
    scores = np.array([0.9, 0.8, 0.2, 0.1])
    assert precision_at_topk(labels, scores, 2) == pytest.approx(0.5)
    returns = np.array([0.05, -0.02, 0.01, 0.03])
    assert hit_rate(returns, scores, 2) == pytest.approx(0.5)
    assert mean_topk_return(returns, scores, 2) == pytest.approx(0.015)
    assert precision_at_topk(labels, scores, 0) is None


def test_topk_selection_is_deterministic_on_ties() -> None:
    scores = np.array([1.0, 1.0, 1.0, 0.0])
    first = topk_selection(scores, 2)
    second = topk_selection(scores, 2)
    assert first.tolist() == second.tolist() == [0, 1]


def test_layered_returns_are_monotone_for_a_perfect_model() -> None:
    scores = np.arange(50, dtype=float)
    returns = 0.01 * scores
    result = layered_returns(scores, returns, n_layers=5)
    assert result.n_layers == 5
    assert result.long_short == pytest.approx(0.01 * 49 * (1.0 - 0.2) + 0.0, abs=0.06)
    assert result.monotonicity == pytest.approx(1.0)
    assert list(result.layer_returns) == sorted(result.layer_returns)


def test_layered_returns_detect_a_flat_profile() -> None:
    rng = np.random.default_rng(0)
    scores = rng.normal(size=200)
    returns = rng.normal(size=200)
    result = layered_returns(scores, returns, n_layers=5)
    assert abs(result.long_short or 0.0) < 0.15


def test_cross_sectional_ic_summary_and_ir() -> None:
    dates = np.array(["d1"] * 4 + ["d2"] * 4)
    scores = np.array([1.0, 2.0, 3.0, 4.0, 4.0, 3.0, 2.0, 1.0])
    returns = np.array([1.0, 2.0, 3.0, 4.0, -4.0, -3.0, -2.0, -1.0])
    summary = cross_sectional_ic(scores, returns, dates)
    assert summary["ic_mean"] == pytest.approx(0.0)
    assert summary["n_ic_days"] == 2
    assert summary["ic_positive_ratio"] == pytest.approx(0.5)
    perfect = cross_sectional_ic(
        np.arange(8, dtype=float), np.arange(8, dtype=float), dates
    )
    assert perfect["ic_mean"] == pytest.approx(1.0)
    assert perfect["icir"] is None  # zero dispersion across days


def test_calibration_metrics_on_hand_values() -> None:
    probs = np.array([0.5, 0.5, 0.9, 0.1])
    labels = np.array([1.0, 0.0, 1.0, 0.0])
    from strategy.model_validation.metrics import brier_score

    assert brier_score(probs, labels) == pytest.approx((0.25 + 0.25 + 0.01 + 0.01) / 4)
    flat = np.array([0.5, 0.5, 0.5, 0.5])
    mixed = np.array([1.0, 0.0, 1.0, 0.0])
    assert expected_calibration_error(flat, mixed, bins=2) == pytest.approx(0.0)
    assert expected_calibration_error(probs, labels, bins=10) == pytest.approx(
        0.05, abs=1e-9
    )
    overconfident = np.array([0.99, 0.99])
    wrong = np.array([0.0, 0.0])
    assert expected_calibration_error(overconfident, wrong) == pytest.approx(
        0.99, abs=1e-9
    )


def test_max_drawdown_uses_compounded_equity() -> None:
    returns = np.array([0.10, -0.20, 0.05])
    assert max_drawdown(returns) == pytest.approx(0.88 / 1.10 - 1.0, abs=1e-12)
    assert max_drawdown(np.array([0.1, 0.2])) == pytest.approx(0.0, abs=1e-12)
    assert max_drawdown(np.array([np.nan])) is None


def test_one_sided_turnover_definition() -> None:
    assert one_sided_turnover([["a", "b"], ["a", "c"]]) == pytest.approx(1 / 3)
    assert one_sided_turnover([["a", "b"], ["a", "b"]]) == pytest.approx(0.0)
    assert one_sided_turnover([["a"]]) is None


def test_pd_unique_stable_and_sortable() -> None:
    assert pd_unique(np.array(["2026-01-02", "2026-01-01", "2026-01-02"])) == [
        "2026-01-01",
        "2026-01-02",
    ]


def test_evaluate_scores_bundle_is_complete_and_json_friendly() -> None:
    rng = np.random.default_rng(1)
    n = 60
    signal = rng.normal(size=n)
    scores = signal + 0.3 * rng.normal(size=n)
    metrics: ScoreMetrics = evaluate_scores(
        scores=scores,
        labels=(signal > 0).astype(float),
        returns=0.02 * signal,
        dates=np.array([f"2026-01-{(i % 10) + 1:02d}" for i in range(n)]),
        top_k=5,
        n_layers=5,
    )
    payload = metrics.as_dict()
    for key in (
        "pooled_ic",
        "pooled_rank_ic",
        "ic_mean",
        "pr_auc",
        "precision_at_topk",
        "layers",
        "ece",
    ):
        assert key in payload
    assert payload["n_dates"] == 10
    assert payload["pooled_ic"] > 0.5
    assert payload["layers"]["monotonicity"] > 0.5


def test_evaluate_scores_reports_none_when_labels_are_single_class() -> None:
    metrics = evaluate_scores(
        scores=np.arange(20, dtype=float),
        labels=np.ones(20),
        returns=np.linspace(-0.1, 0.1, 20),
        dates=np.array(["a"] * 10 + ["b"] * 10),
        top_k=3,
    )
    assert metrics.roc_auc is None
    assert metrics.pr_auc is None
    assert metrics.positive_rate == pytest.approx(1.0)


def test_calibration_ignored_for_uncalibrated_logits() -> None:
    metrics = evaluate_scores(
        scores=np.array([-3.0, 2.0, 5.0, -1.0]),
        labels=np.array([0.0, 1.0, 1.0, 0.0]),
        returns=np.array([-0.1, 0.2, 0.3, -0.05]),
        dates=np.array(["a", "a", "b", "b"]),
        top_k=2,
    )
    assert metrics.brier is None and metrics.ece is None
    calibrated = evaluate_scores(
        scores=np.array([0.1, 0.8, 0.9, 0.2]),
        labels=np.array([0.0, 1.0, 1.0, 0.0]),
        returns=np.array([-0.1, 0.2, 0.3, -0.05]),
        dates=np.array(["a", "a", "b", "b"]),
        top_k=2,
    )
    assert calibrated.brier is not None and calibrated.ece is not None
