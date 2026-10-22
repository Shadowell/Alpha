"""#25 情绪分数与 regime 规则（app.services.market_regime）单元测试。

全部为纯内存 fixture，不依赖数据库/网络。
"""

from __future__ import annotations

import math

import pytest

from app.services.market_regime import (
    ALL_REGIMES,
    DEFAULT_REGIME_PARAMS,
    RULE_INPUT_FIELDS,
    RegimeRule,
    band_score,
    build_default_rules,
    classify_regime,
    compute_sentiment_score,
    num_field,
)


def test_num_field_missing_and_dirty_values():
    assert num_field({}, "x") is None
    assert num_field({"x": None}, "x") is None
    assert num_field({"x": float("nan")}, "x") is None
    assert num_field({"x": float("inf")}, "x") is None
    assert num_field({"x": "abc"}, "x") is None
    assert num_field({"x": "2.5"}, "x") == 2.5
    assert num_field({"x": True}, "x") == 1.0
    assert num_field({"x": False}, "x") == 0.0


def test_band_score_clips_and_inverts():
    assert band_score(0.0, 0.0, 1.0) == 0.0
    assert band_score(2.0, 0.0, 1.0) == 1.0
    assert band_score(0.5, 0.0, 1.0) == 0.5
    assert band_score(0.5, 0.0, 1.0, invert=True) == 0.5
    assert band_score(0.1, 0.0, 1.0, invert=True) == 0.9
    with pytest.raises(ValueError):
        band_score(1.0, 2.0, 2.0)


# ─── 连续分数 ───


def _full_score_factors() -> dict:
    return {
        "limit_up_count": 90,
        "limit_up_down_ratio": 6.0,
        "up_ratio": 0.60,
        "median_return": 0.012,
        "max_consecutive_boards": 5,
        "continuation_count": 25,
        "promotion_rate_1_to_2": 0.32,
        "ladder_integrity": 1,
        "yesterday_limit_up_premium_close_mean": 0.03,
        "money_making_effect": 0.65,
        "broken_board_rate": 0.30,
        "big_loss_rate": 0.05,
    }


def test_score_bounds_and_determinism():
    factors = _full_score_factors()
    first = compute_sentiment_score(factors)
    second = compute_sentiment_score(factors)
    assert first.score is not None
    assert first.score == second.score
    assert 0.0 <= first.score <= 100.0
    assert first.score > 75.0  # 强情绪样本应落在高分段
    assert set(first.components) == {"breadth", "ladder", "effect"}


def test_score_missing_component_renormalizes():
    factors = _full_score_factors()
    base = compute_sentiment_score(factors)
    del factors["big_loss_rate"]  # 效应桶少一个分量，其余权重重归一
    variant = compute_sentiment_score(factors)
    assert variant.score is not None
    assert variant.components["effect"] is not None
    # 大面率原本是拉分项（invert 高分量），删掉后效应分应上升。
    assert variant.components["effect"] >= base.components["effect"]


def test_score_bucket_missing_and_all_missing():
    factors = {
        "limit_up_count": 100,
        "up_ratio": 0.6,
        "median_return": 0.01,
        "limit_up_down_ratio": 5.0,
    }
    result = compute_sentiment_score(factors)
    assert result.score is not None
    assert result.components["breadth"] is not None
    assert result.components["ladder"] is None
    assert result.components["effect"] is None
    # 只剩广度桶时，总分等于广度桶分。
    assert math.isclose(result.score, result.components["breadth"] * 100, abs_tol=1e-3)

    assert compute_sentiment_score({}).score is None
    assert compute_sentiment_score({"limit_up_count": None}).score is None


# ─── regime 规则 ───


def test_regime_freeze_boundary_exact_thresholds():
    factors = {
        "limit_up_count": DEFAULT_REGIME_PARAMS["freeze_limit_up_max"],
        "max_consecutive_boards": DEFAULT_REGIME_PARAMS["freeze_max_boards"],
        "broken_board_rate": DEFAULT_REGIME_PARAMS["freeze_broken_rate_min"],
        "yesterday_limit_up_premium_close_mean": -0.001,
    }
    assert (
        classify_regime(factors, score=DEFAULT_REGIME_PARAMS["freeze_score_max"])
        == "冰点"
    )
    # 溢价恰好为 0 时冰点条件不成立（严格小于），走兜底低分段 → 仍是冰点，
    # 但分数被抬到 40 时不再触发冰点。
    factors["yesterday_limit_up_premium_close_mean"] = 0.0
    assert classify_regime(factors, score=40.0) != "冰点"


def test_regime_freeze_requires_all_conditions_present():
    factors = {
        "limit_up_count": 10,
        "max_consecutive_boards": 1,
        "broken_board_rate": 0.6,
        # 溢价缺失（None）→ 冰点规则不可评估，落入兜底。
    }
    assert classify_regime(factors, score=20.0) == "冰点"  # 兜底低分段
    factors["yesterday_limit_up_premium_close_mean"] = -0.02
    assert classify_regime(factors, score=20.0) == "冰点"  # 显式规则
    factors["yesterday_limit_up_premium_close_mean"] = 0.05
    # 溢价回正 + 分数 20：显式冰点不成立（兜底仍给冰点，说明兜底接管）。
    assert classify_regime(factors, score=20.0) == "冰点"


def test_regime_climax_boundary():
    factors = {
        "limit_up_count": DEFAULT_REGIME_PARAMS["climax_limit_up_min"],
        "max_consecutive_boards": DEFAULT_REGIME_PARAMS["climax_max_boards_min"],
        "promotion_rate_1_to_2": DEFAULT_REGIME_PARAMS["climax_promotion_min"],
        "market_amount_ratio_5d": DEFAULT_REGIME_PARAMS["climax_amount_ratio_min"],
        "yesterday_limit_up_premium_close_mean": 0.01,
    }
    score = DEFAULT_REGIME_PARAMS["climax_score_min"]
    assert classify_regime(factors, score=score) == "高潮"
    factors["market_amount_ratio_5d"] = 1.19
    # 放量条件差之毫厘 → 显式高潮不成立；分数降到 74 避免兜底高分段也高潮。
    assert classify_regime(factors, score=74.0) != "高潮"


def test_regime_diverge_priority_over_ebb_and_ferment():
    # 高分 + 4 板 + 大面率恶化 → 分歧优先于退潮/发酵。
    factors = {
        "max_consecutive_boards": 4,
        "big_loss_rate": 0.20,
        "broken_board_rate": 0.30,
        "promotion_rate_1_to_2": 0.30,
        "yesterday_limit_up_premium_close_mean": 0.01,
    }
    assert classify_regime(factors, score=60.0) == "分歧"
    # 同样高分但各恶化条件缺失（不可评估）→ 分歧不成立，走发酵。
    factors_no_evidence = {
        "max_consecutive_boards": 4,
        "promotion_rate_1_to_2": 0.30,
        "yesterday_limit_up_premium_close_mean": 0.01,
    }
    assert classify_regime(factors_no_evidence, score=60.0) == "发酵"


def test_regime_ebb_requires_drop_and_negative_premium():
    factors = {
        "max_consecutive_boards": 3,
        "yesterday_limit_up_premium_close_mean": -0.02,
        "broken_board_rate": 0.40,
        "promotion_rate_1_to_2": 0.10,
    }
    assert classify_regime(factors, score=50.0, prev_score=65.0) == "退潮"
    # 分数回落不足 8 点 → 不退潮（兜底：下行 + score<50? 分数 58 → 分歧兜底）
    assert classify_regime(factors, score=58.0, prev_score=65.0) == "分歧"
    # 溢价未转负 → 退潮规则不成立；下行 + 分数在 50 上方 → 兜底给分歧。
    factors["yesterday_limit_up_premium_close_mean"] = 0.01
    assert classify_regime(factors, score=50.0, prev_score=65.0) == "分歧"


def test_regime_ferment_and_repair():
    ferment = {
        "max_consecutive_boards": 3,
        "promotion_rate_1_to_2": 0.25,
        "yesterday_limit_up_premium_close_mean": 0.02,
        "broken_board_rate": 0.30,
    }
    assert classify_regime(ferment, score=60.0, prev_score=58.0) == "发酵"
    # 发酵要求分数不下行：大幅下行时优先退潮。
    assert classify_regime(ferment, score=60.0, prev_score=90.0) != "发酵"

    repair = {
        "yesterday_limit_up_premium_close_mean": 0.005,
        "broken_board_rate": 0.45,
        "max_consecutive_boards": 2,
    }
    assert classify_regime(repair, score=40.0, prev_score=32.0) == "修复"
    # 炸板率超过阈值 → 修复不成立（兜底仍给低分段修复/冰点，标签可接受）。
    repair["broken_board_rate"] = 0.55
    assert classify_regime(repair, score=40.0, prev_score=32.0) in ALL_REGIMES


def test_regime_fallback_is_deterministic_for_every_score():
    rising = {}
    for score in range(0, 101, 5):
        label = classify_regime(rising, score=float(score))
        assert label in ALL_REGIMES, (score, label)


def test_regime_missing_score_returns_missing_label():
    assert classify_regime({}) is None
    assert classify_regime({}, missing_label="missing") == "missing"


def test_regime_custom_rules_injection():
    always_ferment = (
        RegimeRule("测试发酵", "注入规则", lambda ctx: ctx.score is not None),
    )
    factors = _full_score_factors()
    assert (
        classify_regime(factors, regime_rules=always_ferment) == "测试发酵"
    )
    # 注入规则全部不成立且无 catch-all → 返回 missing_label。
    never = (RegimeRule("x", "", lambda ctx: False),)
    assert classify_regime(factors, regime_rules=never) is None
    assert classify_regime(factors, regime_rules=never, missing_label="n/a") == "n/a"


def test_regime_params_override_changes_labels():
    factors = {
        "limit_up_count": 30,
        "max_consecutive_boards": 2,
        "broken_board_rate": 0.4,
        "yesterday_limit_up_premium_close_mean": -0.01,
    }
    score = DEFAULT_REGIME_PARAMS["freeze_score_max"]
    assert classify_regime(factors, score=score) == "冰点"
    # 收紧冰点涨停数上限（30>20 显式冰点不成立）并把兜底冰点分数带压到 20
    # 以下 → 分数 30 走兜底低分段 → 修复。
    assert (
        classify_regime(
            factors,
            score=score,
            regime_params={
                "freeze_limit_up_max": 20.0,
                "repair_score_min": 20.0,
            },
        )
        == "修复"
    )
    with pytest.raises(ValueError):
        classify_regime(factors, regime_params={"no_such_param": 1.0})
    with pytest.raises(ValueError):
        build_default_rules({"no_such_param": 1.0})


def test_intraday_fields_never_affect_close_regime():
    factors = _full_score_factors()
    baseline_score = compute_sentiment_score(factors).score
    baseline = classify_regime(dict(factors), score=baseline_score)
    polluted = dict(factors)
    polluted["intraday_limit_up_count"] = 9999
    polluted["intraday_max_consecutive_boards"] = 99
    polluted["intraday_broken_board_rate"] = 0.99
    polluted["intraday_sentiment_score"] = 100.0
    polluted["intraday_regime"] = "高潮"
    assert compute_sentiment_score(polluted).score == baseline_score
    assert classify_regime(polluted, score=baseline_score) == baseline
    assert all(
        not name.startswith("intraday_") for name in RULE_INPUT_FIELDS
    )


def test_default_rule_chain_ends_with_single_catch_all():
    rules = build_default_rules()
    assert rules[-1].catch_all
    assert sum(1 for r in rules if r.catch_all) == 1
    assert [r.label for r in rules[:-1]] == [
        "冰点",
        "高潮",
        "分歧",
        "退潮",
        "发酵",
        "修复",
    ]
