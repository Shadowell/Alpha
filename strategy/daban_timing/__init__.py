"""打板情绪周期择时与仓位管理（GitHub issue #32）。

分层与职责（全部为纯计算，不碰数据库 / 网络 / FastAPI）：

* :mod:`~strategy.daban_timing.config` —— 规则参数（六阶段 regime → 仓位
  系数 / 新开仓上限 / 板型限制），可 JSON 外部注入、可整体关闭（回测对照）。
* :mod:`~strategy.daban_timing.regime` —— 严格 PIT 的 regime 取数：收盘标签
  盘后才可用、盘中只用 ``intraday_*`` 估算，两者结构性隔离。
* :mod:`~strategy.daban_timing.timing` —— 当日打板计划（regime、仓位系数、
  允许/禁止条件 + 规则追溯链）。
"""

from __future__ import annotations

from .config import (
    DEFAULT_REGIME_RULES,
    TIMING_REGIMES,
    AlertLimits,
    ConfigError,
    CostConfig,
    PortfolioLimits,
    RegimeTimingRule,
    RiskLimits,
    TimingConfig,
    load_timing_config,
    parse_timing_config,
)
from .regime import (
    RegimeSnapshot,
    close_available_moment,
    parse_decision_at,
    resolve_regime,
    split_intraday,
)
from .timing import (
    TIMING_PLAN_VERSION,
    RuleHit,
    TimingPlan,
    build_timing_plan,
)

__all__ = [
    "DEFAULT_REGIME_RULES",
    "TIMING_PLAN_VERSION",
    "TIMING_REGIMES",
    "AlertLimits",
    "ConfigError",
    "CostConfig",
    "PortfolioLimits",
    "RegimeSnapshot",
    "RegimeTimingRule",
    "RiskLimits",
    "RuleHit",
    "TimingConfig",
    "TimingPlan",
    "build_timing_plan",
    "close_available_moment",
    "load_timing_config",
    "parse_decision_at",
    "parse_timing_config",
    "resolve_regime",
    "split_intraday",
]
