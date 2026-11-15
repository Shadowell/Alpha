"""事件"投资者实际可见时间"对齐（issue #35 的最大风险点）。

为什么要单独一个模块
--------------------
公告数据源（含 akshare ``stock_notice_report``）通常只给 **公告日期**，
不给披露时刻。把公告日期当作可得时间会直接产生前视偏差：晚间 20:00 挂到
巨潮的公告，在公告当日 09:30 的盘前决策里根本不存在。本模块把
「公告日期」翻译成两个可判定的决策日：

* ``close_decision_date``：第一个满足 ``visible_at < D 15:00`` 的交易日 ——
  也就是"T 日收盘后决策、T+1 竞价/开盘接力"这条链路里，事件第一次
  可以被用于决策的日期（对应 #23 场景 B）；
* ``auction_decision_date``：第一个满足 ``visible_at < D 09:15`` 的交易日 ——
  能参与当日集合竞价挂板的最早日期（09:15 起才可挂单）。

默认口径是**保守**的：没有精确时间戳时，一律按"公告当交易日收盘后才可见"
处理（``visible_at = 公告日 15:00:00``，收盘界限用严格小于，因此
15:00:00 本身不算当日可得）。这样盘后公告绝不会进入当日因子，
代价是丢掉一部分真实的当日盘前信息 —— 这是可接受的保守偏差，
而前视偏差不可接受。要拿到激进口径必须由数据源提供精确时间戳。

停牌 / 复牌
----------
"看得见但买不到"是第二类时点风险：复牌公告可见当日可能仍停牌，
或者复牌首日一字板。:func:`first_tradeable_date` 只接受显式的停牌区间，
区间未知时返回 ``None`` 并登记 missing，绝不假设"能交易"。

所有时间字符串都是 ``Asia/Shanghai``（A 股无夏令时）。
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Iterable, Mapping, Sequence

TZ_NAME = "Asia/Shanghai"

AUCTION_ORDER_OPEN = time(9, 15)
"""09:15 起集合竞价可以挂单，之前的信息才算"竞价可用"。"""

MARKET_OPEN = time(9, 30)
MORNING_CLOSE = time(11, 30)
AFTERNOON_OPEN = time(13, 0)
MARKET_CLOSE = time(15, 0)

WINDOW_PRE_MARKET = "pre_market"
WINDOW_AUCTION = "auction"
WINDOW_INTRADAY = "intraday"
WINDOW_LUNCH = "lunch_break"
WINDOW_POST_MARKET = "post_market"
WINDOW_NON_TRADING_DAY = "non_trading_day"
WINDOW_UNKNOWN = "unknown"

ALL_WINDOWS: tuple[str, ...] = (
    WINDOW_PRE_MARKET,
    WINDOW_AUCTION,
    WINDOW_INTRADAY,
    WINDOW_LUNCH,
    WINDOW_POST_MARKET,
    WINDOW_NON_TRADING_DAY,
    WINDOW_UNKNOWN,
)

VISIBLE_SOURCE_EXACT = "source_timestamp"
VISIBLE_SOURCE_CONSERVATIVE = "conservative_after_close"
VISIBLE_SOURCE_MANUAL = "manual_override"
VISIBLE_SOURCE_MISSING = "missing"

#: 保守哨兵：无时间戳时视为该交易日收盘后 15:00:00 才可见。
CONSERVATIVE_VISIBLE_TIME = time(15, 0, 0)

_CLOCK_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")


def _iso(d: date) -> str:
    return d.isoformat()


def parse_date(value: Any) -> date | None:
    """宽松解析 YYYYMMDD / YYYY-MM-DD / datetime / ISO datetime。"""

    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip().replace("-", "").replace("/", "")
    if len(text) >= 8 and text[:8].isdigit():
        try:
            return datetime.strptime(text[:8], "%Y%m%d").date()
        except ValueError:
            return None
    return None


_DATETIME_FORMATS: tuple[str, ...] = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M",
    "%Y%m%d%H%M%S",
    "%Y%m%d%H%M",
    "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
)


def _strptime_exact(text: str, fmt: str) -> datetime | None:
    """只有能"整串回写"的格式才算命中。

    Python 的 ``%H/%M/%S`` 允许 1-2 位数字，纯 ``strptime`` 会把
    ``"202603032015"`` 解析成 20:01:05（``%M`` 吃掉 "1"、``%S`` 吃掉 "5"）。
    回写比对能把这种"少一位补零"的错读挡掉。
    """

    try:
        parsed = datetime.strptime(text, fmt)
    except ValueError:
        return None
    if parsed.strftime(fmt) != text:
        return None
    return parsed


def parse_datetime(value: Any) -> datetime | None:
    """解析披露时刻；解析不出来返回 ``None``（调用方必须走保守口径）。"""

    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    text = str(value).strip().rstrip("Z")
    if not text:
        return None
    normalized = text.replace("/", "-")
    for haystack in (normalized, text):
        for fmt in _DATETIME_FORMATS:
            parsed = _strptime_exact(haystack, fmt)
            if parsed is not None:
                return parsed
        if haystack == text:
            break
    # 兜底：先取日期，再尝试从字符串里抠出 HH:MM。
    day = parse_date(normalized)
    if day is None:
        return None
    clock = _extract_clock(normalized)
    if clock is None:
        return datetime.combine(day, time.min)
    return datetime.combine(day, clock)


def _extract_clock(text: str) -> time | None:
    digits = "".join(ch for ch in text if ch.isdigit())
    for start, length in ((8, 6), (8, 4)):
        if len(digits) >= start + length:
            chunk = digits[start : start + length]
            try:
                hour = int(chunk[0:2])
                minute = int(chunk[2:4])
                second = int(chunk[4:6]) if length == 6 else 0
            except ValueError:
                continue
            if 0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59:
                return time(hour, minute, second)
    return None


@dataclass(frozen=True, slots=True)
class TradingCalendar:
    """只依赖已同步交易日的极简日历。

    空日历代表"交易日数据还没同步"（线上 #22 当前状态）—— 此时所有决策日
    都必须显式缺失，而不是用"跳过周末"的自然日推算糊过去。
    """

    trade_dates: tuple[str, ...] = ()

    @classmethod
    def from_values(cls, values: Iterable[Any] | None) -> "TradingCalendar":
        parsed: list[str] = []
        for value in values or ():
            day = parse_date(value)
            if day is not None:
                parsed.append(_iso(day))
        return cls(tuple(sorted(set(parsed))))

    @property
    def available(self) -> bool:
        return bool(self.trade_dates)

    def is_trade_date(self, day: date | str) -> bool:
        value = day if isinstance(day, str) else _iso(day)
        return value in self.trade_dates

    def index_of(self, day: date | str) -> int | None:
        value = day if isinstance(day, str) else _iso(day)
        pos = bisect_left(self.trade_dates, value)
        if pos < len(self.trade_dates) and self.trade_dates[pos] == value:
            return pos
        return None

    def next_trade_date(self, day: date | str) -> str | None:
        """严格大于 ``day`` 的第一个交易日；日历不足时返回 ``None``。"""

        value = day if isinstance(day, str) else _iso(day)
        pos = bisect_right(self.trade_dates, value)
        if pos < len(self.trade_dates):
            return self.trade_dates[pos]
        return None

    def prev_trade_date(self, day: date | str) -> str | None:
        value = day if isinstance(day, str) else _iso(day)
        pos = bisect_left(self.trade_dates, value)
        if pos > 0:
            return self.trade_dates[pos - 1]
        return None

    def on_or_after(self, day: date | str) -> str | None:
        value = day if isinstance(day, str) else _iso(day)
        pos = bisect_left(self.trade_dates, value)
        if pos < len(self.trade_dates):
            return self.trade_dates[pos]
        return None

    def shift(self, day: date | str, offset: int) -> str | None:
        """按交易日偏移（offset 可为负）；越界返回 ``None``。"""

        idx = self.index_of(day)
        if idx is None:
            anchor = self.on_or_after(day)
            idx = self.index_of(anchor) if anchor is not None else None
        if idx is None:
            return None
        target = idx + int(offset)
        if 0 <= target < len(self.trade_dates):
            return self.trade_dates[target]
        return None

    def as_list(self) -> list[str]:
        return list(self.trade_dates)


@dataclass(frozen=True, slots=True)
class TradingHalt:
    """显式停牌区间：``[start, end]`` 含端点，ISO 日期字符串。"""

    start: str
    end: str | None = None

    def covers(self, day: str) -> bool:
        if day < self.start:
            return False
        return self.end is None or day <= self.end


def suspension_for(
    symbol: str,
    day: str,
    halts: Sequence[TradingHalt] | Mapping[str, Sequence[TradingHalt]],
) -> TradingHalt | None:
    """查某标的在某日是否处于停牌区间内。

    ``halts`` 可以是 ``{symbol: [TradingHalt, ...]}`` 或裸区间列表
    （裸列表按"同一标的"解释）。
    """

    if isinstance(halts, Mapping):
        candidates = halts.get(symbol, ())
    else:
        candidates = halts
    for halt in candidates:
        if halt.covers(day):
            return halt
    return None


def classify_window(visible_at: datetime, calendar: TradingCalendar) -> str:
    """按 A 股交易时段给可见时刻分类。"""

    day = visible_at.date()
    if not calendar.available:
        return WINDOW_UNKNOWN
    if not calendar.is_trade_date(day):
        return WINDOW_NON_TRADING_DAY
    clock = visible_at.timetz().replace(tzinfo=None)
    if clock < AUCTION_ORDER_OPEN:
        return WINDOW_PRE_MARKET
    if clock < MARKET_OPEN:
        return WINDOW_AUCTION
    if MARKET_OPEN <= clock < MORNING_CLOSE:
        return WINDOW_INTRADAY
    if MORNING_CLOSE <= clock < AFTERNOON_OPEN:
        return WINDOW_LUNCH
    if AFTERNOON_OPEN <= clock < MARKET_CLOSE:
        return WINDOW_INTRADAY
    return WINDOW_POST_MARKET


@dataclass(slots=True)
class DisclosureTiming:
    """一条公告的可见时间与决策日对齐结果。"""

    announce_date: str | None
    visible_at: str | None
    visible_at_source: str
    window: str
    close_decision_date: str | None
    auction_decision_date: str | None
    calendar_available: bool
    notes: list[str] = field(default_factory=list)

    @property
    def missing(self) -> bool:
        return self.visible_at is None or self.close_decision_date is None

    @property
    def is_after_close(self) -> bool:
        return self.window in (WINDOW_POST_MARKET, WINDOW_NON_TRADING_DAY)

    def as_dict(self) -> dict[str, Any]:
        return {
            "announce_date": self.announce_date,
            "disclosure_time": self.visible_at,
            "disclosure_window": self.window,
            "visible_at_source": self.visible_at_source,
            "close_decision_date": self.close_decision_date,
            "auction_decision_date": self.auction_decision_date,
            "calendar_available": int(self.calendar_available),
            "timing_missing": int(self.missing),
            "is_after_close": int(self.is_after_close),
            "timing_notes": ";".join(self.notes),
        }


def _parse_clock_only(value: Any) -> time | None:
    """解析裸时刻 ``HH:MM[:SS]``（公告源常把日期与时间分两列返回）。"""

    text = str(value or "").strip()
    match = _CLOCK_RE.match(text)
    if match is None:
        return None
    hour, minute, second = (
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3) or 0),
    )
    if hour > 23 or minute > 59 or second > 59:
        return None
    return time(hour, minute, second)


def _stamp_from_inputs(
    announce_time: Any, day: date | None, day_parsed_from_time: date | None
) -> datetime | None:
    """把 ``announce_time`` 归一成一个 naive datetime 可见时刻。

    支持三种形态：完整时间戳、裸 ``HH:MM``、``{}``。解析不出来返回 ``None``，
    由调用方回落到保守口径。
    """

    if announce_time is None:
        return None
    if day_parsed_from_time is not None:
        full = parse_datetime(announce_time)
        if full is not None and full.time() != time.min:
            return full
    if day is None:
        return None
    clock = _parse_clock_only(announce_time)
    if clock is not None:
        return datetime.combine(day, clock)
    return None


def resolve_disclosure(
    *,
    announce_date: Any,
    announce_time: Any = None,
    calendar: TradingCalendar | None = None,
    exact_visible_at: Any = None,
    conservative: bool = True,
) -> DisclosureTiming:
    """把公告日期/时刻解析为「可见时刻 + 两个决策日」。

    优先级：``exact_visible_at`` > ``announce_date + announce_time``（可解析出
    时分）> 保守哨兵（``conservative=True``）> 缺失。

    ``conservative=False`` 只在调用方确认数据源给的就是"披露即入库"的
    真实时间戳时才允许使用，且会写入 notes 供审计。
    """

    cal = calendar or TradingCalendar()
    notes: list[str] = []

    explicit = parse_datetime(exact_visible_at)
    day = parse_date(announce_date) if announce_date is not None else None
    stamped = _stamp_from_inputs(announce_time, day, parse_date(announce_time))

    if stamped is None and day is not None:
        # announce_date 本身可能带时刻（"2026-03-03 20:30"）。
        carried = parse_datetime(announce_date)
        if carried is not None and carried.time() != time.min:
            stamped = carried

    visible: datetime | None = None
    source = VISIBLE_SOURCE_MISSING
    if explicit is not None:
        visible = explicit
        source = VISIBLE_SOURCE_MANUAL
        day = visible.date()
    elif stamped is not None:
        visible = stamped
        source = VISIBLE_SOURCE_EXACT
        day = visible.date()
    elif day is not None and conservative:
        visible = datetime.combine(day, CONSERVATIVE_VISIBLE_TIME)
        source = VISIBLE_SOURCE_CONSERVATIVE
        notes.append("no_disclosure_timestamp:assume_after_close")
    elif day is not None:
        # 调用方明知没时间戳还要求激进口径 —— 只登记，不静默放宽。
        visible = datetime.combine(day, CONSERVATIVE_VISIBLE_TIME)
        source = VISIBLE_SOURCE_CONSERVATIVE
        notes.append("non_conservative_requested_but_no_timestamp")
    else:
        notes.append("announce_date_missing")

    if visible is None:
        return DisclosureTiming(
            announce_date=_iso(day) if day is not None else None,
            visible_at=None,
            visible_at_source=source,
            window=WINDOW_UNKNOWN,
            close_decision_date=None,
            auction_decision_date=None,
            calendar_available=cal.available,
            notes=notes,
        )

    if not cal.available:
        return DisclosureTiming(
            announce_date=_iso(day) if day is not None else None,
            visible_at=visible.isoformat(sep=" "),
            visible_at_source=source,
            window=WINDOW_UNKNOWN,
            close_decision_date=None,
            auction_decision_date=None,
            calendar_available=False,
            notes=notes + ["trade_calendar_unavailable:decision_date_missing"],
        )

    window = classify_window(visible, cal)
    close_decision = _first_decision_date(cal, visible, MARKET_CLOSE)
    auction_decision = _first_decision_date(cal, visible, AUCTION_ORDER_OPEN)
    if close_decision is None or auction_decision is None:
        notes.append("decision_date_beyond_loaded_calendar")
    return DisclosureTiming(
        announce_date=_iso(visible.date()),
        visible_at=visible.isoformat(sep=" "),
        visible_at_source=source,
        window=window,
        close_decision_date=close_decision,
        auction_decision_date=auction_decision,
        calendar_available=True,
        notes=notes,
    )


def _first_decision_date(
    cal: TradingCalendar,
    visible_at: datetime,
    clock: time,
) -> str | None:
    """第一个满足 ``visible_at < 当日 clock`` 的交易日。"""

    day = visible_at.date()
    # 最多向后找 15 个交易日（覆盖长假 + 数据缺日），超出即判定不足。
    for _ in range(15):
        candidate = cal.on_or_after(day)
        if candidate is None:
            return None
        anchor = parse_date(candidate)
        if anchor is None:  # pragma: no cover - calendar 已规范化，防御分支
            return None
        if visible_at < datetime.combine(anchor, clock):
            return candidate
        nxt = cal.next_trade_date(anchor)
        if nxt is None:
            return None
        day = parse_date(nxt) or anchor + timedelta(days=1)
    return None


def usable_for_decision(
    timing: DisclosureTiming, decision_date: str, *, mode: str = "close"
) -> bool:
    """事件在 ``decision_date`` 的决策时点是否可用（严格 PIT 判定）。

    ``mode="close"`` 对应 T 日收盘后决策（#23 场景 B 的 T 日），
    ``mode="auction"`` 对应 D 日 09:15 挂集合竞价单。
    """

    if timing.missing:
        return False
    target = decision_date if isinstance(decision_date, str) else _iso(decision_date)
    if mode == "auction":
        anchor = timing.auction_decision_date
    elif mode == "close":
        anchor = timing.close_decision_date
    else:
        raise ValueError(f"unknown decision mode: {mode!r}")
    return anchor is not None and target >= anchor


def first_tradeable_date(
    decision_date: str | None,
    *,
    calendar: TradingCalendar,
    halts: Sequence[TradingHalt] | Mapping[str, Sequence[TradingHalt]] | None = None,
    symbol: str = "",
) -> str | None:
    """决策日之后第一个"真的能下单"的交易日；停牌区间未知时返回 ``None``。

    复牌事件要单独看：复牌公告的可见时间通常早于复牌日，但标的在复牌前
    无法成交，所以 ``halts`` 覆盖的日期一律跳过。
    """

    if decision_date is None:
        return None
    if not calendar.available:
        return None
    cursor: str | None = decision_date
    for _ in range(30):
        if cursor is None:
            return None
        if halts is None:
            return cursor
        halt = suspension_for(symbol, cursor, halts)
        if halt is None:
            return cursor
        if halt.end is None:
            return None
        cursor = calendar.next_trade_date(halt.end)
    return None
