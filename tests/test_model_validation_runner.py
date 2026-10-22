"""End-to-end increment validation runs: keep / reject, reports, robustness (#36)."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from strategy.model_validation.contracts import ContractError, Panel, validate_panel
from strategy.model_validation.fixtures import SyntheticConfig, make_synthetic_panel
from strategy.model_validation.gate import GateConfig
from strategy.model_validation.models import ModelArtifactError, load_model
from strategy.model_validation.runner import (
    ROLE_BASELINE,
    ROLE_CANDIDATE,
    ModelSpec,
    ValidationConfig,
    aggregate_windows,
    data_fingerprint,
    default_models,
    model_cards,
    run_validation,
)
from strategy.model_validation.splitting import InsufficientDataError, WalkForwardConfig


def _panel(scenario: str, **kwargs: object) -> Panel:
    return make_synthetic_panel(
        SyntheticConfig(
            scenario=scenario,  # type: ignore[arg-type]
            n_dates=int(kwargs.get("n_dates", 46)),
            n_symbols=8,
            seq_len=5,
            label_horizon=2,
            seed=11,
            untradable_ratio=0.12,
        )
    )


def _config(**overrides: object) -> ValidationConfig:
    params: dict[str, object] = {
        "seed": 7,
        "label_horizon": 2,
        "top_k": 3,
        "walk_forward": WalkForwardConfig(
            test_size=5, train_size=14, embargo=1, min_train_dates=4
        ),
        "gate": GateConfig(min_windows=3),
        "models": default_models(),
    }
    params.update(overrides)
    return ValidationConfig(**params)  # type: ignore[arg-type]


def test_noise_only_sequence_candidate_is_marked_不保留(tmp_path: Path) -> None:
    result = run_validation(
        _panel("pure_noise_sequence"), _config(), output_dir=tmp_path
    )

    assert result.metadata["n_folds"] >= 3
    assert result.overall_decision == "reject"
    assert result.kept_candidates() == []
    assert set(result.rejected_candidates()) == {
        "sequence_candidate",
        "blend_candidate",
    }

    decision = json.loads((tmp_path / "decision.json").read_text(encoding="utf-8"))
    assert decision["overall_decision"] == "reject"
    assert decision["overall_decision_zh"] == "不保留"
    for name in decision["rejected_candidates"]:
        assert "不保留" in decision["verdicts"][name]["decision_zh"]
        assert decision["verdicts"][name]["checks"], (
            "the report must show which checks were run"
        )

    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "tabular_baseline" in report and "sequence_candidate" in report
    assert "不保留" in report
    assert "泄漏与切分自检" in report


def test_sequence_candidate_with_real_increment_is_kept(tmp_path: Path) -> None:
    result = run_validation(
        _panel("informative_sequence"), _config(), output_dir=tmp_path
    )

    assert result.overall_decision == "keep"
    assert "sequence_candidate" in result.kept_candidates()
    verdict = result.verdicts["sequence_candidate"]
    assert verdict.checks and all(check.passed for check in verdict.checks)

    baseline = result.aggregates["tabular_baseline"]["mean"]
    candidate = result.aggregates["sequence_candidate"]["mean"]
    assert candidate["metrics.pooled_rank_ic"] > baseline["metrics.pooled_rank_ic"]
    assert candidate["portfolio.net_mean"] > baseline["portfolio.net_mean"]
    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "保留候选模型" in report


def test_identical_inputs_produce_identical_reports(tmp_path: Path) -> None:
    panel = _panel("pure_noise_sequence")
    first = run_validation(panel, _config(), output_dir=tmp_path / "a")
    second = run_validation(panel, _config(), output_dir=tmp_path / "b")

    def stable(result) -> str:
        # wall-clock timings are the only non-deterministic part of a run
        payload = []
        for record in result.to_json()["windows"]:
            item = {k: v for k, v in record.items() if k != "cost"}
            item["cost"] = {"param_count": record["cost"].get("param_count")}
            payload.append(item)
        return json.dumps(payload, sort_keys=True, default=str)

    assert stable(first) == stable(second), (
        "runs must be reproducible at the metric level"
    )
    assert first.metadata["config_hash"] == second.metadata["config_hash"]
    assert first.metadata["data_fingerprint"] == second.metadata["data_fingerprint"]

    def decisions(dir_name: str) -> str:
        payload = json.loads(
            (tmp_path / dir_name / "decision.json").read_text(encoding="utf-8")
        )
        # only wall-clock numbers inside check details can differ between runs
        for verdict in payload["verdicts"].values():
            verdict["checks"] = [
                {"name": c["name"], "passed": c["passed"]} for c in verdict["checks"]
            ]
        return json.dumps(payload, sort_keys=True)

    assert decisions("a") == decisions("b"), (
        "the keep/reject outcome must be reproducible"
    )


def test_config_hash_changes_when_the_gate_gets_looser() -> None:
    strict = _config(gate=GateConfig(min_windows=3))
    loose = _config(gate=GateConfig(min_windows=3, max_drawdown_worsening=0.5))
    assert strict.config_hash() != loose.config_hash()


def test_assumed_executability_never_produces_a_keep(tmp_path: Path) -> None:
    panel = _panel("informative_sequence")
    frame = panel.frame.drop(columns=["can_buy", "can_sell", "executability_reason"])
    stripped = Panel(
        frame=frame,
        features=panel.features,
        columns=panel.columns,
        sequence=panel.sequence,
    )
    result = run_validation(stripped, _config(), output_dir=tmp_path)

    assert result.overall_decision == "reject"
    assert result.metadata["executability_source"] == "assumed"
    for verdict in result.verdicts.values():
        assert any(
            "executability_from_upstream" in blocker for blocker in verdict.blockers
        )


def test_untradable_rows_cannot_be_selected_even_when_the_model_loves_them() -> None:
    panel = validate_panel(_panel("informative_sequence"), label_horizon=2)
    result = run_validation(panel, _config(), output_dir=None)
    baseline = [
        w for w in result.windows if w.model == "tabular_baseline" and w.status == "ok"
    ]
    assert all(w.portfolio["excluded_not_buyable"] > 0 for w in baseline)
    assert all(w.portfolio["n_selected"] <= w.portfolio["n_eligible"] for w in baseline)


def test_missing_optional_dependency_degrades_to_skipped_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "torch", None)  # force the "not installed" branch
    models = (
        ModelSpec(
            "tabular_baseline",
            ROLE_BASELINE,
            "RidgeTabularModel",
            {"name": "tabular_baseline"},
        ),
        ModelSpec(
            "gru_candidate", ROLE_CANDIDATE, "TorchSequenceAdapter", {"name": "gru"}
        ),
    )
    result = run_validation(
        _panel("informative_sequence"), _config(models=models), output_dir=tmp_path
    )

    gru = [w for w in result.windows if w.model == "gru_candidate"]
    assert gru and all(w.status == "skipped" for w in gru)
    assert "OptionalDependencyError" in gru[0].reason
    assert result.overall_decision == "reject"
    assert any(
        "all_windows_scored" in b for b in result.verdicts["gru_candidate"].blockers
    )
    assert result.aggregates["gru_candidate"]["n_scored_windows"] == 0


def test_broken_model_spec_is_recorded_instead_of_crashing_the_run() -> None:
    models = (
        ModelSpec(
            "tabular_baseline",
            ROLE_BASELINE,
            "RidgeTabularModel",
            {"name": "tabular_baseline"},
        ),
        ModelSpec(
            "bad_candidate",
            ROLE_CANDIDATE,
            "SequenceTemporalModel",
            {"length_policy": "bogus"},
        ),
    )
    result = run_validation(_panel("pure_noise_sequence"), _config(models=models))

    bad = [w for w in result.windows if w.model == "bad_candidate"]
    assert bad and all(w.status == "failed" for w in bad)
    assert "ValueError" in bad[0].reason
    assert result.verdicts["bad_candidate"].decision == "reject"


def test_an_unusable_baseline_aborts_the_comparison_instead_of_faking_a_win() -> None:
    models = (
        ModelSpec("nope", ROLE_BASELINE, "DoesNotExist", {}),
        ModelSpec("sequence_candidate", ROLE_CANDIDATE, "SequenceTemporalModel", {}),
    )
    with pytest.raises(ModelArtifactError, match="no scored window"):
        run_validation(_panel("pure_noise_sequence"), _config(models=models))


def test_require_sequence_rejects_a_tabular_only_panel() -> None:
    panel = _panel("pure_noise_sequence")
    frame = panel.frame
    tabular_only = Panel(
        frame=frame, features=panel.features, columns=panel.columns, sequence=None
    )
    with pytest.raises(ContractError, match="requires a sequence bundle"):
        run_validation(tabular_only, _config(require_sequence=True))


def test_short_history_raises_instead_of_a_thin_report() -> None:
    panel = make_synthetic_panel(
        SyntheticConfig(n_dates=8, n_symbols=4, seq_len=3, label_horizon=2)
    )
    with pytest.raises(InsufficientDataError):
        run_validation(panel, _config())


def test_run_needs_one_baseline_and_at_least_one_candidate() -> None:
    only_baseline = (
        ModelSpec("tabular_baseline", ROLE_BASELINE, "RidgeTabularModel", {}),
    )
    with pytest.raises(ValueError, match="candidate"):
        run_validation(_panel("pure_noise_sequence"), _config(models=only_baseline))
    two_baselines = (
        ModelSpec("a", ROLE_BASELINE, "RidgeTabularModel", {}),
        ModelSpec("b", ROLE_BASELINE, "RidgeTabularModel", {}),
        ModelSpec("c", ROLE_CANDIDATE, "SequenceTemporalModel", {}),
    )
    with pytest.raises(ValueError, match="baseline"):
        run_validation(_panel("pure_noise_sequence"), _config(models=two_baselines))


def test_no_skill_constant_candidate_is_rejected() -> None:
    models = (
        ModelSpec(
            "tabular_baseline",
            ROLE_BASELINE,
            "RidgeTabularModel",
            {"name": "tabular_baseline"},
        ),
        ModelSpec(
            "constant_candidate",
            ROLE_CANDIDATE,
            "ConstantScoreModel",
            {"name": "constant"},
        ),
    )
    result = run_validation(_panel("informative_sequence"), _config(models=models))

    assert result.verdicts["constant_candidate"].decision == "reject"
    baseline_mean = result.aggregates["tabular_baseline"]["mean"]
    const_mean = result.aggregates["constant_candidate"]["mean"]
    assert const_mean["metrics.pooled_ic"] is None or (
        const_mean["metrics.pooled_ic"] or 0.0
    ) < (baseline_mean["metrics.pooled_ic"] or 0.0)


def test_reports_include_cost_latency_and_model_cards(tmp_path: Path) -> None:
    result = run_validation(
        _panel("informative_sequence"), _config(), output_dir=tmp_path
    )

    cards = json.loads((tmp_path / "model_cards.json").read_text(encoding="utf-8"))
    assert len(cards) == len(result.aggregates)
    for card in cards:
        assert card["applicable_zh"] and card["failure_zh"]
        assert card["data_dependency_zh"]
        assert {
            "param_count",
            "predict_ms_per_sample_mean",
            "peak_rss_mb_max",
            "device",
        } <= set(card["capacity"])

    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "换手 / 成本敏感性" in report
    assert "成本 / 延迟 / 容量" in report
    assert "推理 ms/样本" in report

    windows_csv = pd.read_csv(tmp_path / "windows.csv")
    assert {
        "fold_index",
        "model",
        "status",
        "metrics.ic_mean",
        "portfolio.net_mean",
    } <= set(windows_csv.columns)
    assert windows_csv["fold_index"].nunique() >= 3


def test_saved_artifacts_reload_and_reproduce_scores(tmp_path: Path) -> None:
    models = (
        ModelSpec(
            "tabular_baseline",
            ROLE_BASELINE,
            "RidgeTabularModel",
            {"name": "tabular_baseline"},
        ),
        ModelSpec(
            "sequence_candidate",
            ROLE_CANDIDATE,
            "SequenceTemporalModel",
            {"name": "sequence_candidate"},
        ),
    )
    run_validation(
        _panel("informative_sequence"),
        _config(models=models, save_artifacts=True),
        output_dir=tmp_path,
    )

    saved = sorted((tmp_path / "models").glob("*.npz"))
    assert saved, "fitted models must be persisted for reproducibility"
    restored = load_model(saved[0])
    assert restored.describe()["name"] in {m.name for m in models}
    assert list((tmp_path / "models").glob("*.npz.json")), (
        "every artifact needs a readable header"
    )


def test_window_records_are_json_serialisable(tmp_path: Path) -> None:
    result = run_validation(
        _panel("pure_noise_sequence"), _config(), output_dir=tmp_path
    )
    payload = json.dumps(result.to_json(), ensure_ascii=False, default=str)
    assert "sequence_candidate" in payload
    bundle = json.loads((tmp_path / "metrics.json").read_text(encoding="utf-8"))
    assert bundle["metadata"]["config"]["gate"]["min_windows"] == 3
    assert bundle["overall_decision"] in {"keep", "reject"}


def test_aggregations_summarise_windows_and_leakage() -> None:
    result = run_validation(_panel("pure_noise_sequence"), _config())
    agg = aggregate_windows(result.windows, default_models(), _config())
    assert set(agg) == {"tabular_baseline", "sequence_candidate", "blend_candidate"}
    assert agg["tabular_baseline"]["n_scored_windows"] >= 3
    assert agg["tabular_baseline"]["cost"]["param_count_max"] > 0
    assert all(
        w.leakage["train_rows_with_label_window_in_test"] == 0 for w in result.windows
    )


def test_data_fingerprint_tracks_the_sample_universe() -> None:
    panel = validate_panel(_panel("pure_noise_sequence"), label_horizon=2)
    first = data_fingerprint(panel)
    assert first["n_rows"] == len(panel)
    trimmed = validate_panel(
        Panel(
            frame=panel.frame.iloc[:-5].copy(),
            features=panel.features,
            columns=panel.columns,
            sequence=None,
        ),
        label_horizon=2,
    )
    assert data_fingerprint(trimmed)["row_key_sha256"] != first["row_key_sha256"]


def test_model_cards_reference_the_run_provenance() -> None:
    result = run_validation(_panel("pure_noise_sequence"), _config())
    cards = {card["model"]: card for card in model_cards(result)}
    assert cards["tabular_baseline"]["decision"] is None, (
        "the baseline is a reference, not a candidate"
    )
    assert cards["sequence_candidate"]["decision"] == "reject"
    assert cards["sequence_candidate"]["config_hash"] == result.metadata["config_hash"]
    assert np.isfinite(
        float(cards["sequence_candidate"]["capacity"]["train_seconds_mean"])
    )
