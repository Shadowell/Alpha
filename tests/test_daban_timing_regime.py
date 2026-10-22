"""#32 严格 PIT 的 regime 取数（strategy.daban_timing.regime）单元测试。

核心锁定：盘后标签只有在收盘后才可用；盘中只能用 ``intraday_*``；
两个口径互不污染；缺数一律显式缺失。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from strategy.daban_timing.regime import (
    close_available_moment,
    parse_decision_at,
    resolve_regime,
    split_intraday,
)

TRADE_DATE = "2026-10-22"


def _close_factors(**overrides):
    factors = {
        "trade_date": TRADE_DATE,
        "regime": "发酵",
        "sentiment_score": 62.5,
        "prev_regime": "修复",
        "prev_sentiment_score": 55.0,
        "limit_up_count": 88,
    }
    factors.update(overrides)
    return factors


def test_close_label_available_after_close_time():
    snapshot = resolve_regime(
        _close_factors(), trade_date=TRADE_DATE, decision_at=f"{TRADE_DATE} 15:30"
    )
    assert (snapshot.source, snapshot.regime, snapshot.score) == ("close", "发酵", 62.5)
    assert snapshot.available is True
    assert snapshot.prev_regime == "修复"
    assert snapshot.prev_score == 55.0


def test_close_label_before_close_time_is_not_available():
    snapshot = resolve_regime(
        _close_factors(),
        trade_date=TRADE_DATE,
        decision_at=f"{TRADE_DATE} 10:00",
        mode="close",
    )
    assert snapshot.source == "missing"
    assert snapshot.available is False
    assert snapshot.missing == ("regime_label_not_available_at_decision_time",)
    assert snapshot.regime is None  # 绝不提前泄露收盘标签


def test_auto_mode_falls_back_to_intraday_before_close():
    factors = _close_factors(
        intraday_regime="高潮",
        intraday_sentiment_score=78.0,
        regime=None,
        sentiment_score=None,
    )
    snapshot = resolve_regime(
        factors, trade_date=TRADE_DATE, decision_at=f"{TRADE_DATE} 14:30"
    )
    assert snapshot.source == "intraday"
    assert snapshot.regime == "高潮"
    assert snapshot.score == 78.0
    assert "close_label_not_yet_available_use_intraday" in snapshot.missing


def test_intraday_fields_never_leak_into_close_branch():
    # 盘中估算与收盘标签故意不同：mode=close 只可能拿到收盘标签。
    factors = _close_factors(intraday_regime="冰点", intraday_sentiment_score=12.0)
    close = resolve_regime(
        factors, trade_date=TRADE_DATE, decision_at=f"{TRADE_DATE} 16:00"
    )
    assert (close.regime, close.score, close.source) == ("发酵", 62.5, "close")
    intraday = resolve_regime(
        factors,
        trade_date=TRADE_DATE,
        decision_at=f"{TRADE_DATE} 16:00",
        mode="intraday",
    )
    assert (intraday.regime, intraday.score, intraday.source) == (
        "冰点",
        12.0,
        "intraday",
    )


def test_intraday_mode_ignores_close_columns():
    factors = {
        "trade_date": TRADE_DATE,
        "intraday_regime": "分歧",
        "intraday_sentiment_score": 57.0,
    }
    snapshot = resolve_regime(
        factors,
        trade_date=TRADE_DATE,
        decision_at=f"{TRADE_DATE} 11:00",
        mode="intraday",
    )
    assert snapshot.regime == "分歧"
    assert snapshot.missing == ()


def test_missing_intraday_estimate_is_explicit():
    snapshot = resolve_regime(
        _close_factors(),
        trade_date=TRADE_DATE,
        decision_at=f"{TRADE_DATE} 10:00",
        mode="intraday",
    )
    assert snapshot.source == "missing"
    assert snapshot.missing == ("intraday_regime_missing",)


def test_future_label_is_rejected():
    snapshot = resolve_regime(
        _close_factors(trade_date="2026-10-23", regime="高潮"),
        trade_date="2026-10-23",
        decision_at=f"{TRADE_DATE} 16:00",
    )
    assert snapshot.source == "missing"
    assert "regime_label_from_future" in snapshot.missing


def test_label_date_mismatch_is_rejected():
    snapshot = resolve_regime(
        _close_factors(trade_date="2026-10-21"),
        trade_date=TRADE_DATE,
        decision_at=f"{TRADE_DATE} 16:00",
    )
    assert snapshot.source == "missing"
    assert snapshot.missing[0].startswith("regime_label_date_mismatch")


def test_unscored_day_has_no_regime_and_marks_missing():
    snapshot = resolve_regime(
        _close_factors(regime=None, sentiment_score=None),
        trade_date=TRADE_DATE,
        decision_at=f"{TRADE_DATE} 16:00",
        mode="close",
    )
    assert snapshot.regime is None
    assert snapshot.missing == ("regime_label_missing",)
    # 只有分数缺失、标签仍在：可用但显式标记缺数
    snapshot2 = resolve_regime(
        _close_factors(sentiment_score=float("nan")),
        trade_date=TRADE_DATE,
        decision_at=f"{TRADE_DATE} 16:00",
    )
    assert snapshot2.available is True
    assert snapshot2.score is None
    assert snapshot2.missing == ("sentiment_score_missing",)


def test_next_day_close_label_is_available():
    snapshot = resolve_regime(
        _close_factors(), trade_date=TRADE_DATE, decision_at="2026-10-23 09:25"
    )
    assert snapshot.source == "close"


def test_split_intraday_partitions_keys():
    close, intraday = split_intraday(
        {
            "regime": "发酵",
            "intraday_regime": "冰点",
            "limit_up_count": 3,
            "intraday_limit_up_count": 9,
        }
    )
    assert set(close) == {"regime", "limit_up_count"}
    assert set(intraday) == {"intraday_regime", "intraday_limit_up_count"}


def test_parse_decision_at_forms():
    assert parse_decision_at("2026-10-22") == datetime(2026, 10, 22, 0, 0)
    assert parse_decision_at("2026-10-22 15:30") == datetime(2026, 10, 22, 15, 30)
    assert parse_decision_at("2026-10-22T15:30:05") == datetime(2026, 10, 22, 15, 30, 5)
    assert parse_decision_at("2026/10/22 15:30") == datetime(2026, 10, 22, 15, 30)
    dotted = ".".join(("2026", "10", "22")) + " 15:30:10"
    assert parse_decision_at(dotted) == datetime(2026, 10, 22, 15, 30, 10)
    aware = datetime(2026, 10, 22, 8, 0, tzinfo=timezone.utc)  # UTC 08:00 = 北京 16:00
    assert parse_decision_at(aware) == datetime(2026, 10, 22, 16, 0)
    assert close_available_moment(TRADE_DATE, "15:05") == datetime(2026, 10, 22, 15, 5)
    with pytest.raises(ValueError):
        parse_decision_at("2026-13-45 99:99")


def test_unknown_mode_raises():
    with pytest.raises(ValueError):
        resolve_regime(
            _close_factors(), trade_date=TRADE_DATE, decision_at="x", mode="weekly"
        )


def test_label_availability_is_monotonic_in_decision_time():
    factors = _close_factors()
    assert (
        resolve_regime(
            factors, trade_date=TRADE_DATE, decision_at="2026-10-22 14:00", mode="close"
        ).source
        == "missing"
    )
    assert (
        resolve_regime(
            factors,
            trade_date=TRADE_DATE,
            decision_at="2026-10-22 15:04:59",
            mode="close",
        ).source
        == "missing"
    )
    assert (
        resolve_regime(
            factors, trade_date=TRADE_DATE, decision_at="2026-10-22 15:05"
        ).source
        == "close"
    )
    assert (
        resolve_regime(
            factors, trade_date=TRADE_DATE, decision_at="2026-10-26 09:00"
        ).source
        == "close"
    )


def test_auto_mode_records_close_reason_when_falling_back_after_close():
    # 收盘标签缺失（例如盘后调度未跑）+ 有盘中估算 → 用估算并记录原因
    factors = _close_factors(regime=None, sentiment_score=None)
    factors.pop("regime")
    factors["intraday_regime"] = "退潮"
    factors["intraday_sentiment_score"] = 28.0
    snapshot = resolve_regime(
        factors, trade_date=TRADE_DATE, decision_at=f"{TRADE_DATE} 16:00"
    )
    assert snapshot.source == "intraday"
    assert snapshot.missing == ("close_regime_label_missing_use_intraday",)
