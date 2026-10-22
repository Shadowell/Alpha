"""Issue #51：交易日窗口计算工具。

用于判断「本地 K 线缓存最新日期」与「实时行情决策交易日」之间落后多少个交易日。
优先使用交易日历（akshare/DB 的 trade_date 列表）；日历缺失或覆盖不到目标区间时，
回退到「按工作日近似」——宁可近似判定为陈旧而跳过，也不把混期数据当新鲜数据混算。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Iterable


def _to_date(value: str | date | datetime) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()[:10]
    if not text:
        return None
    try:
        return datetime.strptime(text.replace("-", ""), "%Y%m%d").date()
    except ValueError:
        return None


def normalize_trade_days(trade_days: Iterable[str | date | datetime] | None) -> list[date]:
    days = sorted({d for d in (_to_date(item) for item in (trade_days or [])) if d is not None})
    return days


def latest_trade_date_on_or_before(day: date, trade_days: list[date] | None = None) -> date:
    """返回 <= day 的最近交易日。日历可用时以日历为准；否则回退为最近的工作日近似。"""
    days = trade_days or []
    if days:
        candidates = [d for d in days if d <= day]
        if candidates:
            return max(candidates)
    probe = day
    for _ in range(10):  # 最多回退 10 天，覆盖长假后日历未更新的情形
        if probe.weekday() < 5:
            return probe
        probe -= timedelta(days=1)
    return probe


def count_missing_trading_days(
    kline_max_date: str | date,
    target_trade_date: str | date,
    trade_days: list[date] | None = None,
) -> int:
    """K 线缓存落后决策交易日的交易日数（区间 (kline_max, target] 内的交易日个数）。

    - kline 已覆盖目标日（>= target）时返回 0。
    - 日历覆盖不到区间时按工作日近似统计。
    """
    kline = _to_date(kline_max_date)
    target = _to_date(target_trade_date)
    if kline is None or target is None:
        return -1  # 无法判定：调用方按「不可验证」处理（跳过而不是混算）
    if kline >= target:
        return 0
    days = trade_days or []
    if days and min(days) <= kline and max(days) >= target:
        return sum(1 for d in days if kline < d <= target)
    # 工作日近似回退
    count = 0
    probe = kline + timedelta(days=1)
    while probe <= target:
        if probe.weekday() < 5:
            count += 1
        probe += timedelta(days=1)
    return count
