"""#27 模型可解释性（置换重要度 + 单样本线性归因）。"""

from __future__ import annotations

from typing import Any, Callable, Sequence

import numpy as np


def permutation_importance(
    predict_fn: Callable[[np.ndarray], np.ndarray],
    X: np.ndarray,
    y: np.ndarray,
    feature_names: Sequence[str],
    *,
    seed: int = 42,
) -> list[dict[str, Any]]:
    """计算基于 Brier/MSE 增量的特征置换重要度。"""
    X_arr = np.asarray(X, dtype=float)
    y_arr = np.asarray(y, dtype=float)
    if len(y_arr) == 0:
        return []

    base_pred = predict_fn(X_arr)
    base_loss = float(np.mean((base_pred - y_arr) ** 2))
    rng = np.random.default_rng(seed)

    results: list[dict[str, Any]] = []
    for idx, name in enumerate(feature_names):
        X_perm = X_arr.copy()
        X_perm[:, idx] = rng.permutation(X_perm[:, idx])
        perm_pred = predict_fn(X_perm)
        perm_loss = float(np.mean((perm_pred - y_arr) ** 2))
        results.append(
            {
                "feature": name,
                "importance": float(perm_loss - base_loss),
                "baseline_loss": base_loss,
                "permuted_loss": perm_loss,
            }
        )
    results.sort(key=lambda r: abs(r["importance"]), reverse=True)
    return results


def explain_single_sample(
    estimator: Any,
    x_row: np.ndarray,
    feature_names: Sequence[str],
    *,
    top_k: int = 5,
) -> list[dict[str, Any]]:
    """返回单样本对预测贡献最大的前 K 个特征。"""
    w = getattr(estimator, "weights_", None)
    mean = getattr(estimator, "mean_", None)
    std = getattr(estimator, "std_", None)
    if w is None or mean is None or std is None:
        return []

    x = np.asarray(x_row, dtype=float)
    x_imp = np.where(np.isfinite(x), x, mean)
    z = (x_imp - mean) / std
    contribs = z * w
    items = [
        {"feature": name, "contribution": float(c), "z_value": float(zv)}
        for name, c, zv in zip(feature_names, contribs, z)
    ]
    items.sort(key=lambda r: abs(r["contribution"]), reverse=True)
    return items[:top_k]


__all__ = ["explain_single_sample", "permutation_importance"]
