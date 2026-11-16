"""#27 因子评估体系（RankIC / ICIR / 分层收益单调性 / 共线性 VIF / 衰减 / 拥挤度）。"""

from __future__ import annotations

from typing import Any, Sequence

import numpy as np
import pandas as pd


def _spearman_corr(x: pd.Series, y: pd.Series) -> float:
    valid = pd.concat([x, y], axis=1).dropna()
    if len(valid) < 3:
        return float("nan")
    rx = valid.iloc[:, 0].rank()
    ry = valid.iloc[:, 1].rank()
    if rx.std(ddof=0) == 0 or ry.std(ddof=0) == 0:
        return float("nan")
    return float(rx.corr(ry))


def evaluate_factor_ic(
    frame: pd.DataFrame,
    factor_names: Sequence[str],
    *,
    return_col: str = "gross_return",
    date_col: str = "trade_date",
    n_quantiles: int = 5,
) -> pd.DataFrame:
    """按交易日计算各因子的 RankIC、ICIR 与分层收益单调性。"""
    records: list[dict[str, Any]] = []
    for fac in factor_names:
        if fac not in frame.columns or return_col not in frame.columns:
            continue
        daily_ics: list[float] = []
        for _, grp in frame.groupby(date_col):
            ic = _spearman_corr(
                pd.to_numeric(grp[fac], errors="coerce"),
                pd.to_numeric(grp[return_col], errors="coerce"),
            )
            if np.isfinite(ic):
                daily_ics.append(ic)
        mean_ic = float(np.mean(daily_ics)) if daily_ics else float("nan")
        std_ic = float(np.std(daily_ics, ddof=1)) if len(daily_ics) > 1 else float("nan")
        icir = mean_ic / std_ic if np.isfinite(std_ic) and std_ic > 1e-8 else float("nan")

        # 分层收益（Q_top - Q_bottom）
        sub = frame[[fac, return_col]].apply(pd.to_numeric, errors="coerce").dropna()
        mono_spread = float("nan")
        if len(sub) >= n_quantiles * 2 and sub[fac].nunique() >= n_quantiles:
            sub["q"] = pd.qcut(sub[fac], q=n_quantiles, labels=False, duplicates="drop")
            q_means = sub.groupby("q")[return_col].mean()
            if len(q_means) >= 2:
                mono_spread = float(q_means.iloc[-1] - q_means.iloc[0])

        is_dead = bool(
            (np.isfinite(mean_ic) and abs(mean_ic) < 0.01)
            or (np.isfinite(icir) and abs(icir) < 0.1 and len(daily_ics) >= 10)
        )
        records.append(
            {
                "factor": fac,
                "n_dates": len(daily_ics),
                "rank_ic_mean": mean_ic,
                "rank_ic_std": std_ic,
                "icir": icir,
                "top_bottom_spread": mono_spread,
                "is_weak_or_dead": is_dead,
            }
        )
    return pd.DataFrame(records)


def compute_vif_table(frame: pd.DataFrame, factor_names: Sequence[str]) -> pd.DataFrame:
    """计算因子共线性 VIF（方差膨胀因子）。"""
    cols = [c for c in factor_names if c in frame.columns]
    if len(cols) < 2:
        return pd.DataFrame([{"factor": c, "vif": 1.0} for c in cols])
    mat = frame[cols].apply(pd.to_numeric, errors="coerce").dropna()
    if len(mat) < len(cols) + 2:
        return pd.DataFrame([{"factor": c, "vif": float("nan")} for c in cols])

    corr = mat.corr().to_numpy()
    try:
        inv = np.linalg.pinv(corr)
        vifs = np.diag(inv)
    except Exception:
        vifs = np.full(len(cols), np.nan)
    return pd.DataFrame(
        [{"factor": c, "vif": float(v)} for c, v in zip(cols, vifs)]
    )


__all__ = ["compute_vif_table", "evaluate_factor_ic"]
