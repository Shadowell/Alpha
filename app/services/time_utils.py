from __future__ import annotations

from datetime import date, datetime, time

from app.config import APP_TZ


def now_cn() -> datetime:
    return datetime.now(APP_TZ)


def now_cn_iso() -> str:
    """当前上海时间（aware）的 ISO 8601 字符串。"""
    return now_cn().isoformat()


def now_cn_naive() -> datetime:
    """上海墙钟时间，但去掉 tzinfo。

    仅供仍以 naive ``datetime.now()`` 做比较的历史调用方
    （如 ``AkshareDataProvider.symbol_name_cache``）使用；新代码必须用
    :func:`now_cn`。待这些调用方迁移到 aware 时间后应删除本函数。
    """
    return datetime.now(APP_TZ).replace(tzinfo=None)


def ensure_cn_tz(dt: datetime) -> datetime:
    """把 datetime 归一到 Asia/Shanghai；naive 值按上海墙钟时间解释。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=APP_TZ)
    return dt.astimezone(APP_TZ)


def parse_cn_datetime(value: str | datetime) -> datetime:
    """解析 ISO 8601 时间并归一到 Asia/Shanghai（兼容历史 naive 字符串）。"""
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    return ensure_cn_tz(parsed)


def today_str() -> str:
    return now_cn().date().isoformat()


def parse_trade_date(value: str | None) -> date:
    if not value:
        return now_cn().date()
    return date.fromisoformat(value)


def is_market_open(dt: datetime | None = None) -> bool:
    cur = dt or now_cn()
    t = cur.timetz().replace(tzinfo=None)
    morning = time(9, 30) <= t <= time(11, 30)
    afternoon = time(13, 0) <= t <= time(15, 0)
    return morning or afternoon


def is_after_close(dt: datetime | None = None) -> bool:
    cur = dt or now_cn()
    return cur.timetz().replace(tzinfo=None) >= time(15, 0)


def elapsed_market_ratio(dt: datetime | None = None) -> float:
    """Return today's trading progress in [0,1]."""
    cur = dt or now_cn()
    t = cur.timetz().replace(tzinfo=None)

    segments = [
        (time(9, 30), time(11, 30), 120),
        (time(13, 0), time(15, 0), 120),
    ]
    total = 240
    passed = 0

    for start, end, length in segments:
        if t <= start:
            continue
        if t >= end:
            passed += length
            continue
        cur_minutes = (datetime.combine(cur.date(), t) - datetime.combine(cur.date(), start)).seconds / 60
        passed += cur_minutes

    ratio = max(0.01, min(passed / total, 1.0))
    return ratio
