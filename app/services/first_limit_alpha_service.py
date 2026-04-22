from __future__ import annotations

from pathlib import Path
import asyncio
import time
from typing import Any

import pandas as pd

from app.services.data_provider import AkshareDataProvider
from app.services.kline_store import KlineSQLiteStore
from app.services.sqlite_store import SQLiteStateStore
from app.services.time_utils import now_cn
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

DEFAULT_GRAPHIC_CONFIG: dict[str, Any] = {
    "threshold_candidate": 6.0,
    "threshold_focus": 10.0,
    "threshold_buy": 14.0,
    "max_backtrack_days": 10,
    "model_backend": "baseline",
}
GRAPHIC_STATE_KEY = "first_limit_alpha_graphic"


class FirstLimitAlphaService:
    def __init__(
        self,
        kline_store: KlineSQLiteStore,
        provider: AkshareDataProvider,
        state_store: SQLiteStateStore | None = None,
        artifact_root: str | Path = "data/first_limit_alpha",
    ) -> None:
        self.kline_store = kline_store
        self.provider = provider
        self.state_store = state_store
        self.root = Path(artifact_root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = asyncio.Lock()
        self.running = False
        self.progress: dict[str, Any] = {
            "phase": "idle",
            "current": 0,
            "total": 0,
            "detail": "",
            "started_at": None,
            "finished_at": None,
        }
        self.graphic_snapshot: dict[str, Any] = self._load_graphic_state() or {
            "trade_date": "",
            "updated_at": "",
            "config": dict(DEFAULT_GRAPHIC_CONFIG),
            "pools": {"candidate": [], "focus": [], "buy": []},
            "meta": {},
        }

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

    def _load_graphic_state(self) -> dict[str, Any] | None:
        if self.state_store is None:
            return None
        try:
            return self.state_store.get_kv(GRAPHIC_STATE_KEY)
        except Exception:
            return None

    def _save_graphic_state(self) -> None:
        if self.state_store is None:
            return
        try:
            self.state_store.set_kv(GRAPHIC_STATE_KEY, self.graphic_snapshot)
        except Exception:
            pass

    def get_status(self) -> dict[str, Any]:
        return {
            "artifact_root": str(self.root),
            "latest_dataset": str(self._latest_dir("datasets")) if self._latest_dir("datasets") else None,
            "latest_features": str(self._latest_dir("features")) if self._latest_dir("features") else None,
            "latest_model": str(self._latest_dir("models")) if self._latest_dir("models") else None,
            "latest_sequence_model": str(self._latest_dir("sequence_models")) if self._latest_dir("sequence_models") else None,
            "latest_graphic_trade_date": self.graphic_snapshot.get("trade_date"),
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
        baseline_meta: dict[str, Any] = {}
        latest_model = self._latest_dir("models")
        if latest_model and (latest_model / "meta.json").exists():
            baseline_meta = read_json(latest_model / "meta.json")
        result = FirstLimitSequenceTrainer(self.kline_store.db_path).train(
            samples,
            out_dir,
            cfg=sequence_cfg,
            baseline_metrics=baseline_meta,
        )
        result["output_dir"] = str(out_dir)
        return result

    def get_graphic_config(self) -> dict[str, Any]:
        cfg = dict(DEFAULT_GRAPHIC_CONFIG)
        cfg.update(self.graphic_snapshot.get("config", {}))
        return cfg

    def update_graphic_config(self, patch: dict[str, Any]) -> dict[str, Any]:
        cfg = self.get_graphic_config()
        for key, value in patch.items():
            if key in DEFAULT_GRAPHIC_CONFIG:
                cfg[key] = value
        self.graphic_snapshot["config"] = cfg
        self._save_graphic_state()
        return cfg

    def get_graphic_snapshot(self) -> dict[str, Any]:
        payload = dict(self.graphic_snapshot)
        payload["progress"] = dict(self.progress)
        payload["running"] = self.running
        return payload

    async def run_graphic_scan(self, trade_date: str | None = None) -> dict[str, Any]:
        if self.running:
            return self.get_graphic_snapshot()
        async with self.lock:
            self.running = True
            self.progress = {
                "phase": "prepare",
                "current": 0,
                "total": 0,
                "detail": "准备首板图形候选",
                "started_at": now_cn().isoformat(),
                "finished_at": None,
            }
            try:
                payload = await self._execute_graphic_scan(trade_date=trade_date)
                self.graphic_snapshot = payload
                self._save_graphic_state()
            finally:
                self.progress["finished_at"] = now_cn().isoformat()
                self.running = False
        return self.get_graphic_snapshot()

    async def _execute_graphic_scan(self, trade_date: str | None = None) -> dict[str, Any]:
        t0 = time.time()
        cfg = self.get_graphic_config()
        model_path = self._load_latest_model_path()
        if model_path is None:
            self.progress["error"] = "baseline model not found"
            return {
                "trade_date": "",
                "updated_at": now_cn().isoformat(),
                "config": cfg,
                "pools": {"candidate": [], "focus": [], "buy": []},
                "meta": {"error": "baseline model not found"},
            }
        trade_dates = self.kline_store.get_trade_dates_from_db()
        if not trade_dates:
            self.progress["error"] = "no trade dates found"
            return {
                "trade_date": "",
                "updated_at": now_cn().isoformat(),
                "config": cfg,
                "pools": {"candidate": [], "focus": [], "buy": []},
                "meta": {"error": "no trade dates found"},
            }
        target_dates = [trade_date] if trade_date else list(reversed(trade_dates[-int(cfg["max_backtrack_days"]) :]))
        builder = FirstLimitAlphaDataBuilder(self.kline_store.db_path, name_map=await self._name_map())
        selected_date = ""
        candidate_frame = pd.DataFrame()
        for idx, dt in enumerate(target_dates, start=1):
            self.progress.update(phase="scan", current=idx, total=len(target_dates), detail=f"扫描 {dt}")
            candidate_frame = builder.build_candidate_frame(trade_date=dt, build_cfg=SampleBuildConfig())
            if not candidate_frame.empty:
                selected_date = dt
                break
        if candidate_frame.empty:
            return {
                "trade_date": trade_date or trade_dates[-1],
                "updated_at": now_cn().isoformat(),
                "config": cfg,
                "pools": {"candidate": [], "focus": [], "buy": []},
                "meta": {
                    "entries_count": 0,
                    "trigger": "manual",
                    "elapsed_sec": round(time.time() - t0, 2),
                    "error": "no first-limit candidates found",
                },
            }
        self.progress.update(phase="feature", detail=f"生成特征 {selected_date}", current=1, total=3)
        feature_frame, feature_meta = FirstLimitFeatureBuilder(self.kline_store.db_path).transform_samples(
            candidate_frame, feature_cfg=FeatureConfig()
        )
        self.progress.update(phase="score", detail=f"模型打分 {selected_date}", current=2, total=3)
        scored = FirstLimitInferenceEngine(model_path).predict(feature_frame)
        pools = self._build_graphic_pools(scored, cfg)
        self.progress.update(phase="done", detail=f"完成 {selected_date}", current=3, total=3)
        return {
            "trade_date": selected_date,
            "updated_at": now_cn().isoformat(),
            "config": cfg,
            "pools": pools,
            "meta": {
                "entries_count": int(len(scored)),
                "feature_count": int(feature_meta.get("feature_count", 0)),
                "elapsed_sec": round(time.time() - t0, 2),
                "scanned_dates": target_dates,
                "model_backend": cfg.get("model_backend", "baseline"),
                "model_path": str(model_path),
                "thresholds": {
                    "candidate": cfg["threshold_candidate"],
                    "focus": cfg["threshold_focus"],
                    "buy": cfg["threshold_buy"],
                },
            },
        }

    @staticmethod
    def _build_graphic_pools(scored: pd.DataFrame, cfg: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
        if scored.empty:
            return {"candidate": [], "focus": [], "buy": []}
        items = scored.sort_values("first_limit_score", ascending=False).copy()
        result = {"candidate": [], "focus": [], "buy": []}
        for _, row in items.iterrows():
            payload = {
                "symbol": str(row.get("symbol", "")),
                "name": row.get("name", ""),
                "trade_date": str(row.get("trade_date", "")),
                "first_limit_score": round(float(row.get("first_limit_score", 0.0)), 4),
                "proba_continuation": round(float(row.get("proba_continuation", 0.0)), 6),
                "proba_strong_3d": round(float(row.get("proba_strong_3d", 0.0)), 6),
                "proba_break_risk": round(float(row.get("proba_break_risk", 0.0)), 6),
                "close": round(float(row.get("close", 0.0)), 4),
                "pct_change_today": round(float(row.get("pct_change_today", 0.0) * 100.0), 2),
                "consolidation_amp": round(float(row.get("consolidation_amp", 0.0) * 100.0), 2),
                "consolidation_volatility": round(float(row.get("consolidation_volatility", 0.0) * 100.0), 2),
                "volume_ratio_20d": round(float(row.get("volume_ratio_20d", 0.0)), 2),
                "distance_to_20d_high": round(float(row.get("distance_to_20d_high", 0.0) * 100.0), 2),
                "open_gap_pct": round(float(row.get("open_gap_pct", 0.0) * 100.0), 2),
                "limit_quality": round(float(row.get("limit_quality", 0.0) * 100.0), 2),
            }
            score = float(payload["first_limit_score"])
            if score >= float(cfg["threshold_buy"]):
                result["buy"].append(payload)
            elif score >= float(cfg["threshold_focus"]):
                result["focus"].append(payload)
            elif score >= float(cfg["threshold_candidate"]):
                result["candidate"].append(payload)
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
