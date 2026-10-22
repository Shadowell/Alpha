"""另类情绪数据的规范记录与缺失值语义（issue #33）。

设计约束（来自 issue 正文，不允许放宽）：

1. **Point-in-Time**：每条记录同时带 ``event_time``（事件实际发生 / 公开发布
   时间）与 ``as_of_time``（Alpha 首次可见时间）。因子只能用在 ``as_of_time``
   之后的样本上，当日快照必须延后可见，禁止未来函数。
2. **缺失 ≠ 0 分**：无数据一律用 ``None`` 表示，并配套 ``available`` 掩码；
   禁止 ``fillna(0)`` 把缺失当有效信号。
3. **可追溯 / 可删除 / 可审计**：原始文本、链接、时间戳、抓取状态都要留痕，
   支持按标的 / 来源 / 日期物理删除。
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app.config import APP_TZ
from app.services.time_utils import ensure_cn_tz

# ─── 来源常量 ───

SOURCE_INTERACTIVE_QA = "interactive_qa"
"""互动易问答（巨潮资讯 cninfo，官方披露渠道）。"""

SOURCE_ATTENTION = "eastmoney_attention"
"""东财千股千评市场关注度快照（公开数据中心的 API）。"""

SOURCE_NEWS = "eastmoney_news"
"""东财个股新闻（有条件接入，默认关闭，见 sources.py）。"""

KNOWN_SOURCES: tuple[str, ...] = (
    SOURCE_INTERACTIVE_QA,
    SOURCE_ATTENTION,
    SOURCE_NEWS,
)

# 日频快照型来源的可见延迟：数据中心当日的盘子只有收盘结算后才对外可读，
# 因此 as_of = trade_date 15:30（Asia/Shanghai），当日盘中不可用。
DAILY_SNAPSHOT_VISIBLE_HOUR = 15
DAILY_SNAPSHOT_VISIBLE_MINUTE = 30

VALUE_DICT = dict[str, float | None]
MASK_DICT = dict[str, bool]


def is_missing(value: Any) -> bool:
    """显式缺失判定：``None`` / NaN / NaT / 空字符串都算「无数据」。"""
    if value is None:
        return True
    if value is _NaT:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    if isinstance(value, str) and not value.strip():
        return True
    return False


try:  # pandas 的 NaT 与 stdlib 值共用同一套缺失语义
    import pandas as _pd

    _NaT = _pd.NaT
except Exception:  # pragma: no cover - pandas 是仓库既有依赖，缺失时降级
    _NaT = None  # type: ignore[assignment]


def as_cn_datetime(value: Any) -> datetime | None:
    """把任意上游时间值归一成 Asia/Shanghai 的 aware datetime。

    * naive 字符串 / datetime 按上海墙钟时间解释（上游 cninfo / 东财都是 CN 时间）；
    * aware 值直接换算到上海时区；
    * 无法解析 → ``None``（缺失，不返回 0 点占位值）。
    """
    if value is None or is_missing(value):
        return None
    if isinstance(value, datetime):
        return ensure_cn_tz(value)
    text = str(value).strip()
    if not text:
        return None
    normalized = text.replace("/", "-")
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        try:
            import pandas as pd

            stamp = pd.to_datetime(text, errors="coerce")
        except Exception:
            return None
        if stamp is None or pd.isna(stamp):
            return None
        parsed = stamp.to_pydatetime()
    return ensure_cn_tz(parsed)


def snapshot_visible_time(trade_date_value: Any) -> datetime | None:
    """日频快照的可见时间：交易日 15:30（Asia/Shanghai）。"""
    dt = as_cn_datetime(trade_date_value)
    if dt is None:
        return None
    return dt.replace(
        hour=DAILY_SNAPSHOT_VISIBLE_HOUR,
        minute=DAILY_SNAPSHOT_VISIBLE_MINUTE,
        second=0,
        microsecond=0,
    )


def trade_date_of(value: datetime | None) -> str:
    return value.astimezone(APP_TZ).date().isoformat() if value else ""


def content_hash(*parts: Any) -> str:
    """稳定去重指纹：只使用文本 / 时间 / 编号，绝不含密钥或个人信息。"""
    digest = hashlib.sha256()
    for part in parts:
        text = "" if part is None else str(part)
        digest.update(text.strip().encode("utf-8", errors="replace"))
        digest.update(b"\x1f")
    return digest.hexdigest()[:32]


@dataclass(slots=True)
class AltRecord:
    """一条可追溯的另类数据原子记录。"""

    source: str
    symbol: str
    event_time: datetime | None
    as_of_time: datetime | None
    title: str | None = None
    text: str | None = None
    url: str | None = None
    external_id: str | None = None
    metrics: dict[str, float | None] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    fetched_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.source not in KNOWN_SOURCES:
            raise ValueError(f"unknown alt data source: {self.source}")
        for name in ("event_time", "as_of_time", "fetched_at"):
            value = getattr(self, name)
            if isinstance(value, datetime) and value.tzinfo is None:
                setattr(self, name, ensure_cn_tz(value))
        if self.as_of_time is not None and self.event_time is not None:
            if self.as_of_time < self.event_time:
                # as_of 早于事件时间等价于未来函数，直接拒绝。
                raise ValueError(
                    f"as_of_time({self.as_of_time}) precedes event_time({self.event_time})"
                )

    @property
    def trade_date(self) -> str:
        anchor = self.as_of_time or self.event_time
        return trade_date_of(anchor)

    def dedup_key(self) -> str:
        if self.external_id:
            return content_hash(self.source, self.symbol, "id", self.external_id)
        return content_hash(
            self.source,
            self.symbol,
            (self.event_time or self.as_of_time).isoformat()
            if (self.event_time or self.as_of_time)
            else "",
            (self.title or "")[:200],
            (self.text or "")[:500],
        )

    def to_row(self) -> dict[str, Any]:
        """落库行（JSON 字段由 store 负责序列化）。"""
        return {
            "source": self.source,
            "symbol": self.symbol,
            "external_id": self.external_id,
            "event_time": self.event_time.isoformat() if self.event_time else None,
            "as_of_time": self.as_of_time.isoformat() if self.as_of_time else None,
            "trade_date": self.trade_date,
            "title": self.title,
            "text": self.text,
            "url": self.url,
            "metrics": self.metrics,
            "meta": self.meta,
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
            "content_hash": self.dedup_key(),
        }

    def sanitized_repr(self) -> str:
        """日志安全表示：不输出正文与链接，只输出结构与时间。"""
        return (
            f"AltRecord(source={self.source}, symbol={self.symbol}, "
            f"trade_date={self.trade_date}, event_time={self.event_time}, "
            f"as_of_time={self.as_of_time}, metric_keys={sorted(self.metrics)})"
        )


@dataclass(slots=True)
class AltSentimentDaily:
    """``(symbol, trade_date)`` 粒度的因子行。"""

    symbol: str
    trade_date: str
    as_of_time: datetime
    values: VALUE_DICT = field(default_factory=dict)
    availability: MASK_DICT = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)
    record_count: int = 0
    status: str = "ok"
    notes: dict[str, Any] = field(default_factory=dict)
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.as_of_time.tzinfo is None:
            self.as_of_time = ensure_cn_tz(self.as_of_time)

    @property
    def alt_attention_score(self) -> float | None:
        value = self.values.get("alt_attention_score")
        return None if is_missing(value) else float(value)  # type: ignore[arg-type]

    def factor(self, name: str) -> float | None:
        value = self.values.get(name)
        if is_missing(value):
            return None
        return float(value)  # type: ignore[arg-type]

    def is_available(self, name: str) -> bool:
        return bool(self.availability.get(name, False))

    def visible_after(self, moment: datetime) -> bool:
        """Point-in-Time 判定：``moment`` 是否已经能看到这一行。"""
        anchor = ensure_cn_tz(moment)
        return anchor >= self.as_of_time

    def to_row(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "trade_date": self.trade_date,
            "as_of_time": self.as_of_time.isoformat(),
            "values": self.values,
            "availability": self.availability,
            "sources": self.sources,
            "record_count": self.record_count,
            "status": self.status,
            "notes": self.notes,
            "updated_at": (self.updated_at or self.as_of_time).isoformat(),
        }


def lag_hours(start: datetime | None, end: datetime | None) -> float | None:
    """回复时长（小时）。任一端缺失或非正 → None（缺失，而不是 0）。"""
    if start is None or end is None:
        return None
    delta: timedelta = end - start
    hours = delta.total_seconds() / 3600.0
    if hours < 0:
        return None
    return round(hours, 4)
