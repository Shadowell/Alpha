from __future__ import annotations

from dataclasses import dataclass, field, fields, asdict
from typing import Any
from zoneinfo import ZoneInfo


@dataclass
class StrategyConfig:
    """策略参数配置 — 仅保留三池盘中评分与迁池参数。"""

    # ── 盘中评分权重 ──
    score_weight_breakout: float = 35.0
    score_weight_volume: float = 25.0
    score_weight_above_vwap: float = 8.0
    score_weight_close_ge_open: float = 6.0
    score_weight_drawdown: float = 6.0
    penalty_gap_up: float = 8.0
    penalty_drawdown: float = 6.0
    penalty_near_limit: float = 6.0
    near_limit_pct: float = 9.2

    # ── 池迁移规则 ──
    focus_score_threshold: float = 60.0
    focus_score_immediate: float = 70.0
    focus_consecutive_minutes: int = 3
    buy_score_threshold: float = 78.0
    buy_breakout_price_buffer: float = 1.003
    buy_volume_ratio_threshold: float = 1.3
    buy_breakout_consecutive_minutes: int = 2
    downgrade_score_threshold: float = 65.0
    downgrade_consecutive_minutes: int = 5
    buy_pool_max_size: int = 5

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StrategyConfig:
        valid_fields = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in data.items() if k in valid_fields}
        return cls(**filtered)

    def merge(self, overrides: dict[str, Any]) -> StrategyConfig:
        current = self.to_dict()
        valid_fields = {f.name for f in fields(self.__class__)}
        for k, v in overrides.items():
            if k in valid_fields:
                current[k] = v
        return self.__class__(**current)


APP_TZ = ZoneInfo("Asia/Shanghai")
POOL_CANDIDATE = "candidate"
POOL_FOCUS = "focus"
POOL_BUY = "buy"
VALID_POOLS = {POOL_CANDIDATE, POOL_FOCUS, POOL_BUY}

TAG_COLORS = {
    1: "#ef4444",
    2: "#f97316",
    3: "#3b82f6",
}
