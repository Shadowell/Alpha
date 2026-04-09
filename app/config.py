from __future__ import annotations

from dataclasses import dataclass
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class StrategyConfig:
    period_days: int = 20
    close_price_threshold: float = 5.0
    market_capital_low_threshold: float = 30 * 10**8
    market_capital_up_threshold: float = 160 * 10**8
    pct_chg_limit_up_threshold: float = 9.8

    box_range_threshold: float = 0.18
    volume_shrink_threshold: float = 0.75
    pre_breakout_buffer: float = 0.995

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


APP_TZ = ZoneInfo("Asia/Shanghai")
POOL_CANDIDATE = "candidate"
POOL_FOCUS = "focus"
POOL_BUY = "buy"
VALID_POOLS = {POOL_CANDIDATE, POOL_FOCUS, POOL_BUY}

TAG_COLORS = {
    1: "#ef4444",  # red
    2: "#f97316",  # orange
    3: "#3b82f6",  # blue
}
