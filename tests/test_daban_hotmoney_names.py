"""#34 席位名称标准化 / 别名合并 / 可审计名录单测（issue 验收项：席位别名）。

纯函数测试，不访问数据库、不访问网络。
"""

from __future__ import annotations

import pytest

from strategy.daban_hotmoney.seat_names import (
    HOT_MONEY_LABELS,
    KIND_BROKER_BRANCH,
    KIND_INSTITUTION,
    KIND_NORTHBOUND,
    KIND_TRADE_UNIT,
    LABEL_DISCLAIMER,
    SEAT_ALIAS_RULES,
    HotMoneyLabel,
    SeatAliasRule,
    apply_alias_rules,
    label_audit_rows,
    normalization_report,
    normalize_seat_name,
    seat_label,
    standard_seat_key,
)


def test_broker_full_name_is_shortened_and_deduped():
    seat = normalize_seat_name("国泰君安证券股份有限公司上海江苏路证券营业部")
    assert seat.standard_name == "国泰君安上海江苏路营业部"
    assert seat.method == "alias"
    assert seat.kind == KIND_BROKER_BRANCH
    assert seat.broker == "国泰君安"
    assert seat.is_standardized is True


def test_longest_broker_key_wins():
    # 中信建投 不能被 中信 抢先匹配
    seat = normalize_seat_name("中信建投证券股份有限公司上海南京西路证券营业部")
    assert seat.broker == "中信建投"
    assert seat.standard_name == "中信建投上海南京西路营业部"
    other = normalize_seat_name("中信证券股份有限公司北京总部")
    assert other.broker == "中信"


def test_registered_noise_and_fullwidth_are_stripped():
    seat = normalize_seat_name("  海通证券股份有限公司（已注销）上海XT营业部 ")
    assert seat.standard_name == "海通上海XT营业部"
    assert "已注销" not in seat.standard_name
    # NFKC 会把全角括号转半角，因此记录的是半角形态的噪声 token
    assert any("已注销" in token for token in seat.noise_removed)
    assert "股份有限公司" in seat.noise_removed
    fullwidth = normalize_seat_name("海通证券股份有限公司上海ＸＴ营业部")
    assert fullwidth.standard_name == seat.standard_name


def test_institution_and_northbound_special_texts():
    assert (
        normalize_seat_name("机构专用").standard_name == "机构专用"
        and normalize_seat_name("机构专用").kind == KIND_INSTITUTION
    )
    assert normalize_seat_name("沪股通专用").kind == KIND_NORTHBOUND
    assert normalize_seat_name("深股通专用").standard_name == "深股通"
    assert normalize_seat_name("沪股通").standard_name == "沪股通"


def test_trade_unit_seat_kind():
    seat = normalize_seat_name("招商证券交易单元123456")
    assert seat.kind == KIND_TRADE_UNIT
    assert seat.method == "alias"


def test_unknown_text_is_not_forced_into_a_group():
    seat = normalize_seat_name("某某投资咨询有限公司")
    assert seat.standard_name is None
    assert seat.method == "unmatched"
    assert seat.is_standardized is False


def test_empty_and_none_inputs():
    assert normalize_seat_name("").method == "empty"
    assert normalize_seat_name(None).method == "empty"
    assert standard_seat_key(None) is None


def test_alias_rule_merges_text_variants_only():
    # 默认表里就有"省略营业部后缀"的文本变体规则
    assert standard_seat_key("中信证券股份有限公司北京总部") == "中信北京总部营业部", (
        SEAT_ALIAS_RULES
    )
    custom = (
        SeatAliasRule(
            pattern=r"^华泰深圳$",
            standard_name="华泰深圳营业部",
            source="测试：同一营业部两种写法",
        ),
    )
    merged = normalize_seat_name(
        "华泰证券股份有限公司深圳证券营业部", alias_rules=custom
    )
    # 标准化后是"华泰深圳营业部"，自定义规则不应误伤其它文本
    assert merged.standard_name == "华泰深圳营业部"
    assert apply_alias_rules("银河北京", custom)[1] is None


def test_alias_rule_requires_auditable_source():
    with pytest.raises(ValueError):
        SeatAliasRule(pattern=r"^x$", standard_name="y", source="   ")
    with pytest.raises(ValueError):
        SeatAliasRule(pattern="", standard_name="y", source="src")


def test_normalization_report_coverage_by_record():
    rows = [
        "国泰君安证券股份有限公司上海江苏路证券营业部",
        "机构专用",
        "沪股通专用",
        "某某投资咨询有限公司",  # 未覆盖
    ]
    report = normalization_report(rows)
    assert report["seat_records_total"] == 4
    assert report["seat_records_standardized"] == 3
    assert report["seat_name_coverage"] == pytest.approx(0.75)
    assert report["coverage_met"] is False
    assert report["coverage_status"] == "fail"
    assert report["coverage_target"] == 0.95
    assert report["unmatched_examples"] == ["某某投资咨询有限公司"]


def test_normalization_report_no_data_is_neutral():
    report = normalization_report([])
    assert report["seat_name_coverage"] is None
    assert report["coverage_status"] == "no_data"
    assert report["coverage_met"] is False


def test_curated_labels_are_empty_and_unlabeled_by_default():
    # issue：游资标签必须人工维护且可审计；默认不标注任何人
    assert HOT_MONEY_LABELS == ()
    assert seat_label("国泰君安上海江苏路营业部") is None
    assert seat_label(None) is None


def test_hot_money_label_guard_and_audit_rows():
    label = HotMoneyLabel(
        standard_name="国泰君安上海江苏路营业部",
        label="测试标签",
        style_hint="relay",
        source="公开龙虎榜文本 + 人工复盘记录（示例）",
        curated_by="research",
        curated_at="2026-10-01",
        confidence=0.6,
    )
    with pytest.raises(ValueError):
        HotMoneyLabel(
            standard_name="x",
            label="y",
            style_hint=None,
            source="",
            curated_by="me",
            curated_at="2026-10-01",
        )
    with pytest.raises(ValueError):
        HotMoneyLabel(
            standard_name="x",
            label="y",
            style_hint=None,
            source="src",
            curated_by="me",
            curated_at="2026-10-01",
            confidence=1.5,
        )
    rows = label_audit_rows([label])
    assert rows[0]["label_source"].startswith("公开龙虎榜")
    assert rows[0]["label_disclaimer"] == LABEL_DISCLAIMER
    assert seat_label("国泰君安上海江苏路营业部", labels=[label]) is label
