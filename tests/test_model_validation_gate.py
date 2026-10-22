"""Gate semantics: when must a complex model be marked 不保留 (issue #36)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from strategy.model_validation.gate import (
    DEFAULT_RULES,
    GateConfig,
    GateRule,
    REJECT,
    evaluate_gate,
    window_value,
)


def _window(
    fold: int,
    *,
    ic: float,
    pr_auc: float,
    precision: float,
    net_mean: float,
    cum: float,
    status: str = "ok",
    drawdown: float = -0.05,
    turnover: float = 0.2,
    coverage: float = 0.9,
) -> dict[str, Any]:
    return {
        "fold_index": fold,
        "status": status,
        "reason": "" if status == "ok" else "simulated failure",
        "metrics": {
            "ic_mean": ic,
            "pr_auc": pr_auc,
            "precision_at_topk": precision,
        },
        "portfolio": {
            "net_mean": net_mean,
            "cum_net_return": cum,
            "max_drawdown": drawdown,
            "turnover": turnover,
            "coverage_ratio": coverage,
        },
    }


BASE = [
    _window(i, ic=0.05, pr_auc=0.20, precision=0.40, net_mean=0.001, cum=0.01)
    for i in range(3)
]
GOOD_EXTRA = {
    "executability_source": "upstream",
    "coverage_ratio": 0.85,
    "predict_ms_per_sample": 0.5,
    "train_seconds": 0.2,
    "param_count": 2_000,
    "baseline_param_count": 12,
}


def test_candidate_with_stable_increment_is_kept() -> None:
    better = [
        _window(i, ic=0.09, pr_auc=0.26, precision=0.50, net_mean=0.0025, cum=0.02)
        for i in range(3)
    ]
    verdict = evaluate_gate("gru", better, BASE, GateConfig(), extra=GOOD_EXTRA)

    assert verdict.decision == "keep"
    assert verdict.kept
    assert verdict.blockers == []
    assert all(check.passed for check in verdict.checks)


def test_candidate_without_increment_is_rejected_with_a_readable_reason() -> None:
    worse = [
        _window(i, ic=0.02, pr_auc=0.15, precision=0.30, net_mean=-0.001, cum=-0.01)
        for i in range(3)
    ]
    verdict = evaluate_gate("gru", worse, BASE, GateConfig(), extra=GOOD_EXTRA)

    assert verdict.decision == REJECT
    assert "不保留" in verdict.conclusion_zh()
    assert any("ic_mean" in b for b in verdict.blockers)
    payload = json.dumps(verdict.as_dict(), ensure_ascii=False)
    assert "不保留候选模型" in payload


def test_lucky_single_window_cannot_pass_the_beat_ratio() -> None:
    spiked = [
        _window(0, ic=0.30, pr_auc=0.60, precision=0.90, net_mean=0.05, cum=0.30),
        _window(1, ic=0.01, pr_auc=0.15, precision=0.30, net_mean=-0.002, cum=-0.02),
        _window(2, ic=0.01, pr_auc=0.15, precision=0.30, net_mean=-0.002, cum=-0.02),
    ]
    verdict = evaluate_gate("gru", spiked, BASE, GateConfig(), extra=GOOD_EXTRA)

    assert verdict.decision == REJECT
    failing = {check.name for check in verdict.checks if not check.passed}
    assert any(
        "beat_ratio" in check.detail for check in verdict.checks if not check.passed
    )
    assert "rule[metrics.ic_mean]" in failing


def test_two_windows_are_not_enough_evidence() -> None:
    better = [
        _window(i, ic=0.09, pr_auc=0.26, precision=0.50, net_mean=0.0025, cum=0.02)
        for i in range(2)
    ]
    verdict = evaluate_gate(
        "gru", better, better, GateConfig(min_windows=3), extra=GOOD_EXTRA
    )

    assert verdict.decision == REJECT
    assert any(
        check.name == "min_windows" and not check.passed for check in verdict.checks
    )


def test_assumed_executability_blocks_a_keep_decision() -> None:
    better = [
        _window(i, ic=0.09, pr_auc=0.26, precision=0.50, net_mean=0.0025, cum=0.02)
        for i in range(3)
    ]
    extra = {**GOOD_EXTRA, "executability_source": "assumed"}
    verdict = evaluate_gate("gru", better, BASE, GateConfig(), extra=extra)

    assert verdict.decision == REJECT
    assert any("executability_from_upstream" in b for b in verdict.blockers)

    lenient = evaluate_gate(
        "gru", better, BASE, GateConfig(require_executability=False), extra=extra
    )
    assert lenient.decision == "keep", (
        "the block must come from the executability rule only"
    )


def test_failed_window_blocks_the_candidate() -> None:
    broken = [
        _window(0, ic=0.09, pr_auc=0.26, precision=0.50, net_mean=0.0025, cum=0.02),
        _window(
            1,
            ic=0.09,
            pr_auc=0.26,
            precision=0.50,
            net_mean=0.0025,
            cum=0.02,
            status="failed",
        ),
        _window(2, ic=0.09, pr_auc=0.26, precision=0.50, net_mean=0.0025, cum=0.02),
    ]
    verdict = evaluate_gate("gru", broken, BASE, GateConfig(), extra=GOOD_EXTRA)

    assert verdict.decision == REJECT
    assert any("all_windows_scored" in b for b in verdict.blockers)

    lenient = evaluate_gate(
        "gru",
        broken,
        BASE,
        GateConfig(require_all_windows_scored=False),
        extra=GOOD_EXTRA,
    )
    assert lenient.decision == "keep"


@pytest.mark.parametrize(
    "extra_patch,expected_blocker",
    [
        ({"predict_ms_per_sample": 120.0}, "latency_budget"),
        ({"train_seconds": 5_000.0}, "train_time_budget"),
        ({"param_count": 10_000_000}, "capacity_budget"),
        ({"coverage_ratio": 0.2}, "candidate_coverage"),
    ],
)
def test_cost_and_coverage_budgets_are_enforced(
    extra_patch: dict[str, float], expected_blocker: str
) -> None:
    better = [
        _window(i, ic=0.09, pr_auc=0.26, precision=0.50, net_mean=0.0025, cum=0.02)
        for i in range(3)
    ]
    verdict = evaluate_gate(
        "gru", better, BASE, GateConfig(), extra={**GOOD_EXTRA, **extra_patch}
    )

    assert verdict.decision == REJECT
    assert any(
        expected_blocker in check.name for check in verdict.checks if not check.passed
    )


def test_drawdown_and_turnover_regressions_are_rejected() -> None:
    risky = [
        _window(
            i,
            ic=0.09,
            pr_auc=0.26,
            precision=0.50,
            net_mean=0.0025,
            cum=0.02,
            drawdown=-0.40,
        )
        for i in range(3)
    ]
    assert (
        evaluate_gate("gru", risky, BASE, GateConfig(), extra=GOOD_EXTRA).decision
        == REJECT
    )

    churning = [
        _window(
            i,
            ic=0.09,
            pr_auc=0.26,
            precision=0.50,
            net_mean=0.0025,
            cum=0.02,
            turnover=0.95,
        )
        for i in range(3)
    ]
    verdict = evaluate_gate("gru", churning, BASE, GateConfig(), extra=GOOD_EXTRA)
    assert verdict.decision == REJECT
    assert any("turnover_budget" in b for b in verdict.blockers)


def test_missing_metric_is_treated_as_failure_not_ignored() -> None:
    sparse = [
        {**w, "metrics": {k: v for k, v in w["metrics"].items() if k != "pr_auc"}}
        for w in BASE
    ]
    verdict = evaluate_gate("gru", sparse, BASE, GateConfig(), extra=GOOD_EXTRA)

    assert verdict.decision == REJECT
    assert any("no comparable values" in b for b in verdict.blockers)


def test_windows_are_matched_by_fold_not_by_position() -> None:
    base = [
        _window(10, ic=0.05, pr_auc=0.20, precision=0.40, net_mean=0.001, cum=0.01),
        _window(11, ic=0.05, pr_auc=0.20, precision=0.40, net_mean=0.001, cum=0.01),
        _window(12, ic=0.50, pr_auc=0.90, precision=0.95, net_mean=0.05, cum=0.40),
    ]
    candidate = [
        _window(11, ic=0.09, pr_auc=0.26, precision=0.50, net_mean=0.0025, cum=0.02),
        _window(10, ic=0.09, pr_auc=0.26, precision=0.50, net_mean=0.0025, cum=0.02),
        _window(12, ic=0.09, pr_auc=0.26, precision=0.50, net_mean=0.0025, cum=0.02),
    ]
    verdict = evaluate_gate("gru", candidate, base, GateConfig(), extra=GOOD_EXTRA)

    assert verdict.decision == REJECT, (
        "the huge fold-12 gap must not be paired against the wrong window"
    )
    assert any("avg_delta" in check.detail for check in verdict.checks)


def test_window_value_walks_dotted_paths_defensively() -> None:
    window = {
        "metrics": {"ic_mean": 0.1},
        "portfolio": {"turnover": None},
        "cost": {"param_count": "n/a"},
    }
    assert window_value(window, "metrics.ic_mean") == pytest.approx(0.1)
    assert window_value(window, "portfolio.turnover") is None
    assert window_value(window, "cost.param_count") is None
    assert window_value(window, "missing.deep.path") is None


def test_default_rules_cover_the_issue_metrics() -> None:
    paths = {rule.path for rule in DEFAULT_RULES}
    assert {"metrics.ic_mean", "metrics.pr_auc", "metrics.precision_at_topk"} <= paths
    assert any(path.startswith("portfolio.") for path in paths)
    assert all(rule.min_beat_ratio >= 0.5 for rule in DEFAULT_RULES)
    assert (
        GateConfig(
            rules=(GateRule("metrics.ic_mean", min_delta=0.0, min_beat_ratio=1.0),)
        )
        .rules[0]
        .min_beat_ratio
        == 1.0
    )
