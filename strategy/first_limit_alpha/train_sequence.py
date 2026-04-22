from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import torch
from torch import nn
from torch.utils.data import DataLoader, TensorDataset

from .feature_store import ensure_dir, write_json
from .schema import SequenceConfig
from .sequence_dataset import FirstLimitSequenceDatasetBuilder
from .sequence_model import FirstLimitSequenceModel


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

        dates = sorted(meta["trade_date"].astype(str).unique().tolist())
        split_idx = max(1, int(len(dates) * 0.8))
        train_dates = set(dates[:split_idx])
        test_dates = set(dates[split_idx:])
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
        report = {
            "ok": True,
            "sample_count": int(len(x)),
            "test_count": int(len(x_test)),
            "sequence_config": asdict(cfg),
            "baseline_reference": baseline_metrics or {},
            "test_score_mean": float(np.mean(score)),
            "paths": {"model": str(out_dir / "sequence_model.pt"), "meta": str(out_dir / "meta.json")},
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
