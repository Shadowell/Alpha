"""Out-of-sample metrics in pure numpy + pandas (no sklearn / no GPU, issue #36).

Every metric is a plain function of ``(score, label, realised return, date)`` so
baseline and candidate are scored through *exactly* the same code path - the
issue's "指标与回测口径一致，可直接比较" requirement.

Conventions
-----------
* ``score`` higher = more bullish.  ``label`` is binary (0/1) or continuous.
* ``ret`` is the *gross* realised return of the sample (provided by #26; costs
  are applied by :mod:`strategy.model_validation.portfolio`).
* Metrics that need a cross-section take a ``date`` array and average the daily
  values (this is what makes IC comparable across spiky 打板 samples).
* Degenerate inputs (all-equal scores, single-class labels, < 2 observations)
  return ``None`` instead of raising, and the gate treats ``None`` as a failure
  for that metric rather than silently ignoring it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np

EPS = 1e-12


def _finite(*arrays: np.ndarray) -> np.ndarray:
    stack = np.vstack([np.asarray(a, dtype=np.float64).ravel() for a in arrays])
    return np.isfinite(stack).all(axis=0)


def _percentile_alts(values: np.ndarray, q: Sequence[float]) -> np.ndarray:
    """Linear-interpolation quantiles without pulling in numpy's ``nan*`` helpers."""

    return np.asarray([np.quantile(values, float(p)) for p in q], dtype=np.float64)


def rank_average(values: np.ndarray) -> np.ndarray:
    """Ties get the *average* rank (needed for a correct Spearman)."""

    arr = np.asarray(values, dtype=np.float64)
    order = np.argsort(arr, kind="mergesort")
    ranks = np.empty(arr.size, dtype=np.float64)
    ranks[order] = np.arange(arr.size, dtype=np.float64)
    # collapse ties
    sorted_values = arr[order]
    i = 0
    while i < arr.size:
        j = i
        while j + 1 < arr.size and sorted_values[j + 1] == sorted_values[i]:
            j += 1
        if j > i:
            ranks[order[i : j + 1]] = float((i + j) / 2.0)
        i = j + 1
    return ranks


def pearson(a: np.ndarray, b: np.ndarray) -> float | None:
    mask = _finite(a, b)
    x = np.asarray(a, dtype=np.float64)[mask]
    y = np.asarray(b, dtype=np.float64)[mask]
    if x.size < 3 or np.std(x) < EPS or np.std(y) < EPS:
        return None
    xm, ym = x - x.mean(), y - y.mean()
    denom = float(np.sqrt((xm * xm).sum() * (ym * ym).sum()))
    if denom < EPS:
        return None
    return float((xm * ym).sum() / denom)


def spearman(a: np.ndarray, b: np.ndarray) -> float | None:
    mask = _finite(a, b)
    x = np.asarray(a, dtype=np.float64)[mask]
    y = np.asarray(b, dtype=np.float64)[mask]
    if x.size < 3:
        return None
    return pearson(rank_average(x), rank_average(y))


def information_coefficient(scores: np.ndarray, returns: np.ndarray) -> float | None:
    """Pooled Pearson IC between forecast score and realised return."""

    return pearson(scores, returns)


def rank_information_coefficient(
    scores: np.ndarray, returns: np.ndarray
) -> float | None:
    return spearman(scores, returns)


def cross_sectional_ic(
    scores: np.ndarray,
    returns: np.ndarray,
    dates: np.ndarray,
    *,
    method: str = "spearman",
) -> dict[str, object]:
    """Per-date IC plus the IR-style summary used by the gate."""

    fn = spearman if method == "spearman" else pearson
    values: list[float] = []
    for date in pd_unique(dates):
        mask = dates == date
        value = fn(scores[mask], returns[mask])
        if value is not None:
            values.append(value)
    if not values:
        return {
            "ic_mean": None,
            "ic_std": None,
            "icir": None,
            "ic_t_stat": None,
            "ic_positive_ratio": None,
            "n_ic_days": 0,
        }
    arr = np.asarray(values, dtype=np.float64)
    mean = float(arr.mean())
    std = float(arr.std(ddof=1)) if arr.size > 1 else 0.0
    return {
        "ic_mean": mean,
        "ic_std": std,
        "icir": None if std < EPS else mean / std,
        "ic_t_stat": None if std < EPS else mean / (std / np.sqrt(arr.size)),
        "ic_positive_ratio": float((arr > 0).mean()),
        "n_ic_days": int(arr.size),
    }


def pd_unique(values: np.ndarray) -> list:
    """Stable-ordered unique values (avoids depending on pandas sort semantics)."""

    seen: dict[object, None] = {}
    for v in np.asarray(values).ravel():
        key = v.item() if hasattr(v, "item") else v
        seen.setdefault(key, None)
    return sorted(seen, key=lambda x: str(x))


def roc_auc(labels: np.ndarray, scores: np.ndarray) -> float | None:
    mask = _finite(labels, scores)
    y = np.asarray(labels, dtype=np.float64)[mask]
    s = np.asarray(scores, dtype=np.float64)[mask]
    positives = y > 0.5
    if positives.sum() == 0 or (~positives).sum() == 0:
        return None
    ranks = rank_average(s) + 1.0
    n_pos = int(positives.sum())
    n_neg = int(s.size - n_pos)
    sum_pos = float(ranks[positives].sum())
    return float((sum_pos - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg))


def pr_auc(labels: np.ndarray, scores: np.ndarray) -> float | None:
    """Average precision (step integration) - the PR-AUC the issue asks for."""

    mask = _finite(labels, scores)
    y = np.asarray(labels, dtype=np.float64)[mask]
    s = np.asarray(scores, dtype=np.float64)[mask]
    positives = y > 0.5
    n_pos = int(positives.sum())
    if n_pos == 0 or n_pos == s.size:
        return None
    order = np.argsort(-s, kind="mergesort")
    y_sorted = positives[order].astype(float)
    tp = np.cumsum(y_sorted)
    fp = np.cumsum(1.0 - y_sorted)
    precision = tp / (tp + fp + EPS)
    recall = tp / n_pos
    # AP = sum over steps of (R_k - R_{k-1}) * P_k
    prev_recall = np.concatenate(([0.0], recall[:-1]))
    deltas = recall - prev_recall
    return float(np.sum(deltas * precision))


def topk_selection(scores: np.ndarray, k: int) -> np.ndarray:
    """Row indices of the top-k scores, deterministic on ties (by index order)."""

    if k <= 0:
        return np.asarray([], dtype=int)
    order = np.argsort(-np.asarray(scores, dtype=np.float64), kind="mergesort")
    return order[: min(k, order.size)]


def precision_at_topk(labels: np.ndarray, scores: np.ndarray, k: int) -> float | None:
    y = np.asarray(labels, dtype=np.float64)
    idx = topk_selection(scores, k)
    if idx.size == 0:
        return None
    usable = y[idx]
    if not np.isfinite(usable).all():
        usable = usable[np.isfinite(usable)]
    if usable.size == 0:
        return None
    return float((usable > 0.5).mean())


def hit_rate(returns: np.ndarray, scores: np.ndarray, k: int) -> float | None:
    """Share of the top-k picks with a positive realised return."""

    idx = topk_selection(scores, k)
    if idx.size == 0:
        return None
    r = np.asarray(returns, dtype=np.float64)[idx]
    r = r[np.isfinite(r)]
    if r.size == 0:
        return None
    return float((r > 0).mean())


def mean_topk_return(returns: np.ndarray, scores: np.ndarray, k: int) -> float | None:
    idx = topk_selection(scores, k)
    if idx.size == 0:
        return None
    r = np.asarray(returns, dtype=np.float64)[idx]
    r = r[np.isfinite(r)]
    return None if r.size == 0 else float(r.mean())


def per_date_topk_stats(
    labels: np.ndarray,
    returns: np.ndarray,
    scores: np.ndarray,
    dates: np.ndarray,
    k: int,
) -> dict[str, float | None]:
    """Daily TopK hit metrics averaged over dates - the 打板 portfolio is rebalanced
    every signal day, so a fold-level pooled top-k would be dominated by one day."""

    labels = np.asarray(labels, dtype=np.float64)
    returns = np.asarray(returns, dtype=np.float64)
    scores = np.asarray(scores, dtype=np.float64)
    precisions: list[float] = []
    hits: list[float] = []
    means: list[float] = []
    for date in pd_unique(dates):
        mask = dates == date
        if mask.sum() < 2:
            continue
        idx = topk_selection(scores[mask], k)
        if idx.size == 0:
            continue
        positions = np.flatnonzero(mask)[idx]
        day_labels = labels[positions]
        day_labels = day_labels[np.isfinite(day_labels)]
        if day_labels.size:
            precisions.append(float((day_labels > 0.5).mean()))
        day_returns = returns[positions]
        day_returns = day_returns[np.isfinite(day_returns)]
        if day_returns.size:
            hits.append(float((day_returns > 0).mean()))
            means.append(float(day_returns.mean()))
    if not precisions and not hits:
        return {
            "precision_at_topk": None,
            "hit_rate_topk": None,
            "mean_return_topk": None,
            "n_topk_days": 0,
        }
    return {
        "precision_at_topk": None if not precisions else float(np.mean(precisions)),
        "hit_rate_topk": None if not hits else float(np.mean(hits)),
        "mean_return_topk": None if not means else float(np.mean(means)),
        "n_topk_days": len(hits),
    }


@dataclass(slots=True)
class LayeredResult:
    layer_returns: tuple[float, ...]
    long_short: float | None
    monotonicity: float | None
    n_layers: int

    def as_dict(self) -> dict[str, object]:
        return {
            "layer_returns": [
                None if v is None else round(v, 8) for v in self.layer_returns
            ],
            "long_short": None
            if self.long_short is None
            else round(self.long_short, 8),
            "monotonicity": None
            if self.monotonicity is None
            else round(self.monotonicity, 6),
            "n_layers": self.n_layers,
        }


def layered_returns(
    scores: np.ndarray, returns: np.ndarray, *, n_layers: int = 5
) -> LayeredResult:
    """Quantile-layer (分层) mean returns; layer 0 = lowest score."""

    mask = _finite(scores, returns)
    s = np.asarray(scores, dtype=np.float64)[mask]
    r = np.asarray(returns, dtype=np.float64)[mask]
    if s.size < max(4, 2 * n_layers) or n_layers < 2:
        return LayeredResult(
            layer_returns=(), long_short=None, monotonicity=None, n_layers=n_layers
        )
    cuts = _percentile_alts(s, [(i + 1) / n_layers for i in range(n_layers - 1)])
    edges = np.concatenate(([-np.inf], cuts, [np.inf]))
    means: list[float] = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        sel = (s > lo) & (s <= hi) if np.isfinite(lo) else (s <= hi)
        means.append(float(r[sel].mean()) if sel.any() else float("nan"))
    finite = [m for m in means if np.isfinite(m)]
    long_short = (
        means[-1] - means[0]
        if np.isfinite(means[-1]) and np.isfinite(means[0])
        else None
    )
    pair = [(i, m) for i, m in enumerate(means) if np.isfinite(m)]
    monotonicity = None
    if len(pair) >= 3:
        xs = np.asarray([p[0] for p in pair], dtype=np.float64)
        ys = np.asarray([p[1] for p in pair], dtype=np.float64)
        monotonicity = spearman(xs, ys)
    if not finite:
        long_short = None
    return LayeredResult(
        layer_returns=tuple(
            round(m, 8) if np.isfinite(m) else float("nan") for m in means
        ),
        long_short=None if long_short is None else float(long_short),
        monotonicity=monotonicity,
        n_layers=n_layers,
    )


def brier_score(probabilities: np.ndarray, labels: np.ndarray) -> float | None:
    mask = _finite(probabilities, labels)
    p = np.asarray(probabilities, dtype=np.float64)[mask]
    y = np.asarray(labels, dtype=np.float64)[mask]
    if p.size == 0:
        return None
    return float(np.mean((p - y) ** 2))


def expected_calibration_error(
    probabilities: np.ndarray, labels: np.ndarray, *, bins: int = 10
) -> float | None:
    mask = _finite(probabilities, labels)
    p = np.asarray(probabilities, dtype=np.float64)[mask]
    y = np.asarray(labels, dtype=np.float64)[mask]
    if p.size == 0:
        return None
    edges = np.linspace(0.0, 1.0, bins + 1)
    total = 0.0
    for lo, hi in zip(edges[:-1], edges[1:]):
        # half-open bins, only the last one closes on 1.0 (no double counting)
        sel = (p >= lo) & ((p < hi) if hi < 1.0 else (p <= 1.0))
        if not sel.any():
            continue
        n = int(sel.sum())
        total += (n / p.size) * abs(float(p[sel].mean()) - float(y[sel].mean()))
    return float(total)


def max_drawdown(returns: np.ndarray) -> float | None:
    r = np.asarray(returns, dtype=np.float64)
    r = r[np.isfinite(r)]
    if r.size == 0:
        return None
    equity = np.cumprod(1.0 + r)
    peak = np.maximum.accumulate(equity)
    dd = equity / (peak + EPS) - 1.0
    return float(dd.min())


def one_sided_turnover(selections: Sequence[Sequence[str]]) -> float | None:
    """Average one-sided turnover of the daily top-k sets (0 = no re-shuffling)."""

    previous: set[str] | None = None
    values: list[float] = []
    for sel in selections:
        current = set(sel)
        if not current:
            continue
        if previous is not None:
            union = len(current | previous)
            if union:
                values.append(len(current - previous) / union)
        previous = current
    if not values:
        return None
    return float(np.mean(values))


@dataclass(slots=True)
class ScoreMetrics:
    """Flat, JSON-friendly metric bundle for one (fold, model) pair."""

    n_rows: int
    n_dates: int
    positive_rate: float | None
    pooled_ic: float | None
    pooled_rank_ic: float | None
    ic_mean: float | None
    icir: float | None
    ic_t_stat: float | None
    ic_positive_ratio: float | None
    n_ic_days: int
    roc_auc: float | None
    pr_auc: float | None
    precision_at_topk: float | None
    hit_rate_topk: float | None
    mean_return_topk: float | None
    topk: int
    layers: LayeredResult
    brier: float | None
    ece: float | None
    max_drawdown: float | None

    def as_dict(self) -> dict[str, object]:
        return {
            "n_rows": self.n_rows,
            "n_dates": self.n_dates,
            "positive_rate": _round(self.positive_rate),
            "pooled_ic": _round(self.pooled_ic),
            "pooled_rank_ic": _round(self.pooled_rank_ic),
            "ic_mean": _round(self.ic_mean),
            "icir": _round(self.icir),
            "ic_t_stat": _round(self.ic_t_stat),
            "ic_positive_ratio": _round(self.ic_positive_ratio),
            "n_ic_days": self.n_ic_days,
            "roc_auc": _round(self.roc_auc),
            "pr_auc": _round(self.pr_auc),
            "precision_at_topk": _round(self.precision_at_topk),
            "hit_rate_topk": _round(self.hit_rate_topk),
            "mean_return_topk": _round(self.mean_return_topk),
            "topk": self.topk,
            "layers": self.layers.as_dict(),
            "brier": _round(self.brier),
            "ece": _round(self.ece),
            "max_drawdown": _round(self.max_drawdown),
        }


def _round(value: float | None, digits: int = 8) -> float | None:
    if value is None or not np.isfinite(value):
        return None
    return round(float(value), digits)


def evaluate_scores(
    *,
    scores: np.ndarray,
    labels: np.ndarray,
    returns: np.ndarray,
    dates: np.ndarray,
    top_k: int = 5,
    n_layers: int = 5,
    probabilities: np.ndarray | None = None,
) -> ScoreMetrics:
    """Single entry point that both baseline and candidate go through.

    ``probabilities`` (an optional calibrated 0-1 output from the model) is what
    the calibration metrics use; ranking metrics always use ``scores``, because a
    candidate may emit uncalibrated logits and must still be comparable.
    """

    scores = np.asarray(scores, dtype=np.float64)
    labels = np.asarray(labels, dtype=np.float64)
    returns = np.asarray(returns, dtype=np.float64)
    dates = np.asarray(dates)
    finite_labels = labels[np.isfinite(labels)]
    cross = cross_sectional_ic(scores, returns, dates)
    topk = per_date_topk_stats(labels, returns, scores, dates, top_k)
    probs = (
        scores if probabilities is None else np.asarray(probabilities, dtype=np.float64)
    )
    in_unit_range = (
        np.isfinite(probs).all()
        and float(np.nanmin(probs)) >= -EPS
        and float(np.nanmax(probs)) <= 1.0 + EPS
    )
    return ScoreMetrics(
        n_rows=int(scores.size),
        n_dates=len(pd_unique(dates)),
        positive_rate=None
        if finite_labels.size == 0
        else float((finite_labels > 0.5).mean()),
        pooled_ic=information_coefficient(scores, returns),
        pooled_rank_ic=rank_information_coefficient(scores, returns),
        ic_mean=cross["ic_mean"],
        icir=cross["icir"],
        ic_t_stat=cross["ic_t_stat"],
        ic_positive_ratio=cross["ic_positive_ratio"],
        n_ic_days=int(cross["n_ic_days"]),
        roc_auc=roc_auc(labels, scores),
        pr_auc=pr_auc(labels, scores),
        precision_at_topk=topk["precision_at_topk"],
        hit_rate_topk=topk["hit_rate_topk"],
        mean_return_topk=topk["mean_return_topk"],
        topk=top_k,
        layers=layered_returns(scores, returns, n_layers=n_layers),
        brier=brier_score(probs, labels) if in_unit_range else None,
        ece=expected_calibration_error(probs, labels) if in_unit_range else None,
        max_drawdown=max_drawdown(returns),
    )
