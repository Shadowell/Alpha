"""#32 A 股交易规则校验（strategy.daban_timing.trade_rules）单元测试。

覆盖验收项「模拟盘校验 T+1、100 股整数倍、涨跌停 / 停牌」与「提醒可追溯」。
"""

from __future__ import annotations

import json

import pytest

from strategy.daban_timing.config import TimingConfig, parse_timing_config
from strategy.daban_timing.models import Holding, OrderRequest
from strategy.daban_timing.trade_rules import (
    QuoteSnapshot,
    compute_price_limits,
    limit_ratio_for,
    round_to_lot,
    sellable_quantity,
    validate_order,
)

DATE = "2026-10-22"


def _buy(**overrides) -> OrderRequest:
    payload = {
        "symbol": "600987",
        "name": "测试标的甲",
        "side": "buy",
        "qty": 1000,
        "price": 10.0,
    }
    payload.update(overrides)
    return OrderRequest.from_mapping(payload)


def _hold(**overrides) -> Holding:
    payload = {
        "symbol": "600987",
        "qty": 1000,
        "cost_price": 10.0,
        "current_price": 10.5,
        "open_date": DATE,
    }
    payload.update(overrides)
    return Holding.from_mapping(payload)


def test_limit_ratio_by_board():
    assert limit_ratio_for("600987") == 0.10
    assert limit_ratio_for("000001") == 0.10
    assert limit_ratio_for("300750") == 0.20
    assert limit_ratio_for("688981") == 0.20
    assert limit_ratio_for("830799") == 0.30
    assert limit_ratio_for("600987", is_st=True) == 0.05
    up, down = compute_price_limits(10.0, symbol="600987")
    assert (up, down) == (11.0, 9.0)
    assert compute_price_limits(3.33, symbol="600000") == (3.66, 3.0)  # 四舍五入到分
    assert compute_price_limits(None) == (None, None)


@pytest.mark.parametrize(
    ("qty", "expected"),
    [(1000, 1000), (1050, 1000), (99, 0), (0, 0), (-500, 0), (1500.0, 1500)],
)
def test_round_to_lot_floor(qty, expected):
    assert round_to_lot(qty) == expected


def test_round_to_lot_nearest():
    assert round_to_lot(149, mode="nearest") == 100
    assert round_to_lot(151, mode="nearest") == 200


def test_round_to_lot_modes_and_validation():
    assert round_to_lot(250, 100, mode="floor") == 200
    assert round_to_lot(250, 100, mode="ceil") == 300
    assert round_to_lot(250, 100, mode="nearest") == 300
    with pytest.raises(ValueError):
        round_to_lot(100, mode="half-up")
    with pytest.raises(ValueError):
        round_to_lot(100, lot_size=0)


def test_t_plus_1_blocks_same_day_sell():
    assert sellable_quantity(_hold(), trade_date=DATE) == 0
    assert sellable_quantity(_hold(open_date="2026-10-21"), trade_date=DATE) == 1000
    result = validate_order(
        _buy(side="sell", qty=1000, price=10.5),
        holding=_hold(),
        trade_date=DATE,
    )
    assert result.allowed is False
    assert "trade:t_plus_1" in result.rule_ids
    assert result.sellable_qty == 0


def test_sell_after_t_plus_1_allowed_with_trace_fields():
    result = validate_order(
        _buy(side="sell", qty=1000, price=10.5),
        holding=_hold(open_date="2026-10-21"),
        quote={
            "symbol": "600987",
            "trade_date": DATE,
            "price": 10.5,
            "prev_close": 10.0,
        },
        trade_date=DATE,
    )
    assert result.allowed is True, result.to_dict()
    assert result.sellable_qty == 1000


def test_buy_must_be_lot_multiple():
    result = validate_order(_buy(qty=1550), trade_date=DATE)
    assert result.allowed is False
    assert "trade:lot_size" in result.rule_ids
    assert validate_order(_buy(qty=1000), trade_date=DATE).allowed is True


def test_odd_lot_sell_only_when_closing_position():
    partial = validate_order(
        _buy(side="sell", qty=550, price=10.5),
        holding=_hold(qty=1550, open_date="2026-10-21"),
        trade_date=DATE,
    )
    assert "trade:odd_lot_sell" in partial.rule_ids
    closing = validate_order(
        _buy(side="sell", qty=1550, price=10.5),
        holding=_hold(qty=1550, open_date="2026-10-21"),
        trade_date=DATE,
    )
    assert closing.allowed is True, closing.to_dict()


def test_suspended_blocks_both_sides():
    quote = {"symbol": "600987", "trade_date": DATE, "price": 10.0, "suspended": True}
    assert (
        "trade:suspended"
        in validate_order(_buy(), quote=quote, trade_date=DATE).rule_ids
    )
    sell = validate_order(
        _buy(side="sell", qty=1000),
        holding=_hold(open_date="2026-10-21"),
        quote=quote,
        trade_date=DATE,
    )
    assert "trade:suspended" in sell.rule_ids


def test_limit_up_sealed_and_yizi_reject_buy():
    sealed = {"sealed_at_limit_up": True, "price": 11.0, "limit_up": 11.0}
    result = validate_order(_buy(price=11.0), quote=sealed, trade_date=DATE)
    assert "trade:limit_up_sealed" in result.rule_ids
    assert result.allowed is False
    queue_ok = validate_order(
        _buy(price=11.0), quote=sealed, trade_date=DATE, allow_limit_up_queue_buy=True
    )
    assert queue_ok.allowed is True
    yizi = validate_order(
        _buy(price=11.0),
        quote={"is_yizi_board": True, "price": 11.0, "limit_up": 11.0},
        trade_date=DATE,
    )
    assert "trade:yizi_board" in yizi.rule_ids


def test_limit_down_sealed_rejects_sell():
    result = validate_order(
        _buy(side="sell", qty=1000, price=9.0),
        holding=_hold(open_date="2026-10-21", current_price=9.0),
        quote={"sealed_at_limit_down": True, "price": 9.0, "limit_down": 9.0},
        trade_date=DATE,
    )
    assert "trade:limit_down_sealed" in result.rule_ids


def test_price_out_of_band():
    quote = {"price": 12.0, "prev_close": 10.0, "limit_up": 11.0, "limit_down": 9.0}
    assert (
        "trade:price_above_limit_up"
        in validate_order(_buy(price=12.0), quote=quote, trade_date=DATE).rule_ids
    )
    low = validate_order(
        _buy(price=8.0), quote={**quote, "price": 8.0}, trade_date=DATE
    )
    assert "trade:price_below_limit_down" in low.rule_ids


def test_max_buy_amount_cap_and_derived_qty():
    result = validate_order(
        _buy(qty=None, amount=200_000, price=10.0),
        trade_date=DATE,
        max_buy_amount=150_000,
    )
    assert "trade:max_buy_amount" in result.rule_ids
    assert result.max_buy_qty == 15_000


def test_missing_quote_is_reported_not_assumed_fillable():
    result = validate_order(_buy(), trade_date=DATE)
    assert result.allowed is True  # 价格与手数合法即可提交
    assert "quote_missing:停牌/涨跌停可成交性未校验" in result.checks_skipped
    partial = QuoteSnapshot.from_mapping(
        {"price": 10.0}, symbol="600987", trade_date=DATE
    )
    assert partial.limit_up is None
    assert "price_limits_unknown" in partial.missing


def test_config_lot_size_is_honoured():
    config = parse_timing_config({"portfolio": {"lot_size": 200}})
    result = validate_order(_buy(qty=1500), trade_date=DATE, config=config)
    assert "trade:lot_size" in result.rule_ids
    assert validate_order(
        _buy(qty=1500), trade_date=DATE, config=TimingConfig()
    ).allowed


def test_violations_are_traceable_and_serialisable():
    result = validate_order(
        _buy(qty=1550, price=11.5),
        quote={"price": 11.5, "prev_close": 10.0, "as_of": f"{DATE} 09:40"},
        trade_date=DATE,
    )
    payload = json.loads(json.dumps(result.to_dict(), ensure_ascii=False))
    assert payload["allowed"] is False
    entry = next(
        item for item in payload["violations"] if item["rule_id"] == "trade:lot_size"
    )
    assert entry["price"] == 11.5
    assert entry["threshold"] == 100.0
