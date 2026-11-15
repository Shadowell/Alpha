"""多任务标签头与可成交性口径 (GitHub issue #27)。

issue #27 要求每个分层模型输出

* ``p_continue``  继续涨停概率
* ``p_strong_3d`` 3 日强势概率
* ``p_break``     断板 / 大面风险
* ``p_next_auction_premium`` 次日竞价溢价（回归或分桶）
* 最终排序分 ``limit_up_alpha_score`` 并给出置信度 / 样本量

本模块**不重新计算标签**：全部字段直接取自 #23
(:mod:`strategy.first_limit_alpha.label_engine` / :mod:`labeling`)，只负责
"按分层 + 入场场景挑出正确的那一列、把不可成交样本标成缺失而不是 0"。

纪律（对应 AGENTS.md 的严格 PIT / 不伪造成交）
--------------------------------------------
* 不可成交样本（#23 ``entry_unfilled_A/B = 1``）对应场景的收益标签在 #23 里已经是
  ``None``；这里保持 ``NaN``，并写入 ``y_<head>_is_missing`` 诊断列，**绝不
  fillna(0)**。
* ``p_break`` 的"风险旗标或"只在至少一个旗标可用时判定；全部缺失 → ``NaN``。
* ``can_sell`` 只有当 #23 真的给出了出场日涨停/跌停标记时才声明；否则保持缺列，
  让 #36 的 ``validate_panel`` 把 ``executability_declared`` 判成 ``False``，
  从而 :class:`strategy.model_validation.gate.GateConfig` 拒绝 ``keep``。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

from .tiers import SCENE_A, SCENE_B, TierDef, tier_def

TARGET_PREFIX = "y_"
MISSING_SUFFIX = "_is_missing"

#: #23 ``labeling.LABEL_STATUS_*`` 等价物（不 import 私有常量，避免耦合）。
_LABEL_STATUS_UNFILLED = "unfilled"
_LABEL_STATUS_INSUFFICIENT = "insufficient_horizon"
_LABEL_STATUS_MISSING_FUTURE = "missing_future_data"
_LABEL_STATUS_UNKNOWN_ENTRY = "unknown_entry"

BINARY = "binary"
REGRESSION = "regression"


class TargetError(ValueError):
    """Raised when the label frame cannot supply a declared target head."""


@dataclass(frozen=True, slots=True)
class TargetHead:
    """Declaration of one multi-task head: where the label comes from."""

    key: str
    kind: str
    label_zh: str
    #: 直接取用的 #23 列（与 ``scene_columns`` 二选一）。
    source_column: str = ""
    #: 按入场场景切换的 #23 列，例如收益类 ``{A: "fwd_ret_3d", B: "fwd_ret_3d"}``。
    scene_columns: Mapping[str, str] | None = None
    #: ``p_break`` 这类"多个风险旗标取或"的头。
    any_of_columns: Sequence[str] = ()
    #: 分桶回归头的阈值（> 阈值记 1）。
    threshold: float | None = None
    required_tiers: Sequence[str] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "kind": self.kind,
            "label_zh": self.label_zh,
            "source_column": self.source_column,
            "scene_columns": dict(self.scene_columns or {}),
            "any_of_columns": list(self.any_of_columns),
            "threshold": self.threshold,
            "required_tiers": list(self.required_tiers),
        }


#: issue #27 的四张概率图 + 台阶专属晋级头。
TARGET_HEADS: dict[str, TargetHead] = {
    "p_continue": TargetHead(
        key="p_continue",
        kind=BINARY,
        label_zh="继续涨停概率（T+1..T+continuation_horizon）",
        source_column="is_continuation",
    ),
    "p_strong_3d": TargetHead(
        key="p_strong_3d",
        kind=BINARY,
        label_zh="3 日强势概率（MFE >= strong_threshold）",
        source_column="is_strong_3d",
    ),
    "p_break": TargetHead(
        key="p_break",
        kind=BINARY,
        label_zh="断板 / 大面风险",
        any_of_columns=(
            "is_break_streak_next",
            "is_big_loss",
            "is_next_limit_down",
        ),
    ),
    "p_next_auction_premium": TargetHead(
        key="p_next_auction_premium",
        kind=REGRESSION,
        label_zh="次日竞价溢价（相对 T 日收盘）",
        source_column="next_auction_premium",
    ),
    "next_auction_premium_bucket": TargetHead(
        key="next_auction_premium_bucket",
        kind=BINARY,
        label_zh="次日竞价溢价分桶（>2% 记 1）",
        source_column="next_auction_premium",
        threshold=0.02,
    ),
    "p_promotion_step": TargetHead(
        key="p_promotion_step",
        kind=BINARY,
        label_zh="本档下注台阶是否晋级（is_1_to_2 / is_2_to_3 / is_3_to_4）",
        scene_columns={
            "first_board": "is_1_to_2",
            "second_board": "is_2_to_3",
            "high_board": "is_3_to_4",
        },
    ),
}

#: 用于 #36 ``Panel`` 的毛收益列（未扣费；#36 portfolio 自己扣成本，避免双扣）。
GROSS_RETURN_BY_SCENE: dict[str, str] = {
    SCENE_A: "fwd_ret_3d",
    SCENE_B: "fwd_ret_3d",
}

EXECUTABILITY_COLUMNS: dict[str, dict[str, str]] = {
    SCENE_A: {
        "unfilled": "entry_unfilled_A",
        "status": "entry_status_A",
        "reason": "entry_unfilled_reason_A",
        "price": "entry_price_A",
    },
    SCENE_B: {
        "unfilled": "entry_unfilled_B",
        "status": "entry_status_B",
        "reason": "entry_unfilled_reason_B",
        "price": "entry_price_B",
    },
}

#: 出场日跌停 → 卖不出（#23 的 ``future_limit_flag_d{h}`` 口径）。
EXIT_DOWN_FLAGS: dict[int, str] = {
    1: "future_limit_flag_d1",
    2: "future_limit_flag_d2",
    3: "future_limit_flag_d3",
    4: "future_limit_flag_d4",
    5: "future_limit_flag_d5",
}


def target_column(head: TargetHead, *, board_tier: str | None = None) -> str:
    """Resolve which #23 column feeds this head for the given ladder position."""

    if head.scene_columns:
        if not board_tier:
            raise TargetError(
                f"head {head.key!r} needs board_tier to pick a source column"
            )
        column = str(head.scene_columns.get(board_tier) or "")
        if not column:
            raise TargetError(
                f"head {head.key!r} has no source column for board_tier {board_tier!r}"
            )
        return column
    return head.source_column


def _to_float(value: Any) -> float:
    """``None`` / ``NaN`` / 空串 → ``nan``；**不会**把缺失当成 0。"""

    if value is None:
        return math.nan
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return math.nan
        value = text
    try:
        out = float(value)
    except (TypeError, ValueError):
        return math.nan
    return out


def _binary(series: pd.Series, *, threshold: float | None) -> np.ndarray:
    values = series.to_numpy(dtype=float, na_value=math.nan, copy=True) if hasattr(
        series, "to_numpy"
    ) else np.asarray([_to_float(v) for v in series], dtype=float)
    values = np.where(np.isfinite(values), values, math.nan)
    if threshold is not None:
        out = np.where(np.isfinite(values), (values > threshold).astype(float), math.nan)
        return out
    return np.where(
        np.isfinite(values), np.where(values > 0.5, 1.0, 0.0), math.nan
    )


def _any_of(frame: pd.DataFrame, columns: Sequence[str]) -> np.ndarray:
    """Risk-flag OR: 1 if any available flag is 1, 0 if all available are 0, else NaN."""

    n = len(frame)
    if not columns:
        return np.full(n, math.nan)
    present = [c for c in columns if c in frame.columns]
    if not present:
        return np.full(n, math.nan)
    matrix = np.full((n, len(present)), math.nan)
    for j, col in enumerate(present):
        series = frame[col]
        matrix[:, j] = np.asarray(
            [_to_float(v) for v in series.to_list()], dtype=float
        )
    ones = (matrix > 0.5) & np.isfinite(matrix)
    known = np.isfinite(matrix)
    any_one = ones.any(axis=1)
    all_known_zero = known.any(axis=1) & ~any_one
    out = np.full(n, math.nan)
    out[any_one] = 1.0
    out[all_known_zero] = 0.0
    return out


def build_targets(
    frame: pd.DataFrame,
    tier: str,
    *,
    heads: Iterable[str] | None = None,
    board_tier_column: str = "board_tier",
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Materialise ``y_<head>`` (+ ``y_<head>_is_missing``) columns for one tier.

    Returns ``(targets_frame, stats)``.  ``stats`` reports per-head sample
    counts / positive rates / missing counts — this is exactly issue #27 的
    "每个分层模型有独立样本量、正样本率" 验收项。
    """

    def_: TierDef = tier_def(tier)
    wanted = list(heads) if heads else list(TARGET_HEADS)
    unknown = [h for h in wanted if h not in TARGET_HEADS]
    if unknown:
        raise TargetError(f"unknown target heads: {unknown}")

    board_tier = def_.board_tier
    if board_tier_column in frame.columns and len(frame):
        distinct = {str(v) for v in frame[board_tier_column].unique()}
        if distinct and distinct != {board_tier}:
            raise TargetError(
                f"tier {tier!r} expects board_tier {board_tier!r}, frame has {sorted(distinct)}"
            )

    out = pd.DataFrame(index=range(len(frame)))
    stats: dict[str, Any] = {}
    for key in wanted:
        head = TARGET_HEADS[key]
        if head.key == "p_promotion_step":
            column = target_column(head, board_tier=board_tier)
        else:
            column = head.source_column
        if head.any_of_columns:
            values = _any_of(frame, head.any_of_columns)
            source = "|".join(head.any_of_columns)
        elif column in frame.columns:
            series = frame[column]
            values = _binary(
                series,
                threshold=head.threshold if head.kind == BINARY else None,
            )
            if head.kind == REGRESSION:
                values = np.asarray(
                    [_to_float(v) for v in series.to_list()], dtype=float
                )
            source = column
        else:
            values = np.full(len(frame), math.nan)
            source = f"{column}::<absent>"
        out[f"{TARGET_PREFIX}{key}"] = values
        missing_col = f"{TARGET_PREFIX}{key}{MISSING_SUFFIX}"
        out[missing_col] = ~np.isfinite(values)
        finite = values[np.isfinite(values)]
        positive_rate = None
        if head.kind == BINARY and finite.size:
            positive_rate = float((finite > 0.5).mean())
        stats[key] = {
            "kind": head.kind,
            "label_zh": head.label_zh,
            "source_column": source,
            "source_available": column in frame.columns or bool(head.any_of_columns),
            "n": int(finite.size),
            "n_missing": int(len(values) - finite.size),
            "missing_ratio": round(
                (len(values) - finite.size) / max(1, len(values)), 6
            ),
            "positive_rate": None if positive_rate is None else round(positive_rate, 6),
            "mean": None if head.kind != REGRESSION or not finite.size else round(
                float(finite.mean()), 8
            ),
        }
    return out, stats


def executability_flags(
    frame: pd.DataFrame,
    tier: str,
    *,
    horizon: int = 3,
) -> dict[str, pd.Series]:
    """Derive #36 ``can_buy`` / ``can_sell`` from #23's own executability fields.

    ``can_buy``: the tier's entry scene must be fillable (``entry_unfilled_*!=1``
    and ``entry_status_* == filled`` when that column exists)。
    ``can_sell``: only declared when #23 shipped an exit-day limit flag; a
    position that cannot be exited is excluded rather than counted as a fill.
    """

    def_ = tier_def(tier)
    spec = EXECUTABILITY_COLUMNS[def_.entry_scene]
    n = len(frame)
    result: dict[str, pd.Series] = {}

    unfilled_col = spec["unfilled"]
    if unfilled_col in frame.columns:
        unfilled = np.asarray(
            [_to_float(v) for v in frame[unfilled_col].to_list()], dtype=float
        )
        can_buy = ~(unfilled > 0.5)  # NaN -> False-ish; handled below
        unknown = ~np.isfinite(unfilled)
        can_buy = can_buy & ~unknown
        result["can_buy"] = pd.Series(can_buy, index=frame.index, name="can_buy")
        result["can_buy_known"] = pd.Series(
            ~unknown, index=frame.index, name="can_buy_known"
        )
        reason = (
            frame[spec["reason"]]
            if spec["reason"] in frame.columns
            else pd.Series(["" if ok else "entry_unfilled" for ok in can_buy])
        )
        result["executability_reason"] = pd.Series(
            ["" if bool(ok) else str(r or "entry_unfilled") for ok, r in zip(can_buy, reason)]
        )
    else:
        # No executability column at all -> do NOT fabricate "everything fills".
        result["can_buy"] = pd.Series(
            np.ones(n, dtype=bool), index=frame.index, name="can_buy"
        )
        result["can_buy_known"] = pd.Series(
            np.zeros(n, dtype=bool), index=frame.index, name="can_buy_known"
        )
        result["executability_reason"] = pd.Series(
            ["entry_unfilled_column_absent"] * n, name="executability_reason"
        )

    exit_col = EXIT_DOWN_FLAGS.get(int(horizon)) or EXIT_DOWN_FLAGS.get(3)
    if exit_col and exit_col in frame.columns:
        flags = frame[exit_col].astype("string").fillna("")
        down = np.asarray([str(v).strip().upper() == "D" for v in flags.tolist()])
        result["can_sell"] = pd.Series(~down, index=frame.index, name="can_sell")
        result["can_sell_source"] = exit_col
    else:
        result["can_sell_source"] = ""
    return result


def executability_declared(flags: Mapping[str, Any]) -> bool:
    """True only when both entry and exit tradability come from #23."""

    known = flags.get("can_buy_known")
    buy_ok = bool(known is not None and bool(np.asarray(known, dtype=bool).any()))
    return buy_ok and bool(str(flags.get("can_sell_source") or ""))


# ---------------------------------------------------------------------------
# final ranking score
# ---------------------------------------------------------------------------

DEFAULT_ALPHA_WEIGHTS: dict[str, float] = {
    "p_promotion_step": 0.35,
    "p_continue": 0.25,
    "p_strong_3d": 0.15,
    "next_auction_premium_bucket": 0.15,
    "p_break": -0.10,
}

#: 缺失头的保守填充值：概率用中性 0.5，惩罚头用 0（不引入虚假扣分）。
NEUTRAL_PROBABILITY = 0.5


def combine_heads(
    probabilities: Mapping[str, Sequence[float] | np.ndarray],
    *,
    weights: Mapping[str, float] | None = None,
    available_heads: Sequence[str] | None = None,
) -> dict[str, np.ndarray | float]:
    """Blend per-head probabilities into ``limit_up_alpha_score``.

    Only heads present in ``available_heads`` contribute, and the weights are
    renormalised over those heads — so a tier whose ``p_break`` column is all
    missing degrades gracefully instead of silently getting a free 0.5.

    Returns ``{"score": array, "confidence": array, "weight_sum": float}``;
    ``confidence`` is the retained weight fraction (issue #27 要求"给出置信度"),
    and ``sample_size`` is added by the caller from the training fold.
    """

    active = dict(weights or DEFAULT_ALPHA_WEIGHTS)
    if available_heads is not None:
        allow = set(available_heads)
        active = {k: v for k, v in active.items() if k in allow}
    if not active:
        raise TargetError("combine_heads: no usable heads after filtering")

    arrays: dict[str, np.ndarray] = {}
    for key in active:
        if key not in probabilities:
            raise TargetError(f"combine_heads: missing probability array for {key!r}")
        arr = np.asarray(probabilities[key], dtype=float)
        arrays[key] = arr
    sizes = {int(a.size) for a in arrays.values()}
    if len(sizes) > 1:
        raise TargetError(f"combine_heads: head arrays differ in length: {sizes}")
    n = int(next(iter(sizes))) if sizes else 0

    total_weight = sum(abs(v) for v in active.values())
    score = np.zeros(n, dtype=float)
    used = np.zeros(n, dtype=float)
    for key, weight in active.items():
        arr = arrays[key]
        good = np.isfinite(arr)
        contribution = np.where(good, arr, NEUTRAL_PROBABILITY if weight > 0 else 0.0)
        score += weight * contribution
        used += weight * good.astype(float)
    normalised = score / max(total_weight, 1e-12)
    confidence = np.abs(used) / max(total_weight, 1e-12)
    return {
        "score": np.clip(normalised, 0.0, 1.0) if total_weight else normalised,
        "confidence": np.clip(confidence, 0.0, 1.0),
        "weight_sum": float(total_weight),
    }


def head_keys() -> tuple[str, ...]:
    return tuple(TARGET_HEADS)


def binary_head_keys() -> tuple[str, ...]:
    return tuple(k for k, h in TARGET_HEADS.items() if h.kind == BINARY)


def regression_head_keys() -> tuple[str, ...]:
    return tuple(k for k, h in TARGET_HEADS.items() if h.kind == REGRESSION)


__all__ = [
    "BINARY",
    "DEFAULT_ALPHA_WEIGHTS",
    "EXECUTABILITY_COLUMNS",
    "EXIT_DOWN_FLAGS",
    "GROSS_RETURN_BY_SCENE",
    "MISSING_SUFFIX",
    "NEUTRAL_PROBABILITY",
    "REGRESSION",
    "TARGET_HEADS",
    "TARGET_PREFIX",
    "TargetError",
    "TargetHead",
    "binary_head_keys",
    "build_targets",
    "combine_heads",
    "executability_declared",
    "executability_flags",
    "head_keys",
    "regression_head_keys",
    "target_column",
]
