"""组合约束与仓位缩减（issue #32「组合约束」）。

把 :class:`~strategy.daban_timing.timing.TimingPlan` 的目标暴露翻译成
**每笔可下单订单**：按优先级排队，逐条受 单票上限 / 同题材上限 / 最大持仓数 /
单日新开仓数 / 总暴露 / 现金 / 流动性参与率 / 手数取整 约束，超限时
**缩减（shrunk）**或**拒绝（rejected）**，并把每条命中的规则 id 记进决策，
满足"超限时拒绝或缩减"+"提醒可追溯"两项验收。

口径说明：

* 上限都作用在**目标新增权重**上，存量超限只进 ``over_cap`` 提示（不强平），
  避免"规则变化即被动卖出"造成不可解释的回撤。
* 缺数项（现金 / 成交额 / 题材 / 连板高度）默认跳过该项硬约束并写入
  ``missing``，不填 0；只有 ``PortfolioLimits`` 能确定的才拦。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Iterable

from .config import TimingConfig
from .models import (
    UNKNOWN_THEME,
    Holding,
    OrderRequest,
    PortfolioState,
    normalize_orders,
    round4,
)
from .timing import TimingPlan
from .trade_rules import QuoteSnapshot, round_to_lot, validate_order

ACCEPTED = "accepted"
REJECTED = "rejected"
SHRUNK = "shrunk"


@dataclass(frozen=True)
class AllocationDecision:
    """单笔订单的约束判定结果（含缩减前后对比，便于复盘）。"""

    symbol: str
    side: str
    action: str
    requested_amount: float | None
    final_amount: float | None
    requested_qty: int | None
    final_qty: int | None
    price: float | None
    weight_after: float | None
    name: str = ""
    theme: str | None = None
    board_height: int | None = None
    priority: float = 0.0
    rule_ids: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    caps: dict[str, float | None] = field(default_factory=dict)

    @property
    def accepted(self) -> bool:
        return self.action in (ACCEPTED, SHRUNK)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "theme": self.theme,
            "board_height": self.board_height,
            "priority": self.priority,
            "side": self.side,
            "action": self.action,
            "requested_amount": round4(self.requested_amount),
            "final_amount": round4(self.final_amount),
            "requested_qty": self.requested_qty,
            "final_qty": self.final_qty,
            "price": self.price,
            "weight_after": self.weight_after,
            "rule_ids": list(self.rule_ids),
            "reasons": list(self.reasons),
            "missing": list(self.missing),
            "caps": {k: round4(v) for k, v in self.caps.items()},
        }


@dataclass(frozen=True)
class AllocationResult:
    trade_date: str
    decisions: tuple[AllocationDecision, ...] = ()
    accepted_orders: tuple[OrderRequest, ...] = ()
    over_cap: tuple[dict[str, Any], ...] = ()
    summary: dict[str, Any] = field(default_factory=dict)
    missing: tuple[str, ...] = ()

    def decision(
        self, symbol: str, side: str | None = None
    ) -> AllocationDecision | None:
        for item in self.decisions:
            if item.symbol == symbol and (side is None or item.side == side):
                return item
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "trade_date": self.trade_date,
            "decisions": [item.to_dict() for item in self.decisions],
            "over_cap": [dict(item) for item in self.over_cap],
            "summary": dict(self.summary),
            "missing": list(self.missing),
        }


def _limit_amount(
    equity: float, weight_cap: float | None, current_weight: float | None
) -> float | None:
    """给定权重上限与已有权重，返回还能新增的金额（None 表示无法评估）。"""

    if weight_cap is None or equity <= 0:
        return None
    used = 0.0 if current_weight is None else max(0.0, current_weight)
    return max(0.0, (weight_cap - used) * equity)


def _plan_target_amount(
    plan: TimingPlan, config: TimingConfig, equity: float, gross_before: float | None
) -> float | None:
    """当日允许的最大总仓位金额（含存量，按仓位系数 × 满仓口径折算）。

    系数缺失（regime 未解析）或净值非正时返回 None，由调用方标 ``missing``，
    绝不按"0 仓位"臆造一个拒单理由。
    """

    if plan.position_multiplier is None or equity <= 0:
        return None
    cap = plan.position_multiplier * config.portfolio.max_gross_exposure
    return max(0.0, (cap - (gross_before or 0.0)) * equity)


def allocate_orders(
    orders: Iterable[Any] | Any,
    state: PortfolioState | Mapping[str, Any],
    plan: TimingPlan,
    *,
    config: TimingConfig | None = None,
    quotes: Mapping[str, QuoteSnapshot | Mapping[str, Any]] | None = None,
    enforce_trade_rules: bool = True,
    allow_limit_up_queue_buy: bool = False,
) -> AllocationResult:
    """把候选订单按组合约束落成可执行清单。"""

    config = config or TimingConfig()
    limits = config.portfolio
    if isinstance(state, Mapping):
        state = PortfolioState.from_mapping(state)
    equity = state.equity
    gross_before = state.gross_exposure
    target_amount = _plan_target_amount(plan, config, equity, gross_before)
    theme_weights = state.theme_weights()

    decisions: list[AllocationDecision] = []
    accepted: list[OrderRequest] = []
    missing_global: list[str] = list(state.missing)
    new_symbols = {item.symbol for item in state.holdings if (item.value() or 0) > 0}
    new_used = int(state.new_positions_today or 0)
    max_new = min(plan.max_new_positions, limits.max_new_positions_per_day)
    open_symbols = set(new_symbols)
    added_amount = 0.0
    cash_left = state.cash
    # 卖出优先（先释放风险与现金），再按优先级买入
    ordered = sorted(
        normalize_orders(orders),
        key=lambda o: (0 if o.side == "sell" else 1, -o.priority, o.symbol),
    )
    for order in ordered:
        reason_ids: list[str] = []
        reasons: list[str] = []
        missing: list[str] = []
        price = order.price
        requested = order.requested_amount()
        holding = state.holding(order.symbol)

        quote = None
        if quotes is not None and order.symbol in quotes:
            raw = quotes[order.symbol]
            quote = (
                raw
                if isinstance(raw, QuoteSnapshot)
                else QuoteSnapshot.from_mapping(raw, symbol=order.symbol)
            )
        rule_result = None
        if enforce_trade_rules:
            rule_result = validate_order(
                order,
                holding=holding,
                quote=quote,
                trade_date=plan.trade_date,
                max_buy_amount=(
                    None
                    if not order.is_buy or cash_left is None
                    else max(0.0, cash_left)
                ),
                config=config,
                allow_limit_up_queue_buy=allow_limit_up_queue_buy,
            )
            for violation in rule_result.violations:
                reason_ids.append(violation.rule_id)
                reasons.append(violation.detail)
            for item in rule_result.checks_skipped:
                missing.append(item)
            missing_global.extend(
                item for item in missing if item not in missing_global
            )
            if not rule_result.allowed:
                decisions.append(
                    _decision(
                        order,
                        REJECTED,
                        requested,
                        None,
                        price,
                        None,
                        reason_ids,
                        reasons,
                        missing,
                    )
                )
                continue

        if order.side == "sell":
            qty = order.qty if order.qty is not None else 0
            if rule_result is not None and rule_result.sellable_qty is not None and qty:
                qty = min(qty, rule_result.sellable_qty)
            if qty <= 0:
                decisions.append(
                    _decision(
                        order,
                        REJECTED,
                        requested,
                        None,
                        price,
                        None,
                        reason_ids or ["trade:sellable_zero"],
                        reasons or ["可卖数量为 0"],
                        missing,
                    )
                )
                continue
            final_order = order.with_qty(int(qty))
            accepted.append(final_order)
            decisions.append(
                _decision(
                    final_order,
                    ACCEPTED if qty == (order.qty or qty) else SHRUNK,
                    requested,
                    qty * (price or 0.0) if price else None,
                    price,
                    None,
                    reason_ids,
                    reasons,
                    missing,
                )
            )
            if price:
                cash_left = None if cash_left is None else cash_left + qty * price
            continue

        # ── 买入：择时 + 板型 + 各类仓位上限 ──
        if not plan.allow_new_positions:
            reason_ids.append("timing:no_new_positions")
            reasons.append(f"择时禁止新开仓（regime={plan.regime or '缺失'}）")
        if plan.position_multiplier is None:
            reason_ids.append("timing:multiplier_missing")
            reasons.append("仓位系数缺失（regime 未解析），拒绝新开仓")
        is_new_position = order.symbol not in open_symbols
        if is_new_position and new_used >= max_new:
            reason_ids.append("constraint:max_new_positions_per_day")
            reasons.append(f"已达单日新开仓上限 {max_new}")
        if is_new_position and len(open_symbols) + 1 > plan.max_hold_positions:
            reason_ids.append("constraint:max_hold_positions")
            reasons.append(f"超过最大持仓数 {plan.max_hold_positions}")
        if plan.max_board_height is not None:
            if order.board_height is None:
                missing.append("board_height_unknown")
                if config.portfolio.reject_when_board_height_unknown:
                    reason_ids.append("timing:board_height_unknown")
                    reasons.append("连板高度缺失，按配置拒绝")
            elif order.board_height > plan.max_board_height:
                reason_ids.append("timing:board_height")
                reasons.append(
                    f"{order.board_height} 板超过允许高度 {plan.max_board_height}"
                )
        if plan.ban_high_board and order.board_height is not None:
            if order.board_height >= plan.high_board_height:
                reason_ids.append("timing:high_board_ban")
                reasons.append(
                    f"高位板（{order.board_height} 板 >= {plan.high_board_height}）禁止追买"
                )
        if plan.allowed_board_types is not None:
            if order.board_type is None:
                missing.append("board_type_unknown")
            elif order.board_type not in set(plan.allowed_board_types):
                reason_ids.append("timing:board_type")
                reasons.append(
                    f"板型 {order.board_type} 不在允许清单 {plan.allowed_board_types}"
                )
        if plan.required_condition and config.enforce_required_conditions:
            if order.weak_to_strong is not True:
                reason_ids.append("timing:required_condition")
                reasons.append(
                    f"{plan.regime} 要求条件「{plan.required_condition}」未满足"
                )

        if reason_ids and any(
            item.startswith(("timing:", "constraint:")) for item in reason_ids
        ):
            decisions.append(
                _decision(
                    order,
                    REJECTED,
                    requested,
                    None,
                    price,
                    None,
                    reason_ids,
                    reasons,
                    missing,
                )
            )
            continue

        caps: dict[str, float | None] = {}
        candidates: list[tuple[str, float]] = []
        current_weight = state.weight(order.symbol) or 0.0
        stock_cap = _limit_amount(equity, plan.max_position_per_stock, current_weight)
        if stock_cap is not None:
            caps["max_position_per_stock_amount"] = stock_cap
            candidates.append(("constraint:per_stock_cap", stock_cap))
        else:
            missing.append("per_stock_cap_unavailable")
        theme_key = order.theme or UNKNOWN_THEME
        if order.theme is None:
            missing.append("theme_unknown")
        theme_weight = theme_weights.get(theme_key)
        theme_cap = _limit_amount(equity, plan.max_theme_concentration, theme_weight)
        if theme_cap is not None:
            caps["max_theme_concentration_amount"] = theme_cap
            candidates.append(("constraint:theme_cap", theme_cap))
        else:
            missing.append("theme_cap_unavailable")
        if target_amount is not None:
            caps["gross_exposure_amount"] = max(0.0, target_amount - added_amount)
            candidates.append(
                ("constraint:gross_exposure", max(0.0, target_amount - added_amount))
            )
        else:
            missing.append("gross_exposure_unavailable")
        if cash_left is not None:
            reserve = equity * config.portfolio.min_cash_reserve
            caps["cash_amount"] = max(0.0, cash_left - reserve)
            candidates.append(("constraint:cash", max(0.0, cash_left - reserve)))
        else:
            missing.append("cash_unknown")
        turnover = (
            order.turnover_amount
            if order.turnover_amount is not None
            else (quote.turnover_amount if quote is not None else None)
        )
        if turnover is not None:
            caps["liquidity_amount"] = (
                turnover * config.portfolio.liquidity_participation_rate
            )
            candidates.append(
                ("constraint:liquidity_participation", caps["liquidity_amount"])
            )
        else:
            missing.append("turnover_amount_unknown")

        budget = requested
        for cap_name, cap_value in candidates:
            if budget is None:
                budget = cap_value
                reason_ids.append(cap_name)
                reasons.append(f"按 {cap_name} 首次限定金额 {round(cap_value, 2)}")
                continue
            if cap_value + 1e-9 < budget:
                reason_ids.append(cap_name)
                reasons.append(
                    f"金额从 {round(budget, 2)} 缩减到 {round(cap_value, 2)}（{cap_name}）"
                )
                budget = cap_value
        if budget is None:
            decisions.append(
                _decision(
                    order,
                    REJECTED,
                    requested,
                    None,
                    price,
                    None,
                    tuple(reason_ids) + ("constraint:budget_unknown",),
                    tuple(reasons) + ("金额与所有上限均不可评估，拒绝新开仓"),
                    missing,
                )
            )
            continue
        qty = order.qty
        if price:
            qty = round_to_lot_amount(budget, price, config.portfolio.lot_size)
        elif qty is None:
            decisions.append(
                _decision(
                    order,
                    REJECTED,
                    requested,
                    None,
                    price,
                    None,
                    ("trade:price_required",),
                    ("缺少价格且未给数量，无法按手数取整"),
                    missing,
                )
            )
            continue
        if qty <= 0:
            decisions.append(
                _decision(
                    order,
                    REJECTED,
                    requested,
                    None,
                    price,
                    None,
                    tuple(reason_ids) + ("constraint:lot_rounds_to_zero",),
                    tuple(reasons) + (f"缩减后金额 {round(budget, 2)} 不足 1 手",),
                    missing,
                )
            )
            continue
        final_amount = qty * price if price else budget
        weight_after = (
            None
            if equity <= 0
            else round4((current_weight * equity + final_amount) / equity)
        )
        order_final = order.with_qty(int(qty))
        accepted.append(order_final)
        decisions.append(
            _decision(
                order_final,
                ACCEPTED
                if requested is not None and abs(requested - final_amount) < 1e-6
                else SHRUNK,
                requested,
                final_amount,
                price,
                weight_after,
                reason_ids,
                reasons,
                missing,
                caps,
            )
        )
        if is_new_position:
            open_symbols.add(order.symbol)
            new_used += 1
        added_amount += final_amount or 0.0
        if cash_left is not None and final_amount:
            cash_left = cash_left - final_amount

    over_cap = _over_cap_notes(state, plan, theme_weights)
    summary = {
        "orders": len(decisions),
        "accepted": sum(1 for d in decisions if d.action == ACCEPTED),
        "shrunk": sum(1 for d in decisions if d.action == SHRUNK),
        "rejected": sum(1 for d in decisions if d.action == REJECTED),
        "new_positions": new_used - int(state.new_positions_today or 0),
        "max_new_positions": max_new,
        "target_gross_exposure": plan.position_multiplier,
        "gross_before": gross_before,
        "gross_added": round4(added_amount / equity) if equity > 0 else None,
        "cash_left": round4(cash_left) if cash_left is not None else None,
    }
    return AllocationResult(
        trade_date=plan.trade_date,
        decisions=tuple(decisions),
        accepted_orders=tuple(accepted),
        over_cap=tuple(over_cap),
        summary=summary,
        missing=tuple(dict.fromkeys(missing_global)),
    )


def round_to_lot_amount(amount: float, price: float, lot_size: int) -> int:
    """金额 -> 可买整手数（向下取整，负价/零价一律 0 手）。"""

    if price <= 0:
        return 0
    return round_to_lot(amount / price, lot_size, mode="floor")


def _decision(
    order: OrderRequest,
    action: str,
    requested: float | None,
    final_amount: float | None,
    price: float | None,
    weight_after: float | None,
    rule_ids: Iterable[str],
    reasons: Iterable[str],
    missing: Iterable[str],
    caps: dict[str, float | None] | None = None,
) -> AllocationDecision:
    return AllocationDecision(
        symbol=order.symbol,
        side=order.side,
        action=action,
        name=order.name,
        theme=order.theme,
        board_height=order.board_height,
        priority=order.priority,
        requested_amount=requested,
        final_amount=final_amount,
        requested_qty=order.qty,
        final_qty=order.qty if action in (ACCEPTED, SHRUNK) else None,
        price=price,
        weight_after=weight_after,
        rule_ids=tuple(dict.fromkeys(rule_ids)),
        reasons=tuple(reasons),
        missing=tuple(dict.fromkeys(missing)),
        caps=dict(caps or {}),
    )


def _over_cap_notes(
    state: PortfolioState, plan: TimingPlan, theme_weights: Mapping[str, float | None]
) -> list[dict[str, Any]]:
    notes: list[dict[str, Any]] = []
    for item in state.holdings:
        if not isinstance(item, Holding):
            continue
        weight = state.weight(item.symbol)
        if weight is None:
            continue
        if (
            plan.max_position_per_stock is not None
            and weight > plan.max_position_per_stock + 1e-9
        ):
            notes.append(
                {
                    "symbol": item.symbol,
                    "kind": "per_stock",
                    "weight": round4(weight),
                    "cap": round4(plan.max_position_per_stock),
                    "rule_id": "advisory:per_stock_over_cap",
                    "action": "建议减仓（不强平）",
                }
            )
    theme_key_seen: set[str] = set()
    for key, weight in theme_weights.items():
        if weight is None or key in theme_key_seen or key == UNKNOWN_THEME:
            continue
        theme_key_seen.add(key)
        if (
            plan.max_theme_concentration is not None
            and weight > plan.max_theme_concentration + 1e-9
        ):
            notes.append(
                {
                    "theme": key,
                    "kind": "theme",
                    "weight": round4(weight),
                    "cap": round4(plan.max_theme_concentration),
                    "rule_id": "advisory:theme_over_cap",
                    "action": "建议减仓（不强平）",
                }
            )
    return notes
