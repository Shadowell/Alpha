"""issue #32 择时与仓位规则的可配置参数（规则不写死在代码里）。

设计约束：

* **同一配置 = 同一输出**：所有阈值/系数集中在 :class:`TimingConfig`，
  ``from_mapping`` / ``load_timing_config`` 从外部（JSON、DB、环境变量拼装）
  注入，回测与实时共用一条代码路径，保证"可回测、可关闭"。
* **关闭开关**：``enable_timing=False`` 时仓位系数恒为 1.0、不做板型限制，
  只保留组合约束；用于回测对照（择时开 / 关）。
* **可解释**：规则以六阶段 regime 为键（与 #25 ``market_regime`` 标签同名），
  每条规则携带 ``forbidden`` / ``actions`` / ``description`` 文案，输出侧
  直接引用规则 id，便于提醒追溯。
* **缺数不填 0**：regime 缺失时按 ``unknown_regime_mode`` 显式处理
  （默认 ``block``：禁止新开仓 + ``missing`` 标记），而不是把缺失当冰点。
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from app.services.market_regime import (
    ALL_REGIMES,
    REGIME_CLIMAX,
    REGIME_DIVERGE,
    REGIME_EBB,
    REGIME_FERMENT,
    REGIME_FREEZE,
    REGIME_REPAIR,
)

#: 六阶段标签直接复用 #25 真相源，避免策略层与情绪引擎口径漂移。
TIMING_REGIMES: tuple[str, ...] = tuple(ALL_REGIMES)

assert set(TIMING_REGIMES) == {
    REGIME_FREEZE,
    REGIME_REPAIR,
    REGIME_FERMENT,
    REGIME_CLIMAX,
    REGIME_DIVERGE,
    REGIME_EBB,
}, "regime 标签与 app.services.market_regime 不一致"


class ConfigError(ValueError):
    """配置非法（缺 regime、越界阈值、不可解析文件）。"""


def _finite(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be a number, got {value!r}") from exc
    if not math.isfinite(number):
        raise ConfigError(f"{name} must be finite, got {value!r}")
    return number


def _ratio(value: Any, name: str) -> float:
    number = _finite(value, name)
    if not 0.0 <= number <= 1.0:
        raise ConfigError(f"{name} must be within [0, 1], got {number}")
    return round(number, 6)


def _non_negative_int(value: Any, name: str) -> int:
    number = _finite(value, name)
    if number < 0 or not float(number).is_integer():
        raise ConfigError(f"{name} must be a non-negative integer, got {value!r}")
    return int(number)


def _optional_int(value: Any, name: str) -> int | None:
    if value is None:
        return None
    return _non_negative_int(value, name)


@dataclass(frozen=True)
class RegimeTimingRule:
    """单个 regime 的择时规则（issue #32「建议初始规则」的落地口径）。

    字段语义：

    * ``position_multiplier``：当日组合目标暴露系数（0–1），乘在
      ``PortfolioLimits.max_gross_exposure`` 上。
    * ``max_new_positions``：当日最大新开仓数。
    * ``max_position_per_stock`` / ``max_theme_concentration``：当日单票 /
      同题材权重上限（对存量超限只提示、不强平）。
    * ``max_hold_positions``：最大持仓只数（None 表示用组合默认值）。
    * ``max_board_height``：允许的最高连板高度（1 = 只做首板；None = 不限）。
    * ``ban_high_board``：是否禁止追高位板（高度 >= ``high_board_height``）。
    * ``allowed_board_types``：允许板型白名单（None 表示不限；``连板``/
      ``断板反包``/``炸板``/``一字`` 由 #26 ``layers["board_type"]`` 传入）。
    * ``required_condition``：软条件（如 ``弱转强``）；``enforce_required_
      conditions`` 打开后，缺失该标记的候选会被拒绝。
    """

    regime: str
    allow_new_positions: bool
    position_multiplier: float
    max_new_positions: int
    max_position_per_stock: float
    max_theme_concentration: float
    max_hold_positions: int | None = None
    max_board_height: int | None = None
    ban_high_board: bool = False
    allowed_board_types: tuple[str, ...] | None = None
    required_condition: str | None = None
    actions: tuple[str, ...] = ()
    forbidden: tuple[str, ...] = ()
    description: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "position_multiplier",
            _ratio(self.position_multiplier, "position_multiplier"),
        )
        object.__setattr__(
            self,
            "max_position_per_stock",
            _ratio(self.max_position_per_stock, "max_position_per_stock"),
        )
        object.__setattr__(
            self,
            "max_theme_concentration",
            _ratio(self.max_theme_concentration, "max_theme_concentration"),
        )
        object.__setattr__(
            self,
            "max_new_positions",
            _non_negative_int(self.max_new_positions, "max_new_positions"),
        )
        if self.max_hold_positions is not None and self.max_hold_positions < 0:
            raise ConfigError("max_hold_positions must be >= 0")

    @property
    def rule_id(self) -> str:
        return f"timing:{self.regime}"

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["allowed_board_types"] = (
            None if self.allowed_board_types is None else list(self.allowed_board_types)
        )
        payload["actions"] = list(self.actions)
        payload["forbidden"] = list(self.forbidden)
        payload["rule_id"] = self.rule_id
        return payload


#: 默认规则：严格照抄 issue #32 的初始口径（冰点 0.2 只做首板 / 修复 0.5 优先
#: 新题材首板 / 发酵 1.0 允许 1进2、2进3 / 高潮 0.7 禁追高位板 + 分批止盈 /
#: 分歧 0.5 只做弱转强 / 退潮 0 只卖不买 + 禁 3 板以上）。
DEFAULT_REGIME_RULES: tuple[RegimeTimingRule, ...] = (
    RegimeTimingRule(
        regime=REGIME_FREEZE,
        allow_new_positions=True,
        position_multiplier=0.20,
        max_new_positions=1,
        max_position_per_stock=0.10,
        max_theme_concentration=0.20,
        max_hold_positions=2,
        max_board_height=1,
        ban_high_board=True,
        actions=("只做首板低位",),
        forbidden=("禁止高位板", "禁止追连板"),
        description="冰点：0.2 仓位，只做首板低位，不打高位板",
    ),
    RegimeTimingRule(
        regime=REGIME_REPAIR,
        allow_new_positions=True,
        position_multiplier=0.50,
        max_new_positions=2,
        max_position_per_stock=0.15,
        max_theme_concentration=0.30,
        max_hold_positions=3,
        max_board_height=1,
        ban_high_board=True,
        actions=("优先新题材首板",),
        forbidden=("禁止追高位板",),
        description="修复：0.5 仓位，优先新题材首板",
    ),
    RegimeTimingRule(
        regime=REGIME_FERMENT,
        allow_new_positions=True,
        position_multiplier=1.00,
        max_new_positions=3,
        max_position_per_stock=0.20,
        max_theme_concentration=0.40,
        max_hold_positions=5,
        max_board_height=3,
        actions=("允许 1进2 / 2进3",),
        forbidden=(),
        description="发酵：1.0 仓位，允许 1进2 / 2进3",
    ),
    RegimeTimingRule(
        regime=REGIME_CLIMAX,
        allow_new_positions=True,
        position_multiplier=0.70,
        max_new_positions=2,
        max_position_per_stock=0.15,
        max_theme_concentration=0.30,
        max_hold_positions=4,
        max_board_height=3,
        ban_high_board=True,
        actions=("分批止盈",),
        forbidden=("禁止追高位板",),
        description="高潮：0.7 仓位，禁止追高位板，分批止盈",
    ),
    RegimeTimingRule(
        regime=REGIME_DIVERGE,
        allow_new_positions=True,
        position_multiplier=0.50,
        max_new_positions=1,
        max_position_per_stock=0.10,
        max_theme_concentration=0.20,
        max_hold_positions=3,
        max_board_height=2,
        ban_high_board=True,
        required_condition="弱转强",
        actions=("只做弱转强",),
        forbidden=("禁止追高位板", "禁止一致性加速板"),
        description="分歧：0.5 仓位，只做弱转强",
    ),
    RegimeTimingRule(
        regime=REGIME_EBB,
        allow_new_positions=False,
        position_multiplier=0.0,
        max_new_positions=0,
        max_position_per_stock=0.0,
        max_theme_concentration=0.40,
        max_hold_positions=0,
        max_board_height=2,
        actions=("只卖不买",),
        forbidden=("禁止新开仓", "禁止 3 板以上"),
        description="退潮：0 仓位，只卖不买；连板高度 3 板以上禁买",
    ),
)

#: 未知 / 缺失 regime 时的中性仓位系数（仅 ``unknown_regime_mode="neutral"`` 生效）。
NEUTRAL_MULTIPLIER_DEFAULT = 0.5


@dataclass(frozen=True)
class PortfolioLimits:
    """组合层约束（issue #32「组合约束」默认值）。"""

    max_position_per_stock: float = 0.20  # 单票上限 20%
    max_theme_concentration: float = 0.40  # 同题材上限 40%
    max_hold_positions: int = 5  # 最大持仓数
    max_new_positions_per_day: int = 3  # 单日最大新开仓数（绝对兜底）
    max_gross_exposure: float = 1.0  # 满仓口径（再乘 regime 仓位系数）
    min_cash_reserve: float = 0.0  # 保留现金比例
    liquidity_participation_rate: float = 0.05  # 单票买入额 <= 成交额 × 参与率
    high_board_height: int = 4  # 高度 >= 该值视为"高位板"
    lot_size: int = 100  # 100 股整数倍
    allow_odd_lot_sell: bool = True  # 清仓时允许卖出零股
    reject_when_board_height_unknown: bool = False  # 连板高度缺失时是否硬拒

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "max_position_per_stock",
            _ratio(self.max_position_per_stock, "max_position_per_stock"),
        )
        object.__setattr__(
            self,
            "max_theme_concentration",
            _ratio(self.max_theme_concentration, "max_theme_concentration"),
        )
        object.__setattr__(
            self,
            "max_gross_exposure",
            _ratio(self.max_gross_exposure, "max_gross_exposure"),
        )
        object.__setattr__(
            self, "min_cash_reserve", _ratio(self.min_cash_reserve, "min_cash_reserve")
        )
        object.__setattr__(
            self,
            "liquidity_participation_rate",
            _ratio(self.liquidity_participation_rate, "liquidity_participation_rate"),
        )
        object.__setattr__(
            self,
            "max_hold_positions",
            _non_negative_int(self.max_hold_positions, "max_hold_positions"),
        )
        object.__setattr__(
            self,
            "max_new_positions_per_day",
            _non_negative_int(
                self.max_new_positions_per_day, "max_new_positions_per_day"
            ),
        )
        if self.lot_size <= 0:
            raise ConfigError("lot_size must be > 0")
        if self.high_board_height < 2:
            raise ConfigError("high_board_height must be >= 2")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RiskLimits:
    """回撤熔断 + 分数软阈值（issue #32「组合回撤熔断」）。

    * 回撤 >= ``drawdown_half_threshold``（默认 8%）→ 仓位系数减半；
    * 回撤 >= ``drawdown_halt_threshold``（默认 12%）→ 停止新开仓；
    * 熔断状态**粘性**：需要回撤修复到 ``half_recover_threshold`` /
      ``halt_recover_threshold`` 以下才解除，避免在阈值附近反复开关。
    """

    drawdown_half_threshold: float = 0.08
    drawdown_halt_threshold: float = 0.12
    half_recover_threshold: float = 0.04
    halt_recover_threshold: float = 0.06
    half_multiplier: float = 0.5
    low_score_floor: float = 35.0  # 低分段减仓阈值（0–100 情绪分）
    low_score_gate_regimes: tuple[str, ...] = (REGIME_REPAIR, REGIME_DIVERGE)
    high_score_take_profit: float = 85.0  # 极高情绪分 → 触发分批止盈提示

    def __post_init__(self) -> None:
        half = _ratio(self.drawdown_half_threshold, "drawdown_half_threshold")
        halt = _ratio(self.drawdown_halt_threshold, "drawdown_halt_threshold")
        if halt < half:
            raise ConfigError(
                "drawdown_halt_threshold must be >= drawdown_half_threshold"
            )
        if _ratio(self.half_recover_threshold, "half_recover_threshold") > half:
            raise ConfigError(
                "half_recover_threshold must be <= drawdown_half_threshold"
            )
        if _ratio(self.halt_recover_threshold, "halt_recover_threshold") > halt:
            raise ConfigError(
                "halt_recover_threshold must be <= drawdown_halt_threshold"
            )
        if not 0.0 < _finite(self.half_multiplier, "half_multiplier") <= 1.0:
            raise ConfigError("half_multiplier must be in (0, 1]")
        _finite(self.low_score_floor, "low_score_floor")
        _finite(self.high_score_take_profit, "high_score_take_profit")

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["low_score_gate_regimes"] = list(self.low_score_gate_regimes)
        return payload


@dataclass(frozen=True)
class AlertLimits:
    """止损 / 止盈 / 时间止损 / 盘中提醒阈值。全部提醒 = 建议，需人工确认。"""

    stop_loss_pct: float = 0.07  # 固定止损 7%
    take_profit_pct: float = 0.15  # 固定止盈 15%
    max_hold_days: int = 2  # 时间止损：持有 N 个交易日
    trailing_stop_from_peak_pct: float = 0.08  # 高点回撤（0 关闭）
    avg_price_break_tolerance: float = 0.005  # 跌破分时均价容忍度
    seal_weakening_ratio: float = 0.5  # 封单较基准下降 >= 50%
    take_profit_steps: tuple[float, ...] = (0.10, 0.20)  # 分批止盈档位

    def __post_init__(self) -> None:
        for name in (
            "stop_loss_pct",
            "take_profit_pct",
            "trailing_stop_from_peak_pct",
            "avg_price_break_tolerance",
            "seal_weakening_ratio",
        ):
            object.__setattr__(self, name, _ratio(getattr(self, name), name))
        object.__setattr__(
            self,
            "max_hold_days",
            _non_negative_int(self.max_hold_days, "max_hold_days"),
        )
        steps = tuple(
            _ratio(step, "take_profit_step") for step in self.take_profit_steps
        )
        object.__setattr__(self, "take_profit_steps", steps)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["take_profit_steps"] = list(self.take_profit_steps)
        return payload


@dataclass(frozen=True)
class CostConfig:
    """回测成本（与 ``app.services.paper_trading`` 默认费率保持一致）。"""

    commission_rate: float = 0.00025
    min_commission: float = 5.0
    stamp_tax_rate: float = 0.0005
    slippage_rate: float = 0.001

    def __post_init__(self) -> None:
        for name in ("commission_rate", "stamp_tax_rate", "slippage_rate"):
            object.__setattr__(self, name, _ratio(getattr(self, name), name))
        if _finite(self.min_commission, "min_commission") < 0:
            raise ConfigError("min_commission must be >= 0")

    def buy_cost(self, notional: float) -> float:
        notional = max(0.0, _finite(notional, "notional"))
        return max(notional * self.commission_rate, self.min_commission)

    def sell_cost(self, notional: float) -> float:
        notional = max(0.0, _finite(notional, "notional"))
        return max(notional * self.commission_rate, self.min_commission) + notional * (
            self.stamp_tax_rate
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TimingConfig:
    """择时 + 仓位 + 风险 + 提醒 + 成本的全量可配置参数（可 JSON 往返）。"""

    enable_timing: bool = True
    enforce_required_conditions: bool = False
    unknown_regime_mode: str = "block"  # block=禁止新开仓; neutral=中性系数
    neutral_multiplier: float = NEUTRAL_MULTIPLIER_DEFAULT
    close_available_after: str = "15:05"  # 收盘 regime 标签可用时刻（东八区）
    rules_by_regime: Mapping[str, RegimeTimingRule] = field(
        default_factory=lambda: {rule.regime: rule for rule in DEFAULT_REGIME_RULES}
    )
    portfolio: PortfolioLimits = field(default_factory=PortfolioLimits)
    risk: RiskLimits = field(default_factory=RiskLimits)
    alerts: AlertLimits = field(default_factory=AlertLimits)
    costs: CostConfig = field(default_factory=CostConfig)

    def __post_init__(self) -> None:
        if self.unknown_regime_mode not in ("block", "neutral"):
            raise ConfigError('unknown_regime_mode must be "block" or "neutral"')
        object.__setattr__(
            self,
            "neutral_multiplier",
            _ratio(self.neutral_multiplier, "neutral_multiplier"),
        )
        _validate_clock(self.close_available_after)
        normalized = {str(k): v for k, v in dict(self.rules_by_regime).items()}
        unknown = set(normalized) - set(TIMING_REGIMES)
        if unknown:
            raise ConfigError(f"rules_by_regime has unknown regimes: {sorted(unknown)}")
        missing = set(TIMING_REGIMES) - set(normalized)
        if missing:
            raise ConfigError(
                f"rules_by_regime must cover all regimes, missing: {sorted(missing)}"
            )
        object.__setattr__(self, "rules_by_regime", normalized)

    def rule(self, regime: str | None) -> RegimeTimingRule | None:
        if regime is None:
            return None
        return self.rules_by_regime.get(str(regime))

    def disabled(self) -> TimingConfig:
        """关闭择时（回测对照组）：系数恒 1.0，不做板型 / 新开仓限制。"""

        flat = {
            regime: replace(
                rule,
                allow_new_positions=True,
                position_multiplier=1.0,
                max_new_positions=self.portfolio.max_new_positions_per_day,
                max_position_per_stock=self.portfolio.max_position_per_stock,
                max_theme_concentration=self.portfolio.max_theme_concentration,
                max_hold_positions=self.portfolio.max_hold_positions,
                max_board_height=None,
                ban_high_board=False,
                allowed_board_types=None,
                required_condition=None,
                actions=(),
                forbidden=(),
            )
            for regime, rule in self.rules_by_regime.items()
        }
        return replace(self, enable_timing=False, rules_by_regime=flat)

    def to_dict(self) -> dict[str, Any]:
        return {
            "enable_timing": self.enable_timing,
            "enforce_required_conditions": self.enforce_required_conditions,
            "unknown_regime_mode": self.unknown_regime_mode,
            "neutral_multiplier": self.neutral_multiplier,
            "close_available_after": self.close_available_after,
            "rules_by_regime": {
                regime: rule.to_dict() for regime, rule in self.rules_by_regime.items()
            },
            "portfolio": self.portfolio.to_dict(),
            "risk": self.risk.to_dict(),
            "alerts": self.alerts.to_dict(),
            "costs": self.costs.to_dict(),
        }


def _validate_clock(text: str) -> None:
    parts = str(text).strip().split(":")
    if len(parts) != 2:
        raise ConfigError(f"close_available_after must be HH:MM, got {text!r}")
    hour, minute = (
        _non_negative_int(parts[0], "hour"),
        _non_negative_int(parts[1], "minute"),
    )
    if hour > 23 or minute > 59:
        raise ConfigError(f"close_available_after out of range: {text!r}")


_RULE_FIELDS = tuple(RegimeTimingRule.__dataclass_fields__)  # type: ignore[attr-defined]


def _rule_from_mapping(
    payload: Mapping[str, Any], base: RegimeTimingRule
) -> RegimeTimingRule:
    """部分覆盖：未提供的字段沿用默认规则（``position_multiplier`` 之类
    不会因为漏写而被静默置 0，避免"缺数当成禁买信号"）。"""

    data = dict(payload)
    unknown = set(data) - set(_RULE_FIELDS) - {"rule_id"}
    if unknown:
        raise ConfigError(f"rule {base.regime} has unknown keys: {sorted(unknown)}")
    board_types = data.get("allowed_board_types", base.allowed_board_types)
    if board_types is not None:
        if isinstance(board_types, str) or not isinstance(board_types, Sequence):
            raise ConfigError(
                f"allowed_board_types must be a list of strings for {base.regime}"
            )
    for key in ("actions", "forbidden"):
        if key in data:
            value = data[key] or ()
            if isinstance(value, str) or not isinstance(value, Sequence):
                raise ConfigError(f"{key} must be a list of strings for {base.regime}")
            data[key] = tuple(str(item) for item in value)
    if "allow_new_positions" in data:
        data["allow_new_positions"] = bool(data["allow_new_positions"])
    if "ban_high_board" in data:
        data["ban_high_board"] = bool(data["ban_high_board"])
    for key in ("max_board_height", "max_hold_positions"):
        if key in data:
            data[key] = _optional_int(data[key], key)
    if "allowed_board_types" in data:
        data["allowed_board_types"] = (
            None if board_types is None else tuple(str(item) for item in board_types)
        )
    if data.get("required_condition") is not None:
        data["required_condition"] = str(data["required_condition"])
    data.pop("rule_id", None)
    return replace(base, **{k: v for k, v in data.items() if k in _RULE_FIELDS})


def parse_timing_config(payload: Mapping[str, Any]) -> TimingConfig:
    """从外部 dict 构造配置；未提供的键回落到默认值。

    支持部分覆盖（只改 ``rules_by_regime.发酵.position_multiplier`` 这类），
    覆盖后的六阶段完整性由 :class:`TimingConfig` 校验。
    """

    if not isinstance(payload, Mapping):
        raise ConfigError("timing config must be a mapping")
    base = TimingConfig()
    scalar_keys = (
        "enable_timing",
        "enforce_required_conditions",
        "unknown_regime_mode",
        "neutral_multiplier",
        "close_available_after",
    )
    allowed_top = set(scalar_keys) | {
        "rules_by_regime",
        "portfolio",
        "risk",
        "alerts",
        "costs",
    }
    unknown_top = set(payload) - allowed_top
    if unknown_top:
        raise ConfigError(f"timing config has unknown keys: {sorted(unknown_top)}")
    kwargs: dict[str, Any] = {}
    for name in scalar_keys:
        if name in payload:
            value = payload[name]
            if name in ("enable_timing", "enforce_required_conditions"):
                value = bool(value)
            kwargs[name] = value
    if "portfolio" in payload:
        kwargs["portfolio"] = _build_section(
            PortfolioLimits, payload["portfolio"], "portfolio"
        )
    if "risk" in payload:
        kwargs["risk"] = _build_section(RiskLimits, payload["risk"], "risk")
    if "alerts" in payload:
        kwargs["alerts"] = _build_section(AlertLimits, payload["alerts"], "alerts")
    if "costs" in payload:
        kwargs["costs"] = _build_section(CostConfig, payload["costs"], "costs")
    if "rules_by_regime" in payload:
        raw = payload["rules_by_regime"]
        if not isinstance(raw, Mapping):
            raise ConfigError("rules_by_regime must be a mapping of regime -> rule")
        merged = dict(base.rules_by_regime)
        for regime, rule_payload in raw.items():
            if regime not in TIMING_REGIMES:
                raise ConfigError(f"unknown regime in rules_by_regime: {regime!r}")
            if not isinstance(rule_payload, Mapping):
                raise ConfigError(f"rule for {regime} must be a mapping")
            merged[str(regime)] = _rule_from_mapping(rule_payload, merged[str(regime)])
        kwargs["rules_by_regime"] = merged
    return replace(base, **kwargs)


def _build_section(cls: type, payload: Any, name: str) -> Any:
    if isinstance(payload, cls):
        return payload
    if not isinstance(payload, Mapping):
        raise ConfigError(f"{name} must be a mapping")
    allowed = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
    unknown = set(payload) - allowed
    if unknown:
        raise ConfigError(f"{name} has unknown keys: {sorted(unknown)}")
    return cls(**dict(payload))


def load_timing_config(source: str | Path | Mapping[str, Any]) -> TimingConfig:
    """加载配置：dict / JSON 文件路径 / JSON 字符串三种入口。

    路径不存在时按 JSON 文本解析；读文件失败（权限等）抛出
    :class:`ConfigError`，异常信息只含路径，不回显文件内容。
    """

    if isinstance(source, Mapping):
        return parse_timing_config(source)
    text = str(source)
    raw = text
    try:
        is_file = Path(text).is_file()
    except (OSError, ValueError):
        is_file = False
    if is_file:
        try:
            raw = Path(text).read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"cannot read timing config file: {exc}") from exc
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"invalid timing config json ({type(exc).__name__})") from exc
    if not isinstance(payload, Mapping):
        raise ConfigError("timing config json must be an object")
    return parse_timing_config(payload)


DEFAULT_TIMING_CONFIG = TimingConfig()
