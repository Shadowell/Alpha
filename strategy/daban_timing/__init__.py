"""打板情绪周期择时与仓位管理（GitHub issue #32）。

分层与职责（全部为纯计算，不碰数据库 / 网络 / FastAPI）：

* :mod:`~strategy.daban_timing.config` —— 规则参数（六阶段 regime → 仓位
  系数 / 新开仓上限 / 板型限制），可 JSON 外部注入、可整体关闭（回测对照）。
* :mod:`~strategy.daban_timing.regime` —— 严格 PIT 的 regime 取数：收盘标签
  盘后才可用、盘中只用 ``intraday_*`` 估算，两者结构性隔离。
* :mod:`~strategy.daban_timing.timing` —— 当日打板计划（regime、仓位系数、
  允许/禁止条件 + 规则追溯链）。
* :mod:`~strategy.daban_timing.models` —— 持仓 / 组合 / 拟下单输入契约。
* :mod:`~strategy.daban_timing.trade_rules` —— T+1、100 股整数倍、停牌、
  涨跌停不可成交、买入金额上限等交易规则校验。
* :mod:`~strategy.daban_timing.constraints` —— 组合约束（单票 / 同题材 /
  持仓数 / 单日新开仓 / 总暴露 / 流动性）与超限缩减。
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
from .constraints import (
    ACCEPTED,
    REJECTED,
    SHRUNK,
    AllocationDecision,
    AllocationResult,
    allocate_orders,
)
from .models import (
    BUY,
    SELL,
    Holding,
    OrderRequest,
    PortfolioState,
    normalize_orders,
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
from .trade_rules import (
    QuoteSnapshot,
    TradeRuleResult,
    Violation,
    compute_price_limits,
    limit_ratio_for,
    round_to_lot,
    sellable_quantity,
    validate_order,
)

__all__ = [
    "ACCEPTED",
    "AlertLimits",
    "AllocationDecision",
    "AllocationResult",
    "BUY",
    "ConfigError",
    "CostConfig",
    "DEFAULT_REGIME_RULES",
    "Holding",
    "OrderRequest",
    "PortfolioLimits",
    "PortfolioState",
    "QuoteSnapshot",
    "REJECTED",
    "RegimeSnapshot",
    "RegimeTimingRule",
    "RiskLimits",
    "RuleHit",
    "SELL",
    "SHRUNK",
    "TIMING_PLAN_VERSION",
    "TIMING_REGIMES",
    "TimingConfig",
    "TimingPlan",
    "TradeRuleResult",
    "Violation",
    "allocate_orders",
    "build_timing_plan",
    "close_available_moment",
    "compute_price_limits",
    "limit_ratio_for",
    "load_timing_config",
    "normalize_orders",
    "parse_decision_at",
    "parse_timing_config",
    "resolve_regime",
    "round_to_lot",
    "sellable_quantity",
    "split_intraday",
    "validate_order",
]
