"""事件可见时间对齐单测（issue #35 验收项：盘后公告不进入当日因子）。"""

from __future__ import annotations

import pytest

from strategy.daban_events.disclosure import (
    TradingCalendar,
    TradingHalt,
    VISIBLE_SOURCE_CONSERVATIVE,
    VISIBLE_SOURCE_EXACT,
    VISIBLE_SOURCE_MANUAL,
    VISIBLE_SOURCE_MISSING,
    WINDOW_AUCTION,
    WINDOW_INTRADAY,
    WINDOW_NON_TRADING_DAY,
    WINDOW_POST_MARKET,
    WINDOW_PRE_MARKET,
    WINDOW_UNKNOWN,
    classify_window,
    first_tradeable_date,
    parse_datetime,
    resolve_disclosure,
    usable_for_decision,
)

DATES = [
    "2026-03-02",
    "2026-03-03",
    "2026-03-04",
    "2026-03-05",
    "2026-03-06",
    "2026-03-09",  # 3-07/3-08 为周末
]
CAL = TradingCalendar.from_values(DATES)


def test_calendar_parses_and_sorts_and_dedups():
    cal = TradingCalendar.from_values(
        ["20260304", "2026-03-02", "2026-03-02", None, ""]
    )
    assert cal.as_list() == ["2026-03-02", "2026-03-04"]
    assert cal.available is True
    assert TradingCalendar().available is False


def test_next_prev_and_shift():
    assert CAL.next_trade_date("2026-03-06") == "2026-03-09"
    assert CAL.prev_trade_date("2026-03-03") == "2026-03-02"
    assert CAL.shift("2026-03-03", 2) == "2026-03-05"
    assert CAL.next_trade_date("2026-03-09") is None
    assert CAL.shift("2026-03-09", 1) is None


def test_no_timestamp_is_conservatively_after_close():
    """公告日期 != 可得时间：无时间戳必须按盘后可见，决策顺延到次日。"""

    timing = resolve_disclosure(announce_date="2026-03-03", calendar=CAL)
    assert timing.visible_at_source == VISIBLE_SOURCE_CONSERVATIVE
    assert timing.window == WINDOW_POST_MARKET
    assert timing.close_decision_date == "2026-03-04"
    assert timing.auction_decision_date == "2026-03-04"
    assert "no_disclosure_timestamp:assume_after_close" in timing.notes


def test_after_close_announcement_never_usable_on_same_day():
    """验收项硬约束：晚间公告在公告当日 15:00 决策时不可用。"""

    timing = resolve_disclosure(
        announce_date="2026-03-03", announce_time="20:15", calendar=CAL
    )
    assert timing.visible_at_source == VISIBLE_SOURCE_EXACT
    assert usable_for_decision(timing, "2026-03-03", mode="close") is False
    assert usable_for_decision(timing, "2026-03-03", mode="auction") is False
    assert usable_for_decision(timing, "2026-03-04", mode="close") is True
    assert usable_for_decision(timing, "2026-03-04", mode="auction") is True


def test_exact_1500_boundary_is_not_intraday():
    timing = resolve_disclosure(announce_date="2026-03-03 15:00", calendar=CAL)
    assert timing.window == WINDOW_POST_MARKET
    assert timing.close_decision_date == "2026-03-04"


@pytest.mark.parametrize(
    ("stamp", "window", "close_d", "auction_d"),
    [
        ("08:30", WINDOW_PRE_MARKET, "2026-03-03", "2026-03-03"),
        ("09:20", WINDOW_AUCTION, "2026-03-03", "2026-03-04"),
        ("10:30", WINDOW_INTRADAY, "2026-03-03", "2026-03-04"),
        ("14:00", WINDOW_INTRADAY, "2026-03-03", "2026-03-04"),
        ("18:40", WINDOW_POST_MARKET, "2026-03-04", "2026-03-04"),
    ],
)
def test_intraday_and_premarket_alignment(
    stamp: str, window: str, close_d: str, auction_d: str
):
    timing = resolve_disclosure(
        announce_date="2026-03-03", announce_time=stamp, calendar=CAL
    )
    assert timing.window == window
    assert timing.close_decision_date == close_d
    assert timing.auction_decision_date == auction_d


def test_intraday_news_is_usable_for_close_decision_only():
    """盘中公告：当日收盘决策可用，但当日竞价不可用（不能倒推成盘前信息）。"""

    timing = resolve_disclosure(
        announce_date="2026-03-03", announce_time="11:00", calendar=CAL
    )
    assert usable_for_decision(timing, "2026-03-03", mode="close") is True
    assert usable_for_decision(timing, "2026-03-03", mode="auction") is False


def test_weekend_and_holiday_roll_to_next_trade_date():
    timing = resolve_disclosure(
        announce_date="2026-03-07", announce_time="19:00", calendar=CAL
    )
    assert timing.window == WINDOW_NON_TRADING_DAY
    assert timing.close_decision_date == "2026-03-09"
    assert timing.auction_decision_date == "2026-03-09"


def test_missing_calendar_marks_explicit_absence():
    """交易日没同步（线上 #22 当前状态）时决策日必须缺失，不能自然日推算。"""

    timing = resolve_disclosure(announce_date="2026-03-03", calendar=TradingCalendar())
    assert timing.calendar_available is False
    assert timing.window == WINDOW_UNKNOWN
    assert timing.close_decision_date is None
    assert timing.auction_decision_date is None
    assert timing.missing is True
    assert "trade_calendar_unavailable:decision_date_missing" in timing.notes


def test_decision_date_beyond_calendar_is_missing_not_guessed():
    timing = resolve_disclosure(
        announce_date="2026-03-09", announce_time="20:00", calendar=CAL
    )
    assert timing.close_decision_date is None
    assert timing.missing is True
    assert "decision_date_beyond_loaded_calendar" in timing.notes


def test_missing_announce_date():
    timing = resolve_disclosure(announce_date=None, calendar=CAL)
    assert timing.visible_at is None
    assert timing.visible_at_source == VISIBLE_SOURCE_MISSING
    assert timing.missing is True


def test_manual_override_source_flag():
    timing = resolve_disclosure(
        announce_date="2026-03-03",
        exact_visible_at="2026-03-03 07:10:00",
        calendar=CAL,
    )
    assert timing.visible_at_source == VISIBLE_SOURCE_MANUAL
    assert timing.auction_decision_date == "2026-03-03"


def test_non_conservative_request_still_falls_back():
    """调用方要求激进口径但没有时间戳 —— 只登记，不静默放宽。"""

    timing = resolve_disclosure(
        announce_date="2026-03-03", calendar=CAL, conservative=False
    )
    assert timing.window == WINDOW_POST_MARKET
    assert "non_conservative_requested_but_no_timestamp" in timing.notes


def test_unparseable_time_falls_back_to_conservative():
    timing = resolve_disclosure(
        announce_date="2026-03-03", announce_time="待定", calendar=CAL
    )
    assert timing.visible_at_source == VISIBLE_SOURCE_CONSERVATIVE


def test_parse_datetime_handles_common_formats():
    assert parse_datetime("2026-03-03 20:15:00").isoformat() == "2026-03-03T20:15:00"
    assert parse_datetime("202603032015").isoformat() == "2026-03-03T20:15:00"
    assert parse_datetime("2026/03/03").isoformat() == "2026-03-03T00:00:00"
    assert parse_datetime("") is None
    assert parse_datetime("无") is None


def test_classify_window_on_empty_calendar_is_unknown():
    from datetime import datetime

    assert (
        classify_window(datetime(2026, 3, 3, 10, 0), TradingCalendar())
        == WINDOW_UNKNOWN
    )


def test_usable_for_decision_rejects_unknown_mode():
    timing = resolve_disclosure(announce_date="2026-03-03", calendar=CAL)
    with pytest.raises(ValueError):
        usable_for_decision(timing, "2026-03-04", mode="intraday")


def test_first_tradeable_date_skips_suspension():
    """停牌复牌：公告可见日不等于可交易日。"""

    halts = {"000001": [TradingHalt(start="2026-03-03", end="2026-03-05")]}
    assert (
        first_tradeable_date("2026-03-03", calendar=CAL, halts=halts, symbol="000001")
        == "2026-03-06"
    )


def test_first_tradeable_date_open_ended_suspension_is_unknown():
    halts = [TradingHalt(start="2026-03-03")]
    assert (
        first_tradeable_date("2026-03-04", calendar=CAL, halts=halts, symbol="X")
        is None
    )


def test_first_tradeable_date_requires_calendar():
    assert first_tradeable_date("2026-03-04", calendar=TradingCalendar()) is None
    assert first_tradeable_date(None, calendar=CAL) is None
    assert first_tradeable_date("2026-03-04", calendar=CAL) == "2026-03-04"


def test_timing_dict_flags_missing_and_after_close():
    timing = resolve_disclosure(announce_date="2026-03-03", calendar=CAL)
    payload = timing.as_dict()
    assert payload["timing_missing"] == 0
    assert payload["is_after_close"] == 1
    assert payload["disclosure_window"] == WINDOW_POST_MARKET
