"""#27 分层 Walk-Forward 训练与样本外评估（按交易日切分 + embargo）。"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from strategy.first_limit_alpha.tiered_model.alpha_eval import evaluate_tier_predictions
from strategy.first_limit_alpha.tiered_model.calibration import PlattCalibrator
from strategy.first_limit_alpha.tiered_model.dataset import TierDataset
from strategy.first_limit_alpha.tiered_model.estimators import (
    NumpyRidgeRegressor,
    build_classifier,
)


def run_tier_walkforward(
    ds: TierDataset,
    *,
    n_splits: int = 3,
    embargo_dates: int = 3,
    min_train_dates: int = 5,
    backend: str = "numpy",
    top_k: int = 3,
) -> dict[str, Any]:
    """对单个板型档位执行严格时序 walk-forward 训练与样本外评估。"""
    frame = ds.frame.sort_values("trade_date").reset_index(drop=True)
    dates = sorted(frame["trade_date"].astype(str).unique().tolist())
    y_col = f"y_{ds.primary_head}"
    return_col = "fwd_ret_3d" if "fwd_ret_3d" in frame.columns else y_col

    if len(dates) < min_train_dates + embargo_dates + 2:
        return {
            "tier": ds.tier,
            "status": "SKIPPED_INSUFFICIENT_DATES",
            "n_dates": len(dates),
            "folds": [],
            "oos_metrics": {},
        }

    test_dates_pool = dates[min_train_dates + embargo_dates :]
    fold_size = max(1, len(test_dates_pool) // max(1, n_splits))

    oos_frames: list[pd.DataFrame] = []
    folds_summary: list[dict[str, Any]] = []
    last_clf = None

    for fold_idx in range(n_splits):
        start_i = fold_idx * fold_size
        if start_i >= len(test_dates_pool):
            break
        end_i = (
            len(test_dates_pool)
            if fold_idx == n_splits - 1
            else min(len(test_dates_pool), (fold_idx + 1) * fold_size)
        )
        test_dts = set(test_dates_pool[start_i:end_i])
        first_test_dt = min(test_dts)
        first_test_pos = dates.index(first_test_dt)
        train_cutoff_pos = first_test_pos - embargo_dates
        if train_cutoff_pos <= 0:
            continue
        train_dts = set(dates[:train_cutoff_pos])

        train_df = frame[frame["trade_date"].isin(train_dts)].copy()
        test_df = frame[frame["trade_date"].isin(test_dts)].copy()
        if train_df.empty or test_df.empty or y_col not in train_df.columns:
            continue

        feats = list(ds.features)
        y_tr = pd.to_numeric(train_df[y_col], errors="coerce")
        valid_tr = y_tr.notna()
        if valid_tr.sum() < 5 or y_tr[valid_tr].nunique() < 2:
            continue

        X_tr = train_df.loc[valid_tr, feats].apply(pd.to_numeric, errors="coerce").to_numpy()
        clf = build_classifier(backend=backend, feature_names=feats)
        clf.fit(X_tr, y_tr[valid_tr].to_numpy())
        last_clf = clf

        # 概率校准（仅在训练集拟合）
        raw_tr_p = clf.predict_proba(X_tr)
        if raw_tr_p.ndim == 2:
            raw_tr_p = raw_tr_p[:, 1]
        cal = PlattCalibrator().fit(raw_tr_p, y_tr[valid_tr].to_numpy())

        # 回归头（若存在次日溢价标签）
        reg_col = "y_p_next_auction_premium"
        reg = None
        if reg_col in train_df.columns:
            y_reg = pd.to_numeric(train_df[reg_col], errors="coerce")
            v_reg = y_reg.notna()
            if v_reg.sum() >= 5:
                X_reg = train_df.loc[v_reg, feats].apply(pd.to_numeric, errors="coerce").to_numpy()
                reg = NumpyRidgeRegressor(feature_names=tuple(feats)).fit(X_reg, y_reg[v_reg].to_numpy())

        X_te = test_df[feats].apply(pd.to_numeric, errors="coerce").to_numpy()
        raw_te_p = clf.predict_proba(X_te)
        if raw_te_p.ndim == 2:
            raw_te_p = raw_te_p[:, 1]
        cal_te_p = cal.transform(raw_te_p)

        test_df["pred_prob"] = cal_te_p
        if reg is not None:
            test_df["pred_premium"] = reg.predict(X_te)
            test_df["limit_up_alpha_score"] = 70.0 * cal_te_p + 30.0 * np.clip(
                test_df["pred_premium"] * 5.0 + 0.5, 0.0, 1.0
            )
        else:
            test_df["limit_up_alpha_score"] = 100.0 * cal_te_p

        if return_col not in test_df.columns:
            test_df[return_col] = np.nan

        oos_frames.append(test_df)
        folds_summary.append(
            {
                "fold": fold_idx,
                "train_dates": len(train_dts),
                "test_dates": len(test_dts),
                "n_train": int(valid_tr.sum()),
                "n_test": len(test_df),
            }
        )

    if not oos_frames:
        return {
            "tier": ds.tier,
            "status": "SKIPPED_NO_VALID_FOLDS",
            "n_dates": len(dates),
            "folds": folds_summary,
            "oos_metrics": {},
        }

    oos_all = pd.concat(oos_frames, ignore_index=True)
    metrics = evaluate_tier_predictions(
        oos_all, y_col=y_col, return_col=return_col, top_k=top_k
    )
    return {
        "tier": ds.tier,
        "primary_head": ds.primary_head,
        "status": "OK",
        "n_dates": len(dates),
        "folds": folds_summary,
        "oos_metrics": metrics,
        "oos_frame": oos_all,
        "last_estimator": last_clf,
    }


__all__ = ["run_tier_walkforward"]
