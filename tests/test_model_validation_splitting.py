"""Walk-forward / embargo / purge guarantees for the #36 validation scaffold."""

from __future__ import annotations

import numpy as np
import pytest

from strategy.model_validation.contracts import Panel, validate_panel
from strategy.model_validation.fixtures import SyntheticConfig, make_synthetic_panel
from strategy.model_validation.splitting import (
    EmbargoViolation,
    FoldRows,
    FoldSpec,
    InsufficientDataError,
    WalkForwardConfig,
    build_walk_forward_folds,
    effective_embargo,
    leakage_probe,
    split_panel_rows,
    validate_fold,
)


def _panel(n_dates: int = 46, n_symbols: int = 5, horizon: int = 2) -> Panel:
    return validate_panel(
        make_synthetic_panel(
            SyntheticConfig(
                n_dates=n_dates,
                n_symbols=n_symbols,
                seq_len=4,
                label_horizon=horizon,
                scenario="pure_noise_sequence",
                untradable_ratio=0.1,
                seed=3,
            )
        ),
        label_horizon=horizon,
    )


def _config(**kwargs: object) -> WalkForwardConfig:
    params: dict[str, object] = {
        "test_size": 5,
        "train_size": 12,
        "embargo": 1,
        "min_train_dates": 4,
    }
    params.update(kwargs)
    return WalkForwardConfig(**params)  # type: ignore[arg-type]


def dates_of(panel: Panel) -> list[str]:
    return sorted(panel.frame[panel.columns.event_date].astype(str).unique().tolist())


def test_folds_are_strictly_temporal_and_oos_windows_never_overlap() -> None:
    panel = _panel()
    cfg = _config()
    folds = build_walk_forward_folds(dates_of(panel), cfg, label_horizon=2)

    assert len(folds) >= 3, "the gate certifies only with >= 3 walk-forward windows"
    for fold in folds:
        assert max(fold.train_dates) < min(fold.test_dates)
        assert set(fold.train_dates).isdisjoint(fold.test_dates)
        assert set(fold.valid_dates).isdisjoint(fold.test_dates)
    seen: set[str] = set()
    for fold in folds:
        assert seen.isdisjoint(fold.test_dates), (
            "OOS windows must not overlap each other"
        )
        seen.update(fold.test_dates)
    assert [f.test_start for f in folds] == sorted(f.test_start for f in folds)


def test_embargo_band_sits_between_train_and_test() -> None:
    panel = _panel()
    cfg = _config(embargo=3)
    folds = build_walk_forward_folds(dates_of(panel), cfg, label_horizon=2)
    calendar = dates_of(panel)
    for fold in folds:
        assert fold.embargo_dates, "a fold must record the withheld guard band"
        assert set(fold.embargo_dates).isdisjoint(fold.train_dates)
        assert set(fold.embargo_dates).isdisjoint(fold.test_dates)
        assert max(fold.embargo_dates) < min(fold.test_dates)
        # the guard band is exactly the ``guard`` calendar dates before the test block
        guard = effective_embargo(cfg, fold.label_horizon)
        test_index = calendar.index(fold.test_start)
        assert list(fold.embargo_dates) == calendar[test_index - guard : test_index]


def test_effective_embargo_is_at_least_the_label_horizon() -> None:
    assert effective_embargo(_config(embargo=0), label_horizon=5) == 5
    assert effective_embargo(_config(embargo=4), label_horizon=2) == 4


def test_purging_removes_train_rows_whose_label_window_reaches_test() -> None:
    panel = _panel(horizon=5)
    cfg = _config(embargo=1)
    folds = build_walk_forward_folds(dates_of(panel), cfg, label_horizon=5)
    for fold in folds:
        rows = split_panel_rows(panel, fold, cfg)
        probe = leakage_probe(panel, rows)
        assert probe["train_rows_with_label_window_in_test"] == 0
        assert probe["position_gap"] >= 5 + 1
        assert rows.purged_train_rows >= 0


def test_multi_horizon_labels_purge_per_row() -> None:
    """A row with a long label horizon must be dropped even if others survive."""

    panel = _panel(horizon=2)
    cfg = _config(embargo=1)
    folds = build_walk_forward_folds(dates_of(panel), cfg, label_horizon=2)
    fold = folds[2]
    calendar = dates_of(panel)
    test_index = calendar.index(fold.test_start)
    leaking_date = calendar[
        test_index - 3
    ]  # inside the train block, horizon 2 -> no leak yet

    frame = panel.frame.copy()
    before = int((frame[panel.columns.event_date] == leaking_date).sum())
    rows = split_panel_rows(panel, fold, cfg)
    assert rows.purged_train_rows == 0
    assert leakage_probe(panel, rows)["train_rows_with_label_window_in_test"] == 0

    frame.loc[
        frame[panel.columns.event_date] == leaking_date, panel.columns.label_horizon
    ] = 6.0
    leaked = validate_panel(
        Panel(
            frame=frame,
            features=panel.features,
            columns=panel.columns,
            sequence=panel.sequence,
        ),
        label_horizon=2,
    )
    rows_after = split_panel_rows(leaked, fold, cfg)
    assert rows_after.purged_train_rows == before, "long-horizon rows must be purged"
    assert (
        leakage_probe(leaked, rows_after)["train_rows_with_label_window_in_test"] == 0
    )


def test_handcrafted_leaky_fold_is_rejected() -> None:
    calendar = [f"2026-01-{d:02d}" for d in range(1, 21)]
    cfg = _config(embargo=2)
    leaky = FoldSpec(
        fold_index=0,
        train_dates=tuple(calendar[:10]),
        valid_dates=(),
        test_dates=tuple(calendar[8:13]),  # overlaps train
        embargo_dates=tuple(calendar[10:12]),
        label_horizon=2,
    )
    with pytest.raises(EmbargoViolation, match="BOTH train and test"):
        validate_fold(leaky, calendar, cfg, label_horizon=2)

    adjacent = FoldSpec(
        fold_index=1,
        train_dates=tuple(calendar[:12]),
        valid_dates=(),
        test_dates=tuple(calendar[13:18]),  # only 1 date gap, embargo requires 2
        embargo_dates=tuple(calendar[12:13]),
        label_horizon=2,
    )
    with pytest.raises(EmbargoViolation, match="embargo violated"):
        validate_fold(adjacent, calendar, cfg, label_horizon=2)


def test_purging_makes_a_handcrafted_leaky_fold_row_disjoint() -> None:
    """Even if a caller builds an overlapping fold, no row can be in both sides."""

    from strategy.model_validation.splitting import assert_split_disjoint

    panel = _panel(n_dates=40)
    cfg = _config(embargo=2, min_train_dates=2)
    calendar = dates_of(panel)
    leaky = FoldSpec(
        fold_index=0,
        train_dates=tuple(calendar[:20]),
        valid_dates=(),
        test_dates=tuple(calendar[18:24]),  # deliberately overlaps the train block
        embargo_dates=tuple(calendar[16:18]),
        label_horizon=2,
    )
    rows = split_panel_rows(panel, leaky, cfg)
    assert set(int(i) for i in rows.train_index).isdisjoint(
        int(i) for i in rows.test_index
    )

    with pytest.raises(EmbargoViolation, match="row-level leakage"):
        assert_split_disjoint(
            FoldRows(
                fold=leaky,
                train_index=np.asarray([0, 1, 2]),
                valid_index=np.asarray([], dtype=int),
                test_index=np.asarray([2, 3]),
            )
        )


def test_insufficient_history_raises_instead_of_thin_evidence() -> None:
    cfg = _config(test_size=5, train_size=12, embargo=2)
    with pytest.raises(InsufficientDataError):
        build_walk_forward_folds(
            [f"2026-01-{d:02d}" for d in range(1, 11)], cfg, label_horizon=2
        )


def test_fold_without_test_rows_raises() -> None:
    panel = _panel(n_dates=40)
    cfg = _config(embargo=2)
    calendar = dates_of(panel)
    fold = FoldSpec(
        fold_index=0,
        train_dates=tuple(calendar[:20]),
        valid_dates=(),
        embargo_dates=tuple(calendar[20:22]),
        # dates that exist in the calendar but were filtered out of the panel
        test_dates=tuple(f"2030-01-{i:02d}" for i in range(1, 6)),
        label_horizon=2,
    )
    with pytest.raises(InsufficientDataError):
        split_panel_rows(panel, fold, cfg)


def test_expanding_window_keeps_the_full_history_start() -> None:
    panel = _panel(n_dates=44)
    cfg = _config(train_size=None, embargo=2)
    folds = build_walk_forward_folds(dates_of(panel), cfg, label_horizon=2)
    calendar = dates_of(panel)
    assert cfg.expanding
    for fold in folds:
        assert fold.train_dates[0] == calendar[0]


def test_rolling_window_train_length_is_capped() -> None:
    panel = _panel(n_dates=44)
    cfg = _config(train_size=8, embargo=2)
    folds = build_walk_forward_folds(dates_of(panel), cfg, label_horizon=2)
    assert all(len(f.train_dates) <= 8 for f in folds)
    assert all(len(f.train_dates) == 8 for f in folds[1:])


def test_valid_block_is_used_for_early_stopping_without_touching_test() -> None:
    panel = _panel(n_dates=48)
    cfg = _config(valid_size=3, embargo=2)
    folds = build_walk_forward_folds(dates_of(panel), cfg, label_horizon=2)
    for fold in folds:
        assert len(fold.valid_dates) == 3
        assert max(fold.valid_dates) < min(fold.test_dates)
        rows = split_panel_rows(panel, fold, cfg)
        assert rows.valid_index.size > 0
        assert set(int(i) for i in rows.valid_index).isdisjoint(
            int(i) for i in rows.test_index
        )


def test_fold_records_are_json_serialisable() -> None:
    import json

    panel = _panel(n_dates=42)
    cfg = _config(embargo=2)
    fold = build_walk_forward_folds(dates_of(panel), cfg, label_horizon=2)[0]
    rows = split_panel_rows(panel, fold, cfg)
    payload = json.dumps(rows.summary(), ensure_ascii=False, default=str)
    assert fold.test_start in payload
    assert "purged_train_rows" in payload
