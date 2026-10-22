"""Hard keep / reject gate for complex models (issue #36).

The issue is explicit: *没有增量就不上线，这是硬门槛*.  So the verdict is computed
from thresholds, not from a human eyeballing a table, and a ``keep`` requires
**every** check to pass:

* at least ``min_windows`` (default 3) walk-forward OOS windows were scored;
* each rule's metric improved by at least ``min_delta`` on average **and** in at
  least ``min_beat_ratio`` of the windows (a lucky single window cannot pass);
* the candidate did not blow the drawdown / turnover / coverage budgets;
* executability came from #26 rather than an "everything fills" assumption;
* inference latency, training time and parameter count stay inside the budget.

Any failed check produces ``decision="reject"`` with a Chinese conclusion
``"不保留"`` that is written verbatim into the report + decision.json.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import mean
from typing import Any, Iterable, Sequence

KEEP = "keep"
REJECT = "reject"
CONCLUSION_ZH = {KEEP: "保留候选模型", REJECT: "不保留候选模型（样本外无稳定增量）"}


@dataclass(frozen=True, slots=True)
class GateRule:
    """A single paired-metric improvement requirement."""

    path: str  # dotted path inside the per-window aggregate, e.g. "metrics.ic_mean"
    min_delta: float
    min_beat_ratio: float = 0.6
    label_zh: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "min_delta": self.min_delta,
            "min_beat_ratio": self.min_beat_ratio,
            "label_zh": self.label_zh,
        }


DEFAULT_RULES: tuple[GateRule, ...] = (
    GateRule(
        "metrics.ic_mean",
        min_delta=0.01,
        min_beat_ratio=0.6,
        label_zh="横截面 rank IC 均值",
    ),
    GateRule("metrics.pr_auc", min_delta=0.01, min_beat_ratio=0.6, label_zh="PR-AUC"),
    GateRule(
        "metrics.precision_at_topk",
        min_delta=0.02,
        min_beat_ratio=0.6,
        label_zh="TopK 命中率",
    ),
    GateRule(
        "portfolio.net_mean",
        min_delta=0.0,
        min_beat_ratio=0.6,
        label_zh="费用后 TopK 平均收益",
    ),
    GateRule(
        "portfolio.cum_net_return",
        min_delta=0.0,
        min_beat_ratio=0.6,
        label_zh="费用后累计收益",
    ),
)


@dataclass(frozen=True, slots=True)
class GateConfig:
    min_windows: int = 3
    rules: tuple[GateRule, ...] = DEFAULT_RULES
    require_executability: bool = True
    max_drawdown_worsening: float = 0.05
    max_turnover_increase: float = 0.15
    min_coverage_ratio: float = 0.5
    require_all_windows_scored: bool = True
    latency_budget_ms_per_sample: float = 25.0
    max_train_seconds_per_window: float = 600.0
    max_param_ratio_vs_baseline: float = 200.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "min_windows": self.min_windows,
            "rules": [rule.as_dict() for rule in self.rules],
            "require_executability": self.require_executability,
            "max_drawdown_worsening": self.max_drawdown_worsening,
            "max_turnover_increase": self.max_turnover_increase,
            "min_coverage_ratio": self.min_coverage_ratio,
            "require_all_windows_scored": self.require_all_windows_scored,
            "latency_budget_ms_per_sample": self.latency_budget_ms_per_sample,
            "max_train_seconds_per_window": self.max_train_seconds_per_window,
            "max_param_ratio_vs_baseline": self.max_param_ratio_vs_baseline,
        }


@dataclass(slots=True)
class GateCheck:
    name: str
    passed: bool
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass(slots=True)
class GateVerdict:
    candidate: str
    decision: str
    checks: list[GateCheck] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)

    @property
    def kept(self) -> bool:
        return self.decision == KEEP

    def conclusion_zh(self) -> str:
        return CONCLUSION_ZH[self.decision]

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate": self.candidate,
            "decision": self.decision,
            "decision_zh": self.conclusion_zh(),
            "kept": self.kept,
            "blockers": list(self.blockers),
            "checks": [check.as_dict() for check in self.checks],
        }


def window_value(window: dict[str, Any], path: str) -> float | None:
    node: Any = window
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    if node is None:
        return None
    try:
        value = float(node)
    except (TypeError, ValueError):
        return None
    return value if value == value else None  # drop NaN


def _paired(
    baseline: Sequence[dict[str, Any]], candidate: Sequence[dict[str, Any]], path: str
) -> list[tuple[float, float]]:
    cand_by_fold = {int(w["fold_index"]): w for w in candidate}
    pairs: list[tuple[float, float]] = []
    for base_window in baseline:
        fold = int(base_window["fold_index"])
        other = cand_by_fold.get(fold)
        if other is None:
            continue
        b = window_value(base_window, path)
        c = window_value(other, path)
        if b is None or c is None:
            continue
        pairs.append((b, c))
    return pairs


def evaluate_gate(
    candidate_name: str,
    candidate_windows: Sequence[dict[str, Any]],
    baseline_windows: Sequence[dict[str, Any]],
    config: GateConfig = GateConfig(),
    *,
    extra: dict[str, Any] | None = None,
) -> GateVerdict:
    """Compare one candidate against the baseline and emit a keep / reject verdict."""

    extra = extra or {}
    verdict = GateVerdict(candidate=candidate_name, decision=KEEP)

    def add(name: str, passed: bool, detail: str) -> None:
        verdict.checks.append(GateCheck(name=name, passed=bool(passed), detail=detail))
        if not passed:
            verdict.blockers.append(f"{name}: {detail}")

    n_candidate = len(candidate_windows)
    add(
        "min_windows",
        n_candidate >= config.min_windows
        and len(baseline_windows) >= config.min_windows,
        f"candidate has {n_candidate} scored OOS windows, baseline has {len(baseline_windows)}, "
        f"required >= {config.min_windows}",
    )

    unscored = [w for w in candidate_windows if str(w.get("status", "ok")) != "ok"]
    if config.require_all_windows_scored:
        add(
            "all_windows_scored",
            not unscored,
            "all windows scored"
            if not unscored
            else f"{len(unscored)} window(s) degraded/failed: "
            f"{[(int(w['fold_index']), w.get('status'), str(w.get('reason'))[:60]) for w in unscored][:3]}",
        )

    executable = str(extra.get("executability_source", "assumed"))
    if config.require_executability:
        add(
            "executability_from_upstream",
            executable == "upstream",
            f"executability_source={executable!r} (需要 #26 提供 can_buy/can_sell；"
            f"'assumed' 表示按全部可成交假设，不允许上线)",
        )

    coverage = extra.get("coverage_ratio")
    if coverage is not None:
        add(
            "candidate_coverage",
            float(coverage) >= config.min_coverage_ratio,
            f"coverage_ratio={float(coverage):.4f} required >= {config.min_coverage_ratio}",
        )

    for rule in config.rules:
        pairs = _paired(baseline_windows, candidate_windows, rule.path)
        if not pairs:
            add(
                f"rule[{rule.path}]",
                False,
                f"no comparable values for {rule.path} on the same folds",
            )
            continue
        deltas = [c - b for b, c in pairs]
        avg = float(mean(deltas))
        beat = sum(1 for d in deltas if d > 0) / len(deltas)
        ok = avg >= rule.min_delta and beat >= rule.min_beat_ratio
        add(
            f"rule[{rule.path}]",
            ok,
            f"{rule.label_zh or rule.path}: avg_delta={avg:+.6f} (min {rule.min_delta:+.6f}), "
            f"beat_ratio={beat:.2f} (min {rule.min_beat_ratio:.2f}) over {len(pairs)} windows",
        )

    base_dd = _mean_path(baseline_windows, "portfolio.max_drawdown")
    cand_dd = _mean_path(candidate_windows, "portfolio.max_drawdown")
    if base_dd is not None and cand_dd is not None:
        add(
            "drawdown_budget",
            cand_dd >= base_dd - config.max_drawdown_worsening,
            f"avg max_drawdown candidate={cand_dd:.6f} baseline={base_dd:.6f} "
            f"allowed worsening={config.max_drawdown_worsening}",
        )

    base_to = _mean_path(baseline_windows, "portfolio.turnover")
    cand_to = _mean_path(candidate_windows, "portfolio.turnover")
    if base_to is not None and cand_to is not None:
        add(
            "turnover_budget",
            cand_to <= base_to + config.max_turnover_increase,
            f"avg turnover candidate={cand_to:.4f} baseline={base_to:.4f} "
            f"allowed +{config.max_turnover_increase}",
        )

    latency = extra.get("predict_ms_per_sample")
    if latency is not None:
        add(
            "latency_budget",
            float(latency) <= config.latency_budget_ms_per_sample,
            f"CPU inference {float(latency):.4f} ms/sample, budget {config.latency_budget_ms_per_sample} ms",
        )

    train_seconds = extra.get("train_seconds")
    if train_seconds is not None:
        add(
            "train_time_budget",
            float(train_seconds) <= config.max_train_seconds_per_window,
            f"train {float(train_seconds):.3f} s/window, budget {config.max_train_seconds_per_window} s",
        )

    params = extra.get("param_count")
    base_params = extra.get("baseline_param_count")
    if params is not None and base_params:
        ratio = float(params) / max(1.0, float(base_params))
        add(
            "capacity_budget",
            ratio <= config.max_param_ratio_vs_baseline,
            f"param ratio candidate/baseline = {ratio:.2f}, budget {config.max_param_ratio_vs_baseline}",
        )

    verdict.decision = KEEP if all(check.passed for check in verdict.checks) else REJECT
    return verdict


def _mean_path(windows: Iterable[dict[str, Any]], path: str) -> float | None:
    values = [v for v in (window_value(w, path) for w in windows) if v is not None]
    return None if not values else float(mean(values))
