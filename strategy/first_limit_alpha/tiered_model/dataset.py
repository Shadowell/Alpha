"""分层模型数据集装配 + #36 ``Panel`` 契约桥 (GitHub issue #27)。

一次构建产出 ``TierDataset``：

* 该档**独立样本池**（由 :mod:`.tiers` 的 ``(board_tier, entry_scene)`` 决定）；
* **多任务标签**（:mod:`.targets`，缺失保持 ``NaN`` + ``*_is_missing``）；
* **PIT 因子矩阵**（:mod:`.pit`，as-of 回看，绝不引入 T 之后的信息）；
* 一个可直接喂给 #36 :func:`~strategy.model_validation.runner.run_validation`
  的 :class:`~strategy.model_validation.contracts.Panel`，从而复用它的
  walk-forward / embargo / purge / 指标 / 门槛判定。

口径说明
--------
``gross_return_column`` 默认 ``"fwd_ret_3d"``（#23 的未扣费前瞻收益）。
**不要**改 ``ret_3d_A`` / ``ret_3d_B``：#23 文档写明它们已经"含手续费"，而 #36
的 portfolio 会再扣一次成本，选它们会造成双重扣费、把绩效算差。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Sequence

import numpy as np
import pandas as pd

from strategy.model_validation.contracts import ColumnNames, Panel

from .pit import PitAudit, attach_factors, select_factor_features
from .targets import (
    GROSS_RETURN_BY_SCENE,
    TARGET_PREFIX,
    build_targets,
    executability_declared,
    executability_flags,
)
from .tiers import (
    TIER_ORDER,
    TierDef,
    assert_tiers_disjoint,
    assign_board_tiers,
    scene_of,
    select_tier,
    tier_def,
)

#: 因子列命名空间：#23 标签与 #24 因子共用 ``open_times`` / ``limit_times`` /
#: ``fd_amount`` / ``float_mv`` 等列名，不加前缀会在 concat 时静默覆盖。
FACTOR_PREFIX = "mx_"

DEFAULT_FEATURES: tuple[str, ...] = (
    "fd_ratio_float_mv",
    "first_seal_minute",
    "seal_duration_min",
    "open_times",
    "on_board_amount_ratio",
    "seal_quality_proxy",
    "limit_up_count_250d",
    "first_board_next_day_premium_avg",
    "continuation_success_rate",
    "broken_board_rate",
    "limit_up_freq_250d",
    "up_stat_limit_density",
)

#: 每档的"主标签头"：#36 Panel 的 label 列 + 门槛规则读的那一个概率。
PRIMARY_HEAD_BY_TIER: dict[str, str] = {
    "first_board": "p_promotion_step",
    "one_to_two": "p_promotion_step",
    "two_to_three": "p_promotion_step",
    "high_board": "p_promotion_step",
    "reversal": "next_auction_premium_bucket",
}


@dataclass(slots=True)
class DatasetConfig:
    tier: str
    features: tuple[str, ...] = DEFAULT_FEATURES
    heads: tuple[str, ...] = ()
    primary_head: str = ""
    high_board_min_times: int | None = None
    exit_horizon: int = 3
    gross_return_column: str = ""
    min_samples: int | None = None

    def resolved_primary_head(self) -> str:
        if self.primary_head:
            return self.primary_head
        return PRIMARY_HEAD_BY_TIER[self.tier]

    def as_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "n_features": len(self.features),
            "features": list(self.features),
            "heads": list(self.heads),
            "primary_head": self.resolved_primary_head(),
            "high_board_min_times": self.high_board_min_times,
            "exit_horizon": self.exit_horizon,
            "gross_return_column": self.gross_return_column or "fwd_ret_3d",
            "min_samples": self.min_samples,
        }


@dataclass(slots=True)
class TierDataset:
    """One tier's ready-to-train matrix + its #36 panel."""

    tier: str
    def_: TierDef
    frame: pd.DataFrame
    features: tuple[str, ...]
    heads: tuple[str, ...]
    primary_head: str
    stats: dict[str, Any] = field(default_factory=dict)
    panel: Panel | None = None

    @property
    def n_rows(self) -> int:
        return int(len(self.frame))

    @property
    def n_events(self) -> int:
        return int(self.frame["trade_date"].nunique()) if len(self.frame) else 0

    def feature_matrix(self) -> pd.DataFrame:
        return self.frame.loc[:, list(self.features)]

    def target_column(self, head: str) -> pd.Series:
        column = f"{TARGET_PREFIX}{head}"
        if column not in self.frame.columns:
            raise KeyError(f"head {head!r} not built for tier {self.tier!r}")
        return self.frame[column]

    def summary(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "tier_zh": self.def_.label_zh,
            "entry_scene": self.def_.entry_scene,
            "bet_zh": self.def_.bet_zh,
            "primary_head": self.primary_head,
            "n_rows": self.n_rows,
            "n_events": self.n_events,
            **self.stats,
        }


def tier_column_name(head: str) -> str:
    return f"{TARGET_PREFIX}{head}"


def build_tier_dataset(
    label_frame: pd.DataFrame,
    factor_frame: pd.DataFrame,
    config: DatasetConfig,
    *,
    board_tier_column: str = "board_tier",
    with_panel: bool = True,
) -> TierDataset:
    """Assemble one tier's dataset; raises loudly instead of training on junk."""

    def_ = tier_def(config.tier)
    required = {"symbol", "trade_date"}
    missing = sorted(required - set(label_frame.columns))
    if missing:
        raise ValueError(f"label frame missing key columns: {missing}")

    working = label_frame
    if board_tier_column not in working.columns:
        working, _ = assign_board_tiers(
            working,
            high_board_min_times=config.high_board_min_times,
            column=board_tier_column,
        )
    rows = select_tier(working, config.tier, board_tier_column=board_tier_column)
    if rows.empty:
        raise ValueError(
            f"tier {config.tier!r} has zero rows in this label frame "
            f"(board_tier={def_.board_tier!r}); 分层样本池为空，禁止降级混合训练"
        )

    features = tuple(config.features)
    if not features:
        features = select_factor_features(
            factor_frame, candidate_columns=[c for c in factor_frame.columns]
        )
    heads = tuple(config.heads) or _default_heads(config.tier)
    primary = config.resolved_primary_head()
    if primary not in heads:
        heads = (primary, *heads)

    frames: list[pd.DataFrame] = []
    audit_chunks: list[PitAudit] = []
    materialised = tuple(f"{FACTOR_PREFIX}{f}" for f in features)
    if len(features):
        assembled, audit = attach_factors(
            rows,
            factor_frame,
            features=features,
            label_date="trade_date",
            factor_date="as_of_date",
            prefix=FACTOR_PREFIX,
        )
        frames.append(assembled)
        audit_chunks.append(audit)
    else:
        frames.append(rows.copy())
        audit_chunks.append(PitAudit(n_label_rows=int(len(rows))))
    targets, target_stats = build_targets(rows, config.tier, heads=heads)
    frames.append(targets)
    frame = pd.concat(frames, axis=1)
    duplicated = [c for c in frame.columns if list(frame.columns).count(c) > 1]
    if duplicated:
        raise ValueError(
            f"dataset assembly produced duplicate columns {duplicated[:6]}: "
            "因子列名与 #23 标签列冲突，应通过 FACTOR_PREFIX 命名空间隔离"
        )

    min_samples = (
        config.min_samples
        if config.min_samples is not None
        else max(def_.min_samples, 1)
    )
    usable = _usable_rows(frame, primary, config.tier, exit_horizon=config.exit_horizon)
    stats: dict[str, Any] = {
        "target_stats": target_stats,
        "pit": audit_chunks[0].as_dict(),
        "primary_head": primary,
        "n_usable_for_primary": int(usable),
        "min_samples": int(min_samples),
        "enough_samples": bool(usable >= min_samples),
        "heads": list(heads),
        "feature_columns": list(materialised),
    }
    stats["positive_rate_primary"] = _positive_rate(frame, primary)
    stats["baseline"] = _baseline_rate(label_frame, primary, config.tier)

    panel: Panel | None = None
    if with_panel:
        panel = to_validation_panel(
            frame,
            config.tier,
            features=materialised,
            primary_head=primary,
            exit_horizon=config.exit_horizon,
            gross_return_column=config.gross_return_column,
        )
    return TierDataset(
        tier=config.tier,
        def_=def_,
        frame=frame,
        features=materialised,
        heads=heads,
        primary_head=primary,
        stats=stats,
        panel=panel,
    )


def _default_heads(tier: str) -> tuple[str, ...]:
    if tier == "reversal":
        return (
            "next_auction_premium_bucket",
            "p_next_auction_premium",
            "p_continue",
            "p_break",
        )
    return (
        "p_promotion_step",
        "p_continue",
        "p_strong_3d",
        "p_break",
        "next_auction_premium_bucket",
        "p_next_auction_premium",
    )


def _usable_rows(
    frame: pd.DataFrame, primary: str, tier: str, *, exit_horizon: int = 3
) -> int:
    """Rows that can actually train the primary head: label present **and** the
    tier's entry scene is declared executable by #23."""

    column = tier_column_name(primary)
    if column not in frame.columns:
        return 0
    label_ok = pd.to_numeric(frame[column], errors="coerce").notna().to_numpy(dtype=bool)
    flags = executability_flags(frame, tier, horizon=exit_horizon)
    known = flags.get("can_buy_known")
    if known is None:
        return int(label_ok.sum())
    buyable = np.asarray(known, dtype=bool)
    return int((label_ok & buyable).sum())


def _positive_rate(frame: pd.DataFrame, head: str) -> float | None:
    column = tier_column_name(head)
    if column not in frame.columns:
        return None
    values = pd.to_numeric(frame[column], errors="coerce").dropna()
    if values.empty:
        return None
    return round(float((values > 0.5).mean()), 6)


def _baseline_rate(label_frame: pd.DataFrame, head: str, tier: str) -> dict[str, Any]:
    """随机 / 全市场基线正样本率 —— issue #27 的"基线对比"验收项。

    全市场口径 = 同一标签帧里**所有**涨停样本（不分层）在同一 head 上的正样本率；
    随机口径 = 0.5 之外的先验，即该 head 在全池的边际分布。
    """

    from .tiers import board_tier_of

    column = {
        "p_promotion_step": ("is_1_to_2", "is_2_to_3", "is_3_to_4"),
        "p_continue": ("is_continuation",),
        "p_strong_3d": ("is_strong_3d",),
        "p_break": ("is_break_streak_next", "is_big_loss"),
        "next_auction_premium_bucket": ("next_auction_premium",),
    }.get(head, ())
    if not column:
        return {"available": False, "reason": f"no raw source mapping for {head!r}"}
    pool = label_frame
    values = []
    for name in column:
        if name not in pool.columns:
            continue
        series = pd.to_numeric(pool[name], errors="coerce")
        if name == "next_auction_premium":
            values.extend((series > 0.02).dropna().to_list())
        else:
            values.extend(series.dropna().to_list())
    if not values:
        return {"available": False, "reason": "raw source columns absent",
                "tier": tier}
    arr = [float(v) for v in values if isinstance(v, (int, float)) and not math.isnan(float(v))]
    if not arr:
        return {"available": False, "reason": "all raw values missing", "tier": tier}
    positive = float(np.mean([1.0 if v > 0.5 else 0.0 for v in arr]))
    return {
        "available": True,
        "pool_positive_rate": round(positive, 6),
        "pool_n": len(arr),
        "random_baseline_rate": 0.5,
        "tier": tier,
    }


def to_validation_panel(
    frame: pd.DataFrame,
    tier: str,
    *,
    features: Sequence[str],
    primary_head: str,
    exit_horizon: int = 3,
    gross_return_column: str = "",
) -> Panel:
    """Wrap a tier dataset frame into the #36 ``Panel`` contract.

    ``can_sell`` is only materialised when #23 shipped an exit-day limit flag;
    otherwise the column stays absent and #36 marks the run
    ``executability_source="assumed"``, which in turn makes
    :class:`strategy.model_validation.gate.GateConfig` refuse a ``keep`` verdict.
    """

    def_ = tier_def(tier)
    scene = scene_of(tier)
    flags = executability_flags(frame, tier, horizon=exit_horizon)
    payload = pd.DataFrame(
        {
            "symbol": frame["symbol"].astype(str).to_numpy(),
            "event_date": frame["trade_date"].astype(str).to_numpy(),
            "label": pd.to_numeric(
                frame[tier_column_name(primary_head)], errors="coerce"
            ).to_numpy(dtype=float),
        }
    )
    gross_column = gross_return_column or GROSS_RETURN_BY_SCENE[scene]
    if gross_column in frame.columns:
        payload["outcome_return"] = pd.to_numeric(
            frame[gross_column], errors="coerce"
        ).to_numpy(dtype=float)
    if "next_auction_premium" in frame.columns:
        payload["next_day_premium"] = pd.to_numeric(
            frame["next_auction_premium"], errors="coerce"
        ).to_numpy(dtype=float)
    if "label_horizon_days" in frame.columns:
        payload["label_horizon"] = pd.to_numeric(
            frame["label_horizon_days"], errors="coerce"
        ).to_numpy(dtype=float)
    payload["can_buy"] = flags["can_buy"].to_numpy(dtype=bool)
    can_sell = flags.get("can_sell")
    if can_sell is not None:
        payload["can_sell"] = can_sell.to_numpy(dtype=bool)
    payload["executability_reason"] = flags["executability_reason"].to_numpy()
    for name in features:
        payload[name] = pd.to_numeric(frame[name], errors="coerce").to_numpy(dtype=float)

    columns = ColumnNames(
        symbol="symbol",
        event_date="event_date",
        label="label",
        gross_return="outcome_return",
        next_day_premium="next_day_premium",
        can_buy="can_buy",
        can_sell="can_sell",
        executability_reason="executability_reason",
        label_horizon="label_horizon",
    )
    panel = Panel(
        frame=payload,
        features=tuple(features),
        columns=columns,
    )
    panel.executability_declared = bool(
        executability_declared(flags) and can_sell is not None
    )
    del def_
    return panel


def ensure_board_tiers(
    label_frame: pd.DataFrame,
    *,
    high_board_min_times: int | None = None,
    column: str = "board_tier",
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Assign ``column`` unless the caller already did (idempotent)."""

    if column in label_frame.columns:
        counts = (
            label_frame[column].astype(str).value_counts().to_dict()
        )
        return label_frame, {
            "n_rows": int(len(label_frame)),
            "counts": {str(k): int(v) for k, v in counts.items()},
            "unassigned": int(counts.get("", 0)),
            "pre_assigned": True,
        }
    return assign_board_tiers(
        label_frame, high_board_min_times=high_board_min_times, column=column
    )


def build_all_tier_datasets(
    label_frame: pd.DataFrame,
    factor_frame: pd.DataFrame,
    *,
    features: Sequence[str] | None = None,
    tiers: Sequence[str] = TIER_ORDER,
    high_board_min_times: int | None = None,
    exit_horizon: int = 3,
) -> dict[str, TierDataset]:
    """Build every requested tier, skipping (with a reason) the empty ones."""

    assigned, audit = ensure_board_tiers(
        label_frame, high_board_min_times=high_board_min_times
    )
    disjoint = assert_tiers_disjoint(assigned)
    out: dict[str, TierDataset] = {}
    skipped: dict[str, str] = {}
    for tier in tiers:
        cfg = DatasetConfig(
            tier=tier,
            features=tuple(features) if features else DEFAULT_FEATURES,
            high_board_min_times=high_board_min_times,
            exit_horizon=exit_horizon,
        )
        try:
            out[tier] = build_tier_dataset(assigned, factor_frame, cfg)
        except ValueError as exc:
            skipped[tier] = str(exc)
    for dataset in out.values():
        dataset.stats["board_tier_audit"] = audit
        dataset.stats["tier_disjoint"] = disjoint
    if skipped:
        for dataset in out.values():
            dataset.stats["skipped_tiers"] = dict(skipped)
    return out


__all__ = [
    "DEFAULT_FEATURES",
    "PRIMARY_HEAD_BY_TIER",
    "DatasetConfig",
    "TierDataset",
    "build_all_tier_datasets",
    "build_tier_dataset",
    "tier_column_name",
    "to_validation_panel",
]
