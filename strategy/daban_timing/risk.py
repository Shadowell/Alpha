"""组合回撤熔断（issue #32「组合回撤熔断：≥8% 降半仓，≥12% 停止新开仓」）。

状态机口径：

* 回撤以**历史峰值净值**为基准（``peak_equity`` 由调用方维护或由权益序列推导）；
* 触发后**粘性**保留，必须回撤修复到 ``half_recover_threshold`` /
  ``halt_recover_threshold`` 以下才逐级解除，避免在 8% 边界反复开关造成
  仓位系数抖动；
* 净值缺失 / 峰值缺失时**不改状态**，只写 ``missing``，绝不把"没有数据"
  当成"没有回撤"。
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping, Sequence
from typing import Any, Iterable

from .config import TimingConfig

LEVEL_NORMAL = "normal"
LEVEL_HALF = "half"
LEVEL_HALT = "halt"
LEVEL_ORDER = (LEVEL_NORMAL, LEVEL_HALF, LEVEL_HALT)


def drawdown(peak: float | None, equity: float | None) -> float | None:
    if peak is None or equity is None or peak <= 0:
        return None
    return max(0.0, round((peak - equity) / peak, 6))


@dataclass(frozen=True)
class BreakerState:
    """某一时点的熔断状态（可序列化、可回测重放）。"""

    level: str
    drawdown: float | None
    peak_equity: float | None
    equity: float | None
    actions: tuple[str, ...] = ()
    rule_ids: tuple[str, ...] = ()
    detail: str = ""
    trade_date: str | None = None
    missing: tuple[str, ...] = ()
    since: str | None = None

    @property
    def allow_new_positions(self) -> bool:
        return self.level != LEVEL_HALT

    @property
    def multiplier_scale(self) -> float:
        """作用于择时系数的额外缩放：半仓 0.5，停止新开仓不再缩放。"""

        return 0.5 if self.level == LEVEL_HALF else 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "drawdown": self.drawdown,
            "peak_equity": self.peak_equity,
            "equity": self.equity,
            "actions": list(self.actions),
            "rule_ids": list(self.rule_ids),
            "detail": self.detail,
            "trade_date": self.trade_date,
            "missing": list(self.missing),
            "since": self.since,
            "allow_new_positions": self.allow_new_positions,
            "multiplier_scale": self.multiplier_scale,
        }


def _level_from_drawdown(value: float, config: TimingConfig) -> str:
    risk = config.risk
    if value >= risk.drawdown_halt_threshold:
        return LEVEL_HALT
    if value >= risk.drawdown_half_threshold:
        return LEVEL_HALF
    return LEVEL_NORMAL


def evaluate_breaker(
    *,
    equity: float | None,
    peak_equity: float | None = None,
    prev_state: BreakerState | Mapping[str, Any] | None = None,
    trade_date: str = "",
    config: TimingConfig | None = None,
) -> BreakerState:
    """按当前净值与峰值判定熔断级别（带逐级解除的粘性）。"""

    config = config or TimingConfig()
    risk = config.risk
    prev: dict[str, Any] = {}
    if isinstance(prev_state, BreakerState):
        prev = prev_state.to_dict()
    elif isinstance(prev_state, Mapping):
        prev = dict(prev_state)
    prev_level = str(prev.get("level") or LEVEL_NORMAL)
    if prev_level not in LEVEL_ORDER:
        prev_level = LEVEL_NORMAL
    peak = peak_equity if peak_equity is not None else prev.get("peak_equity")
    if equity is None:
        return BreakerState(
            level=prev_level,
            drawdown=None,
            peak_equity=peak,
            equity=None,
            rule_ids=("risk:equity_missing",),
            detail="净值缺失，维持上一状态，不臆造回撤",
            trade_date=trade_date or None,
            missing=("equity_missing",),
            since=prev.get("since"),
        )
    peak_value = equity if peak is None else max(float(peak), equity)
    current_dd = drawdown(peak_value, equity)
    if current_dd is None:
        return BreakerState(
            level=prev_level,
            drawdown=None,
            peak_equity=round(peak_value, 4),
            equity=round(equity, 4),
            rule_ids=("risk:peak_invalid",),
            detail="峰值净值非正，无法计算回撤，维持上一状态",
            trade_date=trade_date or None,
            missing=("peak_equity_invalid",),
            since=prev.get("since"),
        )
    raw_level = _level_from_drawdown(current_dd, config)
    level = raw_level
    if prev_level == LEVEL_HALT:
        # 停止新开仓逐级解除：先回半仓，回撤修复到 half_recover 以下才完全恢复
        if current_dd <= risk.half_recover_threshold:
            level = LEVEL_NORMAL
        elif current_dd <= risk.halt_recover_threshold:
            level = LEVEL_HALF
        else:
            level = LEVEL_HALT
    elif prev_level == LEVEL_HALF and raw_level == LEVEL_NORMAL:
        level = LEVEL_NORMAL if current_dd <= risk.half_recover_threshold else LEVEL_HALF
    actions: list[str] = []
    rule_ids: list[str] = ["risk:breaker_normal"]
    if level == LEVEL_HALF:
        actions.append("half")
        rule_ids = ["risk:drawdown_half"]
    elif level == LEVEL_HALT:
        actions.append("halt")
        rule_ids = ["risk:drawdown_halt"]
    since = prev.get("since")
    if level != LEVEL_NORMAL and since is None:
        since = trade_date or None
    if level == LEVEL_NORMAL:
        since = None
    return BreakerState(
        level=level,
        drawdown=current_dd,
        peak_equity=round(peak_value, 4),
        equity=round(equity, 4),
        actions=tuple(actions),
        rule_ids=tuple(rule_ids),
        detail=(
            f"回撤 {current_dd:.2%}（峰值 {round(peak_value, 2)}）→ {level}"
            if level != LEVEL_NORMAL
            else f"回撤 {current_dd:.2%} 未触发熔断"
        ),
        trade_date=trade_date or None,
        since=None if since is None else str(since),
    )


def replay_breaker(
    equity_curve: Sequence[float] | Sequence[tuple[str, float]],
    *,
    config: TimingConfig | None = None,
    start_date: str = "",
) -> list[BreakerState]:
    """按时间顺序重放权益曲线，得到每日熔断状态序列（回测用）。"""

    items: list[tuple[str | None, float]] = []
    for entry in equity_curve:
        if isinstance(entry, tuple) or isinstance(entry, list):
            items.append((str(entry[0]), float(entry[1])))
        else:
            items.append((None, float(entry)))
    states: list[BreakerState] = []
    peak: float | None = None
    prev: BreakerState | None = None
    for index, (date, equity) in enumerate(items):
        peak = equity if peak is None else max(peak, equity)
        prev = evaluate_breaker(
            equity=equity,
            peak_equity=peak,
            prev_state=prev,
            trade_date=date or (start_date if index == 0 else ""),
            config=config,
        )
        states.append(prev)
    return states


def max_drawdown(equity_curve: Iterable[float]) -> float | None:
    """权益曲线的最大回撤（None 表示样本不足）。"""

    values = [float(x) for x in equity_curve if x is not None]
    if len(values) < 2:
        return None
    peak = values[0]
    worst = 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak)
    return round(worst, 6)
