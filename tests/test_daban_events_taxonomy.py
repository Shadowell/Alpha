"""事件类型体系单测（issue #35 验收项：覆盖至少 6 类事件 + 规则兜底）。"""

from __future__ import annotations

import pytest

from strategy.daban_events.taxonomy import (
    EVENT_CODES,
    EVENT_DIVIDEND,
    EVENT_EARNINGS_UP,
    EVENT_MAJOR_CONTRACT,
    EVENT_NEUTRAL,
    EVENT_NEW_BUSINESS,
    EVENT_REGULATORY_NEGATIVE,
    EVENT_RESTRUCTURING,
    EVENT_SHAREHOLDER_ACTION,
    POLARITY_NEGATIVE,
    POLARITY_NEUTRAL,
    POLARITY_POSITIVE,
    SUBSTANTIVE_EVENT_CODES,
    classify_event,
    event_type_doc,
    from_notice_tag,
    match_keywords,
)


def test_covers_at_least_six_event_types():
    """验收项「事件抽取覆盖至少 6 类事件」——实质性类型必须 >= 6。"""

    assert len(SUBSTANTIVE_EVENT_CODES) >= 6
    assert set(EVENT_CODES) == {
        EVENT_RESTRUCTURING,
        EVENT_EARNINGS_UP,
        EVENT_MAJOR_CONTRACT,
        EVENT_SHAREHOLDER_ACTION,
        EVENT_NEW_BUSINESS,
        EVENT_REGULATORY_NEGATIVE,
        EVENT_DIVIDEND,
        EVENT_NEUTRAL,
    }


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("关于筹划重大资产重组暨控制权变更的停牌公告", EVENT_RESTRUCTURING),
        ("2026年第一季度业绩预告：净利润同比增长256%", EVENT_EARNINGS_UP),
        ("关于收到中标通知书的公告", EVENT_MAJOR_CONTRACT),
        ("关于以集中竞价交易方式回购股份的进展公告", EVENT_SHAREHOLDER_ACTION),
        ("关于新产品通过临床试验并获批上市的公告", EVENT_NEW_BUSINESS),
        ("关于收到中国证监会立案告知书的公告", EVENT_REGULATORY_NEGATIVE),
        ("2025年度权益分派实施公告：每10股送3股转增4股派现金", EVENT_DIVIDEND),
    ],
)
def test_each_event_type_is_recognised(title: str, expected: str):
    result = classify_event(title)
    assert result.code == expected
    assert result.confidence > 0
    assert result.hits, "命中必须带原文证据"


def test_polarity_mapping():
    assert classify_event("关于收到问询函的公告").polarity == POLARITY_NEGATIVE
    assert classify_event("关于签订重大合同公告").polarity == POLARITY_POSITIVE
    assert classify_event("关于召开2025年度股东大会的通知").polarity == POLARITY_NEUTRAL


def test_no_keyword_hit_falls_back_to_neutral_not_guessed():
    """规则兜底：无命中一律中性，绝不猜一个事件类型。"""

    result = classify_event("关于更换保荐代表人的公告")
    assert result.code == EVENT_NEUTRAL
    assert result.confidence == 0.0
    assert result.is_substantive is False


def test_non_substantive_risk_flags_are_recorded():
    result = classify_event("关于重大合同的补充公告（风险提示性公告）")
    assert result.non_substantive is True
    assert "supplement_notice" in result.risk_flags
    assert "risk_reminder" in result.risk_flags


def test_evidence_spans_point_at_real_text():
    title = "关于以集中竞价交易方式回购股份的公告"
    result = classify_event(title)
    for hit in result.hits:
        assert title[hit.start : hit.end] == hit.keyword


def test_claimed_term_not_in_text_is_not_invented():
    """LLM 声称看到了"控制权变更"但原文没有 -> 只能登记 unmatched，不改类型。"""

    title = "关于签订日常经营框架协议的 announcement"
    result = classify_event(title, extra_terms=["控制权变更"])
    assert "unmatched_claimed_term" in result.risk_flags
    assert result.code != EVENT_RESTRUCTURING


def test_claimed_term_present_is_accepted_as_evidence():
    title = "关于签署股权转让协议的公告"
    clean = classify_event(title)
    with_term = classify_event(title, extra_terms=["股权转让"])
    assert clean.code == EVENT_RESTRUCTURING
    assert any(h.keyword == "股权转让" for h in with_term.hits)


def test_body_only_hit_is_weaker_than_title_hit():
    title = "关于召开股东大会的通知"
    body = "公司拟收购标的资产，交易对方为控股股东一致行动人。"
    weak = classify_event(title)
    strong = classify_event(title, body=body)
    assert weak.code == EVENT_NEUTRAL
    assert strong.code == EVENT_RESTRUCTURING
    assert strong.confidence < classify_event(body).confidence


def test_notice_tag_alignment_is_read_only_mapping():
    """与 app/services/notice_service.py 的关键词标签口径对齐。"""

    assert from_notice_tag("业绩预增") == EVENT_EARNINGS_UP
    assert from_notice_tag("资产重组") == EVENT_RESTRUCTURING
    assert from_notice_tag("重大合同") == EVENT_MAJOR_CONTRACT
    assert from_notice_tag("股份回购") == EVENT_SHAREHOLDER_ACTION
    assert from_notice_tag("高额分红") == EVENT_DIVIDEND
    assert from_notice_tag("产品突破") == EVENT_NEW_BUSINESS
    # 不属于 issue #35 事件清单 -> 保留但不给事件强度
    assert from_notice_tag("融资获批") == EVENT_NEUTRAL
    assert from_notice_tag(None) == EVENT_NEUTRAL


def test_match_keywords_respects_code_filter():
    hits = match_keywords(
        "关于回购股份暨业绩预增的公告", codes=[EVENT_SHAREHOLDER_ACTION]
    )
    assert {h.code for h in hits} == {EVENT_SHAREHOLDER_ACTION}


def test_event_type_doc_is_serialisable_and_complete():
    doc = event_type_doc()
    assert set(doc) == set(EVENT_CODES)
    assert doc[EVENT_RESTRUCTURING]["label_zh"] == "并购重组"
    assert doc[EVENT_MAJOR_CONTRACT]["requires_amount"] is True
