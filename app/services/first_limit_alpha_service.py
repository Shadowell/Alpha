from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from app.services.data_provider import AkshareDataProvider
from app.services.kline_store import KlineSQLiteStore
from strategy.first_limit_alpha import (
    BacktestConfig,
    FeatureConfig,
    FirstLimitAlphaDataBuilder,
    FirstLimitBacktester,
    FirstLimitBaselineTrainer,
    FirstLimitFeatureBuilder,
    FirstLimitInferenceEngine,
    LabelConfig,
    SampleBuildConfig,
    SequenceConfig,
    TrainingConfig,
)
from strategy.first_limit_alpha.feature_store import build_version_dir, latest_child, read_dataframe, read_json, write_json
from strategy.first_limit_alpha.train_sequence import FirstLimitSequenceTrainer


class FirstLimitAlphaService:
    def __init__(
        self,
        kline_store: KlineSQLiteStore,
        provider: AkshareDataProvider,
        artifact_root: str | Path = "data/first_limit_alpha",
    ) -> None:
        self.kline_store = kline_store
        self.provider = provider
        self.root = Path(artifact_root)
        self.root.mkdir(parents=True, exist_ok=True)

    async def _name_map(self) -> dict[str, str]:
        try:
            persisted = self.kline_store.load_symbol_names()
            if persisted:
                return persisted
        except Exception:
            pass
        try:
            return await self.provider.get_symbol_name_map()
        except Exception:
            return {}

    def _latest_dir(self, category: str) -> Path | None:
        return latest_child(self.root / category)

    def get_status(self) -> dict[str, Any]:
        return {
            "artifact_root": str(self.root),
            "latest_dataset": str(self._latest_dir("datasets")) if self._latest_dir("datasets") else None,
            "latest_features": str(self._latest_dir("features")) if self._latest_dir("features") else None,
            "latest_model": str(self._latest_dir("models")) if self._latest_dir("models") else None,
            "latest_sequence_model": str(self._latest_dir("sequence_models")) if self._latest_dir("sequence_models") else None,
        }

    async def build_dataset(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        build_cfg: SampleBuildConfig | None = None,
        label_cfg: LabelConfig | None = None,
    ) -> dict[str, Any]:
        out_dir = build_version_dir(self.root, "datasets", prefix="first_limit_alpha")
        builder = FirstLimitAlphaDataBuilder(self.kline_store.db_path, name_map=await self._name_map())
        result = builder.build_dataset(
            output_dir=out_dir,
            build_cfg=build_cfg,
            label_cfg=label_cfg,
            start_date=start_date,
            end_date=end_date,
        )
        result["output_dir"] = str(out_dir)
        return result

    def _load_latest_samples(self) -> pd.DataFrame:
        latest = self._latest_dir("datasets")
        if latest is None:
            return pd.DataFrame()
        path = latest / "samples.csv"
        if not path.exists():
            return pd.DataFrame()
        return read_dataframe(path)

    def _load_latest_features(self) -> pd.DataFrame:
        latest = self._latest_dir("features")
        if latest is None:
            return pd.DataFrame()
        path = latest / "features.csv"
        if not path.exists():
            return pd.DataFrame()
        return read_dataframe(path)

    def _load_latest_model_path(self) -> Path | None:
        latest = self._latest_dir("models")
        if latest is None:
            return None
        path = latest / "model.joblib"
        return path if path.exists() else None

    def build_features(self, feature_cfg: FeatureConfig | None = None) -> dict[str, Any]:
        samples = self._load_latest_samples()
        out_dir = build_version_dir(self.root, "features", prefix="first_limit_alpha")
        result = FirstLimitFeatureBuilder(self.kline_store.db_path).build_features(samples, out_dir, feature_cfg=feature_cfg)
        result["output_dir"] = str(out_dir)
        return result

    def train_baseline(self, training_cfg: TrainingConfig | None = None) -> dict[str, Any]:
        features = self._load_latest_features()
        out_dir = build_version_dir(self.root, "models", prefix="baseline")
        result = FirstLimitBaselineTrainer().train(features, out_dir, config=training_cfg)
        result["output_dir"] = str(out_dir)
        return result

    def train_sequence(self, sequence_cfg: SequenceConfig | None = None) -> dict[str, Any]:
        samples = self._load_latest_samples()
        out_dir = build_version_dir(self.root, "sequence_models", prefix="gru")
        baseline_meta = {}
        latest_model = self._latest_dir("models")
        if latest_model and (latest_model / "meta.json").exists():
            baseline_meta = read_json(latest_model / "meta.json")
        result = FirstLimitSequenceTrainer(self.kline_store.db_path).train(
            samples,
            out_dir,
            cfg=sequence_cfg,
            baseline_metrics=baseline_meta.get("metrics", {}),
        )
        result["output_dir"] = str(out_dir)
        return result

    def run_inference(self, trade_date: str | None = None) -> dict[str, Any]:
        model_path = self._load_latest_model_path()
        if model_path is None:
            return {"ok": False, "reason": "baseline model not found"}
        features = self._load_latest_features()
        if features.empty:
            return {"ok": False, "reason": "feature frame not found"}
        if trade_date:
            features = features[features["trade_date"].astype(str) == trade_date].copy()
        else:
            latest_date = features["trade_date"].astype(str).max()
            features = features[features["trade_date"].astype(str) == latest_date].copy()
        if features.empty:
            return {"ok": False, "reason": "no features on target trade_date"}
        scored = FirstLimitInferenceEngine(model_path).predict(features)
        out_dir = build_version_dir(self.root, "reports", prefix="inference")
        scored_path = out_dir / "predictions.csv"
        scored.to_csv(scored_path, index=False)
        payload = {
            "ok": True,
            "trade_date": trade_date or str(scored["trade_date"].iloc[0]),
            "count": int(len(scored)),
            "top_items": scored.head(20)[
                [
                    "symbol",
                    "name",
                    "trade_date",
                    "first_limit_score",
                    "proba_continuation",
                    "proba_strong_3d",
                    "proba_break_risk",
                ]
            ].to_dict(orient="records"),
            "paths": {"predictions": str(scored_path)},
        }
        write_json(payload, out_dir / "meta.json")
        return payload

    def backtest_latest(self, config: BacktestConfig | None = None) -> dict[str, Any]:
        latest = self._latest_dir("models")
        if latest is None:
            return {"ok": False, "reason": "baseline model result not found"}
        pred_path = latest / "test_predictions.csv"
        if not pred_path.exists():
            return {"ok": False, "reason": "test_predictions.csv not found"}
        predictions = read_dataframe(pred_path)
        result = FirstLimitBacktester().run(predictions, config=config)
        out_dir = build_version_dir(self.root, "reports", prefix="backtest")
        write_json(result, out_dir / "backtest.json")
        result["ok"] = True
        result["paths"] = {"report": str(out_dir / "backtest.json")}
        return result
