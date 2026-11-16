"""#27 概率校准与可靠性评估（Platt scaling / Brier / ECE / 可靠性曲线）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np


@dataclass
class PlattCalibrator:
    """一维逻辑校准器：对原始概率 logit 做线性标定。"""

    coef_: float = 1.0
    intercept_: float = 0.0

    def fit(self, probs: np.ndarray, y_true: np.ndarray, *, n_iter: int = 150, lr: float = 0.1) -> PlattCalibrator:
        p = np.clip(np.asarray(probs, dtype=float), 1e-6, 1.0 - 1e-6)
        y = np.asarray(y_true, dtype=float)
        logits = np.log(p / (1.0 - p))
        a, b = 1.0, 0.0
        for _ in range(n_iter):
            pred = 1.0 / (1.0 + np.exp(-np.clip(a * logits + b, -30.0, 30.0)))
            err = pred - y
            a -= lr * float(np.mean(err * logits))
            b -= lr * float(np.mean(err))
        self.coef_ = a
        self.intercept_ = b
        return self

    def transform(self, probs: np.ndarray) -> np.ndarray:
        p = np.clip(np.asarray(probs, dtype=float), 1e-6, 1.0 - 1e-6)
        logits = np.log(p / (1.0 - p))
        return 1.0 / (1.0 + np.exp(-np.clip(self.coef_ * logits + self.intercept_, -30.0, 30.0)))


def brier_score(y_true: np.ndarray, probs: np.ndarray) -> float:
    y = np.asarray(y_true, dtype=float)
    p = np.asarray(probs, dtype=float)
    if len(y) == 0:
        return float("nan")
    return float(np.mean((p - y) ** 2))


def reliability_curve(
    y_true: np.ndarray, probs: np.ndarray, *, n_bins: int = 5
) -> dict[str, Any]:
    y = np.asarray(y_true, dtype=float)
    p = np.asarray(probs, dtype=float)
    if len(y) == 0:
        return {"ece": float("nan"), "brier": float("nan"), "bins": []}

    edges = np.linspace(0.0, 1.0, n_bins + 1)
    bins: list[dict[str, Any]] = []
    ece = 0.0
    n = len(y)
    for i in range(n_bins):
        lo, hi = float(edges[i]), float(edges[i + 1])
        if i == n_bins - 1:
            mask = (p >= lo) & (p <= hi)
        else:
            mask = (p >= lo) & (p < hi)
        cnt = int(np.sum(mask))
        if cnt == 0:
            continue
        conf = float(np.mean(p[mask]))
        acc = float(np.mean(y[mask]))
        ece += (cnt / n) * abs(acc - conf)
        bins.append({"lo": lo, "hi": hi, "count": cnt, "mean_prob": conf, "empirical_rate": acc})

    return {"ece": float(ece), "brier": brier_score(y, p), "bins": bins}


__all__ = ["PlattCalibrator", "brier_score", "reliability_curve"]
