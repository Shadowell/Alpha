"""市场情绪周期（regime）与连续情绪分数 —— issue #25 纯计算层。

口径约定（与 ``app.services.sentiment_factors`` 共同构成 #25 计算真相源）：

* 只消费**收盘后可见**的日频因子（当日涨停/炸板池、昨日涨停池今日行情、
  当日全市场统计、指数与资金流）。参与规则与打分的字段名全部列在
  ``RULE_INPUT_FIELDS`` 白名单里；``intraday_*`` 估算字段永远不在白名单中，
  因此盘中估算数据在结构上不可能污染收盘 regime（隔离由测试锁定）。
* ``sentiment_score`` 为 0–100 连续分：各分量按绝对锚点做分段线性归一
  （不依赖滚动样本/历史分位），同输入必同输出，历史回填与实时增量公式
  完全一致，保证幂等。
* 缺失数据不参与打分：某分量缺失时按剩余权重重新归一；某桶（广度/梯队/
  效应）全缺失时整桶剔除再归一；全部缺失时分数与 regime 返回 ``None``，
  绝不填 0（0 会被下游误读为"冰点"）。
* regime 规则是**有序规则链**：自上而下第一条全部条件可评估且成立的规则
  获胜；最后一条兜底规则按分数带 + 动量给出确定性标签。规则阈值可通过
  ``build_default_rules(params)`` 覆盖，也可以整体注入自定义
  ``Sequence[RegimeRule]``。默认阈值面向 3 年以上全周期稳定性（冰点/退潮
  必然出现），不以最近一年调参。

六阶段标签：冰点 / 修复 / 发酵 / 高潮 / 分歧 / 退潮。

规则直觉（默认参数下）：

===========  =============================================
regime       核心条件
===========  =============================================
冰点         涨停数低位 + 连板高度<=2 + 炸板率高 + 溢价转负
高潮         涨停数高位 + 连板高度>=5 + 1进2晋级率高 + 放量
分歧         分数与连板高度仍在高位，但炸板/大面/溢价任一恶化
退潮         从高位回落（分数下降>=8 且昨分>=60）+ 溢价转负
发酵         中高位且梯队完整、晋级率与溢价为正、分数不下行
修复         冰点后分数上行、溢价回正、炸板率回落
===========  =============================================
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

REGIME_FREEZE = "冰点"
REGIME_REPAIR = "修复"
REGIME_FERMENT = "发酵"
REGIME_CLIMAX = "高潮"
REGIME_DIVERGE = "分歧"
REGIME_EBB = "退潮"

ALL_REGIMES: tuple[str, ...] = (
    REGIME_FREEZE,
    REGIME_REPAIR,
    REGIME_FERMENT,
    REGIME_CLIMAX,
    REGIME_DIVERGE,
    REGIME_EBB,
)


def num_field(factors: Mapping[str, Any], key: str) -> float | None:
    """把因子值转成 float；None/NaN/Inf/不可解析一律返回 None（缺失）。"""
    if key not in factors:
        return None
    value = factors[key]
    if isinstance(value, bool):
        # bool 视为确定性标记（如 ladder_integrity），允许参与打分。
        return 1.0 if value else 0.0
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(result) or math.isinf(result):
        return None
    return result


# ─── 连续分数 ───


@dataclass(frozen=True)
class ScoreComponent:
    """单个打分分量：``field`` 在 [lo, hi] 上线性映射到 [0, 1]。

    ``invert=True`` 时取反向（数值越大情绪越差，如炸板率）。
    """

    field: str
    lo: float
    hi: float
    weight: float
    invert: bool = False


def _clip01(value: float) -> float:
    return max(0.0, min(1.0, value))


def band_score(value: float, lo: float, hi: float, invert: bool = False) -> float:
    if hi <= lo:
        raise ValueError(f"band_score requires hi > lo, got lo={lo}, hi={hi}")
    raw = (value - lo) / (hi - lo)
    raw = _clip01(raw)
    return round(1.0 - raw, 6) if invert else round(raw, 6)


# 桶权重：广度 / 连板梯队 / 赚钱效应。效应桶权重最高，因为打板胜率
# 最直接依赖"昨日涨停今天有没有溢价"。
DEFAULT_BUCKET_WEIGHTS: Mapping[str, float] = {
    "breadth": 0.35,
    "ladder": 0.30,
    "effect": 0.35,
}

# 绝对锚点按 2019–2025 全市场涨停家数/高度分布取稳定区间：
# 冰点日常涨停 20–40 家、高潮 100–150 家；连板高度 1–7；
# 炸板率常态 25%–65%；昨日涨停溢价 -2%–+4%。
DEFAULT_SCORE_BUCKETS: Mapping[str, tuple[ScoreComponent, ...]] = {
    "breadth": (
        ScoreComponent("limit_up_count", 20.0, 120.0, 0.35),
        ScoreComponent("limit_up_down_ratio", 1.0, 8.0, 0.15),
        ScoreComponent("up_ratio", 0.35, 0.65, 0.20),
        ScoreComponent("median_return", -0.02, 0.02, 0.30),
    ),
    "ladder": (
        ScoreComponent("max_consecutive_boards", 1.0, 7.0, 0.35),
        ScoreComponent("continuation_count", 0.0, 30.0, 0.20),
        ScoreComponent("promotion_rate_1_to_2", 0.0, 0.40, 0.25),
        ScoreComponent("ladder_integrity", 0.0, 1.0, 0.20),
    ),
    "effect": (
        ScoreComponent(
            "yesterday_limit_up_premium_close_mean", -0.02, 0.04, 0.35
        ),
        ScoreComponent("money_making_effect", 0.30, 0.70, 0.25),
        ScoreComponent("broken_board_rate", 0.25, 0.65, 0.25, invert=True),
        ScoreComponent("big_loss_rate", 0.0, 0.30, 0.15, invert=True),
    ),
}


@dataclass(frozen=True)
class SentimentScoreResult:
    score: float | None
    components: Mapping[str, float | None]


def compute_sentiment_score(
    factors: Mapping[str, Any],
    *,
    buckets: Mapping[str, Sequence[ScoreComponent]] | None = None,
    bucket_weights: Mapping[str, float] | None = None,
) -> SentimentScoreResult:
    """按桶加权求连续情绪分（0–100）；缺失分量重归一，全缺失返回 None。"""
    buckets = buckets or DEFAULT_SCORE_BUCKETS
    bucket_weights = bucket_weights or DEFAULT_BUCKET_WEIGHTS
    component_scores: dict[str, float | None] = {}
    bucket_total = 0.0
    bucket_weight_used = 0.0
    for bucket_name, specs in buckets.items():
        used = 0.0
        acc = 0.0
        for spec in specs:
            value = num_field(factors, spec.field)
            if value is None:
                continue
            acc += spec.weight * band_score(value, spec.lo, spec.hi, spec.invert)
            used += spec.weight
        if used <= 0.0:
            component_scores[bucket_name] = None
            continue
        bucket_score = acc / used
        weight = float(bucket_weights.get(bucket_name, 0.0))
        if weight > 0.0:
            bucket_total += weight * bucket_score
            bucket_weight_used += weight
        component_scores[bucket_name] = round(bucket_score, 6)
    if bucket_weight_used <= 0.0:
        return SentimentScoreResult(score=None, components=component_scores)
    return SentimentScoreResult(
        score=round(100.0 * bucket_total / bucket_weight_used, 4),
        components=component_scores,
    )


# ─── 规则 regime ───

RulePredicate = Callable[["RegimeContext"], bool]


@dataclass(frozen=True)
class RegimeRule:
    label: str
    description: str
    predicate: RulePredicate
    catch_all: bool = False


@dataclass(frozen=True)
class RegimeContext:
    """规则评估上下文：当日因子 + 当日分数 + 前一交易日对应值。"""

    factors: Mapping[str, Any]
    score: float | None
    prev_factors: Mapping[str, Any] | None
    prev_score: float | None
    params: Mapping[str, float]

    def value(self, key: str) -> float | None:
        return num_field(self.factors, key)

    def prev_value(self, key: str) -> float | None:
        if self.prev_factors is None:
            return None
        return num_field(self.prev_factors, key)

    @property
    def momentum(self) -> float | None:
        """当日分数相对昨日的变化；昨日缺失时返回 None（视为持平）。"""
        if self.score is None or self.prev_score is None:
            return None
        return self.score - self.prev_score

    def is_rising(self) -> bool:
        eps = self.params["momentum_eps"]
        momentum = self.momentum
        return momentum is not None and momentum > eps

    def is_falling(self) -> bool:
        eps = self.params["momentum_eps"]
        momentum = self.momentum
        return momentum is not None and momentum < -eps


DEFAULT_REGIME_PARAMS: Mapping[str, float] = {
    # 冰点
    "freeze_score_max": 30.0,
    "freeze_limit_up_max": 30.0,
    "freeze_max_boards": 2.0,
    "freeze_broken_rate_min": 0.40,
    # 高潮
    "climax_score_min": 75.0,
    "climax_limit_up_min": 80.0,
    "climax_max_boards_min": 5.0,
    "climax_promotion_min": 0.25,
    "climax_amount_ratio_min": 1.20,
    # 分歧
    "diverge_score_min": 55.0,
    "diverge_max_boards_min": 4.0,
    "diverge_broken_rate_min": 0.45,
    "diverge_big_loss_rate_min": 0.15,
    # 退潮
    "ebb_prev_score_min": 60.0,
    "ebb_score_drop_min": 8.0,
    # 发酵
    "ferment_score_min": 45.0,
    "ferment_max_boards_min": 3.0,
    "ferment_promotion_min": 0.20,
    # 修复
    "repair_score_min": 30.0,
    "repair_score_max": 55.0,
    "repair_broken_rate_max": 0.50,
    # 动量阈值（分数单位，0–100 制）
    "momentum_eps": 1.0,
}


def _lt(value: float | None, bound: float) -> bool:
    return value is not None and value < bound


def _le(value: float | None, bound: float) -> bool:
    return value is not None and value <= bound


def _gt(value: float | None, bound: float) -> bool:
    return value is not None and value > bound


def _ge(value: float | None, bound: float) -> bool:
    return value is not None and value >= bound


def _in_range(value: float | None, lo: float, hi: float) -> bool:
    return value is not None and lo <= value < hi


def _any_evaluable_true(
    ctx: RegimeContext, keys: Sequence[str], bounds: Sequence[float],
    invert: Sequence[bool],
) -> bool:
    """至少一个条件可评估，且所有可评估条件中至少一个成立。"""
    evaluable = 0
    hit = False
    for key, bound, inverse in zip(keys, bounds, invert):
        value = ctx.value(key)
        if value is None:
            continue
        evaluable += 1
        ok = value <= bound if inverse else value >= bound
        if ok:
            hit = True
    return evaluable > 0 and hit


def _rule_freeze(ctx: RegimeContext) -> bool:
    p = ctx.params
    return (
        _le(ctx.score, p["freeze_score_max"])
        and _le(ctx.value("limit_up_count"), p["freeze_limit_up_max"])
        and _le(
            ctx.value("max_consecutive_boards"), p["freeze_max_boards"]
        )
        and _ge(
            ctx.value("broken_board_rate"), p["freeze_broken_rate_min"]
        )
        and _lt(ctx.value("yesterday_limit_up_premium_close_mean"), 0.0)
    )


def _rule_climax(ctx: RegimeContext) -> bool:
    p = ctx.params
    return (
        _ge(ctx.score, p["climax_score_min"])
        and _ge(ctx.value("limit_up_count"), p["climax_limit_up_min"])
        and _ge(
            ctx.value("max_consecutive_boards"), p["climax_max_boards_min"]
        )
        and _ge(ctx.value("promotion_rate_1_to_2"), p["climax_promotion_min"])
        and _ge(
            ctx.value("market_amount_ratio_5d"), p["climax_amount_ratio_min"]
        )
        and _ge(ctx.value("yesterday_limit_up_premium_close_mean"), 0.0)
    )


def _rule_diverge(ctx: RegimeContext) -> bool:
    p = ctx.params
    if not _ge(ctx.score, p["diverge_score_min"]):
        return False
    if not _ge(ctx.value("max_consecutive_boards"), p["diverge_max_boards_min"]):
        return False
    return _any_evaluable_true(
        ctx,
        (
            "broken_board_rate",
            "big_loss_rate",
            "yesterday_limit_up_premium_close_mean",
        ),
        # 溢价条件用"转负"：上界 0，invert=True（<=0 视为成立）。
        (p["diverge_broken_rate_min"], p["diverge_big_loss_rate_min"], 0.0),
        (False, False, True),
    )


def _rule_ebb(ctx: RegimeContext) -> bool:
    p = ctx.params
    if ctx.prev_score is None or ctx.score is None:
        return False
    if ctx.prev_score < p["ebb_prev_score_min"]:
        return False
    if (ctx.prev_score - ctx.score) < p["ebb_score_drop_min"]:
        return False
    return _lt(ctx.value("yesterday_limit_up_premium_close_mean"), 0.0)


def _rule_ferment(ctx: RegimeContext) -> bool:
    p = ctx.params
    if ctx.score is None:
        return False
    if not _in_range(ctx.score, p["ferment_score_min"], p["climax_score_min"]):
        return False
    if not _ge(
        ctx.value("max_consecutive_boards"), p["ferment_max_boards_min"]
    ):
        return False
    if not _ge(ctx.value("promotion_rate_1_to_2"), p["ferment_promotion_min"]):
        return False
    if not _gt(ctx.value("yesterday_limit_up_premium_close_mean"), 0.0):
        return False
    return not ctx.is_falling()


def _rule_repair(ctx: RegimeContext) -> bool:
    p = ctx.params
    if ctx.score is None:
        return False
    if not _in_range(ctx.score, p["repair_score_min"], p["repair_score_max"]):
        return False
    if not ctx.is_rising():
        return False
    if not _ge(
        ctx.value("yesterday_limit_up_premium_close_mean"), 0.0
    ):
        return False
    return _le(ctx.value("broken_board_rate"), p["repair_broken_rate_max"])


def _rule_fallback(ctx: RegimeContext) -> bool:
    return ctx.score is not None


def _fallback_label(ctx: RegimeContext) -> str:
    assert ctx.score is not None  # 兜底谓词保证
    score = ctx.score
    if score <= ctx.params["repair_score_min"]:
        return REGIME_FREEZE
    if score >= ctx.params["climax_score_min"]:
        return REGIME_CLIMAX
    if ctx.is_falling():
        return REGIME_EBB if score < 50.0 else REGIME_DIVERGE
    if ctx.is_rising():
        return REGIME_REPAIR if score < 50.0 else REGIME_FERMENT
    return REGIME_REPAIR if score < 50.0 else REGIME_FERMENT


def _apply_fallback(ctx: RegimeContext, label: str) -> str:
    return label


def build_default_rules(
    params: Mapping[str, float] | None = None,
) -> tuple[RegimeRule, ...]:
    """按优先级返回默认规则链；最后一条是按分数带+动量的确定性兜底。

    规则谓词本身无状态，阈值通过 ``classify_regime(regime_params=...)`` 注入
    ``RegimeContext.params``；本函数仅校验参数名合法性，便于外部预检。
    """
    merged = dict(DEFAULT_REGIME_PARAMS)
    if params:
        unknown = set(params) - set(merged)
        if unknown:
            raise ValueError(f"unknown regime params: {sorted(unknown)}")
        merged.update(params)
    assert merged  # 参数校验通过即可，阈值在 classify 时注入上下文。
    return (
        RegimeRule(
            REGIME_FREEZE,
            "涨停数低位+连板高度<=2+炸板率高+昨日涨停溢价为负",
            _rule_freeze,
        ),
        RegimeRule(
            REGIME_CLIMAX,
            "涨停数高位+连板高度>=5+1进2晋级率高+成交额放大+溢价非负",
            _rule_climax,
        ),
        RegimeRule(
            REGIME_DIVERGE,
            "高分+高连板下炸板/大面/溢价任一恶化",
            _rule_diverge,
        ),
        RegimeRule(
            REGIME_EBB,
            "自高位分数大幅回落且昨日涨停溢价转负",
            _rule_ebb,
        ),
        RegimeRule(
            REGIME_FERMENT,
            "中高分数、梯队完整、晋级与溢价为正且分数未回落",
            _rule_ferment,
        ),
        RegimeRule(
            REGIME_REPAIR,
            "低分段分数上行、溢价回正、炸板率回落",
            _rule_repair,
        ),
        RegimeRule(
            "fallback",
            "按分数带与动量兜底，保证每个可打分交易日都有标签",
            _rule_fallback,
            catch_all=True,
        ),
    )


# 规则与打分实际读取的收盘因子字段（隔离 intraday_* 的白名单真相源）。
RULE_INPUT_FIELDS: frozenset[str] = frozenset(
    {
        "limit_up_count",
        "limit_up_down_ratio",
        "up_ratio",
        "median_return",
        "max_consecutive_boards",
        "continuation_count",
        "promotion_rate_1_to_2",
        "ladder_integrity",
        "yesterday_limit_up_premium_close_mean",
        "money_making_effect",
        "broken_board_rate",
        "big_loss_rate",
        "market_amount_ratio_5d",
    }
)

assert all(
    not name.startswith("intraday_") for name in RULE_INPUT_FIELDS
), "intraday 估算字段禁止进入收盘 regime 规则"


def classify_regime(
    factors: Mapping[str, Any],
    *,
    score: float | None = None,
    prev_factors: Mapping[str, Any] | None = None,
    prev_score: float | None = None,
    regime_rules: Sequence[RegimeRule] | None = None,
    regime_params: Mapping[str, float] | None = None,
    missing_label: str | None = None,
) -> str | None:
    """返回当日收盘 regime 标签；无法打分时返回 ``missing_label``（默认 None）。

    只读取 ``RULE_INPUT_FIELDS`` 白名单内的字段：即使把 ``intraday_*`` 估算
    值混进 ``factors``，也不会影响判定结果（结构性隔离）。
    """
    if score is None:
        score = compute_sentiment_score(factors).score
    params = dict(DEFAULT_REGIME_PARAMS)
    if regime_params:
        unknown = set(regime_params) - set(params)
        if unknown:
            raise ValueError(
                f"unknown regime params: {sorted(unknown)}"
            )
        params.update(regime_params)
    rules = (
        tuple(regime_rules)
        if regime_rules is not None
        else build_default_rules(params)
    )
    ctx = RegimeContext(
        factors=factors,
        score=score,
        prev_factors=prev_factors,
        prev_score=prev_score,
        params=params,
    )
    if score is None:
        if rules and rules[-1].catch_all:
            return _apply_fallback(ctx, missing_label)
        return missing_label
    for rule in rules:
        if rule.predicate(ctx):
            if rule.catch_all:
                return _apply_fallback(ctx, _fallback_label(ctx))
            return rule.label
    return missing_label
