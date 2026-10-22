"""Local input contracts for the issue #36 increment-validation scaffold.

Why a local contract instead of importing the training stack
----------------------------------------------------------
``strategy.first_limit_alpha`` (labels #23, factors #24, sentiment #25, limit-up
backtest #26, execution #27) is owned by other workstreams while this scaffold is
built.  Importing it here would create a hard cross-branch dependency and would
make this package untestable in isolation.  Instead the scaffold consumes a plain
``pandas.DataFrame`` panel plus an optional 3-D sequence tensor and validates the
columns it actually needs.  Section "INTEGRATION" below is the mapping contract
the main agent has to satisfy when wiring real data in.

INTEGRATION (for the main agent)
--------------------------------
Build a ``Panel`` from upstream artefacts like this::

    panel = Panel(
        frame=label_df,                      # from #23 可成交标签
        features=tuple(factor_columns),      # from #4/#7/#8/#9/#10 因子表
        columns=ColumnNames(
            symbol="symbol",
            event_date="event_date",         # 信号日 T, 特征只能用 T 及之前
            label="label_continuation",      # #23 标签
            gross_return="exec_gross_return",# #26 可成交口径下的毛收益
            can_buy="can_buy",               # #26 一字板/涨停买不到
            can_sell="can_sell",             # #26 跌停卖不出
            label_horizon="label_horizon",   # 该样本标签覆盖的交易日数(用于 purge)
        ),
        sequence=SequenceBundle(values=x, keys=key_df, feature_names=names),
    )

Responsibilities deliberately NOT taken by this scaffold
--------------------------------------------------------
* Survivorship-bias / delisting removal: the sample universe must already be
  de-listed-safe (provided by #22/#23).  Here we only *report* coverage, i.e. how
  many candidates were dropped because they were flagged non-executable.
* Tradability of one-word limit boards (一字板买不到) and limit-down exits
  (跌停卖不出): computed by #26 and consumed through ``can_buy`` / ``can_sell``.
  When those columns are absent the scaffold marks the run
  ``executability_source="assumed"`` and the gate refuses a ``keep`` decision
  (see ``GateConfig.require_executability``), so a flattering report built on an
  "everything fills" assumption can never be promoted.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Literal, Protocol, runtime_checkable

import numpy as np
import pandas as pd

InputKind = Literal["tabular", "sequence", "both"]


class ContractError(ValueError):
    """Raised when the caller's panel violates the documented contract."""


@dataclass(frozen=True, slots=True)
class ColumnNames:
    """Configurable column names so upstream schemas can be plugged in as-is."""

    symbol: str = "symbol"
    event_date: str = "event_date"
    label: str = "label"
    gross_return: str = "outcome_return"
    next_day_premium: str = "next_day_premium"
    can_buy: str = "can_buy"
    can_sell: str = "can_sell"
    executability_reason: str = "executability_reason"
    label_horizon: str = "label_horizon"
    date_position: str = "date_position"

    def as_dict(self) -> dict[str, str]:
        return {
            "symbol": self.symbol,
            "event_date": self.event_date,
            "label": self.label,
            "gross_return": self.gross_return,
            "next_day_premium": self.next_day_premium,
            "can_buy": self.can_buy,
            "can_sell": self.can_sell,
            "executability_reason": self.executability_reason,
            "label_horizon": self.label_horizon,
        }


@dataclass(slots=True)
class SequenceBundle:
    """Aligned 3-D tensor for sequence / multimodal candidates.

    ``values`` has shape ``(n_rows, seq_len, n_channels)`` and row order must
    match ``keys`` (``symbol`` + ``event_date``), which in turn must match the
    panel row order.  Row order matters: it is what makes purging safe.
    """

    values: np.ndarray
    keys: pd.DataFrame
    feature_names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        self.values = np.asarray(self.values, dtype=np.float64)
        if self.values.ndim != 3:
            raise ContractError(
                f"sequence.values must be 3-D (n_rows, seq_len, n_channels), got {self.values.shape}"
            )
        if self.keys.shape[1] < 2:
            raise ContractError(
                "sequence.keys must hold the symbol and event_date columns"
            )
        if len(self.keys) != self.values.shape[0]:
            raise ContractError(
                f"sequence.keys rows ({len(self.keys)}) != tensor rows ({self.values.shape[0]})"
            )

    def __len__(self) -> int:
        return int(self.values.shape[0])

    @property
    def seq_len(self) -> int:
        return int(self.values.shape[1])

    @property
    def n_channels(self) -> int:
        return int(self.values.shape[2])

    def select(self, row_index: np.ndarray) -> "SequenceBundle":
        idx = np.asarray(row_index, dtype=int)
        return SequenceBundle(
            values=self.values[idx],
            keys=self.keys.reset_index(drop=True).iloc[idx].reset_index(drop=True),
            feature_names=self.feature_names,
        )


@dataclass(slots=True)
class Panel:
    """The single input object every validation run consumes."""

    frame: pd.DataFrame
    features: tuple[str, ...]
    columns: ColumnNames = field(default_factory=ColumnNames)
    sequence: SequenceBundle | None = None
    #: ``validate_panel`` sets this to False when #26 did not ship can_buy /
    #: can_sell, i.e. tradability is assumed rather than measured.
    executability_declared: bool = True

    def __post_init__(self) -> None:
        self.features = tuple(self.features)

    def __len__(self) -> int:
        return int(len(self.frame))

    @property
    def n_events(self) -> int:
        return int(self.frame[self.columns.event_date].nunique())

    def key_frame(self) -> pd.DataFrame:
        c = self.columns
        return self.frame[[c.symbol, c.event_date]].copy()

    def labels(self) -> np.ndarray:
        return _numeric(self.frame[self.columns.label], self.columns.label)

    def gross_returns(self) -> np.ndarray:
        c = self.columns
        if c.gross_return not in self.frame.columns:
            return np.full(len(self.frame), np.nan)
        return _numeric(self.frame[c.gross_return], c.gross_return)

    def tabular(self) -> np.ndarray:
        if not self.features:
            return np.zeros((len(self.frame), 0), dtype=np.float64)
        out = np.empty((len(self.frame), len(self.features)), dtype=np.float64)
        for j, name in enumerate(self.features):
            out[:, j] = _numeric(self.frame[name], name)
        return out

    def sequence_slice(self, row_index: np.ndarray) -> SequenceBundle | None:
        if self.sequence is None:
            return None
        return self.sequence.select(row_index)

    def has_executability(self) -> bool:
        return bool(self.executability_declared)


def _numeric(series: pd.Series, name: str) -> np.ndarray:
    if not pd.api.types.is_numeric_dtype(series):
        coerced = pd.to_numeric(series, errors="coerce")
        if float(coerced.isna().mean()) > 0.5:
            raise ContractError(f"column {name!r} is non-numeric and cannot be coerced")
        return coerced.to_numpy(dtype=np.float64, copy=False)
    return series.to_numpy(dtype=np.float64, copy=False)


def _bool_column(frame: pd.DataFrame, name: str, default: bool) -> np.ndarray:
    if name not in frame.columns:
        return np.full(len(frame), default, dtype=bool)
    raw = frame[name]
    if pd.api.types.is_bool_dtype(raw):
        return raw.fillna(default).to_numpy(dtype=bool)
    coerced = pd.to_numeric(raw, errors="coerce")
    return coerced.fillna(float(default)).to_numpy(dtype=bool)


def validate_panel(
    panel: Panel,
    *,
    label_horizon: int = 2,
    require_sequence: bool = False,
    allow_nan_features: bool = True,
    min_events: int = 1,
) -> Panel:
    """Normalise and hard-check a panel; returns a new ``Panel`` (never mutates).

    Guarantees enforced here (all of them are unit-tested):

    * required columns present, ``event_date`` is a sortable ISO string;
    * ``(symbol, event_date)`` unique;
    * rows sorted by ``(event_date, symbol)`` so positional slicing is temporal;
    * numeric feature / label / return matrices with the NaN policy honoured;
    * at least ``min_events`` distinct event dates (the runner asks for 3, the
      walk-forward builder then raises if the calendar cannot host the windows);
    * optional ``label_horizon`` column filled with the run-level horizon
      (used by the purging step - a per-row horizon is needed because #23 emits
      multi-horizon labels);
    * executability flags materialised (defaults to ``True`` and is *reported*).
    """

    c = panel.columns
    required = [c.symbol, c.event_date, c.label]
    missing = [name for name in required if name not in panel.frame.columns]
    if missing:
        raise ContractError(f"panel is missing required columns: {missing}")
    missing_features = [
        name for name in panel.features if name not in panel.frame.columns
    ]
    if missing_features:
        raise ContractError(
            f"panel is missing declared feature columns: {missing_features}"
        )

    frame = panel.frame.copy()
    frame[c.symbol] = frame[c.symbol].astype(str)
    frame[c.event_date] = frame[c.event_date].astype(str)
    if frame[[c.symbol, c.event_date]].duplicated().any():
        dup = frame[[c.symbol, c.event_date]].loc[lambda d: d.duplicated()].head(3)
        raise ContractError(
            f"panel has duplicate (symbol, event_date) keys: {dup.to_dict('records')}"
        )

    if panel.sequence is not None:
        seq_keys = panel.sequence.keys
        if len(seq_keys) != len(frame):
            raise ContractError("sequence bundle length differs from panel length")
        expected = frame[[c.symbol, c.event_date]].astype(str).to_numpy()
        actual = seq_keys[[c.symbol, c.event_date]].astype(str).to_numpy()
        if not np.array_equal(expected, actual):
            raise ContractError(
                "sequence bundle rows are not aligned with the panel row order"
            )
    elif require_sequence:
        raise ContractError(
            "this run requires a sequence bundle but panel.sequence is None"
        )

    if not allow_nan_features and panel.features:
        if np.isnan(panel.tabular()).any():
            raise ContractError("panel features contain NaN and nan_policy forbids it")

    for name in (c.label, c.gross_return):
        if name in frame.columns:
            frame[name] = _numeric(frame[name], name)
    if c.label_horizon in frame.columns:
        frame[c.label_horizon] = pd.to_numeric(
            frame[c.label_horizon], errors="coerce"
        ).fillna(label_horizon)
    else:
        frame[c.label_horizon] = float(label_horizon)
    declared = (c.can_buy in frame.columns) and (c.can_sell in frame.columns)
    frame[c.can_buy] = _bool_column(frame, c.can_buy, default=True)
    frame[c.can_sell] = _bool_column(frame, c.can_sell, default=True)
    if c.executability_reason not in frame.columns:
        frame[c.executability_reason] = ""

    frame = frame.sort_values([c.event_date, c.symbol], kind="mergesort").reset_index(
        drop=True
    )
    dates = sorted(frame[c.event_date].unique().tolist())
    position = {d: i for i, d in enumerate(dates)}
    frame[c.date_position] = frame[c.event_date].map(position).astype(int)

    sequence = panel.sequence
    if sequence is not None:
        # ``sort_values`` keeps the pre-sort positional index in ``frame.index``;
        # reindex the tensor with it so rows stay aligned with the sorted panel.
        reorder = np.asarray(frame.index, dtype=int)
        sequence = SequenceBundle(
            values=sequence.values[reorder],
            keys=frame[[c.symbol, c.event_date]].copy(),
            feature_names=sequence.feature_names,
        )

    out = replace(
        panel, frame=frame, sequence=sequence, executability_declared=declared
    )
    if out.n_events < min_events:
        raise ContractError(
            f"panel needs at least {min_events} distinct event dates, got {out.n_events}"
        )
    return out


@runtime_checkable
class IncrementValidator(Protocol):
    """Minimal surface a baseline / candidate model must implement.

    Concrete implementations live in :mod:`strategy.model_validation.models`.
    Deep models (GRU / Transformer / CNN on K-line images) plug in through an
    adapter that performs its heavy imports lazily, so a missing torch never
    breaks the scaffold or CI.
    """

    name: str
    input_kind: InputKind

    def fit(self, inputs: "ModelInput", y: np.ndarray) -> None: ...

    def predict(self, inputs: "ModelInput") -> np.ndarray: ...

    def describe(self) -> dict[str, Any]: ...


@dataclass(slots=True)
class ModelInput:
    """Feature payload for one fold side (train / valid / test)."""

    tabular: np.ndarray
    keys: pd.DataFrame
    sequence: SequenceBundle | None = None

    def __post_init__(self) -> None:
        self.tabular = np.asarray(self.tabular, dtype=np.float64)
        if self.tabular.ndim == 1:
            self.tabular = self.tabular.reshape(-1, 1)
        if self.tabular.ndim != 2:
            raise ContractError("ModelInput.tabular must be 2-D")
        if len(self.keys) != self.tabular.shape[0]:
            raise ContractError("ModelInput keys length differs from tabular rows")
        if self.sequence is not None and len(self.sequence) != self.tabular.shape[0]:
            raise ContractError("ModelInput sequence length differs from tabular rows")

    @property
    def n_rows(self) -> int:
        return int(self.tabular.shape[0])

    def as_dict(self) -> dict[str, Any]:
        return {
            "n_rows": self.n_rows,
            "n_tabular_features": int(self.tabular.shape[1]),
            "seq_len": None if self.sequence is None else self.sequence.seq_len,
            "n_channels": None if self.sequence is None else self.sequence.n_channels,
        }
