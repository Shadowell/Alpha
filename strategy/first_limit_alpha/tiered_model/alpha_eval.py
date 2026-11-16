"""#27 分层分类 / 排序 / 交易 / 稳定性评估与随机基线对比。"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from strategy.first_limit_alpha.tiered_model.calibration import reliability_curve


def roc_auc_score_binary(y_true: np.ndarray, scores: np.ndarray) -> float:
    y = np.asarray(y_true, dtype=float)
    s = np.asarray(scores, dtype=float)
    pos = s[y == 1]
    neg = s[y == 0]
    if len(pos) == 0 or len(neg) == 0:
        return float("nan")
    wins = 0.0
    total = float(len(pos) * len(neg))
    for p in pos:
        wins += float(np.sum(p > neg)) + 0.5 * float(np.sum(p == neg))
    return wins / total


def precision_at_k(y_true: np.ndarray, scores: np.ndarray, k: int = 3) -> float:
    y = np.asarray(y_true, dtype=float)
    s = np.asarray(scores, dtype=float)
    if len(y) == 0:
        return float("nan")
    order = np.argsort(-s)[: min(k, len(y))]
    return float(np.mean(y[order]))


def ndcg_at_k(y_true: np.ndarray, scores: np.ndarray, k: int = 5) -> float:
    y = np.asarray(y_true, dtype=float)
    s = np.asarray(scores, dtype=float)
    if len(y) == 0:
        return float("nan")
    k_eff = min(k, len(y))
    order = np.argsort(-s)[:k_eff]
    ideal = np.sort(y)[::-1][:k_eff]
    discounts = np.log2(np.arange(2, k_eff + 2))
    dcg = float(np.sum(y[order] / discounts))
    idcg = float(np.sum(ideal / discounts))
    return dcg / idcg if idcg > 0 else 0.0


def evaluate_tier_predictions(
    frame: pd.DataFrame,
    *,
    y_col: str,
    prob_col: str = "pred_prob",
    score_col: str = "limit_up_alpha_score",
    return_col: str = "gross_return",
    date_col: str = "trade_date",
    top_k: int = 3,
    fee_bps: float = 15.0,
) -> dict[str, Any]:
    """综合评估单档模型的分类、排序、交易与随机基线对比。"""
    y = pd.to_numeric(frame[y_col], errors="coerce").to_numpy()
    p = pd.to_numeric(frame[prob_col], errors="coerce").to_numpy()
    s = pd.to_numeric(frame[score_col], errors="coerce").to_numpy()
    r = pd.to_numeric(frame[return_col], errors="coerce").to_numpy()

    valid = np.isfinite(y) & np.isfinite(p) & np.isfinite(s)
    y_v, p_v, s_v = y[valid], p[valid], s[valid]
    rel = reliability_curve(y_v, p_v)
    auc = roc_auc_score_binary(y_v, p_v)

    daily_prec: list[float] = []
    daily_ndcg: list[float] = []
    topk_rets: list[float] = []
    mkt_rets: list[float] = []
    cost = fee_bps / 10000.0

    for _, grp in frame.groupby(date_col):
        gy = pd.to_numeric(grp[y_col], errors="coerce").to_numpy()
        gs = pd.to_numeric(grp[score_col], errors="coerce").to_numpy()
        gr = pd.to_numeric(grp[return_col], errors="coerce").to_numpy()
        mask = np.isfinite(gy) & np.isfinite(gs)
        if not mask.any():
            continue
        daily_prec.append(precision_at_k(gy[mask], gs[mask], k=top_k))
        daily_ndcg.append(ndcg_at_k(gy[mask], gs[mask], k=top_k))
        if np.isfinite(gr[mask]).any():
            order = np.argsort(-gs[mask])[: min(top_k, int(mask.sum()))]
            net = gr[mask][order] - cost
            topk_rets.append(float(np.nanmean(net)))
            mkt_rets.append(float(np.nanmean(gr[mask] - cost)))

    win_rate = float(np.mean([x > 0 for x in topk_rets])) if topk_rets else float("nan")
    avg_topk = float(np.mean(topk_rets)) if topk_rets else float("nan")
    avg_mkt = float(np.mean(mkt_rets)) if mkt_rets else float("nan")

    return {
        "n_samples": int(valid.sum()),
        "auc": auc,
        "brier": rel["brier"],
        "ece": rel["ece"],
        "precision_at_k": float(np.nanmean(daily_prec)) if daily_prec else float("nan"),
        "ndcg_at_k": float(np.nanmean(daily_ndcg)) if daily_ndcg else float("nan"),
        "topk_avg_net_return": avg_topk,
        "market_baseline_net_return": avg_mkt,
        "excess_vs_market": (avg_topk - avg_mkt) if np.isfinite(avg_topk) and np.isfinite(avg_mkt) else float("nan"),
        "topk_win_rate": win_rate,
    }


__all__ = [
    "evaluate_tier_predictions",
    "ndcg_at_k",
    "precision_at_k",
    "roc_auc_score_binary",
]
