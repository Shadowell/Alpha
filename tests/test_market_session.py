"""#57：交易时段服务端口径（market/session 契约 + /api/funnel 附带字段）。

覆盖：交易日盘中 / 午间休市 / 休市 / 盘前四类时点、法定节假日（工作日但日历无该日）、
UTC 输入归一化、china_time 一律 +08:00 aware ISO、端点响应字段与前端契约逐字一致。
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import market as market_router
from app.services import market_session as ms
from app.services.funnel_service import FunnelService
from app.services.market_session import (
    MARKET_SESSION_VALUES,
    build_session_payload,
    classify_market_session,
    is_trading_calendar_day,
    resolve_market_session,
)

CN = ZoneInfo("Asia/Shanghai")


def _cn(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=CN)


# ── 纯函数：时段口径（与前端 core.js._CN_MARKET_RANGES 对齐，左闭右开）──


def test_classify_sessions_on_trading_day():
    assert classify_market_session(_cn(2026, 10, 22, 9, 0)) == "pre"
    assert classify_market_session(_cn(2026, 10, 22, 9, 30)) == "open"
    assert classify_market_session(_cn(2026, 10, 22, 11, 29)) == "open"
    assert classify_market_session(_cn(2026, 10, 22, 11, 30)) == "lunch"
    assert classify_market_session(_cn(2026, 10, 22, 12, 30)) == "lunch"
    assert classify_market_session(_cn(2026, 10, 22, 13, 0)) == "open"
    assert classify_market_session(_cn(2026, 10, 22, 14, 59)) == "open"
    assert classify_market_session(_cn(2026, 10, 22, 15, 0)) == "closed"
    assert classify_market_session(_cn(2026, 10, 22, 20, 0)) == "closed"


def test_non_trading_day_always_holiday():
    assert (
        classify_market_session(_cn(2026, 10, 22, 10, 12), trading_day=False)
        == "holiday"
    )


# ── 交易日判定：日历优先（识别节假日），未覆盖时工作日近似 ──


def test_calendar_marks_statutory_holiday_even_on_weekday():
    # 2026-10-01（周四，国庆）不在日历内；日历覆盖到 10-08
    days = ["2026-09-28", "2026-09-29", "2026-09-30", "2026-10-08", "2026-10-09"]
    assert is_trading_calendar_day(date(2026, 10, 1), days) is False
    assert is_trading_calendar_day(date(2026, 10, 8), days) is True


def test_weekend_and_missing_calendar_fallback():
    # 日历覆盖到 10-26：10-24（周六）不在日历 → 非交易日
    assert (
        is_trading_calendar_day(
            date(2026, 10, 24), ["2026-10-21", "2026-10-22", "2026-10-23", "2026-10-26"]
        )
        is False
    )
    assert (
        is_trading_calendar_day(date(2026, 10, 22), []) is True
    )  # 无日历 → 工作日近似
    assert is_trading_calendar_day(date(2026, 10, 24), []) is False
    # 日历未覆盖今天（同步滞后）→ 回退工作日近似，不武断判休市
    assert is_trading_calendar_day(date(2026, 10, 22), ["2026-10-16"]) is True


# ── payload 契约 ──


def test_payload_trading_day_intraday():
    payload = build_session_payload(
        now=_cn(2026, 10, 22, 10, 12), trade_days=["2026-10-22"]
    )
    assert payload == {
        "market_session": "open",
        "is_trading_day": True,
        "china_time": "2026-10-22T10:12:00+08:00",
    }


def test_payload_lunch_and_holiday():
    lunch = build_session_payload(
        now=_cn(2026, 10, 22, 12, 30), trade_days=["2026-10-22"]
    )
    assert lunch["market_session"] == "lunch"
    holiday = build_session_payload(
        now=_cn(2026, 10, 24, 10, 0), trade_days=["2026-10-23", "2026-10-26"]
    )
    assert holiday["market_session"] == "holiday"
    assert holiday["is_trading_day"] is False


def test_payload_normalizes_non_shanghai_clock():
    utc_now = datetime(2026, 10, 22, 4, 12, tzinfo=timezone.utc)  # = 北京时间 12:12
    payload = build_session_payload(now=utc_now, trade_days=["2026-10-22"])
    assert payload["market_session"] == "lunch"
    assert payload["china_time"] == "2026-10-22T12:12:00+08:00"


def test_naive_now_is_interpreted_as_shanghai_wall_clock():
    payload = build_session_payload(
        now=datetime(2026, 10, 22, 14, 30), trade_days=["2026-10-22"]
    )
    assert payload["market_session"] == "open"
    assert payload["china_time"].endswith("+08:00")


# ── resolve（provider 日历容错）──


class CalendarProvider:
    def __init__(self, days: list[str] | None = None, fail: bool = False):
        self.days = days if days is not None else []
        self.fail = fail

    async def get_trade_days(self):
        if self.fail:
            raise RuntimeError("akshare calendar upstream blew up")
        return pd.DataFrame({"trade_date": self.days})


def test_resolve_uses_provider_calendar():
    payload = asyncio.run(
        resolve_market_session(
            CalendarProvider(["2026-10-22"]), now=_cn(2026, 10, 22, 10, 12)
        )
    )
    assert payload["market_session"] == "open" and payload["is_trading_day"] is True


def test_resolve_degrades_without_calendar_support():
    class NoCalendar:
        pass

    payload = asyncio.run(
        resolve_market_session(NoCalendar(), now=_cn(2026, 10, 24, 10, 12))
    )
    assert payload["market_session"] == "holiday"  # 周六 + 无日历 → 近似休市
    assert payload["china_time"].endswith("+08:00")

    failing = asyncio.run(
        resolve_market_session(
            CalendarProvider(fail=True), now=_cn(2026, 10, 22, 10, 12)
        )
    )
    assert failing["market_session"] == "open"


# ── API 级 ──


def _client(container) -> TestClient:
    app = FastAPI()
    app.include_router(market_router.router, prefix="/api")
    app.state.container = container
    return TestClient(app)


@pytest.mark.parametrize(
    ("fixed_now", "expect_session"),
    [
        (_cn(2026, 10, 22, 10, 12), "open"),
        (_cn(2026, 10, 22, 12, 30), "lunch"),
        (_cn(2026, 10, 1, 10, 12), "holiday"),  # 周四但落在国庆假期
    ],
)
def test_api_market_session_contract(monkeypatch, fixed_now, expect_session):
    monkeypatch.setattr(ms, "now_cn", lambda: fixed_now)
    provider = CalendarProvider(["2026-09-30", "2026-10-08", "2026-10-22"])
    client = _client(SimpleNamespace(provider=provider))
    resp = client.get("/api/market/session")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"market_session", "is_trading_day", "china_time"}
    assert body["market_session"] == expect_session
    assert body["market_session"] in MARKET_SESSION_VALUES
    assert body["is_trading_day"] is (expect_session != "holiday")
    assert body["china_time"].endswith("+08:00")


class _FunnelProvider(CalendarProvider):
    kline_store = None

    async def get_realtime_snapshot(self, **kwargs):
        return pd.DataFrame()

    async def get_symbol_name_map(self, cache_ttl_seconds=3600):
        return {}


def test_api_funnel_carries_session_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(ms, "now_cn", lambda: _cn(2026, 10, 22, 10, 12))
    service = FunnelService(
        provider=_FunnelProvider(["2026-10-22"]),
        persist_db_path=str(tmp_path / "funnel_state.db"),
    )
    service.trade_date = "2026-10-22"
    client = _client(SimpleNamespace(funnel_service=service, provider=service.provider))
    resp = client.get("/api/funnel")
    assert resp.status_code == 200
    body = resp.json()
    assert body["market_session"] == "open"
    assert body["is_trading_day"] is True
    assert body["china_time"] == "2026-10-22T10:12:00+08:00"
