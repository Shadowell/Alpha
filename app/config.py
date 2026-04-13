from __future__ import annotations

from dataclasses import dataclass, field, fields, asdict
from typing import Any
from zoneinfo import ZoneInfo


@dataclass
class StrategyConfig:
    """规则引擎配置 — 所有参数均可通过 API 动态调整。"""

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


# ── 规则分组元信息（用于前端渲染） ──
RULE_GROUPS: list[dict[str, Any]] = [
    {
        "key": "universe",
        "label": "选股宇宙",
        "description": "预筛条件，过滤哪些股票进入分析范围",
        "fields": [
            {"key": "close_price_threshold", "label": "最低股价", "type": "number", "step": 0.5, "min": 0, "unit": "元"},
            {"key": "market_capital_low", "label": "最低市值", "type": "number", "step": 1e8, "min": 0, "unit": "元", "display_divisor": 1e8, "display_unit": "亿"},
            {"key": "market_capital_high", "label": "最高市值", "type": "number", "step": 1e8, "min": 0, "unit": "元", "display_divisor": 1e8, "display_unit": "亿"},
            {"key": "exclude_st", "label": "排除 ST", "type": "boolean"},
            {"key": "exclude_gem", "label": "排除创业板", "type": "boolean"},
            {"key": "exclude_star", "label": "排除科创板", "type": "boolean"},
            {"key": "universe_top_n", "label": "成交额 Top N", "type": "number", "step": 50, "min": 100, "max": 2000},
        ],
    },
    {
        "key": "pattern",
        "label": "形态识别",
        "description": "调整期缩量横盘突破形态的判定参数",
        "fields": [
            {"key": "period_days", "label": "回看天数", "type": "number", "step": 1, "min": 5, "max": 60},
            {"key": "box_range_threshold", "label": "箱体振幅上限", "type": "number", "step": 0.01, "min": 0.01, "max": 1.0},
            {"key": "amp_ratio_threshold", "label": "近5日振幅比上限", "type": "number", "step": 0.05, "min": 0.1, "max": 2.0},
            {"key": "volume_shrink_threshold", "label": "缩量比上限", "type": "number", "step": 0.05, "min": 0.1, "max": 2.0},
            {"key": "volume_recover_threshold", "label": "量能恢复比下限", "type": "number", "step": 0.05, "min": 0.1, "max": 2.0},
            {"key": "pre_breakout_buffer", "label": "突破位缓冲系数", "type": "number", "step": 0.001, "min": 0.9, "max": 1.0},
            {"key": "shadow_support_threshold", "label": "下影线支撑比", "type": "number", "step": 0.001, "min": 0},
            {"key": "chase_risk_return", "label": "追高涨幅阈值", "type": "number", "step": 0.01, "min": 0, "max": 0.2},
            {"key": "chase_risk_vol_ratio", "label": "追高量比阈值", "type": "number", "step": 0.1, "min": 0},
        ],
    },
    {
        "key": "scoring",
        "label": "评分权重",
        "description": "盘中实时评分各维度的权重和惩罚力度",
        "fields": [
            {"key": "score_weight_breakout", "label": "突破强度权重", "type": "number", "step": 1, "min": 0, "max": 100},
            {"key": "score_weight_volume", "label": "量能质量权重", "type": "number", "step": 1, "min": 0, "max": 100},
            {"key": "score_weight_above_vwap", "label": "高于VWAP权重", "type": "number", "step": 1, "min": 0, "max": 50},
            {"key": "score_weight_close_ge_open", "label": "收>=开权重", "type": "number", "step": 1, "min": 0, "max": 50},
            {"key": "score_weight_drawdown", "label": "回撤控制权重", "type": "number", "step": 1, "min": 0, "max": 50},
            {"key": "penalty_gap_up", "label": "高开惩罚力度", "type": "number", "step": 1, "min": 0, "max": 50},
            {"key": "penalty_drawdown", "label": "冲高回落惩罚", "type": "number", "step": 1, "min": 0, "max": 50},
            {"key": "penalty_near_limit", "label": "接近涨停惩罚", "type": "number", "step": 1, "min": 0, "max": 50},
            {"key": "near_limit_pct", "label": "涨停判定涨幅", "type": "number", "step": 0.1, "min": 5, "max": 20, "unit": "%"},
        ],
    },
    {
        "key": "transition",
        "label": "池迁移规则",
        "description": "股票在候选/重点/买入池之间自动迁移的阈值",
        "fields": [
            {"key": "focus_score_threshold", "label": "进入重点池分数", "type": "number", "step": 1, "min": 0, "max": 100},
            {"key": "focus_score_immediate", "label": "立即进重点池分数", "type": "number", "step": 1, "min": 0, "max": 100},
            {"key": "focus_consecutive_minutes", "label": "重点池连续确认(分钟)", "type": "number", "step": 1, "min": 1, "max": 30},
            {"key": "buy_score_threshold", "label": "进入买入池分数", "type": "number", "step": 1, "min": 0, "max": 100},
            {"key": "buy_breakout_price_buffer", "label": "买入突破缓冲系数", "type": "number", "step": 0.001, "min": 1.0, "max": 1.1},
            {"key": "buy_volume_ratio_threshold", "label": "买入量比阈值", "type": "number", "step": 0.1, "min": 0},
            {"key": "buy_breakout_consecutive_minutes", "label": "买入连续确认(分钟)", "type": "number", "step": 1, "min": 1, "max": 30},
            {"key": "downgrade_score_threshold", "label": "降级分数阈值", "type": "number", "step": 1, "min": 0, "max": 100},
            {"key": "downgrade_consecutive_minutes", "label": "降级连续确认(分钟)", "type": "number", "step": 1, "min": 1, "max": 60},
            {"key": "buy_pool_max_size", "label": "买入池最大数量", "type": "number", "step": 1, "min": 1, "max": 20},
        ],
    },
]


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
