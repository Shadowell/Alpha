"""#27 分层模型估计器（numpy 基线 + 可选 sklearn/LightGBM 后端）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

import numpy as np


def _sigmoid(z: np.ndarray) -> np.ndarray:
    z_clip = np.clip(z, -30.0, 30.0)
    return 1.0 / (1.0 + np.exp(-z_clip))


@dataclass
class NumpyLogisticEstimator:
    """轻量 L2 正则逻辑回归基线（纯 numpy，无额外依赖）。"""

    lr: float = 0.2
    n_iter: int = 200
    l2: float = 1e-2
    feature_names: tuple[str, ...] = ()
    weights_: np.ndarray | None = None
    bias_: float = 0.0
    mean_: np.ndarray | None = None
    std_: np.ndarray | None = None

    def fit(self, X: np.ndarray, y: np.ndarray) -> NumpyLogisticEstimator:
        X_arr = np.asarray(X, dtype=float)
        y_arr = np.asarray(y, dtype=float)
        self.mean_ = np.nanmean(X_arr, axis=0)
        self.mean_ = np.where(np.isfinite(self.mean_), self.mean_, 0.0)
        X_imp = np.where(np.isfinite(X_arr), X_arr, self.mean_)
        self.std_ = np.std(X_imp, axis=0)
        self.std_ = np.where(self.std_ > 1e-8, self.std_, 1.0)
        Xs = (X_imp - self.mean_) / self.std_

        n, d = Xs.shape
        w = np.zeros(d, dtype=float)
        b = 0.0
        for _ in range(self.n_iter):
            p = _sigmoid(Xs @ w + b)
            err = p - y_arr
            grad_w = (Xs.T @ err) / max(n, 1) + self.l2 * w
            grad_b = float(np.mean(err)) if n > 0 else 0.0
            w -= self.lr * grad_w
            b -= self.lr * grad_b
        self.weights_ = w
        self.bias_ = b
        return self

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        if self.weights_ is None or self.mean_ is None or self.std_ is None:
            raise RuntimeError("Estimator is not fitted")
        X_arr = np.asarray(X, dtype=float)
        X_imp = np.where(np.isfinite(X_arr), X_arr, self.mean_)
        Xs = (X_imp - self.mean_) / self.std_
        return _sigmoid(Xs @ self.weights_ + self.bias_)


@dataclass
class NumpyRidgeRegressor:
    """轻量 L2 岭回归基线（用于次日竞价溢价回归头）。"""

    alpha: float = 1.0
    feature_names: tuple[str, ...] = ()
    weights_: np.ndarray | None = None
    bias_: float = 0.0
    mean_: np.ndarray | None = None
    std_: np.ndarray | None = None

    def fit(self, X: np.ndarray, y: np.ndarray) -> NumpyRidgeRegressor:
        X_arr = np.asarray(X, dtype=float)
        y_arr = np.asarray(y, dtype=float)
        self.mean_ = np.nanmean(X_arr, axis=0)
        self.mean_ = np.where(np.isfinite(self.mean_), self.mean_, 0.0)
        X_imp = np.where(np.isfinite(X_arr), X_arr, self.mean_)
        self.std_ = np.std(X_imp, axis=0)
        self.std_ = np.where(self.std_ > 1e-8, self.std_, 1.0)
        Xs = (X_imp - self.mean_) / self.std_

        self.bias_ = float(np.mean(y_arr)) if len(y_arr) else 0.0
        yc = y_arr - self.bias_
        d = Xs.shape[1]
        gram = Xs.T @ Xs + self.alpha * np.eye(d)
        self.weights_ = np.linalg.solve(gram, Xs.T @ yc)
        return self

    def predict(self, X: np.ndarray) -> np.ndarray:
        if self.weights_ is None or self.mean_ is None or self.std_ is None:
            raise RuntimeError("Regressor is not fitted")
        X_arr = np.asarray(X, dtype=float)
        X_imp = np.where(np.isfinite(X_arr), X_arr, self.mean_)
        Xs = (X_imp - self.mean_) / self.std_
        return Xs @ self.weights_ + self.bias_


@dataclass
class TierModelBundle:
    tier: str
    features: tuple[str, ...]
    classifiers: dict[str, Any] = field(default_factory=dict)
    regressor: Any | None = None
    backend: str = "numpy_baseline"


def build_classifier(backend: str = "numpy", feature_names: Sequence[str] = ()) -> Any:
    if backend == "lightgbm":
        try:
            import lightgbm as lgb  # type: ignore

            return lgb.LGBMClassifier(n_estimators=50, learning_rate=0.05, verbose=-1)
        except Exception:
            pass
    return NumpyLogisticEstimator(feature_names=tuple(feature_names))


__all__ = [
    "NumpyLogisticEstimator",
    "NumpyRidgeRegressor",
    "TierModelBundle",
    "build_classifier",
]
