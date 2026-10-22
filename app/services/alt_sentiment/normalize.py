"""上游另类数据帧 → 规范记录（issue #33）。

AkShare 各版本列名 / 类型不稳定（例如 ``交易日`` vs ``交易日期``、
``str`` vs ``Timestamp``），这里统一做：

* 列名别名匹配（缺失列不报错，只把对应字段标成缺失）；
* 时间解析并归一到 Asia/Shanghai aware datetime；
* 单位统一（关注度指数无量纲、涨跌幅百分数、回复时长小时）；
* 代码归一（6 位数字）与空行剔除；
* 日频快照 ``as_of_time`` = 交易日 15:30，确保当日信号只能事后使用。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd

from app.services.alt_sentiment.records import (
    SOURCE_ATTENTION,
    SOURCE_INTERACTIVE_QA,
    SOURCE_NEWS,
    AltRecord,
    as_cn_datetime,
    content_hash,
    snapshot_visible_time,
)

# ─── 列名别名表（覆盖 akshare 1.18 ~ 1.19 的命名差异）───

COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "symbol": ("股票代码", "代码", "证券代码", "symbol", "关键词"),
    "name": ("公司简称", "名称", "股票名称", "简称"),
    "trade_date": ("交易日", "交易日期", "日期"),
    "question_time": ("提问时间", "问题时间"),
    "answer_time": ("回答时间", "回复时间"),
    "question": ("问题", "问题内容", "提问内容"),
    "answer": ("回答内容", "回复内容"),
    "question_id": ("问题编号", "提问者编号", "questionId"),
    "answer_id": ("回答ID", "回答编号"),
    "answerer": ("回答者",),
    "channel": ("来源", "渠道", "发布渠道"),
    "update_time": ("更新时间",),
    "title": ("新闻标题", "标题"),
    "content": ("新闻内容", "内容", "摘要"),
    "publish_time": ("发布时间", "发布时间戳", "时间"),
    "media": ("文章来源", "来源媒体", "媒体", "来源"),
    "url": ("新闻链接", "链接", "url"),
    "attention_index": ("关注指数", "用户关注指数", "市场关注度", "关注值"),
    "attention_score": ("综合得分", "千股千评得分"),
    "attention_rank": ("目前排名", "当前排名", "排名"),
    "attention_rank_delta": ("上升", "排名上升"),
    "institution_ratio": ("机构参与度",),
    "turnover_rate": ("换手率",),
    "change_pct": ("涨跌幅",),
}


def normalize_symbol(value: Any) -> str:
    """``SZ000665`` / ``000665.SZ`` / ``600000`` → ``000665``。无法解析返回 ``""``。"""
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if not digits:
        return ""
    tail = digits[-6:]
    return tail if len(tail) == 6 else ""


def _pick(frame: pd.DataFrame, key: str) -> str | None:
    for candidate in COLUMN_ALIASES[key]:
        if candidate in frame.columns:
            return candidate
    return None


def _cell(row: pd.Series, col: str | None) -> Any:
    if col is None:
        return None
    try:
        value = row[col]
    except Exception:
        return None
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    if value is pd.NaT:
        return None
    return value


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "-", "--"}:
        return None
    return text


def _number(value: Any) -> float | None:
    """严格数值解析：不可解析时返回 None（缺失），绝不返回 0.0。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    text = str(value).strip().replace(",", "").replace("%", "")
    if not text or text.lower() in {"nan", "none", "-", "--", "null"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _clean_symbol_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    work = frame.copy()
    symbol_col = _pick(work, "symbol")
    if symbol_col is None:
        return pd.DataFrame()
    work[symbol_col] = work[symbol_col].map(normalize_symbol)
    work = work[work[symbol_col] != ""]
    return work.reset_index(drop=True)


def _timestamp(value: Any) -> datetime | None:
    return as_cn_datetime(value)


def normalize_interactive_frame(
    frame: pd.DataFrame,
    *,
    fetched_at: datetime | None = None,
    max_rows: int = 500,
) -> list[AltRecord]:
    """互动易问答帧 → 记录。

    ``event_time`` 用提问时间（问题公开可查的时刻）；回答时间单独进 metrics，
    未回答时保持缺失（不能当成 0 小时回复速度）。
    """
    work = _clean_symbol_frame(frame)
    if work.empty:
        return []
    symbol_col = _pick(work, "symbol")
    question_col = _pick(work, "question")
    answer_col = _pick(work, "answer")
    qtime_col = _pick(work, "question_time")
    atime_col = _pick(work, "answer_time")
    qid_col = _pick(work, "question_id")
    update_col = _pick(work, "update_time")
    channel_col = _pick(work, "channel")
    name_col = _pick(work, "name")

    records: list[AltRecord] = []
    for _, row in work.head(max(1, int(max_rows))).iterrows():
        symbol = str(row[symbol_col])
        question = _text(_cell(row, question_col))
        answer = _text(_cell(row, answer_col))
        event_time = _timestamp(_cell(row, qtime_col)) or _timestamp(
            _cell(row, update_col)
        )
        if event_time is None:
            # 没有提问时间的条目无法做时间对齐，直接丢弃并留痕。
            continue
        answer_time = _timestamp(_cell(row, atime_col))
        reply_hours = None
        if answer_time is not None and answer_time >= event_time:
            reply_hours = round((answer_time - event_time).total_seconds() / 3600.0, 4)
        elif answer and update_col is not None:
            fallback = _timestamp(_cell(row, update_col))
            if fallback is not None and fallback >= event_time:
                answer_time = fallback
                reply_hours = round((fallback - event_time).total_seconds() / 3600.0, 4)
        external_id = _text(_cell(row, qid_col))
        records.append(
            AltRecord(
                source=SOURCE_INTERACTIVE_QA,
                symbol=symbol,
                event_time=event_time,
                as_of_time=event_time,
                title=question[:120] if question else None,
                text=question,
                url=None,
                external_id=external_id
                or content_hash(symbol, event_time.isoformat(), question or ""),
                metrics={
                    "answered": 1.0 if answer else 0.0,
                    "reply_hours": reply_hours,
                },
                meta={
                    "company_name": _text(_cell(row, name_col)),
                    "channel": _text(_cell(row, channel_col)),
                    "answer_present": bool(answer),
                    "answer_time": answer_time.isoformat() if answer_time else None,
                },
                fetched_at=fetched_at,
            )
        )
    return records


def normalize_attention_frame(
    frame: pd.DataFrame,
    *,
    fetched_at: datetime | None = None,
    max_rows: int = 8000,
) -> list[AltRecord]:
    """千股千评 / 关注指数快照帧 → 日频记录（as_of = 交易日 15:30）。"""
    work = _clean_symbol_frame(frame)
    if work.empty:
        return []
    symbol_col = _pick(work, "symbol")
    date_col = _pick(work, "trade_date")
    focus_col = _pick(work, "attention_index")
    score_col = _pick(work, "attention_score")
    rank_col = _pick(work, "attention_rank")
    delta_col = _pick(work, "attention_rank_delta")
    inst_col = _pick(work, "institution_ratio")
    name_col = _pick(work, "name")

    records: list[AltRecord] = []
    for _, row in work.head(max(1, int(max_rows))).iterrows():
        symbol = str(row[symbol_col])
        trade_day = _text(_cell(row, date_col))
        as_of = snapshot_visible_time(trade_day) if trade_day else None
        if as_of is None:
            # 没有交易日就无法保证不错位到未来，视为不可用记录。
            continue
        metrics = {
            "attention_index": _number(_cell(row, focus_col)),
            "attention_score": _number(_cell(row, score_col)),
            "attention_rank": _number(_cell(row, rank_col)),
            "attention_rank_delta": _number(_cell(row, delta_col)),
            "institution_ratio": _number(_cell(row, inst_col)),
        }
        if all(value is None for value in metrics.values()):
            continue
        records.append(
            AltRecord(
                source=SOURCE_ATTENTION,
                symbol=symbol,
                event_time=as_of,
                as_of_time=as_of,
                title=_text(_cell(row, name_col)),
                text=None,
                url="https://data.eastmoney.com/stockcomment/",
                external_id=content_hash(SOURCE_ATTENTION, symbol, trade_day),
                metrics=metrics,
                meta={"trade_date": trade_day, "kind": "daily_snapshot"},
                fetched_at=fetched_at,
            )
        )
    return records


def normalize_news_frame(
    frame: pd.DataFrame,
    *,
    fetched_at: datetime | None = None,
    max_rows: int = 200,
) -> list[AltRecord]:
    """个股新闻帧 → 记录（标题 + 正文 + 链接 + 发布时间全部留痕，可审计可删除）。"""
    work = _clean_symbol_frame(frame)
    if work.empty:
        return []
    symbol_col = _pick(work, "symbol")
    title_col = _pick(work, "title")
    content_col = _pick(work, "content")
    time_col = _pick(work, "publish_time")
    media_col = _pick(work, "media")
    url_col = _pick(work, "url")

    records: list[AltRecord] = []
    for _, row in work.head(max(1, int(max_rows))).iterrows():
        symbol = str(row[symbol_col])
        publish_time = _timestamp(_cell(row, time_col))
        title = _text(_cell(row, title_col))
        if publish_time is None or title is None:
            continue
        records.append(
            AltRecord(
                source=SOURCE_NEWS,
                symbol=symbol,
                event_time=publish_time,
                as_of_time=publish_time,
                title=title,
                text=_text(_cell(row, content_col)),
                url=_text(_cell(row, url_col)),
                external_id=None,
                metrics={},
                meta={
                    "media": _text(_cell(row, media_col)),
                    "char_count": len(
                        ((title or "") + (_text(_cell(row, content_col)) or ""))
                    ),
                },
                fetched_at=fetched_at,
            )
        )
    for record in records:
        record.external_id = content_hash(
            SOURCE_NEWS, record.symbol, record.event_time.isoformat(), record.title
        )
    return records
