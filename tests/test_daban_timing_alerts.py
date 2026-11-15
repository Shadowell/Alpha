"""#32 持仓提醒（strategy.daban_timing.alerts）单元测试。

覆盖验收项「自动止损 / 止盈提醒」+「所有提醒可追溯（触发规则、时间、价格）」，
并锁定"提醒 = 建议，需人工确认"与 T+1 提示。
"""

from __future__ import annotations

import json

import pytest

from strategy.daban_timing.alerts import (
    KIND_AVG_PRICE_BREAK,
    KIND_PARTIAL_TAKE_PROFIT,
    KIND_SEAL_WEAKENING,
    KIND_STOP_LOSS,
    KIND_TAKE_PROFIT,
    KIND_TIME_STOP,
    KIND_TRAILING_STOP,
    data_gaps,
    evaluate_portfolio_alerts,
    evaluate_position_alerts,
)
from strategy.daban_timing.config import TimingConfig, parse_timing_config
from strategy.daban_timing.models import Holding
from strategy.daban_timing.regime import resolve_regime
from strategy.daban_timing.risk import evaluate_breaker
from strategy.daban_timing.timing import build_timing_plan

DATE = "2026-10-22"
NOW = f"{DATE} 14:30"


def _plan(regime: str, score: float = 60.0):
    snapshot = resolve_regime(
        {"trade_date": DATE, "regime": regime, "sentiment_score": score},
        trade_date=DATE,
        decision_at=f"{DATE} 15:30",
    )
    return build_timing_plan(snapshot, TimingConfig())


def _holding(**overrides) -> Holding:
    payload = {
        "symbol": "600987",
        "name": "测试标的甲",
        "qty": 1000,
        "cost_price": 10.0,
        "current_price": 9.2,
        "open_date": "2026-10-21",
        "theme": "题材一",
    }
    payload.update(overrides)
    return Holding.from_mapping(payload)


def _kinds(alerts) -> set[str]:
    return {a.kind for a in alerts}


def test_stop_loss_alert_is_traceable_and_manual_only():
    alerts = evaluate_position_alerts(
        _holding(), plan=_plan("发酵"), trade_date=DATE, triggered_at=NOW
    )
    assert KIND_STOP_LOSS in _kinds(alerts)  # -8% < -7%
    alert = next(a for a in alerts if a.kind == KIND_STOP_LOSS)
    assert alert.rule_id == "alert:stop_loss"
    assert alert.price == pytest.approx(9.2)
    assert alert.threshold == pytest.approx(-0.07)
    assert alert.actual == pytest.approx(-0.08)
    assert alert.triggered_at == NOW
    assert alert.requires_manual_confirmation is True
    assert "T+1" not in alert.message  # 建仓日为 10-21，可卖
    assert alert.blocked_by_t_plus_1 is False
    assert alert.sellable_qty == 1000
    payload = json.loads(json.dumps([a.to_dict() for a in alerts], ensure_ascii=False))
    assert payload[0]["alert_id"].startswith(f"alert:{DATE}:600987:")


def test_alert_id_is_deterministic():
    plan = _plan("发酵")
    first = evaluate_position_alerts(_holding(), plan=plan, trade_date=DATE, triggered_at=NOW)
    second = evaluate_position_alerts(_holding(), plan=plan, trade_date=DATE, triggered_at=NOW)
    assert [a.alert_id for a in first] == [a.alert_id for a in second]


def test_take_profit_and_partial_steps_depend_on_regime_action():
    climax = evaluate_position_alerts(
        _holding(current_price=11.2), plan=_plan("高潮", 88.0), trade_date=DATE, triggered_at=NOW
    )
    assert KIND_PARTIAL_TAKE_PROFIT in _kinds(climax)
    assert not any(a.kind == KIND_TAKE_PROFIT for a in climax)  # 12% < 15%
    ferment = evaluate_position_alerts(
        _holding(current_price=11.2), plan=_plan("发酵"), trade_date=DATE, triggered_at=NOW
    )
    assert ferment == []
    big_win = evaluate_position_alerts(
        _holding(current_price=11.8), plan=_plan("发酵"), trade_date=DATE, triggered_at=NOW
    )
    assert KIND_TAKE_PROFIT in _kinds(big_win)


def test_time_stop_uses_trading_calendar():
    calendar = {f"2026-10-{d}": 10.0 for d in (15, 16, 19, 20, 21, 22)}
    alerts = evaluate_position_alerts(
        _holding(open_date="2026-10-20"),
        plan=_plan("发酵"),
        trade_date=DATE,
        triggered_at=NOW,
        trade_dates=calendar,
    )
    assert KIND_TIME_STOP in _kinds(alerts)
    alert = next(a for a in alerts if a.kind == KIND_TIME_STOP)
    assert alert.threshold == pytest.approx(2.0)
    assert alert.actual == pytest.approx(2.0)
    # 盈利中的持仓不触发时间止损
    winner = evaluate_position_alerts(
        _holding(open_date="2026-10-20", current_price=10.4),
        plan=_plan("发酵"),
        trade_date=DATE,
        triggered_at=NOW,
        trade_dates=calendar,
    )
    assert KIND_TIME_STOP not in _kinds(winner)


def test_t_plus_1_marks_alert_as_advisory_for_next_day():
    alerts = evaluate_position_alerts(
        _holding(open_date=DATE, current_price=9.0),
        plan=_plan("发酵"),
        trade_date=DATE,
        triggered_at=NOW,
    )
    stop = next(a for a in alerts if a.kind == KIND_STOP_LOSS)
    assert stop.blocked_by_t_plus_1 is True
    assert stop.sellable_qty == 0
    assert stop.suggested_action == "人工确认后再操作"


def test_trailing_stop_and_avg_price_and_seal_weakening():
    quote = {"price": 10.6, "peak_price": 12.0, "vwap": 10.9, "fd_amount": 2_000_000}
    alerts = evaluate_position_alerts(
        _holding(current_price=10.6),
        quote=quote,
        plan=_plan("发酵"),
        trade_date=DATE,
        triggered_at=NOW,
        fd_amount_baseline=6_000_000,
    )
    kinds = _kinds(alerts)
    assert KIND_TRAILING_STOP in kinds
    assert KIND_AVG_PRICE_BREAK in kinds
    assert KIND_SEAL_WEAKENING in kinds
    seal = next(a for a in alerts if a.kind == KIND_SEAL_WEAKENING)
    assert seal.actual == pytest.approx(1 / 3)
    assert seal.threshold == pytest.approx(0.5)


def test_intraday_inputs_missing_are_reported_not_zero():
    alerts = evaluate_position_alerts(
        _holding(current_price=10.0),
        quote={"price": 10.0},
        plan=_plan("发酵"),
        trade_date=DATE,
        triggered_at=NOW,
    )
    assert alerts == []  # 无触发
    missing_alerts = evaluate_position_alerts(
        _holding(current_price=9.0),
        quote={"price": 9.0},
        plan=_plan("发酵"),
        trade_date=DATE,
        triggered_at=NOW,
    )
    assert missing_alerts  # 止损仍成立（不依赖盘中字段）
    assert "vwap_missing" in missing_alerts[0].missing
    assert "fd_amount_missing" in missing_alerts[0].missing


def test_missing_price_or_cost_yields_no_alert_with_markers():
    gaps = data_gaps(_holding(current_price=None, cost_price=10.0))
    assert "price_missing" in gaps
    assert data_gaps(_holding(current_price=None)) == ("price_missing",)
    assert data_gaps(_holding(cost_price=None)) == ("cost_price_missing",)
    # 关键输入缺失时不产出任何提醒（更不用 0 价触发止损）
    assert (
        evaluate_position_alerts(
            _holding(current_price=None), plan=_plan("发酵"), trade_date=DATE, triggered_at=NOW
        )
        == []
    )
    assert (
        evaluate_position_alerts(
            _holding(cost_price=None, current_price=9.0),
            plan=_plan("发酵"),
            trade_date=DATE,
            triggered_at=NOW,
        )
        == []
    )


def test_thresholds_come_from_config():
    config = parse_timing_config({"alerts": {"stop_loss_pct": 0.03, "max_hold_days": 1}})
    alerts = evaluate_position_alerts(
        _holding(current_price=9.7, open_date="2026-10-21"),
        plan=_plan("发酵"),
        trade_date=DATE,
        triggered_at=NOW,
        trade_dates={"2026-10-21": 10.0, "2026-10-22": 9.7},
        config=config,
    )
    assert KIND_STOP_LOSS in _kinds(alerts)
    assert KIND_TIME_STOP in _kinds(alerts)


def test_portfolio_alerts_sorted_with_breaker_note():
    holdings = [
        _holding(symbol="600987", current_price=9.0),
        _holding(symbol="600001", current_price=11.6, name="测试标的乙"),
    ]
    breaker = evaluate_breaker(equity=870_000, peak_equity=1_000_000, trade_date=DATE)
    alerts = evaluate_portfolio_alerts(
        holdings, plan=_plan("退潮", 20.0), trade_date=DATE, triggered_at=NOW, breaker=breaker
    )
    assert alerts[0].kind == "portfolio_drawdown_breaker"
    assert {a.kind for a in alerts} >= {KIND_STOP_LOSS, KIND_TAKE_PROFIT}
    assert all(a.requires_manual_confirmation for a in alerts)
    assert all(a.triggered_at == NOW for a in alerts)
    breaker_alert = alerts[0]
    assert breaker_alert.actual == pytest.approx(0.13)
    assert "停止新开仓" in breaker_alert.message


def test_seal_weakening_ratio_is_configurable():
    alerts = evaluate_position_alerts(
        _holding(current_price=10.0),
        quote={"price": 10.0, "fd_amount": 8_000_000, "vwap": 10.0},
        plan=_plan("发酵"),
        trade_date=DATE,
        triggered_at=NOW,
        fd_amount_baseline=10_000_000,
        config=TimingConfig(),
    )
    assert KIND_SEAL_WEAKENING not in _kinds(alerts)  # 仅下降 20%
    strict = parse_timing_config({"alerts": {"seal_weakening_ratio": 0.15}})
    fired = evaluate_position_alerts(
        _holding(current_price=10.0),
        quote={"price": 10.0, "fd_amount": 8_000_000, "vwap": 10.0},
        plan=_plan("发酵"),
        trade_date=DATE,
        triggered_at=NOW,
        fd_amount_baseline=10_000_000,
        config=strict,
    )
    assert KIND_SEAL_WEAKENING in _kinds(fired)
