"""#57：交易时段与市场状态统一口径（服务端单源，东八区 aware 时间）。

前端 `core.js` 已按此契约消费（字段名固定，勿改）：

```json
{"market_session": "pre|open|lunch|closed|holiday", "is_trading_day": true, "china_time": "ISO-8601 +08:00"}
```

口径约定：
- 时间一律使用 ``time_utils.now_cn()``（aware Asia/Shanghai），禁止 naive ``datetime.now()``；
- 时段边界与前端 ``_CN_MARKET_RANGES`` 对齐：9:30–11:30 / 13:00–15:00 为 ``open``，
  11:30–13:00 为 ``lunch``，15:00 及以后为 ``closed``，其余为 ``pre``；
- 交易日判定复用 #51 的 ``trading_day_utils.normalize_trade_days``：日历覆盖到目标日时
  以日历为准（识别法定节假日），日历缺失或未覆盖时回退「工作日近似」，与 K 线
  新鲜度判定同一套降级语义。
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any, Iterable

from app.services.time_utils import ensure_cn_tz, now_cn
from app.services.trading_day_utils import normalize_trade_days

MARKET_SESSION_VALUES = ("pre", "open", "lunch", "closed", "holiday")

_MORNING_START = time(9, 30)
_MORNING_END = time(11, 30)
_AFTERNOON_START = time(13, 0)
_AFTERNOON_END = time(15, 0)


def is_trading_calendar_day(
    day: date, trade_days: Iterable[str | date | datetime] | None
) -> bool:
    """目标日是否 A 股交易日。日历覆盖到 ``day`` 时按日历判定，否则回退工作日近似。"""
    days = normalize_trade_days(trade_days)
    if days and max(days) >= day:
        return day in set(days)
    return day.weekday() < 5


def classify_market_session(dt: datetime, *, trading_day: bool = True) -> str:
    """把东八区时间归入 pre/open/lunch/closed/holiday（时段边界与前端一致，左闭右开）。"""
    if not trading_day:
        return "holiday"
    cur = ensure_cn_tz(dt)
    wall = cur.timetz().replace(tzinfo=None)
    if (
        _MORNING_START <= wall < _MORNING_END
        or _AFTERNOON_START <= wall < _AFTERNOON_END
    ):
        return "open"
    if _MORNING_END <= wall < _AFTERNOON_START:
        return "lunch"
    if wall >= _AFTERNOON_END:
        return "closed"
    return "pre"


def build_session_payload(
    *,
    now: datetime | None = None,
    trade_days: Iterable[str | date | datetime] | None = None,
) -> dict[str, Any]:
    """产出前端契约字段：market_session / is_trading_day / china_time（+08:00 aware ISO）。"""
    cur = ensure_cn_tz(now if now is not None else now_cn())
    trading_day = is_trading_calendar_day(cur.date(), trade_days)
    return {
        "market_session": classify_market_session(cur, trading_day=trading_day),
        "is_trading_day": trading_day,
        "china_time": cur.isoformat(),
    }


def _extract_trade_dates(raw: Any) -> list[str]:
    """从 provider.get_trade_days() 的 DataFrame 中抽取 trade_date 列（容错）。"""
    try:
        if raw is None or getattr(raw, "empty", True):
            return []
        if "trade_date" not in raw.columns:
            return []
        return [str(v) for v in raw["trade_date"].tolist()]
    except Exception:  # noqa: BLE001 — 日历形态异常时按缺失处理，走工作日近似
        return []


async def resolve_market_session(
    provider: Any, *, now: datetime | None = None
) -> dict[str, Any]:
    """基于 provider 交易日历（DB 优先，远端兜底）构建会话口径 payload。

    provider 缺少 ``get_trade_days`` 或调用失败时不报错：退化为「无日历 → 工作日近似」，
    保证只读接口在任何数据源状态下都有确定响应。
    """
    get_trade_days = getattr(provider, "get_trade_days", None)
    trade_days: list[str] = []
    if callable(get_trade_days):
        try:
            trade_days = _extract_trade_dates(await get_trade_days())
        except Exception:  # noqa: BLE001 — 日历不可用按近似判定，不阻断只读状态接口
            trade_days = []
    return build_session_payload(now=now, trade_days=trade_days)
