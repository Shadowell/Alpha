""""被规则拒绝但事后走强"样本记录（issue #32「误杀机会」风险项）。

择时/仓位规则一定会有误杀：退潮期禁买可能错过反转。为了让规则可复盘、
可迭代（而不是靠感觉调参），本模块把每一次拒绝沉淀成结构化样本，并在
拿到事后收益后标注 ``post_strength``（事后走强）。

* 只做记录与统计，不反向修改任何决策；
* ``forward_return`` 缺失时返回 ``None`` 而不是 0，避免"没有数据 = 没走强"。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .constraints import AllocationDecision, AllocationResult
from .models import to_float


@dataclass(frozen=True)
class RejectedOrder:
    """一次被拒绝的买入候选（含命中规则，便于归因）。"""

    trade_date: str
    symbol: str
    name: str
    theme: str | None
    board_height: int | None
    rule_ids: tuple[str, ...]
    reasons: tuple[str, ...]
    requested_amount: float | None
    priority: float = 0.0

    def key(self) -> str:
        return f"{self.trade_date}|{self.symbol}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "trade_date": self.trade_date,
            "symbol": self.symbol,
            "name": self.name,
            "theme": self.theme,
            "board_height": self.board_height,
            "rule_ids": list(self.rule_ids),
            "reasons": list(self.reasons),
            "requested_amount": self.requested_amount,
            "priority": self.priority,
        }


@dataclass(frozen=True)
class RejectionReview:
    """拒绝样本 + 事后收益的复盘结果。"""

    sample: RejectedOrder
    forward_return: float | None
    post_strength: bool | None
    threshold: float
    missing: tuple[str, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample": self.sample.to_dict(),
            "forward_return": self.forward_return,
            "post_strength": self.post_strength,
            "threshold": self.threshold,
            "missing": list(self.missing),
            "extra": dict(self.extra),
        }


def collect_rejections(
    result: AllocationResult,
    *,
    only_buy: bool = True,
    only_timing: bool = False,
) -> tuple[RejectedOrder, ...]:
    """从约束结果里抽出被拒绝的订单（默认含全部拒绝原因）。"""

    out: list[RejectedOrder] = []
    for decision in result.decisions:
        if decision.action != "rejected":
            continue
        if only_buy and decision.side != "buy":
            continue
        if only_timing and not any(str(r).startswith("timing:") for r in decision.rule_ids):
            continue
        out.append(
            RejectedOrder(
                trade_date=result.trade_date,
                symbol=decision.symbol,
                name=decision.name,
                theme=decision.theme,
                board_height=decision.board_height,
                rule_ids=decision.rule_ids,
                reasons=decision.reasons,
                requested_amount=decision.requested_amount,
            )
        )
    return tuple(out)


def rejection_breakdown(
    rejections: Iterable[RejectedOrder],
) -> dict[str, int]:
    """按规则 id 统计拒绝次数（哪个规则最"杀人"）。"""

    counts: dict[str, int] = {}
    for sample in rejections:
        for rule in sample.rule_ids:
            counts[rule] = counts.get(rule, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def review_rejections(
    rejections: Iterable[RejectedOrder],
    forward_returns: Mapping[str, float] | Mapping[tuple[str, str], float],
    *,
    strength_threshold: float = 0.03,
) -> tuple[RejectionReview, ...]:
    """给拒绝样本补上事后收益并标注是否"事后走强"。

    ``forward_returns`` 支持两种键：``symbol`` 或 ``(trade_date, symbol)``；
    查不到就标 ``forward_return=None`` + ``missing``（不填 0）。
    """

    reviews: list[RejectionReview] = []
    table = dict(forward_returns or {})
    for sample in rejections:
        value = None
        lookup_keys: list[Any] = [(sample.trade_date, sample.symbol), sample.symbol]
        for key in lookup_keys:
            if key in table:
                value = to_float(table[key])
                break
        missing: tuple[str, ...] = ()
        if value is None:
            missing = ("forward_return_missing",)
        post_strength = None if value is None else value >= strength_threshold
        reviews.append(
            RejectionReview(
                sample=sample,
                forward_return=value,
                post_strength=post_strength,
                threshold=round(strength_threshold, 6),
                missing=missing,
            )
        )
    return tuple(reviews)


def review_summary(reviews: Iterable[RejectionReview]) -> dict[str, Any]:
    """复盘摘要：样本数、可评估数、误杀率（事后走强的占比）。"""

    items = list(reviews)
    scored = [r for r in items if r.post_strength is not None]
    strengthened = [r for r in scored if r.post_strength]
    return {
        "samples": len(items),
        "scored": len(scored),
        "unscored": len(items) - len(scored),
        "post_strength": len(strengthened),
        "false_block_rate": (
            round(len(strengthened) / len(scored), 6) if scored else None
        ),
        "by_rule": rejection_breakdown(r.sample for r in items),
        "note": "false_block_rate 仅统计有事后收益的样本，缺失不摊薄",
    }
