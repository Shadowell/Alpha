"""Model behaviour: determinism, missing values, sequence length, artifacts (#36)."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from strategy.model_validation.contracts import (
    ContractError,
    ModelInput,
    SequenceBundle,
)
from strategy.model_validation.models import (
    ARTIFACT_VERSION,
    ConstantScoreModel,
    KronosImageAdapter,
    ModelArtifactError,
    OptionalDependencyError,
    RankBlendModel,
    RidgeTabularModel,
    SequenceLengthError,
    SequenceTemporalModel,
    TorchSequenceAdapter,
    build_model,
    clean_matrix,
    load_model,
    save_model,
    temporal_features,
)


def _keys(n: int) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": [f"{600000 + i:06d}" for i in range(n)],
            "event_date": ["2026-01-05"] * n,
        }
    )


def _tab_input(x: np.ndarray) -> ModelInput:
    return ModelInput(tabular=np.asarray(x, dtype=float), keys=_keys(len(x)))


def _seq_input(values: np.ndarray) -> ModelInput:
    values = np.asarray(values, dtype=float)
    n = values.shape[0]
    return ModelInput(
        tabular=np.zeros((n, 0)),
        keys=_keys(n),
        sequence=SequenceBundle(values=values, keys=_keys(n), feature_names=("a", "b")),
    )


TRAIN_X = np.array([[0.0], [1.0], [2.0], [3.0], [4.0], [5.0]])
TRAIN_Y = 2.5 * TRAIN_X[:, 0] + 1.0


def test_ridge_recovers_a_linear_relation_and_is_deterministic() -> None:
    first = RidgeTabularModel(alpha=1e-6)
    first.fit(_tab_input(TRAIN_X), TRAIN_Y)
    pred_a = first.predict(_tab_input(np.array([[6.0], [-1.0]])))
    second = RidgeTabularModel(alpha=1e-6)
    second.fit(_tab_input(TRAIN_X), TRAIN_Y)
    pred_b = second.predict(_tab_input(np.array([[6.0], [-1.0]])))

    assert np.allclose(pred_a, pred_b, atol=1e-12)
    assert pred_a[0] == pytest.approx(16.0, abs=0.2)
    assert first.describe()["param_count"] == 2  # slope + intercept
    assert first.cost.train_seconds >= 0.0


def test_missing_values_are_imputed_with_training_statistics_not_zero() -> None:
    x = TRAIN_X.copy().astype(float)
    x[1, 0] = np.nan
    model = RidgeTabularModel(alpha=1e-6)
    model.fit(_tab_input(x), TRAIN_Y)
    out = model.predict(_tab_input(np.array([[np.nan], [np.inf], [6.0]])))

    assert np.isfinite(out).all()
    assert out[0] == pytest.approx(out[1])  # NaN and Inf collapse to the same fill
    assert out[2] > out[0]


def test_clean_matrix_uses_column_means_and_survives_empty_input() -> None:
    filled = clean_matrix(np.array([[1.0, np.nan], [np.nan, 3.0]]))
    assert np.isfinite(filled).all()
    empty = clean_matrix(np.zeros((4, 0)))
    assert empty.shape == (4, 0)


def test_feature_count_mismatch_is_a_contract_error() -> None:
    model = RidgeTabularModel()
    model.fit(_tab_input(TRAIN_X), TRAIN_Y)
    with pytest.raises(ContractError, match="feature count mismatch"):
        model.predict(_tab_input(np.array([[1.0, 2.0], [3.0, 4.0]])))


def test_predict_before_fit_is_refused() -> None:
    with pytest.raises(ModelArtifactError, match="not fitted"):
        RidgeTabularModel().predict(_tab_input(TRAIN_X))


def test_constant_model_carries_no_information() -> None:
    model = ConstantScoreModel()
    model.fit(_tab_input(TRAIN_X), TRAIN_Y)
    out = model.predict(_tab_input(np.array([[0.0], [9.0]])))
    assert out[0] == pytest.approx(out[1])
    assert out[0] == pytest.approx(float(np.mean(TRAIN_Y)))


def test_sequence_model_is_order_sensitive() -> None:
    rng = np.random.default_rng(3)
    values = rng.normal(size=(10, 6, 2))
    y = np.array([1.0, 0.0] * 5)
    model = SequenceTemporalModel(alpha=1e-3)
    model.fit(_seq_input(values), y)
    forward = model.predict(_seq_input(values))
    backward = model.predict(_seq_input(values[:, ::-1, :]))

    assert not np.allclose(forward, backward), (
        "a sequence model must not be permutation invariant"
    )
    head = temporal_features(SequenceBundle(values=values, keys=_keys(10)))
    tail = temporal_features(SequenceBundle(values=values[:, ::-1, :], keys=_keys(10)))
    assert head.shape == (10, 5 * 2 + 1)
    assert not np.allclose(head, tail)


def test_sequence_length_policies() -> None:
    values = np.random.default_rng(0).normal(size=(3, 6, 2))
    model = SequenceTemporalModel(seq_len=4, length_policy="error")
    with pytest.raises(SequenceLengthError):
        model.fit(_seq_input(values), np.array([1.0, 0.0, 1.0]))

    truncate = SequenceTemporalModel(seq_len=4, length_policy="truncate")
    truncate.fit(_seq_input(values), np.array([1.0, 0.0, 1.0]))
    short = truncate.predict(_seq_input(values[:, :3, :]))
    assert short.shape == (3,)

    padded = SequenceTemporalModel(seq_len=8, length_policy="pad")
    padded.fit(_seq_input(values), np.array([1.0, 0.0, 1.0]))
    assert padded.predict(_seq_input(values)).shape == (3,)
    assert padded.seq_len == 8


def test_sequence_model_without_a_bundle_fails_loudly() -> None:
    model = SequenceTemporalModel()
    with pytest.raises(ContractError, match="requires inputs.sequence"):
        model.fit(_tab_input(TRAIN_X), TRAIN_Y)


def test_rank_blend_requires_bound_models_and_averages_ranks() -> None:
    base = RidgeTabularModel(name="base", alpha=1e-6)
    base.fit(_tab_input(TRAIN_X), TRAIN_Y)
    cand = ConstantScoreModel(name="const")
    cand.fit(_tab_input(TRAIN_X), TRAIN_Y)

    blend = RankBlendModel(weight=0.5)
    with pytest.raises(ModelArtifactError, match="no bound models"):
        blend.predict(_tab_input(TRAIN_X))
    with pytest.raises(ModelArtifactError, match="bind"):
        RankBlendModel().fit(_tab_input(TRAIN_X), TRAIN_Y)

    blend.bind(base, cand)
    blend.fit(_tab_input(TRAIN_X), TRAIN_Y)
    out = blend.predict(_tab_input(TRAIN_X))
    assert np.isfinite(out).all()
    assert out[0] < out[-1]  # inherits the baseline's ordering
    assert blend.state_dict()["weight"] == pytest.approx(0.5)


def test_optional_torch_dependency_is_reported_not_imported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "torch", None)  # `import torch` now raises
    model = TorchSequenceAdapter(name="gru")
    values = np.random.default_rng(1).normal(size=(4, 5, 2))
    with pytest.raises(OptionalDependencyError, match="torch is not installed"):
        model.fit(_seq_input(values), np.array([1.0, 0.0, 1.0, 0.0]))

    image = KronosImageAdapter()
    with pytest.raises(OptionalDependencyError):
        image.predict(_seq_input(values))


def test_artifacts_do_not_require_torch_at_import_time() -> None:
    import strategy.model_validation.models as models

    assert "torch" not in dir(models)  # the module never imports torch eagerly
    assert set(models.MODEL_REGISTRY) >= {"RidgeTabularModel", "SequenceTemporalModel"}


def test_save_and_load_roundtrip_is_prediction_identical(tmp_path: Path) -> None:
    model = RidgeTabularModel(name="baseline", alpha=0.5)
    model.fit(_tab_input(TRAIN_X), TRAIN_Y)
    probe = _tab_input(np.array([[2.0], [7.0]]))
    expected = model.predict(probe)

    path = save_model(model, tmp_path / "model.npz")
    assert path.exists()
    restored = load_model(path)
    assert restored.name == "baseline"
    assert np.allclose(restored.predict(probe), expected, atol=1e-12)

    seq_model = SequenceTemporalModel(name="seq", length_policy="pad")
    values = np.random.default_rng(2).normal(size=(4, 5, 2))
    seq_model.fit(_seq_input(values), np.array([1.0, 0.0, 1.0, 0.0]))
    seq_path = save_model(seq_model, tmp_path / "seq.npz")
    seq_restored = load_model(seq_path)
    assert np.allclose(
        seq_restored.predict(_seq_input(values)), seq_model.predict(_seq_input(values))
    )


def test_model_loading_failures_are_typed(tmp_path: Path) -> None:
    model = RidgeTabularModel(alpha=0.5)
    model.fit(_tab_input(TRAIN_X), TRAIN_Y)
    path = save_model(model, tmp_path / "model.npz")

    with pytest.raises(ModelArtifactError, match="not found"):
        load_model(tmp_path / "missing.npz")

    (tmp_path / "bad.npz.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "bad.npz").write_bytes(b"junk")
    with pytest.raises(ModelArtifactError, match="corrupt model artifact header"):
        load_model(tmp_path / "bad.npz")

    header = Path(str(path) + ".json")
    meta = json.loads(header.read_text(encoding="utf-8"))
    meta["class"] = "SecretTrainedLLM"
    header.write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(ModelArtifactError, match="not in the validation registry"):
        load_model(path)

    meta = json.loads(header.read_text(encoding="utf-8"))
    meta["class"] = "RidgeTabularModel"
    meta["artifact_version"] = ARTIFACT_VERSION + 99
    header.write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(ModelArtifactError, match="unsupported artifact version"):
        load_model(path)

    path.write_bytes(b"not an npz archive")  # payload no longer matches the header
    meta = json.loads(header.read_text(encoding="utf-8"))
    meta["artifact_version"] = ARTIFACT_VERSION
    header.write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(ModelArtifactError, match="corrupt model artifact payload"):
        load_model(path)


def test_build_model_from_spec_rejects_unknown_classes() -> None:
    built = build_model({"class": "RidgeTabularModel", "name": "x", "alpha": 0.25})
    assert isinstance(built, RidgeTabularModel) and built.name == "x"
    with pytest.raises(ModelArtifactError, match="unknown model class"):
        build_model({"class": "Mystery"})
