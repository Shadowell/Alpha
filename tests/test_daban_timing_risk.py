"""#32 回撤熔断与误杀复盘（strategy.daban_timing.risk / rejections）单元测试。

覆盖验收项「熔断」边界（8% / 12%）与「被规则拒绝但事后走强」样本记录。
"""

from __future__ import annotations

import json

import pytest

from strategy.daban_timing.config import TimingConfig, parse_timing_config
from strategy.daban_timing.constraints import allocate_orders
from strategy.daban_timing.models import PortfolioState
from strategy.daban_timing.regime import resolve_regime
from strategy.daban_timing.rejections import (
    collect_rejections,
    rejection_breakdown,
    review_rejections,
    review_summary,
)
from strategy.daban_timing.risk import (
    LEVEL_HALF,
    LEVEL_HALT,
    LEVEL_NORMAL,
    drawdown,
    evaluate_breaker,
    max_drawdown,
    replay_breaker,
)
from strategy.daban_timing.timing import build_timing_plan

DATE = "2026-10-22"


def _plan(regime: str, score: float = 60.0, *, breaker_actions=()):
    snapshot = resolve_regime(
        {"trade_date": DATE, "regime": regime, "sentiment_score": score},
        trade_date=DATE,
        decision_at=f"{DATE} 15:30",
    )
    return build_timing_plan(snapshot, TimingConfig(), breaker_actions=breaker_actions)


def test_drawdown_math_and_boundaries():
    assert drawdown(1_000_000, 920_000) == pytest.approx(0.08)
    assert drawdown(1_000_000, 880_000) == pytest.approx(0.12)
    assert drawdown(1_000_000, 1_050_000) == 0.0
    assert drawdown(0, 100) is None
    assert drawdown(None, 100) is None

    normal = evaluate_breaker(equity=921_000, peak_equity=1_000_000)
    assert normal.level == LEVEL_NORMAL
    half = evaluate_breaker(equity=920_000, peak_equity=1_000_000)
    assert half.level == LEVEL_HALF
    assert half.actions == ("half",)
    halt = evaluate_breaker(equity=880_000, peak_equity=1_000_000)
    assert halt.level == LEVEL_HALT
    assert halt.allow_new_positions is False
    assert halt.multiplier_scale == 1.0
    assert half.multiplier_scale == 0.5


def test_breaker_is_sticky_and_recovers_stepwise():
    halt = evaluate_breaker(equity=880_000, peak_equity=1_000_000)
    still = evaluate_breaker(
        equity=930_000, peak_equity=1_000_000, prev_state=halt, trade_date="2026-10-23"
    )
    assert still.level == LEVEL_HALT  # 回撤 7% > halt_recover 6%，仍停新开仓
    half = evaluate_breaker(
        equity=945_000, peak_equity=1_000_000, prev_state=still, trade_date="2026-10-24"
    )
    assert half.level == LEVEL_HALF  # 回撤 5.5% <= 6% → 逐级解除到半仓
    ok = evaluate_breaker(
        equity=970_000, peak_equity=1_000_000, prev_state=half, trade_date="2026-10-25"
    )
    assert ok.level == LEVEL_NORMAL
    assert ok.since is None
    stuck = evaluate_breaker(
        equity=950_000, peak_equity=1_000_000, prev_state=half, trade_date="2026-10-26"
    )
    assert stuck.level == LEVEL_HALF  # 5% > half_recover 4%，半仓保持
    assert stuck.since == "2026-10-23"  # 首次触发日一直带在半仓状态上


def test_breaker_missing_equity_keeps_previous_state():
    half = evaluate_breaker(equity=920_000, peak_equity=1_000_000, trade_date=DATE)
    unknown = evaluate_breaker(equity=None, prev_state=half, trade_date="2026-10-23")
    assert unknown.level == LEVEL_HALF
    assert unknown.drawdown is None
    assert "equity_missing" in unknown.missing
    assert unknown.rule_ids == ("risk:equity_missing",)


def test_breaker_thresholds_are_configurable():
    config = parse_timing_config({"risk": {"drawdown_half_threshold": 0.05, "drawdown_halt_threshold": 0.07}})
    state = evaluate_breaker(equity=925_000, peak_equity=1_000_000, config=config)
    assert state.level == LEVEL_HALT
    default = evaluate_breaker(equity=925_000, peak_equity=1_000_000)
    assert default.level == LEVEL_NORMAL  # 默认 8% 阈值下 7.5% 回撤不触发


def test_replay_and_max_drawdown():
    curve = [1_000_000, 995_000, 915_000, 870_000, 900_000, 990_000]
    states = replay_breaker(curve)
    assert [s.level for s in states] == [
        LEVEL_NORMAL,
        LEVEL_NORMAL,
        LEVEL_HALF,
        LEVEL_HALT,
        LEVEL_HALT,
        LEVEL_NORMAL,  # 回撤修复到 1% <= half_recover 4% → 完全解除
    ]
    assert max_drawdown(curve) == pytest.approx(0.13)
    assert max_drawdown([1.0]) is None
    dated = replay_breaker([("2026-10-22", 1_000_000), ("2026-10-23", 915_000)])
    assert dated[1].trade_date == "2026-10-23"


def test_breaker_feeds_timing_plan_multiplier():
    state = evaluate_breaker(equity=915_000, peak_equity=1_000_000)
    plan = _plan("发酵", 70.0, breaker_actions=state.actions)
    assert plan.position_multiplier == pytest.approx(0.5)  # 1.0 × 0.5
    halted = evaluate_breaker(equity=870_000, peak_equity=1_000_000, prev_state=state)
    plan2 = _plan("发酵", 70.0, breaker_actions=halted.actions)
    assert plan2.allow_new_positions is False


def test_breaker_state_is_json_serialisable():
    state = evaluate_breaker(equity=915_000, peak_equity=1_000_000, trade_date=DATE)
    payload = json.loads(json.dumps(state.to_dict(), ensure_ascii=False))
    assert payload["level"] == LEVEL_HALF
    assert payload["drawdown"] == pytest.approx(0.085)


def _rejecting_result():
    """冰点（最多 1 只新开、只做首板）下的三个候选：

    * 600001：首板、优先级最高 -> 成交；
    * 600987：4 板高位 -> 被择时规则拒绝（timing 类）；
    * 600002：首板但排队在新开仓上限之后 -> 被组合约束拒绝（非 timing）。
    """

    orders = [
        {
            "symbol": "600987",
            "name": "测试标的甲",
            "side": "buy",
            "price": 10.0,
            "amount": 50_000.0,
            "theme": "题材一",
            "board_height": 4,
            "priority": 1.0,
        },
        {
            "symbol": "600002",
            "name": "测试标的乙",
            "side": "buy",
            "price": 10.0,
            "amount": 50_000.0,
            "theme": "题材二",
            "board_height": 1,
            "priority": 0.5,
        },
        {
            "symbol": "600001",
            "name": "测试标的丙",
            "side": "buy",
            "price": 10.0,
            "amount": 50_000.0,
            "theme": "题材三",
            "board_height": 1,
            "priority": 2.0,
        },
    ]
    state = PortfolioState.from_mapping(
        {"trade_date": DATE, "equity": 1_000_000.0, "cash": 1_000_000.0, "holdings": []}
    )
    return allocate_orders(orders, state, _plan("冰点", 25.0))


def test_rejection_capture_and_breakdown():
    result = _rejecting_result()
    rejections = collect_rejections(result)
    assert [r.symbol for r in rejections] == ["600987", "600002"]  # 按处理顺序（优先级降序）
    timing_only = collect_rejections(result, only_timing=True)
    assert [r.symbol for r in timing_only] == ["600987"]
    assert timing_only[0].board_height == 4
    assert timing_only[0].theme == "题材一"
    assert timing_only[0].name == "测试标的甲"
    counts = rejection_breakdown(rejections)
    assert counts["timing:board_height"] == 1
    assert "constraint:max_new_positions_per_day" in counts
    json.dumps([r.to_dict() for r in rejections], ensure_ascii=False)


def test_review_rejections_marks_false_blocks_and_missing():
    rejections = collect_rejections(_rejecting_result())
    reviews = review_rejections(
        rejections,
        {(DATE, "600987"): 0.09, "600002": -0.02},
        strength_threshold=0.05,
    )
    by_symbol = {r.sample.symbol: r for r in reviews}
    assert by_symbol["600987"].post_strength is True
    assert by_symbol["600002"].post_strength is False
    assert by_symbol["600987"].forward_return == pytest.approx(0.09)

    unscored = review_rejections(rejections, {})
    assert all(r.forward_return is None for r in unscored)
    assert all("forward_return_missing" in r.missing for r in unscored)
    summary = review_summary(unscored)
    assert summary["scored"] == 0
    assert summary["false_block_rate"] is None
    filled = review_summary(reviews)
    assert filled["false_block_rate"] == pytest.approx(0.5)
