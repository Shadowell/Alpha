"""Deterministic numpy models + optional deep-model adapters (issue #36).

The scaffold must run in CI on Python 3.11 with CPU only, so the bundled
baseline / candidate are closed-form numpy estimators:

* ``RidgeTabularModel`` - the tabular baseline *proxy* (same role LightGBM plays
  in production: table of factors -> score).
* ``SequenceTemporalModel`` - a sequence *candidate* that genuinely uses the time
  axis (last value, EMA, slope, dispersion over the window).  Permuting the
  window changes its prediction, which is what makes it a fair stand-in for a
  GRU / Temporal-CNN when validating "does the sequence view add information".
* ``RankBlendModel`` - stacking / blending of two fitted models.
* ``ConstantScoreModel`` - the "no skill" floor; used to prove the gate can say
  "不保留".

Heavy models are never imported at module import time:
``TorchSequenceAdapter`` (GRU / Transformer) and ``KronosImageAdapter`` (K-line
image CNN) import ``torch`` lazily and raise :class:`OptionalDependencyError`,
which the runner records as a *skipped candidate* instead of failing the run.
That keeps ``requirements*.txt`` untouched and keeps torch out of CI.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .contracts import ContractError, InputKind, ModelInput, SequenceBundle

ARTIFACT_VERSION = 1


class OptionalDependencyError(RuntimeError):
    """Raised by adapters whose heavy dependency is not installed."""


class ModelArtifactError(RuntimeError):
    """Raised when a saved model cannot be reloaded (missing / corrupt / unknown)."""


class SequenceLengthError(ModelArtifactError):
    """Raised when a fold's window length disagrees with the fitted model."""


@dataclass(slots=True)
class ModelCost:
    """Report-only cost budget; the gate reads it, nothing here depends on GPU."""

    param_count: int = 0
    train_seconds: float = 0.0
    predict_ms_per_sample: float | None = None
    peak_rss_mb: float | None = None
    device: str = "cpu"

    def as_dict(self) -> dict[str, Any]:
        return {
            "param_count": self.param_count,
            "train_seconds": round(self.train_seconds, 6),
            "predict_ms_per_sample": (
                None
                if self.predict_ms_per_sample is None
                else round(self.predict_ms_per_sample, 6)
            ),
            "peak_rss_mb": None
            if self.peak_rss_mb is None
            else round(float(self.peak_rss_mb), 2),
            "device": self.device,
        }


def peak_rss_mb() -> float | None:
    """Best-effort RSS reading from the stdlib (report-only, never a gate input).

    ``ru_maxrss`` is bytes on macOS and KiB on Linux, so the unit depends on the
    platform, not on the magnitude of the number.
    """

    try:
        import resource
        import sys

        raw = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    except Exception:  # pragma: no cover - platform without the resource module
        return None
    if not np.isfinite(raw) or raw <= 0:
        return None
    divisor = 1024.0 * 1024.0 if sys.platform == "darwin" else 1024.0
    return raw / divisor


def clean_matrix(x: np.ndarray, means: np.ndarray | None = None) -> np.ndarray:
    """Replace NaN/Inf with the training-column means (0 when unseen)."""

    out = np.array(x, dtype=np.float64, copy=True)
    if out.ndim == 1:
        out = out.reshape(-1, 1)
    if out.shape[1] == 0:
        return out
    fill = (
        np.zeros(out.shape[1], dtype=np.float64)
        if means is None
        else np.asarray(means, dtype=np.float64).ravel()
    )
    if fill.size != out.shape[1]:
        fill = (
            np.resize(fill, out.shape[1])
            if fill.size
            else np.zeros(out.shape[1], dtype=np.float64)
        )
    fill = np.where(np.isfinite(fill), fill, 0.0)
    bad = ~np.isfinite(out)
    out[bad] = np.broadcast_to(fill, out.shape)[bad]
    return out


@dataclass(slots=True)
class FeatureSpace:
    """Train-fitted standardisation + missing-value imputation (deterministic)."""

    means: np.ndarray = field(default_factory=lambda: np.zeros(0))
    scales: np.ndarray = field(default_factory=lambda: np.zeros(0))
    n_features: int = 0

    @classmethod
    def fit(cls, x: np.ndarray) -> "FeatureSpace":
        x = np.array(x, dtype=np.float64, copy=True)
        if x.ndim == 1:
            x = x.reshape(-1, 1)
        if x.shape[1] == 0:
            return cls(means=np.zeros(0), scales=np.zeros(0), n_features=0)
        with np.errstate(invalid="ignore"):
            means = np.where(
                np.isfinite(x).any(axis=0),
                np.nanmean(np.where(np.isfinite(x), x, np.nan), axis=0),
                0.0,
            )
        x = clean_matrix(x, means)
        std = x.std(axis=0)
        scales = np.where(std > 1e-12, std, 1.0)
        return cls(means=means, scales=scales, n_features=int(x.shape[1]))

    def transform(self, x: np.ndarray) -> np.ndarray:
        x = np.array(x, dtype=np.float64, copy=True)
        if x.ndim == 1:
            x = x.reshape(-1, 1)
        if x.shape[1] != self.n_features:
            raise ContractError(
                f"feature count mismatch: fitted {self.n_features}, got {x.shape[1]}"
            )
        if self.n_features == 0:
            return np.zeros((x.shape[0], 0), dtype=np.float64)
        z = clean_matrix(x, self.means)
        return (z - self.means) / self.scales

    def state(self) -> dict[str, np.ndarray]:
        return {"means": self.means, "scales": self.scales}


def _solve_ridge(z: np.ndarray, y: np.ndarray, alpha: float) -> np.ndarray:
    n_features = z.shape[1]
    if n_features == 0:
        return np.zeros(1, dtype=np.float64)
    design = np.hstack([z, np.ones((z.shape[0], 1))])
    penalty = np.eye(n_features + 1, dtype=np.float64) * float(alpha)
    penalty[-1, -1] = 0.0  # never regularise the intercept
    rhs = design.T @ y
    try:
        return np.linalg.solve(design.T @ design + penalty, rhs)
    except np.linalg.LinAlgError:  # pragma: no cover - singular fallback
        return np.linalg.lstsq(design.T @ design + penalty, rhs, rcond=None)[0]


def _design(z: np.ndarray) -> np.ndarray:
    return (
        np.hstack([z, np.ones((z.shape[0], 1))])
        if z.shape[1]
        else np.ones((z.shape[0], 1))
    )


def temporal_features(bundle: SequenceBundle, *, decay: float = 0.85) -> np.ndarray:
    """Order-sensitive summary of a ``(n, L, C)`` window.

    Channels: last value, window mean, exponential moving average (recency
    weighted - the part a plain tabular model cannot reproduce), least-squares
    slope over time, dispersion, and max draw-down of the close-like channel.
    """

    values = bundle.values
    n, length, channels = values.shape
    if length == 0:
        return np.zeros((n, 0), dtype=np.float64)
    weights = decay ** np.arange(length - 1, -1, -1, dtype=np.float64)
    weights = weights / weights.sum()
    cleaned = clean_matrix(values.reshape(n * length, channels)).reshape(
        n, length, channels
    )

    last = cleaned[:, -1, :]
    mean = cleaned.mean(axis=1)
    ema = np.tensordot(cleaned, weights, axes=([1], [0]))
    t = np.arange(length, dtype=np.float64)
    if length > 1:
        tc = t - t.mean()
        denom = float((tc * tc).sum())
        slope = np.einsum("t,nlc->nc", tc, cleaned) / (denom + 1e-12)
        disp = cleaned.std(axis=1)
        cum = np.cumprod(1.0 + np.clip(cleaned[:, :, 0], -0.5, 0.5), axis=1)
        dd = (cum / np.maximum.accumulate(cum, axis=1) - 1.0)[:, -1:]
    else:  # pragma: no cover - seq_len==1 edge case
        slope = np.zeros_like(mean)
        disp = np.zeros_like(mean)
        dd = np.zeros((n, 1), dtype=np.float64)
    return np.hstack([last, mean, ema, slope, disp, dd])


class _BaseModel:
    name = "base"
    input_kind: InputKind = "tabular"

    def __init__(self, *, name: str | None = None, alpha: float = 1.0) -> None:
        if name:
            self.name = name
        self.alpha = float(alpha)
        self.space = FeatureSpace()
        self.coef_: np.ndarray | None = None
        self.cost = ModelCost()
        self.fitted = False

    # -- protocol surface -------------------------------------------------
    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "input_kind": self.input_kind,
            "alpha": self.alpha,
            "n_features": self.space.n_features,
            "fitted": self.fitted,
            **self.cost.as_dict(),
        }

    def fit(
        self, inputs: ModelInput, y: np.ndarray
    ) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def predict(self, inputs: ModelInput) -> np.ndarray:  # pragma: no cover - abstract
        raise NotImplementedError

    # -- helpers ----------------------------------------------------------
    def _require_fitted(self) -> None:
        if not self.fitted or self.coef_ is None:
            raise ModelArtifactError(f"model {self.name!r} is not fitted")

    def _fit_targets(self, y: np.ndarray) -> np.ndarray:
        target = np.asarray(y, dtype=np.float64).ravel()
        target = np.where(np.isfinite(target), target, 0.0)
        return target

    def _record_cost(
        self,
        *,
        train_seconds: float | None = None,
        predict_seconds: float | None = None,
        rows: int = 0,
    ) -> None:
        if train_seconds is not None:
            self.cost.train_seconds = float(train_seconds)
        if predict_seconds is not None and rows:
            self.cost.predict_ms_per_sample = 1000.0 * predict_seconds / rows
        self.cost.peak_rss_mb = peak_rss_mb()

    def state_dict(self) -> dict[str, np.ndarray]:
        self._require_fitted()
        state = dict(self.space.state())
        state["coef"] = np.asarray(self.coef_, dtype=np.float64)
        return state

    def load_state(self, state: dict[str, np.ndarray]) -> None:
        self.space = FeatureSpace(
            means=np.asarray(state["means"], dtype=np.float64),
            scales=np.asarray(state["scales"], dtype=np.float64),
            n_features=int(np.asarray(state["means"]).size),
        )
        self.coef_ = np.asarray(state["coef"], dtype=np.float64)
        self.fitted = True

    def hyperparams(self) -> dict[str, Any]:
        return {"alpha": self.alpha}


class RidgeTabularModel(_BaseModel):
    """Closed-form ridge on the tabular factor block (LightGBM stand-in)."""

    input_kind = "tabular"

    def __init__(
        self, *, name: str = "tabular_ridge_baseline", alpha: float = 1.0
    ) -> None:
        super().__init__(name=name, alpha=alpha)

    def fit(self, inputs: ModelInput, y: np.ndarray) -> None:
        started = time.perf_counter()
        self.space = FeatureSpace.fit(inputs.tabular)
        z = self.space.transform(inputs.tabular)
        target = self._fit_targets(y)
        self.coef_ = _solve_ridge(z, target, self.alpha)
        self.cost.param_count = int(self.coef_.size)
        self.fitted = True
        self._record_cost(
            train_seconds=time.perf_counter() - started, rows=inputs.n_rows
        )

    def predict(self, inputs: ModelInput) -> np.ndarray:
        self._require_fitted()
        started = time.perf_counter()
        z = self.space.transform(inputs.tabular)
        out = _design(z) @ self.coef_
        self._record_cost(
            predict_seconds=time.perf_counter() - started, rows=inputs.n_rows
        )
        return np.where(np.isfinite(out), out, 0.0)


class ConstantScoreModel(_BaseModel):
    """No-skill floor: emits the training mean for every row."""

    input_kind = "tabular"

    def __init__(self, *, name: str = "constant_noop", alpha: float = 0.0) -> None:
        super().__init__(name=name, alpha=alpha)

    def fit(self, inputs: ModelInput, y: np.ndarray) -> None:
        started = time.perf_counter()
        self.space = FeatureSpace.fit(inputs.tabular)
        target = self._fit_targets(y)
        value = float(target.mean()) if target.size else 0.0
        self.coef_ = np.asarray([value], dtype=np.float64)
        self.cost.param_count = 1
        self.fitted = True
        self._record_cost(
            train_seconds=time.perf_counter() - started, rows=inputs.n_rows
        )

    def predict(self, inputs: ModelInput) -> np.ndarray:
        self._require_fitted()
        started = time.perf_counter()
        out = np.full(inputs.n_rows, float(self.coef_[0]), dtype=np.float64)
        self._record_cost(
            predict_seconds=time.perf_counter() - started, rows=inputs.n_rows
        )
        return out

    def state_dict(self) -> dict[str, np.ndarray]:
        self._require_fitted()
        return {"const": np.asarray(self.coef_, dtype=np.float64)}

    def load_state(self, state: dict[str, np.ndarray]) -> None:
        self.coef_ = np.asarray(state["const"], dtype=np.float64)
        self.space = FeatureSpace()
        self.fitted = True


class SequenceTemporalModel(_BaseModel):
    """Sequence candidate: order-derived window features + ridge head."""

    input_kind = "sequence"

    def __init__(
        self,
        *,
        name: str = "sequence_temporal_candidate",
        alpha: float = 1.0,
        decay: float = 0.85,
        use_tabular: bool = False,
        seq_len: int | None = None,
        length_policy: str = "error",
    ) -> None:
        super().__init__(name=name, alpha=alpha)
        if length_policy not in {"error", "truncate", "pad"}:
            raise ValueError(f"unknown length_policy: {length_policy!r}")
        self.decay = float(decay)
        self.use_tabular = bool(use_tabular)
        self.seq_len = seq_len
        self.length_policy = length_policy

    def hyperparams(self) -> dict[str, Any]:
        return {
            "alpha": self.alpha,
            "decay": self.decay,
            "use_tabular": self.use_tabular,
            "seq_len": self.seq_len,
            "length_policy": self.length_policy,
        }

    def _window(self, bundle: SequenceBundle) -> np.ndarray:
        if self.seq_len is None:
            self.seq_len = bundle.seq_len
        length = bundle.seq_len
        target = int(self.seq_len)
        values = bundle.values
        if length > target:
            if self.length_policy == "error":
                raise SequenceLengthError(
                    f"{self.name}: fold seq_len={length} > fitted {target}"
                )
            values = values[:, -target:, :]
        elif length < target:
            if self.length_policy == "error":
                raise SequenceLengthError(
                    f"{self.name}: fold seq_len={length} < fitted {target}"
                )
            if self.length_policy == "pad":
                pad = np.zeros(
                    (values.shape[0], target - length, bundle.n_channels),
                    dtype=np.float64,
                )
                values = np.hstack(
                    [pad, values]
                )  # left-pad: the missing bars are *older*
            # "truncate" on a short window keeps the fewer available recent bars
        return temporal_features(
            SequenceBundle(
                values=values, keys=bundle.keys, feature_names=bundle.feature_names
            ),
            decay=self.decay,
        )

    def _matrix(self, inputs: ModelInput) -> np.ndarray:
        if inputs.sequence is None:
            raise ContractError(f"{self.name} requires inputs.sequence")
        block = self._window(inputs.sequence)
        if self.use_tabular and inputs.tabular.shape[1]:
            return np.hstack([block, inputs.tabular])
        return block

    def fit(self, inputs: ModelInput, y: np.ndarray) -> None:
        started = time.perf_counter()
        block = self._matrix(inputs)
        self.space = FeatureSpace.fit(block)
        target = self._fit_targets(y)
        self.coef_ = _solve_ridge(self.space.transform(block), target, self.alpha)
        self.cost.param_count = int(self.coef_.size)
        self.fitted = True
        self._record_cost(
            train_seconds=time.perf_counter() - started, rows=inputs.n_rows
        )

    def predict(self, inputs: ModelInput) -> np.ndarray:
        self._require_fitted()
        started = time.perf_counter()
        block = self._matrix(inputs)
        out = _design(self.space.transform(block)) @ self.coef_
        self._record_cost(
            predict_seconds=time.perf_counter() - started, rows=inputs.n_rows
        )
        return np.where(np.isfinite(out), out, 0.0)


class RankBlendModel(_BaseModel):
    """Blending / stacking proxy: rank-average of an already fitted pair."""

    input_kind = "both"

    def __init__(self, *, name: str = "rank_blend", weight: float = 0.5) -> None:
        super().__init__(name=name, alpha=float(weight))
        if not 0.0 <= weight <= 1.0:
            raise ValueError("weight must be within [0, 1]")
        self.weight = float(weight)
        self.parts: tuple[_BaseModel, _BaseModel] | None = None

    def bind(self, base: _BaseModel, candidate: _BaseModel) -> None:
        self.parts = (base, candidate)

    def hyperparams(self) -> dict[str, Any]:
        return {"weight": self.weight}

    def fit(self, inputs: ModelInput, y: np.ndarray) -> None:
        if self.parts is None:
            raise ModelArtifactError("RankBlendModel.bind() must be called before fit")
        self.coef_ = np.asarray([self.weight], dtype=np.float64)
        self.fitted = True

    def predict(self, inputs: ModelInput) -> np.ndarray:
        if self.parts is None:
            raise ModelArtifactError("RankBlendModel has no bound models")
        started = time.perf_counter()
        base = self.parts[0].predict(inputs)
        cand = self.parts[1].predict(inputs)
        rb = np.argsort(np.argsort(base)).astype(np.float64)
        rc = np.argsort(np.argsort(cand)).astype(np.float64)
        denom = max(1.0, float(inputs.n_rows - 1))
        out = (1.0 - self.weight) * (rb / denom) + self.weight * (rc / denom)
        self.cost.param_count = 1
        self._record_cost(
            predict_seconds=time.perf_counter() - started, rows=inputs.n_rows
        )
        return out

    def state_dict(self) -> dict[str, np.ndarray]:
        return {"weight": np.asarray([self.weight], dtype=np.float64)}

    def load_state(self, state: dict[str, np.ndarray]) -> None:
        self.weight = float(np.asarray(state["weight"], dtype=np.float64).ravel()[0])
        self.coef_ = np.asarray([self.weight], dtype=np.float64)
        self.fitted = True


MODEL_REGISTRY: dict[str, Callable[..., _BaseModel]] = {
    RidgeTabularModel.__name__: RidgeTabularModel,
    ConstantScoreModel.__name__: ConstantScoreModel,
    SequenceTemporalModel.__name__: SequenceTemporalModel,
    RankBlendModel.__name__: RankBlendModel,
}


class TorchSequenceAdapter(_BaseModel):
    """Optional GRU / Transformer wrapper - lazy ``torch`` import, never at import time."""

    input_kind = "sequence"

    def __init__(
        self,
        *,
        name: str = "torch_gru_optional",
        hidden_size: int = 48,
        epochs: int = 8,
    ) -> None:
        super().__init__(name=name)
        self.hidden_size = int(hidden_size)
        self.epochs = int(epochs)
        self._torch_model: object | None = None

    @staticmethod
    def _module():
        try:  # pragma: no cover - depends on host environment
            import torch
        except Exception as exc:  # noqa: BLE001 - any import failure is "unavailable"
            raise OptionalDependencyError(
                "torch is not installed; sequence deep models are validated offline only"
            ) from exc
        return torch

    def fit(
        self, inputs: ModelInput, y: np.ndarray
    ) -> None:  # pragma: no cover - needs torch
        torch = self._module()
        started = time.perf_counter()
        if inputs.sequence is None:
            raise ContractError(f"{self.name} requires a sequence bundle")
        x = torch.tensor(inputs.sequence.values, dtype=torch.float32)
        model = torch.nn.GRU(x.shape[-1], self.hidden_size, batch_first=True)
        opt = torch.optim.Adam(model.parameters(), lr=1e-3)
        target = torch.tensor(np.asarray(y, dtype=np.float32).reshape(-1, 1))
        for _ in range(self.epochs):
            opt.zero_grad()
            _, out = model(x)
            loss = torch.nn.functional.mse_loss(out.reshape(-1, 1), target)
            loss.backward()
            opt.step()
        self._torch_model = model
        self.coef_ = np.zeros(1, dtype=np.float64)
        self.cost.param_count = int(sum(p.numel() for p in model.parameters()))
        self.cost.device = "cpu"
        self.fitted = True
        self._record_cost(
            train_seconds=time.perf_counter() - started, rows=inputs.n_rows
        )

    def predict(
        self, inputs: ModelInput
    ) -> np.ndarray:  # pragma: no cover - needs torch
        torch = self._module()
        started = time.perf_counter()
        if inputs.sequence is None:
            raise ContractError(f"{self.name} requires a sequence bundle")
        with torch.no_grad():
            _, out = self._torch_model(
                torch.tensor(inputs.sequence.values, dtype=torch.float32)
            )
        result = out.reshape(out.shape[0], -1).mean(dim=1).numpy().astype(np.float64)
        self._record_cost(
            predict_seconds=time.perf_counter() - started, rows=inputs.n_rows
        )
        return np.where(np.isfinite(result), result, 0.0)


class KronosImageAdapter(TorchSequenceAdapter):
    """Placeholder for the K-line image / ViT branch (same optional-import discipline)."""

    input_kind = "sequence"

    def __init__(self, *, name: str = "kronos_image_optional") -> None:
        super().__init__(name=name, hidden_size=0, epochs=0)


def build_model(spec: dict[str, Any]) -> _BaseModel:
    """Instantiate a model from a serialisable spec (keeps runs reproducible)."""

    kind = str(spec.get("class", ""))
    factory = MODEL_REGISTRY.get(kind)
    if factory is None:
        raise ModelArtifactError(
            f"unknown model class {kind!r}; registered: {sorted(MODEL_REGISTRY)} "
            "(deep models must be wired through an optional-import adapter)"
        )
    params = {k: v for k, v in spec.items() if k not in {"class", "role"}}
    return factory(**params)


def save_model(model: _BaseModel, path: str | Path) -> Path:
    """Persist a fitted numpy model as ``.npz`` + JSON header (no pickle)."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    state = model.state_dict()
    meta = {
        "artifact_version": ARTIFACT_VERSION,
        "class": type(model).__name__,
        "name": model.name,
        "params": {**model.hyperparams(), "name": model.name},
        "describe": model.describe(),
        "state_keys": sorted(state),
    }
    with open(target, "wb") as fh:
        np.savez(fh, **state)
    target.with_suffix(target.suffix + ".json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    return target


def load_model(path: str | Path) -> _BaseModel:
    """Reload an artifact; every failure mode raises ``ModelArtifactError``."""

    source = Path(path)
    if not source.exists():
        raise ModelArtifactError(f"model artifact not found: {source}")
    header = source.with_suffix(source.suffix + ".json")
    if not header.exists():
        raise ModelArtifactError(f"model artifact header missing: {header}")
    try:
        meta = json.loads(header.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ModelArtifactError(f"corrupt model artifact header: {source}") from exc
    if int(meta.get("artifact_version", -1)) != ARTIFACT_VERSION:
        raise ModelArtifactError(f"unsupported artifact version in {source}")
    cls_name = str(meta.get("class", ""))
    if cls_name not in MODEL_REGISTRY:
        raise ModelArtifactError(
            f"cannot load {cls_name!r}: class is not in the validation registry"
        )
    try:
        with np.load(source) as data:
            state = {key: data[key] for key in meta["state_keys"]}
    except Exception as exc:  # noqa: BLE001 - translate any npz problem
        raise ModelArtifactError(f"corrupt model artifact payload: {source}") from exc
    model = MODEL_REGISTRY[cls_name](**meta.get("params", {}))
    model.load_state(state)
    return model
