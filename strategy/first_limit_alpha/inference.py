from __future__ import annotations

from pathlib import Path
from typing import Any

import joblib
import pandas as pd


class FirstLimitInferenceEngine:
    def __init__(self, artifact_path: str | Path) -> None:
        self.artifact_path = Path(artifact_path)
        self.bundle = joblib.load(self.artifact_path)
        self.features: list[str] = list(self.bundle["feature_columns"])

    def predict(self, feature_frame: pd.DataFrame) -> pd.DataFrame:
        frame = feature_frame.copy()
        if frame.empty:
            return frame
        x = frame[self.features]
        for target_name, model in self.bundle["models"].items():
            frame[f"proba_{target_name}"] = model.predict_proba(x)[:, 1]
        frame["first_limit_score"] = (
            100.0
            * (
                0.45 * frame["proba_continuation"]
                + 0.40 * frame["proba_strong_3d"]
                + 0.15 * (1.0 - frame["proba_break_risk"])
            )
        ).clip(0.0, 100.0)
        return frame.sort_values(["trade_date", "first_limit_score"], ascending=[True, False]).reset_index(drop=True)

    def metadata(self) -> dict[str, Any]:
        return dict(self.bundle.get("metadata", {}))
