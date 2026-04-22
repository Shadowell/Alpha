from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import torch
from torch import nn
from torch.utils.data import DataLoader, TensorDataset
from sklearn.metrics import average_precision_score, roc_auc_score

from .backtest import FirstLimitBacktester
from .feature_store import ensure_dir, write_dataframe, write_json
from .schema import BacktestConfig
from .schema import SequenceConfig
from .sequence_dataset import FirstLimitSequenceDatasetBuilder
from .sequence_model import FirstLimitSequenceModel


def _safe_auc(y_true: np.ndarray, y_prob: np.ndarray) -> float | None:
    if len(np.unique(y_true)) < 2:
        return None
    return float(roc_auc_score(y_true, y_prob))


def _safe_ap(y_true: np.ndarray, y_prob: np.ndarray) -> float | None:
    if len(np.unique(y_true)) < 2:
        return None
    return float(average_precision_score(y_true, y_prob))


def _resolve_split_dates(meta: pd.DataFrame, baseline_reference: dict[str, Any] | None) -> tuple[set[str], set[str]]:
    if baseline_reference:
        split = baseline_reference.get("split") or {}
        train_dates = {str(d) for d in split.get("train", [])}
        test_dates = {str(d) for d in split.get("test", [])}
        if train_dates and test_dates:
            return train_dates, test_dates
    dates = sorted(meta["trade_date"].astype(str).unique().tolist())
    split_idx = max(1, int(len(dates) * 0.8))
    return set(dates[:split_idx]), set(dates[split_idx:])


def _comparison(sequence_metrics: dict[str, Any], baseline_reference: dict[str, Any] | None, sequence_backtest: dict[str, Any]) -> dict[str, Any]:
    baseline_reference = baseline_reference or {}
    baseline_metrics = baseline_reference.get("metrics") or {}
    baseline_backtest = ((baseline_reference.get("backtest") or {}).get("summary")) or {}
    out: dict[str, Any] = {"targets": {}, "backtest": {}}
    for target, metrics in sequence_metrics.items():
        baseline_target = baseline_metrics.get(target) or {}
        out["targets"][target] = {
            "sequence_test_auc": metrics.get("test_auc"),
            "baseline_test_auc": baseline_target.get("test_auc"),
            "delta_test_auc": (
                None
                if metrics.get("test_auc") is None or baseline_target.get("test_auc") is None
                else round(float(metrics["test_auc"] - baseline_target["test_auc"]), 6)
            ),
            "sequence_test_ap": metrics.get("test_ap"),
            "baseline_test_ap": baseline_target.get("test_ap"),
            "delta_test_ap": (
                None
                if metrics.get("test_ap") is None or baseline_target.get("test_ap") is None
                else round(float(metrics["test_ap"] - baseline_target["test_ap"]), 6)
            ),
        }
    out["backtest"] = {
        "sequence_cum_return": sequence_backtest.get("summary", {}).get("cum_return"),
        "baseline_cum_return": baseline_backtest.get("cum_return"),
        "delta_cum_return": (
            None
            if sequence_backtest.get("summary", {}).get("cum_return") is None or baseline_backtest.get("cum_return") is None
            else round(float(sequence_backtest["summary"]["cum_return"] - baseline_backtest["cum_return"]), 6)
        ),
        "sequence_win_rate": sequence_backtest.get("summary", {}).get("win_rate"),
        "baseline_win_rate": baseline_backtest.get("win_rate"),
        "sequence_max_drawdown": sequence_backtest.get("summary", {}).get("max_drawdown"),
        "baseline_max_drawdown": baseline_backtest.get("max_drawdown"),
    }
    return out


class FirstLimitSequenceTrainer:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    def train(
        self,
        samples: pd.DataFrame,
        output_dir: str | Path,
        cfg: SequenceConfig | None = None,
        baseline_metrics: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        cfg = cfg or SequenceConfig()
        out_dir = ensure_dir(Path(output_dir))
        torch.set_num_threads(1)
        dataset = FirstLimitSequenceDatasetBuilder(self.db_path).build(samples, cfg=cfg)
        x = dataset["x"]
        y = dataset["y"]
        meta = dataset["meta"]
        if len(x) < 32:
            payload = {
                "ok": False,
                "reason": "sequence samples too small",
                "sequence_config": asdict(cfg),
                "sample_count": int(len(x)),
            }
            write_json(payload, out_dir / "meta.json")
            return payload

        meta = meta.copy()
        meta["symbol"] = meta["symbol"].astype(str)
        meta["trade_date"] = meta["trade_date"].astype(str)
        samples = samples.copy()
        samples["symbol"] = samples["symbol"].astype(str)
        samples["trade_date"] = samples["trade_date"].astype(str)
        train_dates, test_dates = _resolve_split_dates(meta, baseline_metrics)
        train_mask = meta["trade_date"].isin(train_dates).to_numpy()
        test_mask = meta["trade_date"].isin(test_dates).to_numpy()
        if train_mask.sum() == 0 or test_mask.sum() == 0:
            train_dates, test_dates = _resolve_split_dates(meta, None)
            train_mask = meta["trade_date"].isin(train_dates).to_numpy()
            test_mask = meta["trade_date"].isin(test_dates).to_numpy()
        x_train = torch.tensor(x[train_mask], dtype=torch.float32)
        y_train = torch.tensor(y[train_mask], dtype=torch.float32)
        x_test = torch.tensor(x[test_mask], dtype=torch.float32)
        y_test = torch.tensor(y[test_mask], dtype=torch.float32)

        loader = DataLoader(TensorDataset(x_train, y_train), batch_size=cfg.batch_size, shuffle=False)
        model = FirstLimitSequenceModel(input_size=x.shape[-1], hidden_size=cfg.hidden_size, dropout=cfg.dropout)
        optimizer = torch.optim.Adam(model.parameters(), lr=cfg.learning_rate)
        criterion = nn.BCEWithLogitsLoss()

        model.train()
        for _ in range(cfg.epochs):
            for xb, yb in loader:
                optimizer.zero_grad()
                out = model(xb)
                loss = (
                    criterion(out["continuation"], yb[:, 0])
                    + criterion(out["strong_3d"], yb[:, 1])
                    + criterion(out["break_risk"], yb[:, 2])
                )
                loss.backward()
                optimizer.step()

        model.eval()
        with torch.no_grad():
            pred = model(x_test)
            cont = torch.sigmoid(pred["continuation"]).cpu().numpy()
            strong = torch.sigmoid(pred["strong_3d"]).cpu().numpy()
            risk = torch.sigmoid(pred["break_risk"]).cpu().numpy()
        score = 100.0 * (0.45 * cont + 0.40 * strong + 0.15 * (1.0 - risk))
        y_test_np = y_test.cpu().numpy()
        metrics = {
            "continuation": {
                "positive_rate_test": round(float(np.mean(y_test_np[:, 0])), 6),
                "test_auc": _safe_auc(y_test_np[:, 0], cont),
                "test_ap": _safe_ap(y_test_np[:, 0], cont),
            },
            "strong_3d": {
                "positive_rate_test": round(float(np.mean(y_test_np[:, 1])), 6),
                "test_auc": _safe_auc(y_test_np[:, 1], strong),
                "test_ap": _safe_ap(y_test_np[:, 1], strong),
            },
            "break_risk": {
                "positive_rate_test": round(float(np.mean(y_test_np[:, 2])), 6),
                "test_auc": _safe_auc(y_test_np[:, 2], risk),
                "test_ap": _safe_ap(y_test_np[:, 2], risk),
            },
        }
        test_meta = meta.loc[test_mask].reset_index(drop=True)
        predictions = test_meta.copy()
        predictions["proba_continuation"] = cont
        predictions["proba_strong_3d"] = strong
        predictions["proba_break_risk"] = risk
        predictions["first_limit_score"] = score
        predictions = predictions.merge(
            samples[
                [
                    "symbol",
                    "trade_date",
                    "name",
                    "entry_open",
                    "label_continuation",
                    "label_strong_3d",
                    "label_break_risk",
                    "ret_close_3d",
                    "ret_close_5d",
                    "d1_high",
                    "d1_low",
                    "d1_close",
                    "d2_high",
                    "d2_low",
                    "d2_close",
                    "d3_high",
                    "d3_low",
                    "d3_close",
                ]
            ],
            on=["symbol", "trade_date"],
            how="left",
        )
        predictions["pred_rank"] = predictions.groupby("trade_date")["first_limit_score"].rank(method="first", ascending=False)
        prediction_path = write_dataframe(predictions, out_dir / "test_predictions.csv")
        backtest = FirstLimitBacktester().run(
            predictions,
            config=BacktestConfig(top_k=5, score_threshold=0.0),
        )
        compare = _comparison(metrics, baseline_metrics, backtest)
        report = {
            "ok": True,
            "sample_count": int(len(x)),
            "test_count": int(len(x_test)),
            "sequence_config": asdict(cfg),
            "baseline_reference": baseline_metrics or {},
            "split": {
                "train": sorted(train_dates),
                "test": sorted(test_dates),
            },
            "metrics": metrics,
            "test_score_mean": float(np.mean(score)),
            "backtest": backtest,
            "comparison_vs_baseline": compare,
            "paths": {
                "model": str(out_dir / "sequence_model.pt"),
                "meta": str(out_dir / "meta.json"),
                "test_predictions": str(prediction_path),
            },
        }
        torch.save(
            {
                "state_dict": model.state_dict(),
                "input_size": int(x.shape[-1]),
                "sequence_config": asdict(cfg),
                "feature_names": dataset["feature_names"],
            },
            out_dir / "sequence_model.pt",
        )
        write_json(report, out_dir / "meta.json")
        return report
