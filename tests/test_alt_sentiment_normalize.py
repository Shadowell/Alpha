"""另类数据规范化单测（issue #33）：字段归一、单位、空数据、时区。"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pandas as pd
import pytest

from app.services.alt_sentiment.normalize import (
    normalize_attention_frame,
    normalize_interactive_frame,
    normalize_news_frame,
    normalize_symbol,
)
from app.services.alt_sentiment.records import (
    SOURCE_ATTENTION,
    SOURCE_INTERACTIVE_QA,
    SOURCE_NEWS,
    AltRecord,
    as_cn_datetime,
    is_missing,
    snapshot_visible_time,
)


def test_normalize_symbol_variants() -> None:
    assert normalize_symbol("SZ000665") == "000665"
    assert normalize_symbol("600000.SH") == "600000"
    assert normalize_symbol(" 301001 ") == "301001"
    assert normalize_symbol(None) == ""
    assert normalize_symbol("abc") == ""


def test_as_cn_datetime_is_shanghai_aware() -> None:
    naive = as_cn_datetime("2026-03-02 09:15:00")
    assert naive is not None and naive.tzinfo is not None
    assert naive.utcoffset().total_seconds() == 8 * 3600
    aware = as_cn_datetime(datetime(2026, 3, 2, 1, 15, tzinfo=timezone.utc))
    assert aware is not None
    assert (aware.hour, aware.minute) == (9, 15)
    assert as_cn_datetime("not-a-date") is None
    assert as_cn_datetime(None) is None


def test_snapshot_visible_time_blocks_intraday_use() -> None:
    as_of = snapshot_visible_time("2026-03-02")
    assert as_of is not None
    assert (as_of.hour, as_of.minute) == (15, 30)
    intraday = datetime(2026, 3, 2, 10, 30, tzinfo=as_of.tzinfo)
    assert intraday < as_of  # 盘中还看不到当日快照


def test_interactive_frame_normalization_and_reply_speed() -> None:
    frame = pd.DataFrame(
        [
            {
                "股票代码": "SZ002594",
                "公司简称": "比亚迪",
                "问题": "请问公司出海进展如何？",
                "提问时间": "2026-03-02 09:00:00",
                "更新时间": "2026-03-03 09:00:00",
                "回答内容": "公司出口持续增长",
                "回答者": "比亚迪",
                "问题编号": "1515",
                "来源": "APP",
            },
            {
                "股票代码": "002594",
                "公司简称": "比亚迪",
                "问题": "未回答的问题正文",
                "提问时间": "2026-03-04 10:00:00",
                "更新时间": None,
                "回答内容": None,
                "回答者": None,
                "问题编号": "1516",
                "来源": "网站",
            },
        ]
    )
    records = normalize_interactive_frame(frame)
    assert len(records) == 2
    first = records[0]
    assert first.source == SOURCE_INTERACTIVE_QA
    assert first.symbol == "002594"
    assert first.metrics["answered"] == 1.0
    assert first.metrics["reply_hours"] == pytest.approx(24.0)
    assert first.trade_date == "2026-03-02"
    second = records[1]
    assert second.metrics["answered"] == 0.0
    # 未回答不能算成 0 小时回复速度
    assert is_missing(second.metrics["reply_hours"])
    assert all(
        r.as_of_time is not None and r.as_of_time.tzinfo is not None for r in records
    )


def test_interactive_drops_rows_without_time_or_symbol() -> None:
    frame = pd.DataFrame(
        [
            {
                "股票代码": "002594",
                "问题": "没有时间戳",
                "提问时间": None,
                "更新时间": None,
            },
            {"股票代码": "", "问题": "没有代码", "提问时间": "2026-03-02 09:00:00"},
        ]
    )
    assert normalize_interactive_frame(frame) == []


def test_interactive_empty_frame_returns_list() -> None:
    assert normalize_interactive_frame(pd.DataFrame()) == []


def test_attention_frame_uses_trade_date_and_keeps_units() -> None:
    frame = pd.DataFrame(
        [
            {
                "代码": "600000",
                "名称": "浦发银行",
                "交易日": date(2026, 3, 2),
                "关注指数": 123.5,
                "综合得分": 3.2,
                "目前排名": 88,
                "上升": -5,
                "机构参与度": 0.31,
            }
        ]
    )
    records = normalize_attention_frame(frame)
    assert len(records) == 1
    record = records[0]
    assert record.source == SOURCE_ATTENTION
    assert record.symbol == "600000"
    assert record.metrics["attention_index"] == pytest.approx(123.5)
    assert record.metrics["attention_rank_delta"] == pytest.approx(-5.0)
    assert record.trade_date == "2026-03-02"
    assert record.as_of_time == snapshot_visible_time("2026-03-02")
    assert record.meta["kind"] == "daily_snapshot"


def test_attention_skips_rows_without_trade_date_or_metrics() -> None:
    frame = pd.DataFrame(
        [
            {"代码": "600000", "交易日": None, "关注指数": 10},
            {
                "代码": "600001",
                "交易日": "2026-03-02",
                "关注指数": None,
                "综合得分": "-",
            },
        ]
    )
    assert normalize_attention_frame(frame) == []


def test_news_frame_keeps_text_and_link() -> None:
    frame = pd.DataFrame(
        [
            {
                "关键词": "603777",
                "新闻标题": "<em>公司</em>中标重大合同",
                "新闻内容": "公告显示订单金额较大",
                "发布时间": "2026-03-02 18:05:00",
                "文章来源": "证券时报",
                "新闻链接": "http://finance.eastmoney.com/a/1.html",
            },
            {"关键词": "603777", "新闻标题": None, "发布时间": "2026-03-02 18:06:00"},
        ]
    )
    records = normalize_news_frame(frame)
    assert len(records) == 1
    record = records[0]
    assert record.source == SOURCE_NEWS
    assert record.symbol == "603777"
    assert record.url == "http://finance.eastmoney.com/a/1.html"
    assert record.meta["media"] == "证券时报"
    assert record.external_id and len(record.external_id) == 32
    assert record.as_of_time == as_cn_datetime("2026-03-02 18:05:00")


def test_number_parser_never_fills_zero() -> None:
    frame = pd.DataFrame(
        [{"代码": "600000", "交易日": "2026-03-02", "关注指数": "abc", "综合得分": "7"}]
    )
    record = normalize_attention_frame(frame)[0]
    assert is_missing(record.metrics["attention_index"])
    assert record.metrics["attention_score"] == pytest.approx(7.0)


def test_record_rejects_as_of_before_event() -> None:
    with pytest.raises(ValueError):
        AltRecord(
            source=SOURCE_NEWS,
            symbol="600000",
            event_time=datetime(2026, 3, 2, 15, 0, tzinfo=timezone.utc),
            as_of_time=datetime(2026, 3, 2, 14, 0, tzinfo=timezone.utc),
        )


def test_record_repr_is_log_safe() -> None:
    record = AltRecord(
        source=SOURCE_NEWS,
        symbol="600000",
        event_time=datetime(2026, 3, 2, 15, 0),
        as_of_time=datetime(2026, 3, 2, 15, 0),
        title="涉密标题正文",
        url="https://example.com/a?token=secret",
    )
    text = record.sanitized_repr()
    assert "涉密标题正文" not in text
    assert "token=secret" not in text
