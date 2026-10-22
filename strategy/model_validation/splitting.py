"""Leakage-proof time splitting: walk-forward + embargo + purging (issue #36).

Three independent guards, all unit-tested:

1. **Walk-forward** - the train block strictly precedes its out-of-sample test
   block and windows only advance in time.  Random splits are impossible by
   construction: folds are generated from a sorted calendar of event dates.
2. **Embargo** - ``guard`` trading dates immediately before a test block are
   withheld from train *and* valid, so nothing from a period that is already
   "inside" the OOS window can leak into fitting.
3. **Purging** - per-sample: a training row whose *label window*
   ``[t+1, t + label_horizon]`` reaches into the test block is dropped.  Needed
   because #23 emits multi-horizon labels (2d / 3d / 5d), so the naive
   "one day before the test start" cut-off still leaks.

The effective guard band is ``max(cfg.embargo, label_horizon)`` - the label
horizon is a floor, never a ceiling.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

import numpy as np
import pandas as pd

from .contracts import Panel


class InsufficientDataError(ValueError):
    """Raised when the panel cannot produce the requested number of folds."""


class EmbargoViolation(RuntimeError):
    """Raised when a split leaks future information into the training side."""


def _as_sorted_dates(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({str(v) for v in values}))


@dataclass(frozen=True, slots=True)
class WalkForwardConfig:
    """Window geometry expressed in *trading dates*, not rows."""

    test_size: int = 6
    train_size: int | None = 24  # None => expanding history
    valid_size: int = 0
    embargo: int = 1  # extra guard band on top of the label horizon
    min_train_dates: int = 4
    min_test_dates: int = 2
    max_folds: int = 24

    def __post_init__(self) -> None:
        if self.test_size < 1:
            raise ValueError("test_size must be >= 1")
        if self.valid_size < 0 or self.embargo < 0:
            raise ValueError("valid_size and embargo must be >= 0")
        if self.min_train_dates < 1:
            raise ValueError("min_train_dates must be >= 1")
        if self.train_size is not None and self.train_size < 1:
            raise ValueError("train_size must be >= 1 or None (expanding)")

    @property
    def expanding(self) -> bool:
        return self.train_size is None

    def as_dict(self) -> dict[str, object]:
        return {
            "test_size": self.test_size,
            "train_size": self.train_size,
            "valid_size": self.valid_size,
            "embargo": self.embargo,
            "min_train_dates": self.min_train_dates,
            "min_test_dates": self.min_test_dates,
            "mode": "expanding" if self.expanding else "rolling",
        }


@dataclass(frozen=True, slots=True)
class FoldSpec:
    """Date-level definition of one validation window."""

    fold_index: int
    train_dates: tuple[str, ...]
    valid_dates: tuple[str, ...]
    test_dates: tuple[str, ...]
    embargo_dates: tuple[str, ...]
    label_horizon: int

    @property
    def train_start(self) -> str:
        return self.train_dates[0]

    @property
    def test_start(self) -> str:
        return self.test_dates[0]

    def as_dict(self) -> dict[str, object]:
        return {
            "fold_index": self.fold_index,
            "train_start": self.train_start,
            "train_end": self.train_dates[-1],
            "train_dates": len(self.train_dates),
            "valid_dates": len(self.valid_dates),
            "embargo_dates": list(self.embargo_dates),
            "test_start": self.test_start,
            "test_end": self.test_dates[-1],
            "test_dates": len(self.test_dates),
            "label_horizon": self.label_horizon,
        }


def effective_embargo(cfg: WalkForwardConfig, label_horizon: int) -> int:
    """The guard band can never be smaller than the label look-ahead horizon."""

    return max(int(cfg.embargo), int(label_horizon))


def build_walk_forward_folds(
    dates: Sequence[str],
    cfg: WalkForwardConfig,
    *,
    label_horizon: int = 2,
) -> list[FoldSpec]:
    """Advance an OOS test block over the calendar, never touching the future.

    Calendar layout of one fold::

        [ train .. ][ valid .. ][ embargo guard ][ test block ]

    Raises :class:`InsufficientDataError` rather than silently returning fewer
    windows than the decision gate can certify (the gate needs ``>= 3``).
    """

    calendar = _as_sorted_dates(dates)
    guard = effective_embargo(cfg, label_horizon)
    n = len(calendar)
    capacity = n - cfg.valid_size - guard - cfg.test_size
    if capacity < cfg.min_train_dates:
        raise InsufficientDataError(
            f"{n} dates cannot host train(>={cfg.min_train_dates}) + valid({cfg.valid_size})"
            f" + embargo({guard}) + test({cfg.test_size})"
        )

    folds: list[FoldSpec] = []
    test_begin = cfg.min_train_dates + cfg.valid_size + guard
    while test_begin + cfg.test_size <= n and len(folds) < cfg.max_folds:
        guard_start = test_begin - guard
        valid_start = guard_start - cfg.valid_size
        train_end = valid_start
        train_begin = 0 if cfg.expanding else max(0, train_end - int(cfg.train_size))
        train_dates = calendar[train_begin:train_end]
        valid_dates = calendar[valid_start:guard_start]
        embargo_dates = calendar[guard_start:test_begin]
        test_dates = calendar[test_begin : test_begin + cfg.test_size]
        if (
            len(train_dates) >= cfg.min_train_dates
            and len(test_dates) >= cfg.min_test_dates
        ):
            folds.append(
                FoldSpec(
                    fold_index=len(folds),
                    train_dates=tuple(train_dates),
                    valid_dates=tuple(valid_dates),
                    test_dates=tuple(test_dates),
                    embargo_dates=tuple(embargo_dates),
                    label_horizon=int(label_horizon),
                )
            )
        test_begin += cfg.test_size

    if not folds:
        raise InsufficientDataError(
            f"cannot build any walk-forward fold from {n} dates with cfg={cfg.as_dict()}"
        )
    for fold in folds:
        validate_fold(fold, calendar, cfg, label_horizon=label_horizon)
    _assert_folds_advance_in_time(folds)
    return folds


def validate_fold(
    fold: FoldSpec,
    calendar: Sequence[str],
    cfg: WalkForwardConfig,
    *,
    label_horizon: int = 2,
) -> None:
    """Assert the date-level guards; raises :class:`EmbargoViolation`."""

    positions = {d: i for i, d in enumerate(calendar)}
    for name, block in (
        ("train", fold.train_dates),
        ("valid", fold.valid_dates),
        ("test", fold.test_dates),
    ):
        unknown = [d for d in block if d not in positions]
        if unknown:
            raise EmbargoViolation(
                f"fold {fold.fold_index}: {name} block has off-calendar dates {unknown[:3]}"
            )

    train_set, valid_set, test_set = (
        set(fold.train_dates),
        set(fold.valid_dates),
        set(fold.test_dates),
    )
    if not test_set:
        raise EmbargoViolation(f"fold {fold.fold_index} has an empty test block")
    if train_set & test_set:
        raise EmbargoViolation(
            f"fold {fold.fold_index}: dates in BOTH train and test: {sorted(train_set & test_set)[:3]}"
        )
    if valid_set & test_set:
        raise EmbargoViolation(
            f"fold {fold.fold_index}: valid/test overlap {sorted(valid_set & test_set)[:3]}"
        )
    if train_set & valid_set:
        raise EmbargoViolation(
            f"fold {fold.fold_index}: train/valid overlap {sorted(train_set & valid_set)[:3]}"
        )
    if train_set & set(fold.embargo_dates):
        raise EmbargoViolation(
            f"fold {fold.fold_index}: train uses embargo dates "
            f"{sorted(train_set & set(fold.embargo_dates))[:3]}"
        )

    guard = effective_embargo(cfg, label_horizon)
    train_pos = [positions[d] for d in fold.train_dates]
    valid_pos = [positions[d] for d in fold.valid_dates]
    test_pos = [positions[d] for d in fold.test_dates]
    if max(train_pos) + guard >= min(test_pos):
        raise EmbargoViolation(
            f"fold {fold.fold_index}: embargo violated - last train position {max(train_pos)}"
            f" + guard {guard} reaches test start {min(test_pos)}"
        )
    if valid_pos and max(valid_pos) + guard >= min(test_pos):
        raise EmbargoViolation(
            f"fold {fold.fold_index}: valid block sits inside the embargo band"
        )
    expected_band = {calendar[i] for i in range(min(test_pos) - guard, min(test_pos))}
    if set(fold.embargo_dates) != expected_band:
        raise EmbargoViolation(
            f"fold {fold.fold_index}: embargo band mismatch: expected {sorted(expected_band)[:3]},"
            f" got {sorted(fold.embargo_dates)[:3]}"
        )


def _assert_folds_advance_in_time(folds: Sequence[FoldSpec]) -> None:
    starts = [f.test_start for f in folds]
    if len(set(starts)) != len(starts):
        raise EmbargoViolation(f"walk-forward OOS windows are not unique: {starts}")
    if starts != sorted(starts):
        raise EmbargoViolation(
            "walk-forward OOS windows must advance in chronological order"
        )
    for prev, nxt in zip(folds, folds[1:], strict=False):
        if set(prev.test_dates) & set(nxt.test_dates):
            raise EmbargoViolation(
                f"OOS windows {prev.fold_index} and {nxt.fold_index} overlap"
            )


@dataclass(slots=True)
class FoldRows:
    """Row-level split of a panel for one fold, after embargo + purging."""

    fold: FoldSpec
    train_index: np.ndarray
    valid_index: np.ndarray
    test_index: np.ndarray
    purged_train_rows: int = 0
    withheld_embargo_rows: int = 0

    def summary(self) -> dict[str, object]:
        return {
            "fold_index": self.fold.fold_index,
            "n_train": int(len(self.train_index)),
            "n_valid": int(len(self.valid_index)),
            "n_test": int(len(self.test_index)),
            "purged_train_rows": int(self.purged_train_rows),
            "withheld_embargo_rows": int(self.withheld_embargo_rows),
            **self.fold.as_dict(),
        }


def _row_positions(panel: Panel) -> np.ndarray:
    return panel.frame[panel.columns.date_position].to_numpy(dtype=int)


def _row_horizons(panel: Panel, rows: np.ndarray) -> np.ndarray:
    col = panel.columns.label_horizon
    frame = panel.frame
    if col not in frame.columns or len(rows) == 0:
        return np.zeros(len(rows), dtype=int)
    raw = pd.to_numeric(frame.loc[rows, col], errors="coerce").fillna(0)
    return np.asarray(raw, dtype=int)


def purge_train_rows(
    panel: Panel, rows: np.ndarray, test_min_position: int
) -> tuple[np.ndarray, int]:
    """Drop training rows whose label window reaches into the OOS block."""

    rows = np.asarray(rows, dtype=int)
    if rows.size == 0:
        return rows, 0
    positions = _row_positions(panel)[rows]
    horizons = _row_horizons(panel, rows)
    keep = positions + horizons < int(test_min_position)
    kept = rows[keep]
    return kept, int(rows.size - kept.size)


def split_panel_rows(panel: Panel, fold: FoldSpec, cfg: WalkForwardConfig) -> FoldRows:
    """Materialise a fold into row-index arrays with embargo + purging applied."""

    columns = panel.columns
    frame = panel.frame
    dates = frame[columns.event_date].to_numpy()
    train_mask = np.isin(dates, np.asarray(fold.train_dates))
    test_mask = np.isin(dates, np.asarray(fold.test_dates))
    embargo_mask = np.isin(dates, np.asarray(fold.embargo_dates))
    valid_mask = (
        np.isin(dates, np.asarray(fold.valid_dates))
        if fold.valid_dates
        else np.zeros(len(frame), bool)
    )

    test_rows = np.flatnonzero(test_mask)
    if test_rows.size == 0:
        raise InsufficientDataError(
            f"fold {fold.fold_index} has no test rows after date filtering"
        )
    positions = _row_positions(panel)
    test_min_position = int(positions[test_rows].min())

    train_rows, purged = purge_train_rows(
        panel, np.flatnonzero(train_mask), test_min_position
    )
    valid_rows, _ = purge_train_rows(
        panel, np.flatnonzero(valid_mask), test_min_position
    )

    rows = FoldRows(
        fold=fold,
        train_index=train_rows,
        valid_index=valid_rows,
        test_index=test_rows,
        purged_train_rows=purged,
        withheld_embargo_rows=int(embargo_mask.sum()),
    )
    assert_split_disjoint(rows)
    return rows


def assert_split_disjoint(rows: FoldRows) -> None:
    train, valid, test = (
        {int(i) for i in rows.train_index},
        {int(i) for i in rows.valid_index},
        {int(i) for i in rows.test_index},
    )
    if train & test:
        raise EmbargoViolation(
            f"row-level leakage: train/test share rows {sorted(train & test)[:5]}"
        )
    if valid & test:
        raise EmbargoViolation(
            f"row-level leakage: valid/test share rows {sorted(valid & test)[:5]}"
        )
    if train & valid:
        raise EmbargoViolation(
            f"row-level leakage: train/valid share rows {sorted(train & valid)[:5]}"
        )


def leakage_probe(panel: Panel, rows: FoldRows) -> dict[str, object]:
    """Independent diagnostic: does any train label window overlap the OOS block?

    Written into the report next to the metrics, so a reviewer can re-check the
    split without re-running the models.
    """

    frame = panel.frame
    positions = _row_positions(panel)
    horizons = _row_horizons(panel, np.arange(len(frame)))
    test_min = int(positions[rows.test_index].min())
    overlaps = [
        int(i)
        for i in rows.train_index
        if int(positions[i]) + int(horizons[i]) >= test_min
    ]
    last_train = int(positions[rows.train_index].max()) if len(rows.train_index) else -1
    return {
        "fold_index": rows.fold.fold_index,
        "train_rows_with_label_window_in_test": len(overlaps),
        "train_last_position": last_train,
        "test_first_position": test_min,
        "position_gap": test_min - last_train,
        "overlap_rows": overlaps[:10],
    }
