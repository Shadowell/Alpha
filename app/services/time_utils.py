from __future__ import annotations

from datetime import date, datetime, time

from app.config import APP_TZ


def now_cn() -> datetime:
    return datetime.now(APP_TZ)


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
