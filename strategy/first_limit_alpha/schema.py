from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class SampleBuildConfig:
    lookback_days: int = 20
    prior_limitup_window: int = 20
    min_history_days: int = 80
    allowed_prefixes: tuple[str, ...] = ("00", "60")
    exclude_st: bool = True
    max_consolidation_amp: float = 0.22
    max_consolidation_volatility: float = 0.04
    max_recent_spike: float = 0.085
    max_volume_cv: float = 0.9
    min_avg_amount: float = 1_000_000.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class LabelConfig:
    continuation_horizon: int = 2
    strong_horizon: int = 3
    break_horizon: int = 2
    evaluation_horizon: int = 5
    strong_threshold: float = 0.12
    break_drawdown_threshold: float = -0.07

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class FeatureConfig:
    include_market_features: bool = True
    fill_value: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class TrainingConfig:
    model_backend: str = "auto"
    random_state: int = 42
    n_estimators: int = 320
    learning_rate: float = 0.05
    num_leaves: int = 31
    min_child_samples: int = 20
    top_k: int = 5
    score_threshold: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class SequenceConfig:
    seq_len: int = 30
    hidden_size: int = 48
    batch_size: int = 64
    epochs: int = 8
    learning_rate: float = 1e-3
    dropout: float = 0.1
    random_state: int = 42

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class BacktestConfig:
    top_k: int = 5
    score_threshold: float = 0.0
    hold_days: int = 3
    take_profit: float = 0.12
    stop_loss: float = -0.07
    fee_bps: float = 8.0
    slippage_bps: float = 10.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ArtifactLayout:
    root: Path

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def datasets_dir(self) -> Path:
        return self.root / "datasets"

    @property
    def features_dir(self) -> Path:
        return self.root / "features"

    @property
    def models_dir(self) -> Path:
        return self.root / "models"

    @property
    def reports_dir(self) -> Path:
        return self.root / "reports"
