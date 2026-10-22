"""分层晋级模型的五档样本池定义 (GitHub issue #27).

issue #27 要求把打板研究从"单样本池 + 单模型"升级为
"首板 / 1进2 / 2进3 / 高位板 / 反包" 五个**独立样本、独立阈值、独立校准**的分层
模型，并明确"不混合训练"。

分层口径完全建立在 #23 (``strategy/first_limit_alpha/label_engine.py`` /
``labeling.classify_board_type`` / ``classify_promotion_stage``) 已经产出的标签表
字段之上，不另起一套判定逻辑，避免两套分层互相漂移。

两维分解：``board_tier`` × ``entry_scene``
------------------------------------------
#23 的文档明确指出："``board_type`` 描述 T 日是几板，而 'N进N+1' 这类下注台阶由
``promotion_stage`` 表达，这样拆是为避免 issue 里'首板'与'1进2'两个分层名互相
覆盖（首板样本赌的就是 1进2）"。

所以分层模型天然是 **(梯队位置, 入场场景)** 的二元组：

======================  ===================  ========  ==========================
model tier               board_tier           scene     交易含义
======================  ===================  ========  ==========================
``first_board`` 首板      ``first_board``      A         T 日涨停排队打首板
``one_to_two`` 1进2       ``first_board``      B         T+1 竞价接力冲 2 板
``two_to_three`` 2进3     ``second_board``     B         T+1 竞价接力冲 3 板
``high_board`` 高位板     ``high_board``       B         >=3 板加速/分歧/断板
``reversal`` 反包         ``reversal``         B         炸板/跌停修复次日溢价
======================  ===================  ========  ==========================

同一 ``board_tier`` 在两个场景下产生两个**不同的模型样本**：#23 的场景 A
(``entry_*_A`` / ``ret_*_A``) 与场景 B (``entry_*_B`` / ``ret_*_B``) 的特征时点、
可成交性判定与标签列都不同，训练矩阵因此互不混用；:func:`assert_tiers_disjoint`
断言 (a) 每个 T 日行只落在一个 ``board_tier``，(b) 注册表内 (board_tier, scene)
组合唯一，(c) 同一场景下不同 model tier 的行集合两两不相交。

缺失处理纪律
------------
``limit_times`` 缺失（``None``）的样本**不猜测**归入任何梯队，落到
``board_tier == ""``（未分层），由 :func:`assign_board_tiers` 计入 ``unassigned``
统计而不是被静默并入某一档。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

import pandas as pd

# ---------------------------------------------------------------------------
# board_tier：T 日在连板梯队中的位置（与 #23 ``board_type`` 中文取值对齐）
# ---------------------------------------------------------------------------

BOARD_FIRST = "first_board"
BOARD_SECOND = "second_board"
BOARD_HIGH = "high_board"
BOARD_REVERSAL = "reversal"

BOARD_TIER_ORDER: tuple[str, ...] = (
    BOARD_FIRST,
    BOARD_SECOND,
    BOARD_HIGH,
    BOARD_REVERSAL,
)

#: #23 ``labeling.classify_board_type`` 里属于反包语境的取值。
REVERSAL_BOARD_TYPES: frozenset[str] = frozenset({"反包", "炸板修复", "地天板"})
#: 反包语境的 ``sample_mode``（#23 ``label_engine.MODE_REVERSAL``）。
REVERSAL_SAMPLE_MODES: frozenset[str] = frozenset({"reversal"})
FIRST_BOARD_LABEL = "首板"
SECOND_BOARD_LABELS: frozenset[str] = frozenset({"二连板", "2连板"})
HIGH_BOARD_LABELS: frozenset[str] = frozenset({"三连板", "高位板", "4连板", "5连板"})

#: #23 ``ExecutableLabelConfig.high_board_min_times`` 默认 3。
DEFAULT_HIGH_BOARD_MIN_TIMES = 3
CONTINUATION_MIN_TIMES = 2

SCENE_A = "A"  # T 日涨停排队（#23 entry_*_A / ret_*_A）
SCENE_B = "B"  # T+1 集合竞价接力（#23 entry_*_B / ret_*_B）

TIER_FIRST_BOARD = "first_board"
TIER_ONE_TO_TWO = "one_to_two"
TIER_TWO_TO_THREE = "two_to_three"
TIER_HIGH_BOARD = "high_board"
TIER_REVERSAL = "reversal"

TIER_ORDER: tuple[str, ...] = (
    TIER_FIRST_BOARD,
    TIER_ONE_TO_TWO,
    TIER_TWO_TO_THREE,
    TIER_HIGH_BOARD,
    TIER_REVERSAL,
)


class TierError(ValueError):
    """Raised when a tier definition or an assignment result is inconsistent."""


def _as_int(value: Any) -> int | None:
    """Numeric cast that never invents a 0 for a missing value."""

    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return int(out)


def is_reversal_context(row: Mapping[str, Any]) -> bool:
    """True when the row is 反包 / 炸板修复 / 地天板 per #23's own fields."""

    board_type = str(row.get("board_type") or "").strip()
    if board_type in REVERSAL_BOARD_TYPES:
        return True
    if str(row.get("sample_mode") or "").strip() in REVERSAL_SAMPLE_MODES:
        return True
    return board_type == "" and str(row.get("limit_shape") or "").strip() == "炸板未回封"


def board_tier_of(
    row: Mapping[str, Any], *, high_board_min_times: int | None = None
) -> str:
    """Return the ladder position of one #23 label row (``""`` = 未分层).

    Precedence is deliberate: 反包语境 first, because its ``limit_times`` is not
    comparable to a clean ladder step; then the ladder height buckets.
    """

    if is_reversal_context(row):
        return BOARD_REVERSAL
    board_type = str(row.get("board_type") or "").strip()
    times = _as_int(row.get("limit_times"))
    hbt = _as_int(high_board_min_times)
    high_min = DEFAULT_HIGH_BOARD_MIN_TIMES if hbt is None or hbt < 2 else hbt

    if board_type in HIGH_BOARD_LABELS or board_type == SECOND_BOARD_LABELS:
        # 中文名优先：#23 已经判过一遍，不再用 limit_times 覆盖它。
        return BOARD_HIGH if board_type in HIGH_BOARD_LABELS else BOARD_SECOND
    if times is None or times < 1:
        return ""
    if times == 1:
        return BOARD_FIRST if board_type in {FIRST_BOARD_LABEL, ""} else ""
    if times >= high_min:
        return BOARD_HIGH
    if times == CONTINUATION_MIN_TIMES:
        return BOARD_SECOND
    return ""


def assign_board_tiers(
    frame: pd.DataFrame,
    *,
    high_board_min_times: int | None = None,
    column: str = "board_tier",
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Attach a ``board_tier`` column; never drops rows, never guesses a tier.

    Returns ``(frame_with_column, summary)``.  The summary reports per-tier
    counts plus ``unassigned`` so a caller sees how much of the universe has no
    usable ladder information instead of silently training on it.
    """

    if column in frame.columns:
        raise TierError(f"column {column!r} already exists; drop it first")
    if "limit_times" not in frame.columns and "board_type" not in frame.columns:
        raise TierError(
            "label frame must carry at least one of 'limit_times' / 'board_type' "
            "from issue #23 to be tierable"
        )
    records = frame.to_dict("records")
    tiers = [
        board_tier_of(row, high_board_min_times=high_board_min_times)
        for row in records
    ]
    out = frame.copy()
    out[column] = tiers
    counts = {key: int(sum(1 for t in tiers if t == key)) for key in BOARD_TIER_ORDER}
    unassigned = int(sum(1 for t in tiers if not t))
    summary = {
        "n_rows": int(len(out)),
        "counts": counts,
        "unassigned": unassigned,
        "coverage_ratio": round((len(tiers) - unassigned) / max(1, len(tiers)), 6),
        "high_board_min_times": (
            DEFAULT_HIGH_BOARD_MIN_TIMES
            if high_board_min_times is None
            else int(high_board_min_times)
        ),
    }
    return out, summary


# ---------------------------------------------------------------------------
# model tier registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TierDef:
    """One tier's identity: which rows, which entry scene, which issue-bet."""

    key: str
    label_zh: str
    board_tier: str
    entry_scene: str
    bet_zh: str
    #: 该档最少需要多少个可标注样本才允许训练（防 tiny-sample 过拟合）。
    min_samples: int = 200

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label_zh": self.label_zh,
            "board_tier": self.board_tier,
            "entry_scene": self.entry_scene,
            "bet_zh": self.bet_zh,
            "min_samples": self.min_samples,
        }


TIER_REGISTRY: dict[str, TierDef] = {
    TIER_FIRST_BOARD: TierDef(
        key=TIER_FIRST_BOARD,
        label_zh="首板（T 日排队打首板）",
        board_tier=BOARD_FIRST,
        entry_scene=SCENE_A,
        bet_zh="首板后 1-3 日晋级 / 溢价 / 断板",
    ),
    TIER_ONE_TO_TWO: TierDef(
        key=TIER_ONE_TO_TWO,
        label_zh="1进2（次日竞价接力冲 2 板）",
        board_tier=BOARD_FIRST,
        entry_scene=SCENE_B,
        bet_zh="首板次日能否连板",
    ),
    TIER_TWO_TO_THREE: TierDef(
        key=TIER_TWO_TO_THREE,
        label_zh="2进3（3 板加速 / 分歧）",
        board_tier=BOARD_SECOND,
        entry_scene=SCENE_B,
        bet_zh="2进3 能否晋级 3 板",
        min_samples=120,
    ),
    TIER_HIGH_BOARD: TierDef(
        key=TIER_HIGH_BOARD,
        label_zh="高位板（>=3 板加速 / 分歧 / 断板）",
        board_tier=BOARD_HIGH,
        entry_scene=SCENE_B,
        bet_zh="高位板加速 / 分歧 / 断板",
        min_samples=120,
    ),
    TIER_REVERSAL: TierDef(
        key=TIER_REVERSAL,
        label_zh="反包（炸板 / 跌停修复次日溢价）",
        board_tier=BOARD_REVERSAL,
        entry_scene=SCENE_B,
        bet_zh="反包板次日溢价",
        min_samples=120,
    ),
}


def tier_def(tier: str) -> TierDef:
    def_ = TIER_REGISTRY.get(tier)
    if def_ is None:
        raise TierError(f"unknown tier {tier!r}; valid: {list(TIER_ORDER)}")
    return def_


def scene_of(tier: str) -> str:
    return tier_def(tier).entry_scene


def select_tier(
    frame: pd.DataFrame,
    tier: str,
    *,
    board_tier_column: str = "board_tier",
) -> pd.DataFrame:
    """Slice one model tier's rows (``board_tier`` must already be assigned)."""

    def_ = tier_def(tier)
    if board_tier_column not in frame.columns:
        raise TierError(
            f"frame has no {board_tier_column!r} column; call assign_board_tiers first"
        )
    mask = frame[board_tier_column].astype(str) == def_.board_tier
    return frame.loc[mask].reset_index(drop=True)


def assert_tiers_disjoint(
    frame: pd.DataFrame,
    *,
    board_tier_column: str = "board_tier",
    keys: Sequence[str] = ("symbol", "trade_date"),
) -> dict[str, Any]:
    """Verify the "不混合训练" claim and return the audit payload.

    Three independent checks:

    1. every row carries at most one ``board_tier`` (guaranteed by construction,
       re-checked so a caller-supplied frame cannot fake it);
    2. the registry's ``(board_tier, entry_scene)`` pairs are unique, so two
       model tiers can never be aliases of each other;
    3. within one entry scene, distinct model tiers select disjoint row sets.
    """

    if board_tier_column not in frame.columns:
        raise TierError(f"frame has no {board_tier_column!r} column")
    missing = [k for k in keys if k not in frame.columns]
    if missing:
        raise TierError(f"row keys {missing} missing; cannot audit tier disjointness")

    pairs = [(d.board_tier, d.entry_scene) for d in TIER_REGISTRY.values()]
    duplicates = sorted({p for p in pairs if pairs.count(p) > 1})
    if duplicates:
        raise TierError(f"tier registry has duplicate (board_tier, scene) {duplicates}")

    keyed = frame[list(keys) + [board_tier_column]].copy()
    for col in keys:
        keyed[col] = keyed[col].astype(str)
    if keyed.duplicated(list(keys)).any():
        n = int(keyed.duplicated(list(keys)).sum())
        raise TierError(
            f"label frame must be unique on {list(keys)} before tiering; {n} dup rows"
        )

    per_scene_rows: dict[str, dict[str, set[tuple[str, ...]]]] = {}
    per_scene: dict[str, dict[str, int]] = {}
    overlaps: list[dict[str, Any]] = []
    by_key = {
        tuple(str(rec[k]) for k in keys): str(rec[board_tier_column])
        for rec in keyed.to_dict("records")
    }
    for tier in TIER_ORDER:
        def_ = TIER_REGISTRY[tier]
        rows = {k for k, bt in by_key.items() if bt == def_.board_tier}
        bucket = per_scene_rows.setdefault(def_.entry_scene, {})
        counts = per_scene.setdefault(def_.entry_scene, {})
        for other, other_rows in bucket.items():
            shared = rows & other_rows
            if shared:
                overlaps.append(
                    {
                        "scene": def_.entry_scene,
                        "tiers": [other, tier],
                        "n_shared_rows": len(shared),
                        "examples": [list(s) for s in sorted(shared)[:3]],
                    }
                )
        bucket[tier] = rows
        counts[tier] = len(rows)
    if overlaps:
        raise TierError(f"tier pools overlap inside one entry scene: {overlaps[:2]}")

    counts = frame[board_tier_column].astype(str).value_counts().to_dict()
    return {
        "board_tier_counts": {str(k): int(v) for k, v in counts.items()},
        "unassigned": int(counts.get("", 0)),
        "rows_per_scene": {k: dict(v) for k, v in per_scene.items()},
        "registry_pairs": [list(p) for p in pairs],
        "disjoint": True,
    }


__all__ = [
    "BOARD_FIRST",
    "BOARD_HIGH",
    "BOARD_REVERSAL",
    "BOARD_SECOND",
    "BOARD_TIER_ORDER",
    "CONTINUATION_MIN_TIMES",
    "DEFAULT_HIGH_BOARD_MIN_TIMES",
    "FIRST_BOARD_LABEL",
    "HIGH_BOARD_LABELS",
    "REVERSAL_BOARD_TYPES",
    "REVERSAL_SAMPLE_MODES",
    "SCENE_A",
    "SCENE_B",
    "SECOND_BOARD_LABELS",
    "TIER_FIRST_BOARD",
    "TIER_HIGH_BOARD",
    "TIER_ONE_TO_TWO",
    "TIER_ORDER",
    "TIER_REGISTRY",
    "TIER_REVERSAL",
    "TIER_TWO_TO_THREE",
    "TierDef",
    "TierError",
    "assign_board_tiers",
    "assert_tiers_disjoint",
    "board_tier_of",
    "is_reversal_context",
    "scene_of",
    "select_tier",
    "tier_def",
]
