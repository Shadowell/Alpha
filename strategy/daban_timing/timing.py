"""每日打板择时计划：regime → 仓位系数 / 新开仓上限 / 禁买条件（issue #32）。

输出字段严格对齐 issue 的「择时规则 → 输出」清单：
``allow_new_positions``、``position_multiplier``、``max_new_positions``、
``max_position_per_stock``、``max_theme_concentration``，外加可追溯的
``rule_ids`` / ``forbidden`` / ``actions`` / ``missing``。

可解释性约定：

* 每个会改变系数的动作都追加一条 ``RuleHit(rule_id, detail, before, after)``，
  最终输出 ``trace``；系数怎么从 1.0 变成 0.35 的，逐条可查。
* regime 缺失时 ``position_multiplier`` 返回 ``None``（显式缺失）而不是 0，
  由上层按 ``unknown_regime_mode`` 决定 block / neutral，避免"缺失 == 空仓"
  这种不可解释的收益崩塌。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from .config import (
    REGIME_CLIMAX,
    TimingConfig,
)
from .regime import RegimeSnapshot

TIMING_PLAN_VERSION = "daban_timing/plan/v1"


@dataclass(frozen=True)
class RuleHit:
    """一条被触发的规则（用于提醒/计划的追溯链）。"""

    rule_id: str
    detail: str
    before: float | None = None
    after: float | None = None
    at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "detail": self.detail,
            "before": self.before,
            "after": self.after,
            "at": self.at,
        }


@dataclass(frozen=True)
class TimingPlan:
    """当日打板计划（择时层输出，组合约束层的输入）。"""

    trade_date: str
    regime: str | None
    regime_source: str
    sentiment_score: float | None
    allow_new_positions: bool
    position_multiplier: float | None
    max_new_positions: int
    max_position_per_stock: float
    max_theme_concentration: float
    max_hold_positions: int
    max_board_height: int | None
    high_board_height: int
    ban_high_board: bool
    allowed_board_types: tuple[str, ...] | None
    required_condition: str | None
    forbidden: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    rule_ids: tuple[str, ...] = ()
    trace: tuple[RuleHit, ...] = ()
    missing: tuple[str, ...] = ()
    decision_at: str | None = None
    enable_timing: bool = True
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def target_gross_exposure(self) -> float | None:
        """目标总暴露 = 组合满仓口径 × 当日仓位系数（None=系数缺失）。"""

        if self.position_multiplier is None:
            return None
        return round(self.position_multiplier, 6)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": TIMING_PLAN_VERSION,
            "trade_date": self.trade_date,
            "regime": self.regime,
            "regime_source": self.regime_source,
            "sentiment_score": self.sentiment_score,
            "allow_new_positions": self.allow_new_positions,
            "position_multiplier": self.position_multiplier,
            "target_gross_exposure": self.target_gross_exposure,
            "max_new_positions": self.max_new_positions,
            "max_position_per_stock": self.max_position_per_stock,
            "max_theme_concentration": self.max_theme_concentration,
            "max_hold_positions": self.max_hold_positions,
            "max_board_height": self.max_board_height,
            "high_board_height": self.high_board_height,
            "ban_high_board": self.ban_high_board,
            "allowed_board_types": (
                None
                if self.allowed_board_types is None
                else list(self.allowed_board_types)
            ),
            "required_condition": self.required_condition,
            "forbidden": list(self.forbidden),
            "actions": list(self.actions),
            "rule_ids": list(self.rule_ids),
            "trace": [hit.to_dict() for hit in self.trace],
            "missing": list(self.missing),
            "decision_at": self.decision_at,
            "enable_timing": self.enable_timing,
            "extra": dict(self.extra),
        }


def _round(value: float) -> float:
    return round(value, 6)


def _blocked_plan(
    snapshot: RegimeSnapshot,
    config: TimingConfig,
    *,
    missing_reasons: Sequence[str],
) -> TimingPlan:
    """regime 不可解析时的计划：显式缺失，不伪造仓位系数。"""

    limits = config.portfolio
    neutral = config.unknown_regime_mode == "neutral"
    hits = [
        RuleHit(
            rule_id="timing:unknown_regime",
            detail=(
                f"regime 缺失（{'/'.join(missing_reasons)}）→ "
                + ("按中性系数处理" if neutral else "禁止新开仓")
            ),
            before=None,
            after=_round(config.neutral_multiplier) if neutral else None,
            at=snapshot.decision_at,
        )
    ]
    multiplier = _round(config.neutral_multiplier) if neutral else None
    if not config.enable_timing:
        multiplier = 1.0
    return TimingPlan(
        trade_date=snapshot.trade_date,
        regime=None,
        regime_source=snapshot.source,
        sentiment_score=snapshot.score,
        allow_new_positions=neutral or not config.enable_timing,
        position_multiplier=multiplier,
        max_new_positions=limits.max_new_positions_per_day if neutral else 0,
        max_position_per_stock=limits.max_position_per_stock,
        max_theme_concentration=limits.max_theme_concentration,
        max_hold_positions=limits.max_hold_positions,
        max_board_height=None,
        high_board_height=limits.high_board_height,
        ban_high_board=False,
        allowed_board_types=None,
        required_condition=None,
        forbidden=() if neutral else ("禁止新开仓（regime 缺失）",),
        actions=() if neutral else ("等待盘后情绪标签",),
        rule_ids=tuple(hit.rule_id for hit in hits),
        trace=tuple(hits),
        missing=tuple(missing_reasons),
        decision_at=snapshot.decision_at,
        enable_timing=config.enable_timing,
    )


def build_timing_plan(
    snapshot: RegimeSnapshot,
    config: TimingConfig | None = None,
    *,
    breaker_actions: Sequence[str] = (),
) -> TimingPlan:
    """把 :class:`RegimeSnapshot` 翻译成当日打板计划。

    ``breaker_actions`` 由 :mod:`strategy.daban_timing.risk` 传入回撤熔断动作
    （``half`` / ``halt``），在这里统一收敛成最终系数，保证"计划 = 单一真相源"。
    """

    config = config or TimingConfig()
    limits = config.portfolio
    if not snapshot.is_resolved:
        reasons = tuple(
            dict.fromkeys(tuple(snapshot.missing) or ("regime_unresolved",))
        )
        return _blocked_plan(snapshot, config, missing_reasons=reasons)

    rule = config.rule(snapshot.regime)
    if rule is None:  # pragma: no cover - 配置校验已保证全覆盖
        return _blocked_plan(
            snapshot, config, missing_reasons=(f"unknown_regime:{snapshot.regime}",)
        )

    hits: list[RuleHit] = [
        RuleHit(
            rule_id=rule.rule_id,
            detail=rule.description
            or f"{rule.regime}: 仓位系数 {rule.position_multiplier}",
            before=None,
            after=_round(rule.position_multiplier),
            at=snapshot.decision_at,
        )
    ]
    multiplier = _round(rule.position_multiplier)
    # 择时关闭（回测对照组）时不做 regime 层面的禁买，只保留组合约束。
    allow_new = bool(rule.allow_new_positions) if config.enable_timing else True
    max_new = (
        int(rule.max_new_positions)
        if config.enable_timing
        else limits.max_new_positions_per_day
    )
    missing = list(snapshot.missing)
    if not config.enable_timing:
        hits.append(
            RuleHit(
                rule_id="timing:disabled",
                detail="择时开关关闭（回测对照组）：仓位系数恒为 1.0，不限板型",
                before=multiplier,
                after=1.0,
                at=snapshot.decision_at,
            )
        )
        multiplier = 1.0

    # ── 情绪分软阈值（连续输入参与决策，保证 score 不只是 regime 的附属）──
    score = snapshot.score
    risk = config.risk
    if score is None:
        missing.append("sentiment_score_missing")
    else:
        if (
            config.enable_timing
            and rule.regime in set(risk.low_score_gate_regimes)
            and score < risk.low_score_floor
        ):
            before = multiplier
            multiplier = _round(multiplier * 0.5)
            hits.append(
                RuleHit(
                    rule_id="timing:low_score_gate",
                    detail=(
                        f"{rule.regime} 但情绪分 {score} < {risk.low_score_floor} → 再减半"
                    ),
                    before=before,
                    after=multiplier,
                    at=snapshot.decision_at,
                )
            )
        if (
            config.enable_timing
            and rule.regime == REGIME_CLIMAX
            and score >= risk.high_score_take_profit
        ):
            hits.append(
                RuleHit(
                    rule_id="timing:high_score_take_profit",
                    detail=f"情绪分 {score} >= {risk.high_score_take_profit} → 分批止盈提示",
                    before=multiplier,
                    after=multiplier,
                    at=snapshot.decision_at,
                )
            )

    # ── 回撤熔断（组合层）──
    for action in breaker_actions:
        token = str(action).strip().lower()
        if token == "half" and multiplier is not None:
            before = multiplier
            multiplier = _round(multiplier * risk.half_multiplier)
            hits.append(
                RuleHit(
                    rule_id="risk:drawdown_half",
                    detail=f"回撤熔断（半仓）：系数 ×{risk.half_multiplier}",
                    before=before,
                    after=multiplier,
                    at=snapshot.decision_at,
                )
            )
        elif token == "halt":
            before = multiplier
            allow_new = False
            max_new = 0
            hits.append(
                RuleHit(
                    rule_id="risk:drawdown_halt",
                    detail="回撤熔断（停止新开仓）",
                    before=before,
                    after=multiplier,
                    at=snapshot.decision_at,
                )
            )

    forbidden = list(rule.forbidden)
    if rule.max_board_height is not None:
        forbidden.append(f"禁止买入 {rule.max_board_height + 1} 板及以上")
    if rule.ban_high_board:
        forbidden.append(f"禁止追高位板（>= {limits.high_board_height} 板）")
    if not allow_new and not forbidden:
        forbidden.append("禁止新开仓")
    actions = list(rule.actions)
    if any(hit.rule_id == "timing:high_score_take_profit" for hit in hits):
        steps = "/".join(f"{step:.0%}" for step in config.alerts.take_profit_steps)
        actions.append(f"分批止盈（档位 {steps}）")
    if multiplier == 0.0 and "只卖不买" not in actions and allow_new is False:
        actions.append("只卖不买")

    return TimingPlan(
        trade_date=snapshot.trade_date,
        regime=rule.regime,
        regime_source=snapshot.source,
        sentiment_score=score,
        allow_new_positions=allow_new and multiplier is not None and multiplier > 0.0,
        position_multiplier=multiplier,
        max_new_positions=max_new if allow_new else 0,
        max_position_per_stock=_round(
            rule.max_position_per_stock
            if config.enable_timing
            else limits.max_position_per_stock
        ),
        max_theme_concentration=_round(
            rule.max_theme_concentration
            if config.enable_timing
            else limits.max_theme_concentration
        ),
        max_hold_positions=(
            rule.max_hold_positions
            if config.enable_timing and rule.max_hold_positions is not None
            else limits.max_hold_positions
        ),
        max_board_height=rule.max_board_height if config.enable_timing else None,
        high_board_height=limits.high_board_height,
        ban_high_board=bool(rule.ban_high_board) and config.enable_timing,
        allowed_board_types=(
            rule.allowed_board_types if config.enable_timing else None
        ),
        required_condition=rule.required_condition if config.enable_timing else None,
        forbidden=tuple(dict.fromkeys(forbidden)),
        actions=tuple(dict.fromkeys(actions)),
        rule_ids=tuple(hit.rule_id for hit in hits),
        trace=tuple(hits),
        missing=tuple(dict.fromkeys(missing)),
        decision_at=snapshot.decision_at,
        enable_timing=config.enable_timing,
        extra={
            "rule": rule.to_dict(),
            "regime_source_available": snapshot.available,
            "breaker_actions": list(breaker_actions),
            "unknown_regime_mode": config.unknown_regime_mode,
        },
    )
