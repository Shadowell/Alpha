"""#32 组合约束与仓位缩减（strategy.daban_timing.constraints）单元测试。

覆盖验收项「组合约束生效：单票 / 同题材 / 持仓数超限时拒绝或缩减」，
并验证缺数项不会被当成 0 处理。
"""

from __future__ import annotations

import json

import pytest

from strategy.daban_timing.config import TimingConfig, parse_timing_config
from strategy.daban_timing.constraints import (
    ACCEPTED,
    REJECTED,
    SHRUNK,
    allocate_orders,
)
from strategy.daban_timing.models import PortfolioState
from strategy.daban_timing.regime import resolve_regime
from strategy.daban_timing.timing import TimingPlan, build_timing_plan

DATE = "2026-10-22"
EQUITY = 1_000_000.0


def _plan(regime: str, score: float = 60.0) -> TimingPlan:
    snapshot = resolve_regime(
        {"trade_date": DATE, "regime": regime, "sentiment_score": score},
        trade_date=DATE,
        decision_at=f"{DATE} 15:30",
    )
    return build_timing_plan(snapshot, TimingConfig())


def _state(
    holdings=(), cash: float | None = EQUITY, new_today: int = 0
) -> PortfolioState:
    return PortfolioState.from_mapping(
        {
            "trade_date": DATE,
            "equity": EQUITY,
            "cash": cash,
            "holdings": list(holdings),
            "new_positions_today": new_today,
        }
    )


def _order(**payload):
    base = {
        "symbol": "600987",
        "name": "测试标的甲",
        "side": "buy",
        "price": 10.0,
        "amount": 200_000.0,
        "theme": "具身智能",
        "board_height": 1,
        "board_type": "换手板",
        "turnover_amount": 20_000_000.0,
        "priority": 1.0,
    }
    base.update(payload)
    return base


def test_per_stock_cap_shrinks_order():
    result = allocate_orders([_order(amount=400_000.0)], _state(), _plan("发酵"))
    decision = result.decision("600987")
    assert decision.action == SHRUNK
    # 发酵单票上限 20% -> 请求 40 万被缩减到 20 万
    assert decision.final_amount == pytest.approx(200_000.0)
    assert decision.weight_after == pytest.approx(0.2)
    assert "constraint:per_stock_cap" in decision.rule_ids


def test_ice_point_caps_position_and_only_first_board():
    orders = [
        _order(symbol="600987", board_height=1),
        _order(symbol="000001", board_height=3),
    ]
    result = allocate_orders(orders, _state(), _plan("冰点"))
    assert result.decision("600987").action in (ACCEPTED, SHRUNK)
    assert result.decision("600987").final_amount == pytest.approx(
        100_000.0
    )  # 10% 上限
    rejected = result.decision("000001")
    assert rejected.action == REJECTED
    assert "timing:board_height" in rejected.rule_ids


def test_ebb_blocks_all_buys():
    result = allocate_orders([_order()], _state(), _plan("退潮", 20.0))
    decision = result.decision("600987")
    assert decision.action == REJECTED
    assert "timing:no_new_positions" in decision.rule_ids
    assert result.accepted_orders == ()


def test_theme_concentration_limits_second_stock():
    holdings = [
        {"symbol": "600001", "theme": "具身智能", "qty": 10_000, "current_price": 24.0},
    ]
    result = allocate_orders(
        [_order(symbol="600002", theme="具身智能", amount=200_000.0)],
        _state(holdings=holdings),
        _plan("发酵"),
    )
    decision = result.decision("600002")
    # 已持有同题材 24 万（24%），题材上限 40% -> 最多再加 16 万
    assert decision.action == SHRUNK
    assert decision.final_amount == pytest.approx(160_000.0)
    assert "constraint:theme_cap" in decision.rule_ids


def test_max_hold_positions_rejects_extra_symbol():
    holdings = [
        {
            "symbol": f"60000{i}",
            "theme": f"题材{i}",
            "qty": 1_000,
            "current_price": 10.0,
        }
        for i in range(1, 6)
    ]
    result = allocate_orders(
        [_order(symbol="600099")], _state(holdings=holdings), _plan("发酵")
    )
    decision = result.decision("600099")
    assert decision.action == REJECTED
    assert "constraint:max_hold_positions" in decision.rule_ids


def test_max_new_positions_per_day_is_smaller_of_plan_and_limits():
    orders = [
        _order(symbol=f"60000{i}", theme=f"题材{i}", priority=float(i))
        for i in range(1, 5)
    ]
    result = allocate_orders(orders, _state(), _plan("发酵"))  # 发酵最多 3 只新开
    accepted = [d for d in result.decisions if d.action in (ACCEPTED, SHRUNK)]
    assert len(accepted) == 3
    rejected = [d for d in result.decisions if d.action == REJECTED]
    assert len(rejected) == 1
    assert "constraint:max_new_positions_per_day" in rejected[0].rule_ids
    assert result.summary["new_positions"] == 3


def test_gross_exposure_already_above_target_rejects_new():
    holdings = [
        {"symbol": "600001", "theme": "甲", "qty": 25_000, "current_price": 40.0}
    ]
    result = allocate_orders(
        [_order(symbol="600002", theme="乙")], _state(holdings=holdings), _plan("冰点")
    )
    decision = result.decision("600002")
    assert decision.action == REJECTED
    assert "constraint:gross_exposure" in decision.rule_ids
    assert decision.final_qty in (None, 0)


def test_liquidity_participation_cap_shrinks_large_order():
    result = allocate_orders(
        [_order(symbol="600987", amount=500_000.0, turnover_amount=2_000_000.0)],
        _state(),
        _plan("发酵"),
    )
    decision = result.decision("600987")
    # 成交额 200 万 × 参与率 5% = 10 万，低于单票上限 20 万
    assert decision.final_amount == pytest.approx(100_000.0)
    assert "constraint:liquidity_participation" in decision.rule_ids


def test_amount_below_one_lot_is_rejected_with_rule():
    result = allocate_orders(
        [_order(symbol="600987", amount=900.0, price=10.0, turnover_amount=100_000.0)],
        _state(),
        _plan("发酵"),
    )
    decision = result.decision("600987")
    assert decision.action == REJECTED
    assert "constraint:lot_rounds_to_zero" in decision.rule_ids


def test_missing_cash_and_turnover_are_marked_not_zero_filled():
    state = _state(cash=None)
    result = allocate_orders(
        [_order(symbol="600987", turnover_amount=None)], state, _plan("发酵")
    )
    decision = result.decision("600987")
    assert decision.action in (ACCEPTED, SHRUNK)
    assert "cash_unknown" in decision.missing
    assert "turnover_amount_unknown" in decision.missing
    assert "constraint:cash" not in decision.rule_ids


def test_unknown_board_height_default_skips_and_can_reject_by_config():
    order = _order(symbol="600987", board_height=None)
    lenient = allocate_orders([order], _state(), _plan("冰点"))
    assert lenient.decision("600987").action in (ACCEPTED, SHRUNK)
    assert "board_height_unknown" in lenient.decision("600987").missing
    strict_config = parse_timing_config(
        {"portfolio": {"reject_when_board_height_unknown": True}}
    )
    strict = allocate_orders([order], _state(), _plan("冰点"), config=strict_config)
    assert strict.decision("600987").action == REJECTED
    assert "timing:board_height_unknown" in strict.decision("600987").rule_ids


def test_required_condition_weak_to_strong():
    config = parse_timing_config({"enforce_required_conditions": True})
    plan = _plan("分歧", 58.0)
    result = allocate_orders(
        [_order(symbol="600987", weak_to_strong=False)], _state(), plan, config=config
    )
    decision = result.decision("600987")
    assert decision.action == REJECTED
    assert "timing:required_condition" in decision.rule_ids
    ok = allocate_orders(
        [_order(symbol="600987", weak_to_strong=True)], _state(), plan, config=config
    )
    assert ok.decision("600987").action in (ACCEPTED, SHRUNK)


def test_sell_orders_bypass_new_position_caps_but_obey_t_plus_1():
    holdings = [
        {
            "symbol": "600001",
            "theme": "甲",
            "qty": 1000,
            "current_price": 10.0,
            "open_date": DATE,
        }
    ]
    plan = _plan("退潮", 20.0)  # 退潮只卖不买
    blocked = allocate_orders(
        [_order(symbol="600001", side="sell", qty=1000, amount=10_500.0)],
        _state(holdings=holdings),
        plan,
    )
    assert blocked.decision("600001", "sell").action == REJECTED
    assert "trade:t_plus_1" in blocked.decision("600001", "sell").rule_ids
    aged = [
        {
            "symbol": "600001",
            "theme": "甲",
            "qty": 1000,
            "current_price": 10.0,
            "open_date": "2026-10-21",
        }
    ]
    allowed = allocate_orders(
        [_order(symbol="600001", side="sell", qty=1000, amount=10_500.0)],
        _state(holdings=aged),
        plan,
    )
    assert allowed.decision("600001", "sell").action == ACCEPTED
    assert len(allowed.accepted_orders) == 1


def test_existing_over_cap_produces_advisory_not_forced_sell():
    holdings = [
        {"symbol": "600001", "theme": "甲题材", "qty": 30_000, "current_price": 40.0}
    ]
    result = allocate_orders([], _state(holdings=holdings), _plan("冰点"))
    kinds = {note["kind"] for note in result.over_cap}
    assert {"per_stock", "theme"} <= kinds
    assert all("advisory:" in note["rule_id"] for note in result.over_cap)


def test_quote_violations_reject_before_sizing():
    result = allocate_orders(
        [_order(symbol="600987")],
        _state(),
        _plan("发酵"),
        quotes={
            "600987": {"price": 10.0, "prev_close": 10.0, "sealed_at_limit_up": True}
        },
    )
    decision = result.decision("600987")
    assert decision.action == REJECTED
    assert "trade:limit_up_sealed" in decision.rule_ids


def test_allocation_is_deterministic_and_json_safe():
    orders = [
        _order(symbol="600987", priority=1.0),
        _order(symbol="000001", theme="别的题材", priority=2.0),
    ]
    state = _state()
    plan = _plan("发酵")
    first = allocate_orders(orders, state, plan).to_dict()
    second = allocate_orders(orders, state, plan).to_dict()
    assert json.dumps(first, ensure_ascii=False, sort_keys=True) == json.dumps(
        second, ensure_ascii=False, sort_keys=True
    )
    assert [d["symbol"] for d in first["decisions"]] == [
        "000001",
        "600987",
    ]  # 优先级排序


def test_disabled_timing_keeps_portfolio_constraints_only():
    config = TimingConfig().disabled()
    snapshot = resolve_regime(
        {"trade_date": DATE, "regime": "退潮", "sentiment_score": 20.0},
        trade_date=DATE,
        decision_at=f"{DATE} 15:30",
    )
    plan = build_timing_plan(snapshot, config)
    result = allocate_orders(
        [_order(symbol="600987", amount=400_000.0)], _state(), plan, config=config
    )
    decision = result.decision("600987")
    assert decision.action == SHRUNK
    assert decision.final_amount == pytest.approx(200_000.0)  # 单票 20% 仍然生效
