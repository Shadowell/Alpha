from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import average_precision_score, roc_auc_score

from .backtest import FirstLimitBacktester
from .feature_store import ensure_dir, write_dataframe, write_json
from .schema import BacktestConfig, TrainingConfig

try:
    from lightgbm import LGBMClassifier
except Exception:
    LGBMClassifier = None


TARGETS = {
    "continuation": "label_continuation",
    "strong_3d": "label_strong_3d",
    "break_risk": "label_break_risk",
}


def _safe_auc(y_true: pd.Series, y_prob: np.ndarray) -> float | None:
    if pd.Series(y_true).nunique(dropna=True) < 2:
        return None
    return float(roc_auc_score(y_true, y_prob))


def _safe_ap(y_true: pd.Series, y_prob: np.ndarray) -> float | None:
    if pd.Series(y_true).nunique(dropna=True) < 2:
        return None
    return float(average_precision_score(y_true, y_prob))


def _feature_columns(frame: pd.DataFrame) -> list[str]:
    exclude = {
        "symbol",
        "name",
        "trade_date",
        "sample_type",
        "entry_date",
        "label_continuation",
        "label_strong_3d",
        "label_break_risk",
        "entry_open",
        "future_max_high_3d",
        "future_min_low_2d",
        "future_close_3d",
        "future_close_5d",
        "ret_high_3d",
        "ret_close_3d",
        "ret_close_5d",
        "ret_min_low_2d",
        "future_limit_up_count_5d",
    }
    derived_future_cols = {f"d{day}_{field}" for day in range(1, 6) for field in ("open", "high", "low", "close")}
    exclude.update(derived_future_cols)
    return [col for col in frame.columns if col not in exclude and pd.api.types.is_numeric_dtype(frame[col])]


def _split_dates(frame: pd.DataFrame) -> dict[str, list[str]]:
    dates = sorted(frame["trade_date"].astype(str).unique().tolist())
    if len(dates) < 10:
        return {"train": dates, "valid": [], "test": dates}
    test_size = max(1, int(len(dates) * 0.15))
    valid_size = max(1, int(len(dates) * 0.15))
    test_dates = dates[-test_size:]
    valid_dates = dates[-(test_size + valid_size) : -test_size]
    train_dates = dates[: -(test_size + valid_size)]
    if not train_dates:
        train_dates = dates[: max(1, len(dates) - test_size)]
    return {"train": train_dates, "valid": valid_dates, "test": test_dates}


def _build_estimator(cfg: TrainingConfig):
    if cfg.model_backend in {"auto", "lightgbm"} and LGBMClassifier is not None:
        return LGBMClassifier(
            n_estimators=cfg.n_estimators,
            learning_rate=cfg.learning_rate,
            num_leaves=cfg.num_leaves,
            min_child_samples=cfg.min_child_samples,
            objective="binary",
            class_weight="balanced",
            random_state=cfg.random_state,
            n_jobs=4,
            verbosity=-1,
        )
    return RandomForestClassifier(
        n_estimators=max(120, cfg.n_estimators),
        max_depth=6,
        min_samples_leaf=8,
        class_weight="balanced_subsample",
        random_state=cfg.random_state,
        n_jobs=4,
    )


def _composite_score(frame: pd.DataFrame) -> pd.Series:
    continuation = frame["proba_continuation"].fillna(0.0)
    strong_3d = frame["proba_strong_3d"].fillna(0.0)
    break_risk = frame["proba_break_risk"].fillna(0.0)
    score = 100.0 * (0.45 * continuation + 0.40 * strong_3d + 0.15 * (1.0 - break_risk))
    return score.clip(0.0, 100.0)


class FirstLimitBaselineTrainer:
    def train(
        self,
        feature_frame: pd.DataFrame,
        output_dir: str | Path,
        config: TrainingConfig | None = None,
        backtest_config: BacktestConfig | None = None,
    ) -> dict[str, Any]:
        cfg = config or TrainingConfig()
        out_dir = ensure_dir(Path(output_dir))
        if feature_frame.empty:
            meta = {"ok": False, "reason": "empty feature frame", "training_config": asdict(cfg)}
            write_json(meta, out_dir / "meta.json")
            return meta

        frame = feature_frame.copy().sort_values(["trade_date", "symbol"]).reset_index(drop=True)
        features = _feature_columns(frame)
        split = _split_dates(frame)
        train_mask = frame["trade_date"].isin(split["train"])
        valid_mask = frame["trade_date"].isin(split["valid"]) if split["valid"] else pd.Series(False, index=frame.index)
        test_mask = frame["trade_date"].isin(split["test"])
        train_df = frame[train_mask].copy()
        valid_df = frame[valid_mask].copy()
        test_df = frame[test_mask].copy()
        if train_df.empty or test_df.empty:
            meta = {"ok": False, "reason": "insufficient train/test split", "training_config": asdict(cfg)}
            write_json(meta, out_dir / "meta.json")
            return meta

        model_bundle: dict[str, Any] = {"models": {}, "feature_columns": features, "targets": {}, "training_config": asdict(cfg)}
        predictions = test_df[["symbol", "name", "trade_date", "entry_open", "d1_high", "d1_low", "d1_close", "d2_high", "d2_low", "d2_close", "d3_high", "d3_low", "d3_close", "ret_close_3d", "ret_close_5d"]].copy()
        metrics: dict[str, Any] = {}
        feature_importance_rows: list[dict[str, Any]] = []

        x_train = train_df[features]
        x_valid = valid_df[features] if not valid_df.empty else None
        x_test = test_df[features]

        for target_name, label_col in TARGETS.items():
            estimator = _build_estimator(cfg)
            y_train = train_df[label_col].astype(int)
            estimator.fit(x_train, y_train)
            proba_test = estimator.predict_proba(x_test)[:, 1]
            predictions[f"proba_{target_name}"] = proba_test
            fold_metric = None
            if x_valid is not None and not valid_df.empty:
                proba_valid = estimator.predict_proba(x_valid)[:, 1]
                fold_metric = {
                    "valid_auc": _safe_auc(valid_df[label_col].astype(int), proba_valid),
                    "valid_ap": _safe_ap(valid_df[label_col].astype(int), proba_valid),
                }
            metrics[target_name] = {
                "positive_rate_train": round(float(y_train.mean()), 6),
                "positive_rate_test": round(float(test_df[label_col].astype(int).mean()), 6),
                "test_auc": _safe_auc(test_df[label_col].astype(int), proba_test),
                "test_ap": _safe_ap(test_df[label_col].astype(int), proba_test),
                "validation": fold_metric,
            }
            feature_importance = getattr(estimator, "feature_importances_", None)
            if feature_importance is not None:
                for feat, imp in zip(features, feature_importance):
                    feature_importance_rows.append({"target": target_name, "feature": feat, "importance": float(imp)})
            model_bundle["models"][target_name] = estimator
            model_bundle["targets"][target_name] = label_col

        predictions["first_limit_score"] = _composite_score(predictions)
        predictions["pred_rank"] = predictions.groupby("trade_date")["first_limit_score"].rank(method="first", ascending=False)
        predictions["label_continuation"] = test_df["label_continuation"].values
        predictions["label_strong_3d"] = test_df["label_strong_3d"].values
        predictions["label_break_risk"] = test_df["label_break_risk"].values

        backtester = FirstLimitBacktester()
        bt = backtester.run(predictions, config=backtest_config or BacktestConfig(top_k=cfg.top_k, score_threshold=cfg.score_threshold))
        feature_importance_df = pd.DataFrame(feature_importance_rows)
        if not feature_importance_df.empty:
            importance_summary = (
                feature_importance_df.groupby("feature", as_index=False)["importance"].mean().sort_values("importance", ascending=False)
            )
        else:
            importance_summary = pd.DataFrame(columns=["feature", "importance"])

        model_bundle["metadata"] = {
            "features": features,
            "metrics": metrics,
            "split": split,
            "backtest": bt["summary"],
        }
        joblib.dump(model_bundle, out_dir / "model.joblib")
        predictions_path = write_dataframe(predictions, out_dir / "test_predictions.csv")
        importance_path = write_dataframe(importance_summary, out_dir / "feature_importance.csv")
        meta = {
            "ok": True,
            "model_backend": "lightgbm" if LGBMClassifier is not None and cfg.model_backend in {"auto", "lightgbm"} else "random_forest",
            "metrics": metrics,
            "split": split,
            "features": features,
            "feature_count": len(features),
            "paths": {
                "model": str(out_dir / "model.joblib"),
                "test_predictions": str(predictions_path),
                "feature_importance": str(importance_path),
                "meta": str(out_dir / "meta.json"),
            },
            "backtest": bt,
            "training_config": asdict(cfg),
        }
        write_json(meta, out_dir / "meta.json")
        return meta
