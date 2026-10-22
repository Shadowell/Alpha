"""情绪解析单测（issue #33）：命中、否定、失败降级、置信度与聚合。"""

from __future__ import annotations

import pytest

from app.services.alt_sentiment.records import is_missing
from app.services.alt_sentiment.sentiment import (
    SentimentScore,
    aggregate_scores,
    bigram_tokens,
    normalize_text,
    score_text,
)


def test_positive_and_negative_hits() -> None:
    positive = score_text("公司中标重大合同 业绩超预期", "订单金额创历史新高")
    assert positive.available
    assert positive.score > 0
    assert 0 < positive.confidence <= 0.85
    assert "中标" in positive.hits

    negative = score_text("因涉嫌违规被立案调查", None)
    assert negative.available and negative.score < 0


def test_negation_flips_direction() -> None:
    plain = score_text("业绩预增")
    negated = score_text("业绩未预增")
    assert plain.score > 0
    assert negated.score is not None and negated.score < 0


def test_empty_or_too_short_text_is_missing_not_zero() -> None:
    empty = score_text(None)
    assert empty.score is None and empty.confidence == 0.0
    assert empty.reason == "empty_text"

    short = score_text("好")
    assert short.score is None
    assert short.reason == "text_too_short"
    assert is_missing(short.score)


def test_no_lexicon_hit_is_weak_neutral() -> None:
    result = score_text("今日召开年度股东大会", "会议按章程完成各项议案")
    assert result.available
    assert result.score == pytest.approx(0.0)
    assert result.confidence <= 0.25


def test_confidence_is_capped() -> None:
    text = "利好 中标 订单 突破 涨停 获批 量产 增持 重组 涨价"
    result = score_text(text, text)
    assert result.confidence <= 0.85
    assert -1.0 <= result.score <= 1.0


def test_html_em_tag_is_stripped() -> None:
    assert normalize_text("<em>中标</em>重大合同") == "中标重大合同"
    result = score_text("<em>中标</em>重大合同", None)
    assert result.score is not None and result.score > 0


def test_aggregate_weights_by_confidence() -> None:
    strong = SentimentScore(0.8, 0.8, hits=("中标",))
    weak = SentimentScore(-0.2, 0.1)
    merged = aggregate_scores([strong, weak])
    assert merged.available
    assert merged.score > 0  # 高置信度正样本主导


def test_aggregate_of_all_missing_is_missing() -> None:
    merged = aggregate_scores([SentimentScore(None, 0.0), SentimentScore(None, 0.0)])
    assert merged.score is None
    assert merged.reason == "no_valid_score"


def test_aggregate_zero_confidence_mass_is_missing() -> None:
    merged = aggregate_scores([SentimentScore(0.5, 0.0)], weights=[0.0])
    assert merged.score is None
    assert merged.reason == "zero_confidence_mass"


def test_bigram_tokens_for_novelty() -> None:
    tokens = bigram_tokens("公司中标 重大合同")
    assert "中标" in tokens
    assert "公司" in tokens
    assert bigram_tokens(None) == frozenset()
