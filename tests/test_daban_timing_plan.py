"""#32 当日打板计划（strategy.daban_timing.timing）单元测试。

覆盖验收项「regime 边界」与「计划可追溯」：六个 regime 各自的仓位系数、
新开仓上限、禁买条件，以及熔断 / 分数软阈值 / 缺数降级。
"""

from __future__ import annotations

import json

import pytest

from strategy.daban_timing.config import TimingConfig, parse_timing_config
from strategy.daban_timing.regime import resolve_regime
from strategy.daban_timing.timing import build_timing_plan

TRADE_DATE = "2026-10-22"
AFTER_CLOSE = f"{TRADE_DATE} 15:30"


def _snapshot(regime: str, score: float | None = 60.0, **extra):
    factors = {
        "trade_date": TRADE_DATE,
        "regime": regime,
        "sentiment_score": score,
        **extra,
    }
    return resolve_regime(factors, trade_date=TRADE_DATE, decision_at=AFTER_CLOSE)


@pytest.mark.parametrize(
    ("regime", "multiplier", "allow", "max_new", "max_height"),
    [
        ("冰点", 0.2, True, 1, 1),
        ("修复", 0.5, True, 2, 1),
        ("发酵", 1.0, True, 3, 3),
        ("高潮", 0.7, True, 2, 3),
        ("分歧", 0.5, True, 1, 2),
        ("退潮", 0.0, False, 0, 2),
    ],
)
def test_plan_per_regime(regime, multiplier, allow, max_new, max_height):
    plan = build_timing_plan(_snapshot(regime), TimingConfig())
    assert plan.regime == regime
    assert plan.regime_source == "close"
    assert plan.position_multiplier == pytest.approx(multiplier)
    assert plan.allow_new_positions is allow
    assert plan.max_new_positions == max_new
    assert plan.max_board_height == max_height
    assert plan.trade_date == TRADE_DATE
    assert plan.rule_ids[0] == f"timing:{regime}"
    assert f"禁止买入 {max_height + 1} 板及以上" in plan.forbidden


def test_ebb_plan_is_sell_only_and_traceable():
    plan = build_timing_plan(_snapshot("退潮", 22.0), TimingConfig())
    assert plan.allow_new_positions is False
    assert plan.position_multiplier == 0.0
    assert "禁止新开仓" in plan.forbidden
    assert "只卖不买" in plan.actions
    assert plan.extra["rule"]["regime"] == "退潮"


def test_score_is_part_of_the_decision():
    weak = build_timing_plan(_snapshot("修复", 28.0), TimingConfig())
    strong = build_timing_plan(_snapshot("修复", 50.0), TimingConfig())
    assert weak.position_multiplier == pytest.approx(0.25)  # 0.5 × 0.5 低分减仓
    assert strong.position_multiplier == pytest.approx(0.5)
    assert "timing:low_score_gate" in weak.rule_ids
    assert "timing:low_score_gate" not in strong.rule_ids


def test_climax_high_score_adds_partial_take_profit_action():
    plan = build_timing_plan(_snapshot("高潮", 90.0), TimingConfig())
    assert "timing:high_score_take_profit" in plan.rule_ids
    assert any("分批止盈" in action for action in plan.actions)
    # 分数未达阈值时不追加该动作
    normal = build_timing_plan(_snapshot("高潮", 80.0), TimingConfig())
    assert "timing:high_score_take_profit" not in normal.rule_ids


def test_breaker_actions_chain_on_top_of_regime():
    snapshot = _snapshot("发酵", 70.0)
    half = build_timing_plan(snapshot, TimingConfig(), breaker_actions=["half"])
    assert half.position_multiplier == pytest.approx(0.5)
    assert half.allow_new_positions is True
    assert "risk:drawdown_half" in half.rule_ids
    halt = build_timing_plan(snapshot, TimingConfig(), breaker_actions=["halt"])
    assert halt.allow_new_positions is False
    assert halt.max_new_positions == 0
    assert "risk:drawdown_halt" in halt.rule_ids
    both = build_timing_plan(snapshot, TimingConfig(), breaker_actions=["half", "halt"])
    assert both.position_multiplier == pytest.approx(0.5)
    assert both.allow_new_positions is False


def test_missing_score_is_marked_not_zero_filled():
    plan = build_timing_plan(_snapshot("发酵", None), TimingConfig())
    assert plan.sentiment_score is None
    assert "sentiment_score_missing" in plan.missing
    assert plan.position_multiplier == pytest.approx(1.0)  # 系数仍来自 regime，不臆造


def test_unknown_regime_block_mode_returns_none_multiplier():
    snapshot = resolve_regime(
        {"trade_date": TRADE_DATE},
        trade_date=TRADE_DATE,
        decision_at=AFTER_CLOSE,
        mode="close",
    )
    plan = build_timing_plan(snapshot, TimingConfig())
    assert plan.regime is None
    assert plan.position_multiplier is None  # 显式缺失，不是 0 信号
    assert plan.allow_new_positions is False
    assert plan.max_new_positions == 0
    assert "regime_label_missing" in plan.missing
    assert plan.rule_ids == ("timing:unknown_regime",)


def test_unknown_regime_neutral_mode():
    config = parse_timing_config({"unknown_regime_mode": "neutral"})
    snapshot = resolve_regime(
        {"trade_date": TRADE_DATE},
        trade_date=TRADE_DATE,
        decision_at=AFTER_CLOSE,
        mode="close",
    )
    plan = build_timing_plan(snapshot, config)
    assert plan.position_multiplier == pytest.approx(0.5)
    assert plan.allow_new_positions is True
    assert "regime_label_missing" in plan.missing


def test_disabled_timing_is_the_backtest_control_group():
    snapshot = _snapshot("退潮", 20.0)
    plan = build_timing_plan(snapshot, TimingConfig().disabled())
    assert plan.position_multiplier == pytest.approx(1.0)
    assert plan.allow_new_positions is True
    assert plan.max_board_height is None
    assert plan.ban_high_board is False
    assert plan.max_position_per_stock == pytest.approx(0.20)
    assert "timing:disabled" in plan.rule_ids


def test_intraday_sourced_plan_is_flagged():
    snapshot = resolve_regime(
        {
            "trade_date": TRADE_DATE,
            "intraday_regime": "发酵",
            "intraday_sentiment_score": 66.0,
        },
        trade_date=TRADE_DATE,
        decision_at=f"{TRADE_DATE} 14:00",
        mode="intraday",
    )
    plan = build_timing_plan(snapshot, TimingConfig())
    assert plan.regime_source == "intraday"
    assert plan.position_multiplier == pytest.approx(1.0)


def test_custom_thresholds_are_honoured():
    config = parse_timing_config(
        {
            "rules_by_regime": {
                "发酵": {"position_multiplier": 0.6, "max_new_positions": 1}
            },
            "portfolio": {"high_board_height": 3},
        }
    )
    plan = build_timing_plan(_snapshot("发酵"), config)
    assert plan.position_multiplier == pytest.approx(0.6)
    assert plan.max_new_positions == 1
    assert plan.high_board_height == 3


def test_plan_is_json_serialisable_and_deterministic():
    snapshot = _snapshot("高潮", 88.0)
    first = build_timing_plan(snapshot, TimingConfig())
    second = build_timing_plan(snapshot, TimingConfig())
    assert json.dumps(first.to_dict(), ensure_ascii=False) == json.dumps(
        second.to_dict(), ensure_ascii=False
    )
    payload = json.loads(json.dumps(first.to_dict(), ensure_ascii=False))
    assert payload["version"].startswith("daban_timing/plan")
    assert {hit["rule_id"] for hit in payload["trace"]} == set(payload["rule_ids"])


def test_target_gross_exposure_matches_multiplier():
    plan = build_timing_plan(_snapshot("冰点"), TimingConfig())
    assert plan.target_gross_exposure == pytest.approx(0.2)
    missing = build_timing_plan(
        resolve_regime(
            {"trade_date": TRADE_DATE}, trade_date=TRADE_DATE, decision_at=AFTER_CLOSE
        ),
        TimingConfig(),
    )
    assert missing.target_gross_exposure is None
