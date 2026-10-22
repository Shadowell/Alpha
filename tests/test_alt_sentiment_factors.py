"""另类因子计算单测（issue #33）：窗口、缺失语义、时间对齐、幂等。"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.services.alt_sentiment.factors import (
    COMPOSITE_WEIGHTS,
    FACTOR_NAMES,
    UNAVAILABLE_FACTORS,
    AltFactorBuilder,
    factor_frame_from_rows,
)
from app.services.alt_sentiment.normalize import (
    normalize_news_frame,
)
from app.services.alt_sentiment.records import (
    SOURCE_ATTENTION,
    SOURCE_INTERACTIVE_QA,
    SOURCE_NEWS,
    AltRecord,
    is_missing,
    snapshot_visible_time,
)
import pandas as pd
import pytest

AS_OF = snapshot_visible_time("2026-03-06")  # 2026-03-06 15:30 CST


def _qa(
    time: datetime, *, answered: bool, reply_hours: float | None = None
) -> AltRecord:
    return AltRecord(
        source=SOURCE_INTERACTIVE_QA,
        symbol="002594",
        event_time=time,
        as_of_time=time,
        title="提问",
        metrics={"answered": 1.0 if answered else 0.0, "reply_hours": reply_hours},
    )


def _attention(day: str, index: float | None) -> AltRecord:
    as_of = snapshot_visible_time(day)
    return AltRecord(
        source=SOURCE_ATTENTION,
        symbol="002594",
        event_time=as_of,
        as_of_time=as_of,
        metrics={
            "attention_index": index,
            "attention_score": 3.0,
            "attention_rank": 50.0,
            "attention_rank_delta": 2.0,
            "institution_ratio": None,
        },
    )


def test_no_records_means_all_factors_missing_not_zero() -> None:
    row = AltFactorBuilder().build("002594", AS_OF, [])
    assert row.status == "no_data"
    assert row.record_count == 0
    assert row.sources == []
    for name in FACTOR_NAMES:
        assert is_missing(row.values[name]), name
        assert row.is_available(name) is False
    assert row.alt_attention_score is None


def test_interactive_windows_and_reply_speed() -> None:
    records = [
        _qa(AS_OF - timedelta(hours=3), answered=True, reply_hours=6.0),
        _qa(AS_OF - timedelta(days=2), answered=False),
        _qa(AS_OF - timedelta(days=5), answered=True, reply_hours=30.0),
        _qa(AS_OF - timedelta(days=20), answered=True, reply_hours=12.0),
    ]
    row = AltFactorBuilder().build("002594", AS_OF, records)
    assert row.factor("interactive_qa_count_1d") == pytest.approx(1.0)
    assert row.factor("interactive_qa_count_3d") == pytest.approx(2.0)
    assert row.factor("interactive_qa_count_7d") == pytest.approx(3.0)
    assert row.factor("interactive_reply_rate_7d") == pytest.approx(2 / 3, abs=1e-6)
    # 7 日窗口内已回答样本 = 6h 与 30h → 中位数 18h
    assert row.factor("interactive_reply_speed_hours_7d") == pytest.approx(18.0)


def test_zero_count_is_real_zero_when_source_covers_symbol() -> None:
    records = [_qa(AS_OF - timedelta(days=25), answered=True, reply_hours=3.0)]
    row = AltFactorBuilder().build("002594", AS_OF, records)
    assert row.factor("interactive_qa_count_7d") == pytest.approx(0.0)
    assert row.is_available("interactive_qa_count_7d") is True
    # 无已回答样本时回复速度是缺失，而不是 0 小时
    assert row.factor("interactive_reply_speed_hours_7d") is None


def test_future_records_are_invisible() -> None:
    records = [_qa(AS_OF + timedelta(minutes=1), answered=True, reply_hours=1.0)]
    row = AltFactorBuilder().build("002594", AS_OF, records)
    assert row.record_count == 0
    assert row.factor("interactive_qa_count_1d") is None


def test_attention_growth_and_missing_on_single_snapshot() -> None:
    records = [_attention("2026-03-06", 200.0)]
    row = AltFactorBuilder().build("002594", AS_OF, records)
    assert row.factor("attention_index") == pytest.approx(200.0)
    # 只有一期数据时无法计算环比 → 显式缺失
    assert row.factor("attention_index_growth_7d") is None


def test_attention_growth_with_baseline() -> None:
    records = [
        _attention("2026-02-27", 100.0),
        _attention("2026-03-06", 150.0),
    ]
    row = AltFactorBuilder().build("002594", AS_OF, records)
    assert row.factor("attention_index_growth_7d") == pytest.approx(1.5)


def test_composite_requires_enough_available_weight() -> None:
    only_attention = [
        AltRecord(
            source=SOURCE_ATTENTION,
            symbol="002594",
            event_time=AS_OF,
            as_of_time=AS_OF,
            metrics={"attention_index": 100.0},
        )
    ]
    row = AltFactorBuilder().build("002594", AS_OF, only_attention)
    assert row.factor("attention_index") == pytest.approx(100.0)
    # 单成分权重 0.35 < 40% 门槛 → 综合分不可信，保持缺失
    assert row.factor("alt_attention_score") is None

    richer = [_attention("2026-02-27", 100.0), _attention("2026-03-06", 150.0)]
    row2 = AltFactorBuilder().build("002594", AS_OF, richer)
    assert row2.factor("alt_attention_score") is not None
    assert 0.0 <= row2.factor("alt_attention_score") <= 1.0


def test_limit_up_interaction_requires_both_inputs() -> None:
    richer = [_attention("2026-02-27", 100.0), _attention("2026-03-06", 150.0)]
    builder = AltFactorBuilder()
    row = builder.build("002594", AS_OF, richer, limit_up=True)
    assert row.factor("alt_attention_x_limit_up") == pytest.approx(
        row.factor("alt_attention_score")
    )
    row_flat = builder.build("002594", AS_OF, richer, limit_up=False)
    assert row_flat.factor("alt_attention_x_limit_up") == pytest.approx(0.0)
    row_none = builder.build("002594", AS_OF, richer)
    assert row_none.factor("alt_attention_x_limit_up") is None


def test_unavailable_source_factors_stay_missing() -> None:
    news = normalize_news_frame(
        pd.DataFrame(
            [
                {
                    "关键词": "002594",
                    "新闻标题": "公司中标重大合同，业绩超预期",
                    "新闻内容": "订单金额较大",
                    "发布时间": (AS_OF - timedelta(hours=2)).isoformat(),
                    "文章来源": "证券时报",
                    "新闻链接": "http://finance.eastmoney.com/a/1.html",
                }
            ]
        )
    )
    row = AltFactorBuilder().build("002594", AS_OF, news)
    assert row.factor("social_mention_count_1d") == pytest.approx(1.0)
    assert row.factor("news_sentiment") is not None and row.factor("news_sentiment") > 0
    assert row.factor("news_sentiment_confidence") > 0
    # 无合规来源的因子必须恒为缺失
    for name in UNAVAILABLE_FACTORS:
        assert row.factor(name) is None, name
    assert set(UNAVAILABLE_FACTORS) == {
        "search_index",
        "search_index_growth",
        "analyst_rating_change",
        "target_price_upside",
    }


def test_growth_with_zero_baseline_is_missing() -> None:
    def _news(day: datetime) -> AltRecord:
        as_of = day
        return AltRecord(
            source=SOURCE_NEWS,
            symbol="002594",
            event_time=as_of,
            as_of_time=as_of,
            title="中标公告",
            text="公司中标",
        )

    records = [_news(AS_OF - timedelta(days=16))]
    row = AltFactorBuilder().build("002594", AS_OF, records)
    assert row.factor("social_mention_count_7d") == pytest.approx(0.0)
    assert row.factor("social_mention_growth") is None  # 基期为 0，不给伪增长


def test_sentiment_change_against_prior_row() -> None:
    builder = AltFactorBuilder()
    news = normalize_news_frame(
        pd.DataFrame(
            [
                {
                    "关键词": "002594",
                    "新闻标题": "涉嫌违规被立案调查",
                    "新闻内容": "公司收到证监会《立案告知书》",
                    "发布时间": (AS_OF - timedelta(hours=1)).isoformat(),
                    "文章来源": "证券时报",
                    "新闻链接": "http://finance.eastmoney.com/a/2.html",
                }
            ]
        )
    )
    today = builder.build("002594", AS_OF, news)
    assert today.factor("social_sentiment") is not None
    assert today.factor("social_sentiment_change") is None  # 无历史行
    prior = builder.build(
        "002594",
        AS_OF - timedelta(days=1),
        [
            AltRecord(
                source=SOURCE_NEWS,
                symbol="002594",
                event_time=AS_OF - timedelta(days=1, hours=2),
                as_of_time=AS_OF - timedelta(days=1, hours=2),
                title="业绩预增公告",
                text="净利润同比增长",
            )
        ],
    )
    today_with_prior = builder.build("002594", AS_OF, news, prior_rows=[prior])
    change = today_with_prior.factor("social_sentiment_change")
    assert change is not None
    assert change < 0  # 由正转负


def test_frame_conversion_keeps_nan_and_availability_mask() -> None:
    row = AltFactorBuilder().build("002594", AS_OF, [_qa(AS_OF, answered=False)])
    frame = factor_frame_from_rows([row])
    assert "attention_index" in frame.columns
    assert "attention_index__available" in frame.columns
    assert not bool(frame["attention_index__available"].iloc[0])
    assert pd.isna(frame["attention_index"].iloc[0])
    assert pd.isna(frame["alt_attention_score"].iloc[0])
    assert not pd.isna(frame["interactive_qa_count_1d"].iloc[0])


def test_composite_weights_sum_to_one() -> None:
    assert sum(COMPOSITE_WEIGHTS.values()) == pytest.approx(1.0)


def test_builder_is_deterministic_for_same_inputs() -> None:
    records = [_attention("2026-02-27", 100.0), _attention("2026-03-06", 150.0)]
    first = AltFactorBuilder().build("002594", AS_OF, records)
    second = AltFactorBuilder().build("002594", AS_OF, list(reversed(records)))
    assert first.values == second.values
    assert first.availability == second.availability
