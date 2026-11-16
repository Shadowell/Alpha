"""#27 端到端分层晋级模型编排 + 因子评估 + 模型卡 + 门槛判定。"""

from __future__ import annotations

from typing import Any, Sequence

import pandas as pd

from strategy.first_limit_alpha.tiered_model.dataset import (
    TierDataset,
    build_all_tier_datasets,
)
from strategy.first_limit_alpha.tiered_model.explain import permutation_importance
from strategy.first_limit_alpha.tiered_model.factors import (
    compute_vif_table,
    evaluate_factor_ic,
)
from strategy.first_limit_alpha.tiered_model.model_card import build_tier_model_card
from strategy.first_limit_alpha.tiered_model.tiers import TIER_ORDER
from strategy.first_limit_alpha.tiered_model.walkforward import run_tier_walkforward


def run_tiered_model_pipeline(
    label_frame: pd.DataFrame,
    factor_frame: pd.DataFrame,
    *,
    features: Sequence[str] | None = None,
    tiers: Sequence[str] = TIER_ORDER,
    n_splits: int = 3,
    embargo_dates: int = 2,
    min_train_dates: int = 4,
    min_auc_gate: float = 0.52,
) -> dict[str, Any]:
    """运行五档分层模型完整实验流水线并返回每档指标、因子评估与模型卡。"""
    datasets: dict[str, TierDataset] = build_all_tier_datasets(
        label_frame, factor_frame, features=features, tiers=tiers
    )
    tier_reports: dict[str, Any] = {}
    model_cards: dict[str, Any] = {}

    for tier_name, ds in datasets.items():
        wf = run_tier_walkforward(
            ds,
            n_splits=n_splits,
            embargo_dates=embargo_dates,
            min_train_dates=min_train_dates,
        )
        y_col = f"y_{ds.primary_head}"
        ret_col = "fwd_ret_3d" if "fwd_ret_3d" in ds.frame.columns else y_col
        factor_ic = evaluate_factor_ic(ds.frame, ds.features, return_col=ret_col)
        vif_df = compute_vif_table(ds.frame, ds.features[:8])

        imp: list[dict[str, Any]] = []
        if wf.get("status") == "OK" and wf.get("last_estimator") is not None:
            oos_df = wf["oos_frame"]
            clf = wf["last_estimator"]
            X_oos = oos_df[list(ds.features)].apply(pd.to_numeric, errors="coerce").to_numpy()
            y_oos = pd.to_numeric(oos_df[y_col], errors="coerce").fillna(0).to_numpy()

            def _pred(x: Any, _clf: Any = clf) -> Any:
                p = _clf.predict_proba(x)
                return p[:, 1] if p.ndim == 2 else p

            imp = permutation_importance(_pred, X_oos, y_oos, ds.features)

        oos_metrics = wf.get("oos_metrics", {})
        auc_val = oos_metrics.get("auc")
        gate_ok = bool(
            wf.get("status") == "OK"
            and auc_val is not None
            and pd.notna(auc_val)
            and float(auc_val) >= min_auc_gate
        )
        dates = sorted(ds.frame["trade_date"].astype(str).unique().tolist())
        train_range = (dates[0], dates[-1]) if dates else ("", "")
        card = build_tier_model_card(
            tier_name,
            primary_head=ds.primary_head,
            train_range=train_range,
            features=ds.features,
            oos_metrics=oos_metrics,
            top_factors=imp,
            gate_passed=gate_ok,
        )
        model_cards[tier_name] = card
        tier_reports[tier_name] = {
            "status": wf.get("status"),
            "n_rows": len(ds.frame),
            "folds": wf.get("folds", []),
            "oos_metrics": oos_metrics,
            "gate_passed": gate_ok,
            "factor_ic_count": len(factor_ic),
            "vif_count": len(vif_df),
            "top_importance": imp[:5],
        }

    return {
        "tiers_evaluated": list(tier_reports.keys()),
        "tier_reports": tier_reports,
        "model_cards": model_cards,
    }


__all__ = ["run_tiered_model_pipeline"]
