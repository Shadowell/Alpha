"""因子评估公共件：截面 IC、分层收益、相关性矩阵（issues #28-#31 共用）。

只提供 **纯计算**，不含任何 IO / 调度 / 网络。样本不足时不抛错也不编造数字，
而是返回 ``status="insufficient_samples"`` 并给出 ``sample_count``，由调用方在
验收报告里显式标注 PARTIAL。

口径
----
* IC 用 **Spearman 秩相关**（rank IC），对异常值稳健；缺失行整体剔除，
  不做任何填充。
* 分层收益按因子值升序切 ``groups`` 层（默认 5 层），返回每层标签均值与
  ``top_minus_bottom``。标签必须是"未来收益 / 事件"口径（由 #23/#26 提供），
  本模块只负责统计，不判 PIT。
* 相关性矩阵用 Pearson（输入已是截面数值）；与基线（成交额 / 量比 / 换手）
  的共线性用 :func:`max_abs_correlation` 判定。
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

#: 单个统计量的最小成对样本量；低于此只报样本数，不报数值。
MIN_SAMPLE_COUNT = 5


def _clean_pair(left: pd.Series, right: pd.Series) -> tuple[pd.Series, pd.Series]:
    a = pd.to_numeric(left, errors="coerce").astype("float64")
    b = pd.to_numeric(right, errors="coerce").astype("float64")
    mask = (
        a.notna()
        & b.notna()
        & np.isfinite(a.astype("float64"))
        & np.isfinite(b.astype("float64"))
    )
    return a[mask], b[mask]


def _pearson(a: Sequence[float], b: Sequence[float]) -> float | None:
    left = np.asarray(a, dtype="float64")
    right = np.asarray(b, dtype="float64")
    if left.size < 2 or right.size < 2:
        return None
    if float(np.std(left)) == 0.0 or float(np.std(right)) == 0.0:
        return None
    value = float(np.corrcoef(left, right)[0, 1])
    if math.isnan(value) or math.isinf(value):
        return None
    return value


def spearman(frame: pd.DataFrame, left: str, right: str) -> dict[str, Any]:
    """两列的 Spearman 秩相关；样本不足或任一侧方差为 0 → ``None``。"""
    if (
        frame is None
        or frame.empty
        or left not in frame.columns
        or right not in frame.columns
    ):
        return {"value": None, "sample_count": 0, "status": "missing_column"}
    a, b = _clean_pair(frame[left], frame[right])
    count = int(a.shape[0])
    if count < MIN_SAMPLE_COUNT:
        return {"value": None, "sample_count": count, "status": "insufficient_samples"}
    value = _pearson(
        a.rank(method="average").to_numpy(), b.rank(method="average").to_numpy()
    )
    if value is None:
        return {"value": None, "sample_count": count, "status": "zero_variance"}
    return {"value": value, "sample_count": count, "status": "ok"}


def rank_ic(
    frame: pd.DataFrame,
    factor_columns: Sequence[str],
    label_column: str,
) -> dict[str, Any]:
    """逐因子相对标签的 rank IC。

    返回 ``{"status", "label", "sample_count", "ic": {factor: value|None}}``；
    ``status`` 为 ``ok`` / ``no_valid_factor`` / ``insufficient_samples``。
    """
    if frame is None or frame.empty:
        return {
            "status": "empty_frame",
            "label": label_column,
            "sample_count": 0,
            "ic": {},
        }
    ic: dict[str, float | None] = {}
    max_count = 0
    for column in factor_columns:
        result = spearman(frame, column, label_column)
        ic[column] = result["value"]
        max_count = max(max_count, int(result["sample_count"]))
    if max_count < MIN_SAMPLE_COUNT:
        return {
            "status": "insufficient_samples",
            "label": label_column,
            "sample_count": max_count,
            "ic": {k: None for k in ic},
            "note": f"成对样本 < {MIN_SAMPLE_COUNT}，不产出 IC 数值",
        }
    if all(value is None for value in ic.values()):
        return {
            "status": "no_valid_factor",
            "label": label_column,
            "sample_count": max_count,
            "ic": ic,
        }
    return {"status": "ok", "label": label_column, "sample_count": max_count, "ic": ic}


def quantile_returns(
    frame: pd.DataFrame,
    factor_column: str,
    label_column: str,
    groups: int = 5,
) -> dict[str, Any]:
    """按因子值分层的标签均值（分层收益）。样本不足直接报状态。"""
    if frame is None or frame.empty or factor_column not in frame.columns:
        return {"status": "missing_column", "factor": factor_column, "layers": []}
    a, b = _clean_pair(frame[factor_column], frame[label_column])
    count = int(a.shape[0])
    if count < max(MIN_SAMPLE_COUNT, groups):
        return {
            "status": "insufficient_samples",
            "factor": factor_column,
            "sample_count": count,
            "layers": [],
        }
    work = pd.DataFrame({"factor": a.to_numpy(), "label": b.to_numpy()})
    try:
        work["bucket"] = pd.qcut(
            work["factor"], q=groups, labels=False, duplicates="drop"
        )
    except ValueError:
        return {
            "status": "degenerate_distribution",
            "factor": factor_column,
            "sample_count": count,
            "layers": [],
        }
    layers: list[dict[str, Any]] = []
    for bucket, sub in work.groupby("bucket", sort=True):
        layers.append(
            {
                "bucket": int(bucket),
                "count": int(sub.shape[0]),
                "mean_label": float(sub["label"].mean()),
            }
        )
    spread: float | None = None
    if len(layers) >= 2:
        spread = float(layers[-1]["mean_label"] - layers[0]["mean_label"])
    return {
        "status": "ok",
        "factor": factor_column,
        "sample_count": count,
        "layers": layers,
        "top_minus_bottom": spread,
    }


def correlation_matrix(
    frame: pd.DataFrame, columns: Sequence[str]
) -> dict[str, Mapping[str, float | None]]:
    """列间 Pearson 相关（成对剔除缺失）。返回 ``{列: {列: 值|None}}``。"""
    names = [
        c
        for c in columns
        if frame is not None and not frame.empty and c in frame.columns
    ]
    matrix: dict[str, Mapping[str, float | None]] = {}
    for left in names:
        row: dict[str, float | None] = {}
        for right in names:
            a, b = _clean_pair(frame[left], frame[right])
            row[right] = (
                _pearson(a.to_numpy(), b.to_numpy())
                if a.shape[0] >= MIN_SAMPLE_COUNT
                else None
            )
        matrix[left] = row
    return matrix


def max_abs_correlation(
    matrix: Mapping[str, Mapping[str, float | None]], column: str
) -> tuple[str | None, float | None]:
    """与 ``column`` 相关的其他列中 |corr| 最大者（排除自身与 None）。"""
    best_name: str | None = None
    best_value: float | None = None
    for other, row in matrix.items():
        if other == column:
            continue
        value = row.get(column)
        if value is None:
            continue
        if best_value is None or abs(value) > abs(best_value):
            best_name, best_value = other, float(value)
    return best_name, best_value


def collinearity_report(
    frame: pd.DataFrame,
    factor_columns: Sequence[str],
    baseline_columns: Sequence[str],
    threshold: float = 0.8,
) -> dict[str, Any]:
    """因子 vs 基线（成交额 / 量比 / 换手）的共线性检查。

    ``status="insufficient_samples"`` 时不判 PASS/FAIL，交由验收报告标 PARTIAL。
    """
    present_baselines = [
        c for c in baseline_columns if frame is not None and c in frame.columns
    ]
    if frame is None or frame.empty or not present_baselines:
        return {
            "status": "missing_baseline",
            "baselines": present_baselines,
            "max_abs": {},
            "flagged": [],
            "threshold": threshold,
        }
    pairs = list(dict.fromkeys([*factor_columns, *present_baselines]))
    matrix = correlation_matrix(frame, pairs)
    max_abs: dict[str, dict[str, float | None]] = {}
    flagged: list[str] = []
    sample = 0
    for factor in factor_columns:
        per_baseline: dict[str, float | None] = {}
        for baseline in present_baselines:
            a, b = _clean_pair(frame.get(factor), frame.get(baseline))
            sample = max(sample, int(a.shape[0]))
            per_baseline[baseline] = (
                _pearson(a.to_numpy(), b.to_numpy())
                if a.shape[0] >= MIN_SAMPLE_COUNT
                else None
            )
        max_abs[factor] = per_baseline
        worst = max(
            (abs(v) for v in per_baseline.values() if v is not None), default=None
        )
        if worst is not None and worst >= threshold:
            flagged.append(factor)
    status = "ok" if sample >= MIN_SAMPLE_COUNT else "insufficient_samples"
    if status != "ok":
        flagged = []
    # 同族冗余（因子 vs 因子）：给 #27 做正交化 / 剪枝的直接依据。
    family_redundancy: dict[str, dict[str, float | None]] = {}
    for factor in factor_columns:
        partner, value = max_abs_correlation(matrix, factor)
        family_redundancy[factor] = {"partner": partner, "corr": value}
    return {
        "status": status,
        "sample_count": sample,
        "baselines": present_baselines,
        "max_abs": max_abs,
        "family_redundancy": family_redundancy,
        "flagged": flagged,
        "threshold": threshold,
    }
