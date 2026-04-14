from __future__ import annotations

from dataclasses import dataclass, field, fields, asdict
from typing import Any
from zoneinfo import ZoneInfo


@dataclass
class StrategyConfig:
    """策略参数配置 — 所有参数均可通过 API 动态调整。"""

    # ── 选股宇宙预筛 ──
    close_price_threshold: float = 5.0
    market_capital_low: float = 30e8
    market_capital_high: float = 160e8
    exclude_st: bool = True
    exclude_gem: bool = True          # 创业板 30x
    exclude_star: bool = True         # 科创板 688x
    universe_top_n: int = 500

    # ── 调整期形态识别 ──
    period_days: int = 20
    box_range_threshold: float = 0.18
    amp_ratio_threshold: float = 0.95
    volume_shrink_threshold: float = 0.75
    volume_recover_threshold: float = 0.85
    pre_breakout_buffer: float = 0.995
    shadow_support_threshold: float = 0.005
    chase_risk_return: float = 0.07
    chase_risk_vol_ratio: float = 2.3

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
