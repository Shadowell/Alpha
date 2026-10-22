"""Synthetic panel fixtures that drive the scaffold without real market data.

The scaffold is built before #23 (executable labels) and #26 (limit-up aware
backtest) land, so it must be testable *now*.  These generators produce a panel
that satisfies the documented contract and lets us prove both sides of the gate:

* ``"informative_sequence"`` - the future outcome depends on a signal that only
  exists in the time-ordered window (recent drift of channel 0), invisible to a
  tabular model.  A sequence candidate can beat the baseline -> ``keep``.
* ``"pure_noise_sequence"`` - the outcome depends only on the tabular factor.
  The sequence candidate has no real edge -> ``reject`` (不保留).

Rows flagged non-executable are given *better* outcomes on purpose: that is the
一字板买不到 situation, where a model that ignores tradability looks artificially
good on paper.  Consuming ``can_buy`` from #26 is therefore not cosmetic.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .contracts import ColumnNames, Panel, SequenceBundle


@dataclass(slots=True)
class SyntheticConfig:
    n_dates: int = 60
    n_symbols: int = 12
    seq_len: int = 8
    label_horizon: int = 2
    seed: int = 7
    scenario: str = (
        "pure_noise_sequence"  # "informative_sequence" | "pure_noise_sequence"
    )
    nan_ratio: float = 0.0  # share of tabular cells replaced by NaN
    untradable_ratio: float = 0.15  # share of rows flagged can_buy=False (一字板)
    noise: float = 0.35

    def as_dict(self) -> dict[str, object]:
        return {
            "n_dates": self.n_dates,
            "n_symbols": self.n_symbols,
            "seq_len": self.seq_len,
            "label_horizon": self.label_horizon,
            "seed": self.seed,
            "scenario": self.scenario,
            "nan_ratio": self.nan_ratio,
            "untradable_ratio": self.untradable_ratio,
            "noise": self.noise,
        }


def _dates(n: int) -> list[str]:
    # Business-day-like labels; string sort == chronological sort by construction.
    stamps = pd.date_range("2026-01-05", periods=n, freq="B")
    return [s.date().isoformat() for s in stamps]


def make_synthetic_panel(cfg: SyntheticConfig | None = None) -> Panel:
    cfg = cfg or SyntheticConfig()
    if cfg.scenario not in {"informative_sequence", "pure_noise_sequence"}:
        raise ValueError(f"unknown scenario: {cfg.scenario!r}")
    rng = np.random.default_rng(cfg.seed)
    dates = _dates(cfg.n_dates)
    symbols = [f"{600000 + i:06d}" for i in range(cfg.n_symbols)]

    # latent drivers, one per (date, symbol)
    tabular_signal = rng.normal(size=(cfg.n_dates, cfg.n_symbols))
    recency_signal = rng.normal(size=(cfg.n_dates, cfg.n_symbols))
    junk = rng.normal(size=(cfg.n_dates, cfg.n_symbols, 3))

    # The outcome is generated from the signals, then the label / gross return
    # are derived from it.  Nothing about the future is used as a *feature*.
    informative = cfg.scenario == "informative_sequence"
    weight_tab = 0.25 if informative else 0.9
    weight_seq = 1.8 if informative else 0.0
    noise = 0.18 if informative else cfg.noise
    future = (
        weight_tab * tabular_signal
        + weight_seq * _recent_mean(recency_signal, window=3)
        + noise * rng.normal(size=(cfg.n_dates, cfg.n_symbols))
    )
    gross = 0.01 * future + 0.004 * rng.normal(size=(cfg.n_dates, cfg.n_symbols))
    label = (future > np.quantile(future, 0.75)).astype(float)

    # 3-D window: channel 0 carries the recency driver as an ordered walk,
    # channels 1..3 are pure noise.  The *tabular* frame never sees it directly.
    walk = np.cumsum(recency_signal, axis=0)
    channels = np.stack(
        [
            np.tanh(walk / 2.0),
            junk[:, :, 0],
            junk[:, :, 1],
            junk[:, :, 2],
        ],
        axis=2,
    )

    rows: list[dict[str, object]] = []
    seq_rows: list[np.ndarray] = []
    keys: list[tuple[str, str]] = []
    for t, date in enumerate(dates):
        for s_i, symbol in enumerate(symbols):
            lo = max(0, t - cfg.seq_len + 1)
            window = channels[lo : t + 1, s_i, :]
            if window.shape[0] < cfg.seq_len:  # left-pad the oldest bars
                pad = np.zeros(
                    (cfg.seq_len - window.shape[0], channels.shape[2]), dtype=np.float64
                )
                window = np.vstack([pad, window])
            feature_vals = [tabular_signal[t, s_i]] + [
                junk[t, s_i, j] for j in range(3)
            ]
            row: dict[str, object] = {
                "symbol": symbol,
                "event_date": date,
                "label": float(label[t, s_i]),
                "outcome_return": float(gross[t, s_i]),
                "next_day_premium": float(0.4 * gross[t, s_i] + 0.001 * rng.normal()),
                "label_horizon": int(cfg.label_horizon),
            }
            for f_i, name in enumerate(FEATURE_NAMES):
                row[name] = float(feature_vals[f_i])
            rows.append(row)
            seq_rows.append(window)
            keys.append((symbol, date))

    frame = pd.DataFrame(rows)
    if cfg.nan_ratio > 0:
        mask = rng.random(frame[FEATURE_NAMES].shape) < cfg.nan_ratio
        values = frame[FEATURE_NAMES].to_numpy(dtype=float)
        values[mask] = np.nan
        frame[FEATURE_NAMES] = values

    n_rows = len(frame)
    can_buy = np.ones(n_rows, dtype=bool)
    if cfg.untradable_ratio > 0:
        idx = rng.choice(n_rows, size=int(n_rows * cfg.untradable_ratio), replace=False)
        can_buy[idx] = False
        # the un-buyable rows are the "gap-up one-word limit" ones: best outcomes
        frame.loc[idx, "outcome_return"] = (
            np.abs(frame.loc[idx, "outcome_return"].to_numpy(dtype=float)) + 0.05
        )
    frame["can_buy"] = can_buy
    frame["can_sell"] = np.ones(n_rows, dtype=bool)
    frame["executability_reason"] = np.where(
        frame["can_buy"], "", "one_word_limit_up_unbuyable"
    )

    keys_frame = pd.DataFrame(keys, columns=["symbol", "event_date"])
    sequence = SequenceBundle(
        values=np.stack(seq_rows),
        keys=keys_frame,
        feature_names=SEQ_CHANNEL_NAMES,
    )
    return Panel(
        frame=frame,
        features=FEATURE_NAMES,
        columns=ColumnNames(),
        sequence=sequence,
    )


def _recent_mean(matrix: np.ndarray, window: int) -> np.ndarray:
    out = np.zeros_like(matrix)
    for t in range(matrix.shape[0]):
        lo = max(0, t - window + 1)
        out[t] = matrix[lo : t + 1].mean(axis=0)
    return out


FEATURE_NAMES: tuple[str, ...] = ("f_tabular", "f_junk_1", "f_junk_2", "f_junk_3")
SEQ_CHANNEL_NAMES: tuple[str, ...] = ("ret", "noise_1", "noise_2", "noise_3")
