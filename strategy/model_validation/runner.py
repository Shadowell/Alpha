"""Run a baseline-vs-candidate increment validation (issue #36).

Pipeline (all steps reproducible from the recorded config + data fingerprint):

1. validate / normalise the panel (row order is made strictly temporal);
2. build walk-forward folds with embargo ``max(embargo, label_horizon)`` + purge;
3. for every fold: fit baseline, fit each candidate, predict the *same* OOS rows;
4. score every model through the identical metric + portfolio code path;
5. aggregate over windows, run the gate, and (optionally) write the report.

Failures are recorded, never hidden: an optional deep dependency that is missing
(``OptionalDependencyError``) or a model artifact that cannot be reloaded
(``ModelArtifactError``) marks that model's window ``status="skipped"``/
``"failed"`` with a reason, and ``GateConfig.require_all_windows_scored`` turns
that into a blocked ``keep`` decision.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean
from typing import Any, Sequence

import numpy as np

from .contracts import ColumnNames, ModelInput, Panel, validate_panel
from .gate import GateConfig, GateVerdict, evaluate_gate
from .metrics import ScoreMetrics, evaluate_scores
from .models import (
    ConstantScoreModel,
    KronosImageAdapter,
    ModelArtifactError,
    OptionalDependencyError,
    RankBlendModel,
    RidgeTabularModel,
    SequenceTemporalModel,
    TorchSequenceAdapter,
    save_model,
)
from .portfolio import PortfolioConfig, PortfolioResult, run_portfolio
from .report import render_markdown_report, write_outputs
from .splitting import (
    WalkForwardConfig,
    build_walk_forward_folds,
    leakage_probe,
    split_panel_rows,
)

ROLE_BASELINE = "baseline"
ROLE_CANDIDATE = "candidate"


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """Declarative, serialisable model definition."""

    name: str
    role: str = ROLE_CANDIDATE
    factory: str = "SequenceTemporalModel"
    params: dict[str, Any] = field(default_factory=dict)
    card: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "role": self.role,
            "factory": self.factory,
            "params": dict(self.params),
        }


@dataclass(frozen=True, slots=True)
class ValidationConfig:
    seed: int = 42
    label_horizon: int = 2
    top_k: int = 5
    n_layers: int = 5
    require_sequence: bool = False
    save_artifacts: bool = False
    walk_forward: WalkForwardConfig = field(default_factory=WalkForwardConfig)
    portfolio: PortfolioConfig = field(default_factory=PortfolioConfig)
    gate: GateConfig = field(default_factory=GateConfig)
    models: tuple[ModelSpec, ...] = ()
    data_version: str = "unknown"
    feature_version: str = "unknown"
    notes: str = ""

    def model_specs(self) -> tuple[ModelSpec, ...]:
        return self.models or default_models()

    def as_dict(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "label_horizon": self.label_horizon,
            "top_k": self.top_k,
            "n_layers": self.n_layers,
            "require_sequence": self.require_sequence,
            "save_artifacts": self.save_artifacts,
            "walk_forward": self.walk_forward.as_dict(),
            "portfolio": self.portfolio.as_dict(),
            "gate": self.gate.as_dict(),
            "models": [spec.as_dict() for spec in self.model_specs()],
            "data_version": self.data_version,
            "feature_version": self.feature_version,
            "notes": self.notes,
        }

    def config_hash(self) -> str:
        payload = json.dumps(
            self.as_dict(), sort_keys=True, ensure_ascii=False, default=str
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def default_models() -> tuple[ModelSpec, ...]:
    """The three bundled CPU-only models: baseline, sequence candidate, blend."""

    return (
        ModelSpec(
            name="tabular_baseline",
            role=ROLE_BASELINE,
            factory="RidgeTabularModel",
            params={"name": "tabular_baseline"},
            card={
                "kind": "tabular",
                "applicable_zh": "全市场表格因子（价格/量能/情绪）已齐备的日常样本",
                "failure_zh": "缺少横截面因子时退化为常数，不能用于纯序列场景",
            },
        ),
        ModelSpec(
            name="sequence_candidate",
            role=ROLE_CANDIDATE,
            factory="SequenceTemporalModel",
            params={
                "name": "sequence_candidate",
                "alpha": 1.0,
                "decay": 0.85,
                "use_tabular": True,
            },
            card={
                "kind": "sequence",
                "applicable_zh": "20-40 日 OHLCV/因子序列，需要时序形态信息的场景",
                "failure_zh": "窗口长度不一致或序列里没有时间信息时增量不稳定",
            },
        ),
        ModelSpec(
            name="blend_candidate",
            role=ROLE_CANDIDATE,
            factory="RankBlendModel",
            params={"name": "blend_candidate", "weight": 0.5},
            card={
                "kind": "ensemble",
                "applicable_zh": "序列模型有微弱增量、但不敢全量替换 baseline 时",
                "failure_zh": "两个模型都失效时排名平均同样失效",
            },
        ),
    )


_FACTORIES: dict[str, Any] = {
    "RidgeTabularModel": RidgeTabularModel,
    "SequenceTemporalModel": SequenceTemporalModel,
    "ConstantScoreModel": ConstantScoreModel,
    "RankBlendModel": RankBlendModel,
    # optional-dependency adapters: constructing them never imports torch, only
    # fit/predict does, and that raises OptionalDependencyError -> status="skipped"
    "TorchSequenceAdapter": TorchSequenceAdapter,
    "KronosImageAdapter": KronosImageAdapter,
}


def _build(spec: ModelSpec) -> Any:
    factory = _FACTORIES.get(spec.factory)
    if factory is None:
        raise ModelArtifactError(f"unknown model factory {spec.factory!r}")
    return factory(**spec.params)


def data_fingerprint(panel: Panel) -> dict[str, Any]:
    columns = panel.columns
    keys = panel.frame[[columns.symbol, columns.event_date]].to_numpy(dtype=str)
    digest = hashlib.sha256(
        "\n".join(f"{a}|{b}" for a, b in keys).encode("utf-8")
    ).hexdigest()
    dates = sorted(set(key[1] for key in keys))
    return {
        "n_rows": int(len(keys)),
        "n_dates": len(dates),
        "first_date": dates[0] if dates else None,
        "last_date": dates[-1] if dates else None,
        "row_key_sha256": digest[:16],
    }


@dataclass(slots=True)
class WindowRecord:
    """Everything one (fold, model) pair contributes to the comparison."""

    fold_index: int
    model: str
    status: str  # ok | skipped | failed
    reason: str
    rows: dict[str, int]
    metrics: dict[str, Any]
    portfolio: dict[str, Any]
    cost: dict[str, Any]
    leakage: dict[str, Any]

    def flat(self) -> dict[str, Any]:
        """Gate-facing shape: ``metrics`` / ``portfolio`` as dotted paths."""

        return {
            "fold_index": self.fold_index,
            "model": self.model,
            "status": self.status,
            "reason": self.reason,
            "rows": dict(self.rows),
            "metrics": dict(self.metrics),
            "portfolio": dict(self.portfolio),
            "cost": dict(self.cost),
            "leakage": dict(self.leakage),
        }


@dataclass(slots=True)
class RunResult:
    metadata: dict[str, Any]
    windows: list[WindowRecord]
    aggregates: dict[str, dict[str, Any]]
    verdicts: dict[str, GateVerdict]
    artifacts: dict[str, str] = field(default_factory=dict)

    @property
    def overall_decision(self) -> str:
        if any(v.kept for v in self.verdicts.values()):
            return "keep"
        return "reject"

    def kept_candidates(self) -> list[str]:
        return [name for name, verdict in self.verdicts.items() if verdict.kept]

    def rejected_candidates(self) -> list[str]:
        return [name for name, verdict in self.verdicts.items() if not verdict.kept]

    def to_json(self) -> dict[str, Any]:
        return {
            "metadata": self.metadata,
            "windows": [w.flat() for w in self.windows],
            "aggregates": self.aggregates,
            "verdicts": {
                name: verdict.as_dict() for name, verdict in self.verdicts.items()
            },
            "overall_decision": self.overall_decision,
            "artifacts": dict(self.artifacts),
        }


def _model_input(panel: Panel, rows: np.ndarray) -> ModelInput:
    tabular = panel.tabular()[rows]
    keys = panel.frame.loc[
        rows, [panel.columns.symbol, panel.columns.event_date]
    ].reset_index(drop=True)
    sequence = None if panel.sequence is None else panel.sequence.select(rows)
    return ModelInput(tabular=tabular, keys=keys, sequence=sequence)


def _empty_metrics(top_k: int, n_layers: int) -> ScoreMetrics:
    from .metrics import LayeredResult

    return ScoreMetrics(
        n_rows=0,
        n_dates=0,
        positive_rate=None,
        pooled_ic=None,
        pooled_rank_ic=None,
        ic_mean=None,
        icir=None,
        ic_t_stat=None,
        ic_positive_ratio=None,
        n_ic_days=0,
        roc_auc=None,
        pr_auc=None,
        precision_at_topk=None,
        hit_rate_topk=None,
        mean_return_topk=None,
        topk=top_k,
        layers=LayeredResult(
            layer_returns=(), long_short=None, monotonicity=None, n_layers=n_layers
        ),
        brier=None,
        ece=None,
        max_drawdown=None,
    )


def _empty_portfolio() -> PortfolioResult:
    return PortfolioResult(
        n_dates=0,
        n_selected=0,
        n_candidates=0,
        n_eligible=0,
        excluded_not_buyable=0,
        excluded_not_sellable=0,
        dates_without_eligible=0,
        coverage_ratio=None,
        executability_source="unknown",
        gross_mean=None,
        net_mean=None,
        hit_rate=None,
        cum_net_return=None,
        max_drawdown=None,
        turnover=None,
        next_day_premium_mean=None,
    )


def _score(
    panel: Panel,
    rows: np.ndarray,
    scores: np.ndarray,
    config: ValidationConfig,
) -> tuple[dict[str, Any], dict[str, Any]]:
    columns = panel.columns
    metrics = evaluate_scores(
        scores=scores,
        labels=panel.labels()[rows],
        returns=panel.gross_returns()[rows],
        dates=panel.frame.loc[rows, columns.event_date].to_numpy(dtype=str),
        top_k=config.top_k,
        n_layers=config.n_layers,
    )
    portfolio = run_portfolio(panel, rows, scores, config.portfolio)
    return metrics.as_dict(), {
        **portfolio.as_dict(),
        **{"net_mean": portfolio.net_mean},
    }


def run_validation(
    panel: Panel,
    config: ValidationConfig | None = None,
    *,
    output_dir: str | Path | None = None,
) -> RunResult:
    """Execute the full comparison and (optionally) persist the report."""

    cfg = config or ValidationConfig()
    np.random.seed(cfg.seed)  # any stochastic model must inherit the recorded seed
    panel = validate_panel(
        panel, label_horizon=cfg.label_horizon, require_sequence=cfg.require_sequence
    )
    columns: ColumnNames = panel.columns
    dates = sorted(panel.frame[columns.event_date].astype(str).unique().tolist())
    folds = build_walk_forward_folds(
        dates, cfg.walk_forward, label_horizon=cfg.label_horizon
    )

    specs = cfg.model_specs()
    baseline_specs = [s for s in specs if s.role == ROLE_BASELINE]
    if len(baseline_specs) != 1:
        raise ValueError(
            f"exactly one baseline model is required, got {len(baseline_specs)}"
        )
    if not [s for s in specs if s.role == ROLE_CANDIDATE]:
        raise ValueError(
            "at least one candidate model is required for an increment comparison"
        )
    candidate_specs = [s for s in specs if s.role == ROLE_CANDIDATE]
    if not candidate_specs:
        raise ValueError(
            "at least one candidate model is required for an increment comparison"
        )

    windows: list[WindowRecord] = []
    out_dir = Path(output_dir) if output_dir else None
    artifacts: dict[str, str] = {}

    for fold in folds:
        rows = split_panel_rows(panel, fold, cfg.walk_forward)
        if rows.train_index.size == 0 or rows.test_index.size == 0:
            windows.append(
                WindowRecord(
                    fold_index=fold.fold_index,
                    model=baseline_specs[0].name,
                    status="failed",
                    reason="empty train or test rows after embargo/purge",
                    rows={
                        "train": int(rows.train_index.size),
                        "valid": int(rows.valid_index.size),
                        "test": int(rows.test_index.size),
                    },
                    metrics=_empty_metrics(cfg.top_k, cfg.n_layers).as_dict(),
                    portfolio=_empty_portfolio().as_dict(),
                    cost={},
                    leakage=leakage_probe(panel, rows),
                )
            )
            continue

        train_input = _model_input(panel, rows.train_index)
        test_input = _model_input(panel, rows.test_index)
        train_y = panel.labels()[rows.train_index]
        fitted: dict[str, Any] = {}
        leakage = leakage_probe(panel, rows)

        for spec in specs:  # baseline first so blends can bind to it
            status, reason = "ok", ""
            scores = np.full(test_input.n_rows, np.nan)
            cost: dict[str, Any] = {}
            model: Any = None
            try:
                # construction is inside the guard: a bad / unloadable model spec
                # must degrade this candidate, not abort the whole comparison
                model = _build(spec)
                if isinstance(model, RankBlendModel):
                    base = fitted.get(baseline_specs[0].name)
                    cand = (
                        fitted.get(candidate_specs[0].name) if candidate_specs else None
                    )
                    if base is None or cand is None:
                        raise ModelArtifactError(
                            "blend requires an already fitted baseline and candidate"
                        )
                    model.bind(base, cand)
                model.fit(train_input, train_y)
                scores = np.asarray(model.predict(test_input), dtype=np.float64)
                if not np.isfinite(scores).all():
                    raise ModelArtifactError("model emitted non-finite scores")
                cost = model.describe()
                if cfg.save_artifacts and out_dir is not None:
                    path = out_dir / "models" / f"fold{fold.fold_index}_{spec.name}.npz"
                    artifacts[f"fold{fold.fold_index}:{spec.name}"] = str(
                        save_model(model, path)
                    )
            except (OptionalDependencyError, ModelArtifactError) as exc:
                status = (
                    "skipped" if isinstance(exc, OptionalDependencyError) else "failed"
                )
                reason = f"{type(exc).__name__}: {exc}"
                cost = {"name": spec.name, "param_count": 0, "train_seconds": 0.0}
            except Exception as exc:  # noqa: BLE001 - never let one model kill the run
                status = "failed"
                reason = f"{type(exc).__name__}: {exc}"
                cost = {"name": spec.name, "param_count": 0, "train_seconds": 0.0}
                model = None
            if model is not None:
                fitted[spec.name] = model

            if status == "ok":
                metrics_dict, portfolio_dict = _score(
                    panel, rows.test_index, scores, cfg
                )
            else:
                metrics_dict = _empty_metrics(cfg.top_k, cfg.n_layers).as_dict()
                portfolio_dict = _empty_portfolio().as_dict()
            windows.append(
                WindowRecord(
                    fold_index=fold.fold_index,
                    model=spec.name,
                    status=status,
                    reason=reason,
                    rows={
                        "train": int(rows.train_index.size),
                        "valid": int(rows.valid_index.size),
                        "test": int(rows.test_index.size),
                        "purged": int(rows.purged_train_rows),
                        "withheld_embargo": int(rows.withheld_embargo_rows),
                    },
                    metrics=metrics_dict,
                    portfolio=portfolio_dict,
                    cost=cost,
                    leakage=leakage,
                )
            )

    baseline_records = [w for w in windows if w.model == baseline_specs[0].name]
    if not any(w.status == "ok" for w in baseline_records):
        raise ModelArtifactError(
            f"baseline {baseline_specs[0].name!r} produced no scored window, the increment "
            f"comparison is meaningless: {sorted({w.reason for w in baseline_records if w.reason})[:3]}"
        )

    aggregates = aggregate_windows(windows, specs, cfg)
    verdicts = verdicts_from_aggregates(windows, specs, cfg)
    metadata = {
        "run_purpose": "issue #36 序列/多模态模型增量验证",
        "config": cfg.as_dict(),
        "config_hash": cfg.config_hash(),
        "seed": cfg.seed,
        "data_fingerprint": data_fingerprint(panel),
        "data_version": cfg.data_version,
        "feature_version": cfg.feature_version,
        "n_folds": len(folds),
        "n_windows": len(windows),
        "calendar_dates": {"first": dates[0], "last": dates[-1], "count": len(dates)},
        "executability_source": aggregates.get(baseline_specs[0].name, {}).get(
            "executability_source", "assumed"
        ),
        "overall_decision": "reject",
    }
    result = RunResult(
        metadata=metadata,
        windows=windows,
        aggregates=aggregates,
        verdicts=verdicts,
        artifacts=artifacts,
    )
    metadata["overall_decision"] = result.overall_decision

    if out_dir is not None:
        artifacts.update(write_outputs(result, out_dir))
    return result


def _numeric_series(records: Sequence[WindowRecord], path: str) -> list[float]:
    from .gate import window_value

    values = [window_value(r.flat(), path) for r in records]
    return [float(v) for v in values if v is not None]


def aggregate_windows(
    windows: Sequence[WindowRecord],
    specs: Sequence[ModelSpec],
    cfg: ValidationConfig,
) -> dict[str, dict[str, Any]]:
    """Per-model cross-window summary that the report and gate both read."""

    out: dict[str, dict[str, Any]] = {}
    paths = (
        "metrics.pooled_ic",
        "metrics.pooled_rank_ic",
        "metrics.ic_mean",
        "metrics.icir",
        "metrics.ic_t_stat",
        "metrics.roc_auc",
        "metrics.pr_auc",
        "metrics.precision_at_topk",
        "metrics.hit_rate_topk",
        "metrics.mean_return_topk",
        "metrics.brier",
        "metrics.ece",
        "portfolio.gross_mean",
        "portfolio.net_mean",
        "portfolio.cum_net_return",
        "portfolio.max_drawdown",
        "portfolio.turnover",
        "portfolio.next_day_premium_mean",
        "portfolio.coverage_ratio",
    )
    for spec in specs:
        records = [w for w in windows if w.model == spec.name]
        scored = [w for w in records if w.status == "ok"]
        summary = {
            "model": spec.name,
            "role": spec.role,
            "factory": spec.factory,
            "card": spec.card,
            "n_windows": len(records),
            "n_scored_windows": len(scored),
            "statuses": sorted({w.status for w in records}),
            "reasons": sorted({w.reason for w in records if w.reason}),
            "executability_source": (
                scored[0].portfolio.get("executability_source") if scored else "unknown"
            ),
            "mean": {
                path: (
                    round(mean(vals), 8)
                    if (vals := _numeric_series(scored, path))
                    else None
                )
                for path in paths
            },
            "min": {
                path: (
                    round(min(vals), 8)
                    if (vals := _numeric_series(scored, path))
                    else None
                )
                for path in paths
            },
            "cost": _cost_summary(records),
            "cost_sensitivity": _cost_sensitivity(scored),
        }
        out[spec.name] = summary
    return out


def _cost_summary(records: Sequence[WindowRecord]) -> dict[str, Any]:
    params = [float(w.cost.get("param_count", 0) or 0) for w in records]
    train = [float(w.cost.get("train_seconds", 0.0) or 0.0) for w in records]
    latency = [
        float(w.cost["predict_ms_per_sample"])
        for w in records
        if w.cost.get("predict_ms_per_sample") is not None
    ]
    rss = [
        float(w.cost["peak_rss_mb"])
        for w in records
        if w.cost.get("peak_rss_mb") is not None
    ]
    return {
        "param_count_max": max(params) if params else 0,
        "train_seconds_mean": round(mean(train), 6) if train else 0.0,
        "train_seconds_max": round(max(train), 6) if train else 0.0,
        "predict_ms_per_sample_mean": round(mean(latency), 6) if latency else None,
        "predict_ms_per_sample_max": round(max(latency), 6) if latency else None,
        "peak_rss_mb_max": round(max(rss), 2) if rss else None,
        "devices": sorted({str(w.cost.get("device", "cpu")) for w in records}),
    }


def _cost_sensitivity(scored: Sequence[WindowRecord]) -> dict[str, float | None]:
    buckets: dict[str, list[float]] = {}
    for record in scored:
        for key, value in (record.portfolio.get("cost_sensitivity") or {}).items():
            if value is None:
                continue
            buckets.setdefault(str(key), []).append(float(value))
    return {
        key: round(mean(vals), 8)
        for key, vals in sorted(buckets.items(), key=lambda kv: float(kv[0]))
    }


def verdicts_from_aggregates(
    windows: Sequence[WindowRecord],
    specs: Sequence[ModelSpec],
    cfg: ValidationConfig,
) -> dict[str, GateVerdict]:
    """Run the gate for each candidate against the single baseline."""

    baseline = next(s for s in specs if s.role == ROLE_BASELINE)
    base_windows = [w.flat() for w in windows if w.model == baseline.name]
    base_cost = aggregate_windows(windows, [baseline], cfg)[baseline.name]["cost"]
    verdicts: dict[str, GateVerdict] = {}
    for spec in specs:
        if spec.role != ROLE_CANDIDATE:
            continue
        records = [w for w in windows if w.model == spec.name]
        agg = aggregate_windows(windows, [spec], cfg)[spec.name]
        cost = agg["cost"]
        extra = {
            "executability_source": agg["executability_source"],
            "coverage_ratio": agg["mean"].get("portfolio.coverage_ratio"),
            "predict_ms_per_sample": cost.get("predict_ms_per_sample_max"),
            "train_seconds": cost.get("train_seconds_max"),
            "param_count": cost.get("param_count_max"),
            "baseline_param_count": base_cost.get("param_count_max"),
        }
        verdicts[spec.name] = evaluate_gate(
            spec.name,
            [w.flat() for w in records],
            base_windows,
            cfg.gate,
            extra=extra,
        )
    return verdicts


def model_cards(result: RunResult) -> list[dict[str, Any]]:
    """Model card per issue #36: 适用场景 / 失效场景 / 数据依赖 / 容量 / 成本."""

    cards: list[dict[str, Any]] = []
    for name, agg in result.aggregates.items():
        card = agg.get("card") or {}
        verdict = result.verdicts.get(name)
        cards.append(
            {
                "model": name,
                "role": agg["role"],
                "kind": card.get("kind", agg["factory"]),
                "applicable_zh": card.get("applicable_zh", "未在模型卡中声明"),
                "failure_zh": card.get("failure_zh", "未在模型卡中声明"),
                "data_dependency_zh": card.get(
                    "data_dependency_zh",
                    "#23 可成交标签 + #26 可成交性标记 + #4/#7-#10 因子表",
                ),
                "capacity": {
                    "param_count": agg["cost"]["param_count_max"],
                    "n_features": agg["mean"].get("n_features"),
                    "train_seconds_mean": agg["cost"]["train_seconds_mean"],
                    "predict_ms_per_sample_mean": agg["cost"][
                        "predict_ms_per_sample_mean"
                    ],
                    "peak_rss_mb_max": agg["cost"]["peak_rss_mb_max"],
                    "device": ",".join(agg["cost"]["devices"]),
                },
                "decision": None if verdict is None else verdict.decision,
                "decision_zh": None if verdict is None else verdict.conclusion_zh(),
                "blockers": [] if verdict is None else list(verdict.blockers),
                "windows": {
                    "total": agg["n_windows"],
                    "scored": agg["n_scored_windows"],
                },
                "seed": result.metadata.get("seed"),
                "config_hash": result.metadata.get("config_hash"),
            }
        )
    return cards


__all__ = [
    "ModelSpec",
    "ROLE_BASELINE",
    "ROLE_CANDIDATE",
    "RunResult",
    "ValidationConfig",
    "WindowRecord",
    "aggregate_windows",
    "data_fingerprint",
    "default_models",
    "model_cards",
    "render_markdown_report",
    "run_validation",
]
