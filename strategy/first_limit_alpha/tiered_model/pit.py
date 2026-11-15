"""严格 PIT 与标签泄漏防线 (GitHub issue #27)。

issue #27 原文的硬约束：

* "训练特征时点 <= 决策时点"；
* "禁止用未来数据做特征选择"；
* #23 明确标注 ``d1_open … d5_close`` / ``future_limit_flag_d1…d5`` /
  ``future_limit_times_d1…d5`` 为"仅供标签复现与调试，**禁止进因子矩阵**"。

本模块提供三件事：

1. :data:`LEAKAGE_FORBIDDEN_EXACT` + :data:`LEAKAGE_FORBIDDEN_PATTERNS` ——
   从 #23 标签表列名推导出的"未来信息"黑名单，:func:`find_leakage_columns`
   用它扫描候选特征列。
2. :func:`assert_pit_features` —— 在训练前一次性拒绝任何命中黑名单的特征，
   错误信息里带上命中的具体列，便于上游修数据而不是下游猜。
3. :func:`attach_factors` —— 把 #24 / #7-#10 的因子表按 ``(symbol, as_of_date)``
   贴到标签表上，**只允许 ``as_of_date <= trade_date``**，同日之外一律丢弃并计数。

纪律：缺数显式缺失（``NaN`` + ``<col>_is_missing``），绝不 ``fillna(0)``。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

import numpy as np
import pandas as pd

MISSING_SUFFIX = "_is_missing"

#: #23 标签表中"未来窗口"字段：任何出现在特征列里的都必须报错。
LEAKAGE_FORBIDDEN_EXACT: frozenset[str] = frozenset(
    {
        "label_end_date",
        "label_horizon_days",
        "label_status",
        "label_complete",
        "label_source",
        "embargo_days",
        "next_limit_times",
        "next_auction_premium",
        "next_open_premium",
        "next_close_return",
        "next_high_premium",
        "next_limit_up_premium",
        "promotion_level_next",
        "promotion_source",
        "is_1_to_2",
        "is_2_to_3",
        "is_3_to_4",
        "is_continuation",
        "is_broken_board",
        "is_break_streak_next",
        "is_next_limit_down",
        "is_next_close_down",
        "is_big_loss",
        "is_strong_3d",
        "is_limit_up_at_close",
        "fwd_ret_3d",
        "fwd_ret_5d",
        "mfe_3d_A",
        "mao_3d_A",
        "mfe_3d_B",
        "mao_3d_B",
        "ret_1d_A",
        "ret_3d_A",
        "ret_5d_A",
        "ret_1d_B",
        "ret_3d_B",
        "ret_5d_B",
        "max_drawdown_3d",
        "max_runup_3d",
        "max_adverse_offset_3d",
        "ret_benchmark_price",
        "ret_benchmark_source",
        "fill_probability",
        "exit_time_short",
        "exit_time_long",
        "entry_time_B",
        "entry_price_A",
        "entry_price_B",
        "auction_price",
        "auction_proxy",
    }
)

#: 前缀/正则口径的未来字段（d1_open … d5_close, future_limit_*）。
LEAKAGE_FORBIDDEN_PATTERNS: tuple[str, ...] = (
    r"^d[1-5]_(open|high|low|close)$",
    r"^future_limit_(flag|times)_d[1-5]$",
    r"^y_",
    r".*_is_missing$",
)

#: 允许进入因子矩阵的 #24 微观结构列前缀（#24 自带 *_is_missing 诊断列）。
FACTOR_MISSING_SUFFIX = MISSING_SUFFIX

Compiled = tuple[re.Pattern[str], ...]


class LeakageError(ValueError):
    """Raised when a candidate feature column carries post-decision information."""


@dataclass(slots=True)
class PitAudit:
    """Report payload for one feature-attachment pass."""

    n_label_rows: int = 0
    n_factor_rows: int = 0
    n_matched: int = 0
    n_dropped_future_factor_rows: int = 0
    n_unmatched_label_rows: int = 0
    feature_columns: tuple[str, ...] = field(default_factory=tuple)
    missing_indicator_columns: tuple[str, ...] = field(default_factory=tuple)
    factor_as_of_min: str | None = None
    factor_as_of_max: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "n_label_rows": self.n_label_rows,
            "n_factor_rows": self.n_factor_rows,
            "n_matched": self.n_matched,
            "n_dropped_future_factor_rows": self.n_dropped_future_factor_rows,
            "n_unmatched_label_rows": self.n_unmatched_label_rows,
            "match_ratio": round(
                self.n_matched / max(1, self.n_label_rows), 6
            ),
            "n_features": len(self.feature_columns),
            "n_missing_indicators": len(self.missing_indicator_columns),
            "factor_as_of_min": self.factor_as_of_min,
            "factor_as_of_max": self.factor_as_of_max,
        }


def _patterns(extra: Iterable[str] | None = None) -> Compiled:
    raw = list(LEAKAGE_FORBIDDEN_PATTERNS) + list(extra or ())
    return tuple(re.compile(p) for p in raw)


def find_leakage_columns(
    columns: Iterable[str], *, extra_patterns: Iterable[str] | None = None
) -> list[str]:
    """Return the subset of ``columns`` that must never be used as features."""

    patterns = _patterns(extra_patterns)
    hits: list[str] = []
    for col in columns:
        name = str(col)
        if name in LEAKAGE_FORBIDDEN_EXACT or any(p.search(name) for p in patterns):
            hits.append(name)
    return hits


def assert_pit_features(
    columns: Sequence[str],
    *,
    decision_columns: Sequence[str] = ("trade_date", "event_date", "as_of_date"),
    extra_patterns: Iterable[str] | None = None,
) -> tuple[str, ...]:
    """Reject any future-information feature; raise :class:`LeakageError`.

    ``decision_columns`` is documentation-only here (the real time check lives in
    :func:`attach_factors`), kept in the signature so a caller cannot "forget"
    which time axis the matrix is supposed to be cut on.
    """

    del decision_columns  # consumed by attach_factors; present for the API contract
    hits = find_leakage_columns(columns, extra_patterns=extra_patterns)
    if hits:
        raise LeakageError(
            "以下列含 T 日决策时点之后的信息，禁止进因子矩阵 "
            f"(issue #27 严格 PIT): {hits[:12]}"
            + (" ..." if len(hits) > 12 else "")
        )
    return tuple(str(c) for c in columns)


def _date_key(series: pd.Series) -> np.ndarray:
    """ISO ``YYYY-MM-DD`` -> sortable ``YYYYMMDD`` int (NaN-ish -> -1)."""

    text = series.astype("string")
    digits = text.str.replace("-", "", regex=False).str.strip()
    numbers = pd.to_numeric(digits, errors="coerce")
    return numbers.fillna(-1).to_numpy(dtype=np.int64)


def attach_factors(
    label_frame: pd.DataFrame,
    factor_frame: pd.DataFrame,
    *,
    features: Sequence[str],
    label_date: str = "trade_date",
    factor_date: str = "as_of_date",
    symbol: str = "symbol",
    prefix: str = "",
    strict_before: bool = False,
    audit: PitAudit | None = None,
) -> tuple[pd.DataFrame, PitAudit]:
    """Point-in-time as-of join of a factor table onto the label table.

    For each label row ``(symbol, T)`` the **latest** factor row with
    ``as_of_date <= T`` is used (``strict_before=True`` tightens it to ``< T``).
    Factor rows dated after their label row can never be matched, and any factor
    row dated beyond the last label date is dropped and counted in
    ``n_dropped_future_factor_rows``, so an accidentally-future-dated factor
    cannot silently improve the score.

    ``prefix`` namespaces the written columns (e.g. ``"mx_"``).  It is *not*
    cosmetic: #23 and #24 share column names (``open_times``, ``limit_times``,
    ``fd_amount``, ``float_mv``), and a silent overwrite there is exactly the
    "features.py 已把 ret_3d 用作回溯收益率" class of bug #23 documents.

    Missing feature cells stay ``NaN`` and get an ``<prefixed>_is_missing``
    indicator; they are never imputed with 0 here (imputation belongs to the
    estimator's train-fold feature space, never to the dataset).

    Same-day is allowed by default because #24
    (:func:`strategy.first_limit_alpha.microstructure_features.build_microstructure_features`)
    builds its ``as_of_date`` row from T and its own *backward* lookback only.
    """

    if label_date not in label_frame.columns:
        raise LeakageError(f"label frame missing date column {label_date!r}")
    if symbol not in label_frame.columns:
        raise LeakageError(f"label frame missing key column {symbol!r}")
    if factor_frame is None or len(factor_frame) == 0:
        raise LeakageError(
            "factor frame is empty: issue #24 因子必须回填后才能训练分层模型"
        )
    if factor_date not in factor_frame.columns:
        raise LeakageError(f"factor frame missing date column {factor_date!r}")
    unknown = [f for f in features if f not in factor_frame.columns]
    if unknown:
        raise LeakageError(f"declared features absent from factor frame: {unknown[:8]}")
    assert_pit_features(list(features))

    left = label_frame.reset_index(drop=True).copy()
    left["_pit_row"] = np.arange(len(left))
    left["_pit_key"] = _date_key(left[label_date])
    last_label_key = int(left["_pit_key"].max()) if len(left) else -1

    right = factor_frame[[symbol, factor_date, *features]].copy()
    right[symbol] = right[symbol].astype(str)
    right["_pit_key"] = _date_key(right[factor_date])
    dropped_future = int((right["_pit_key"] > last_label_key).sum())
    right = right.sort_values([symbol, "_pit_key"], kind="mergesort")

    payload_cols = [symbol, "_pit_key", "_pit_as_of", *features]
    right["_pit_as_of"] = right[factor_date].astype(str)
    left[symbol] = left[symbol].astype(str)

    matched: list[pd.DataFrame] = []
    for key, group in left.sort_values("_pit_key", kind="mergesort").groupby(
        symbol, sort=False
    ):
        side = right.loc[right[symbol] == str(key)]
        if side.empty:
            continue
        joined = pd.merge_asof(
            group.sort_values("_pit_key", kind="mergesort"),
            side.sort_values("_pit_key", kind="mergesort"),
            on="_pit_key",
            direction="backward",
            suffixes=("", "_factor"),
        )
        matched.append(joined)

    out = label_frame.reset_index(drop=True).copy()
    values = {f: np.full(len(out), math.nan) for f in features}
    as_of = np.array([""] * len(out), dtype=object)
    lag = np.full(len(out), math.nan)
    if matched:
        stacked = pd.concat(matched, ignore_index=True)
        rows = stacked["_pit_row"].to_numpy(dtype=int)
        for feature in features:
            column = feature if feature in stacked.columns else f"{feature}_factor"
            values[feature] = np.where(
                pd.isna(stacked[column]).to_numpy(),
                math.nan,
                pd.to_numeric(stacked[column], errors="coerce").to_numpy(dtype=float),
            )
        as_of_stacked = stacked["_pit_as_of"].astype("string").fillna("").to_numpy()
        as_of[rows] = as_of_stacked
        key_stacked = stacked["_pit_key"].to_numpy(dtype=np.int64)
        label_key = out.assign(_k=_date_key(out[label_date]))["_k"].to_numpy()
        lag[rows] = (label_key[rows] - key_stacked).astype(float)
    else:
        rows = np.asarray([], dtype=int)

    for feature in features:
        out[f"{prefix}{feature}"] = _scatter(rows, values[feature], len(out))
    indicators: list[str] = []
    for feature in features:
        ind = f"{prefix}{feature}{FACTOR_MISSING_SUFFIX}"
        out[ind] = ~np.isfinite(out[f"{prefix}{feature}"].to_numpy(dtype=float))
        indicators.append(ind)
    out["factor_as_of_date"] = as_of
    out["factor_lag_days"] = lag

    if strict_before:
        # Recompute with "< T": drop every match whose factor date equals T, i.e.
        # keep only rows where the previous available factor date exists.
        same_day = np.asarray(
            [
                bool(a) and str(a) == str(t)
                for a, t in zip(as_of, out[label_date].to_numpy())
            ]
        )
        bad = np.asarray(rows, dtype=int)[same_day[rows]] if len(rows) else rows
        for feature in features:
            column = f"{prefix}{feature}"
            current = out[column].to_numpy(dtype=float)
            current[bad] = math.nan
            out[column] = current
        for feature in features:
            column = f"{prefix}{feature}"
            out[f"{column}{FACTOR_MISSING_SUFFIX}"] = ~np.isfinite(
                out[column].to_numpy(dtype=float)
            )

    result = audit or PitAudit()
    first_feature = f"{prefix}{features[0]}" if features else None
    result.n_label_rows = int(len(out))
    result.n_factor_rows = int(len(factor_frame))
    result.n_matched = (
        int(np.isfinite(out[first_feature].to_numpy(dtype=float)).sum())
        if first_feature
        else 0
    )
    result.n_dropped_future_factor_rows = dropped_future
    result.n_unmatched_label_rows = int(len(out) - result.n_matched)
    result.feature_columns = tuple(f"{prefix}{f}" for f in features)
    result.missing_indicator_columns = tuple(indicators)
    all_dates = factor_frame[factor_date].astype(str)
    result.factor_as_of_min = str(all_dates.min()) if len(all_dates) else None
    result.factor_as_of_max = str(all_dates.max()) if len(all_dates) else None
    return out, result


def _scatter(rows: np.ndarray, values: np.ndarray, size: int) -> np.ndarray:
    """Write ``values`` at positional ``rows`` into a fresh ``size`` array.

    ``rows`` comes from a per-symbol ``groupby`` so it is *not* monotonic in the
    destination frame; a plain ``out[rows] = values`` is still correct because
    the stacked order matches the row order.
    """

    out = np.full(size, math.nan)
    if len(rows) == 0:
        return out
    if len(values) != len(rows):
        raise LeakageError(
            f"_scatter length mismatch: rows={len(rows)} values={len(values)}"
        )
    out[np.asarray(rows, dtype=int)] = np.asarray(values, dtype=float)
    return out


def select_factor_features(
    frame: pd.DataFrame,
    *,
    candidate_columns: Sequence[str] | None = None,
    exclude_missing_indicators: bool = True,
) -> tuple[str, ...]:
    """Pick the numeric, non-leaking feature columns of an assembled frame."""

    pool = list(candidate_columns) if candidate_columns else list(frame.columns)
    if exclude_missing_indicators:
        pool = [c for c in pool if not str(c).endswith(FACTOR_MISSING_SUFFIX)]
    numeric = [
        c
        for c in pool
        if c in frame.columns and pd.api.types.is_numeric_dtype(frame[c])
    ]
    return assert_pit_features(numeric)


def is_missing_column(column: str) -> bool:
    return str(column).endswith(FACTOR_MISSING_SUFFIX)


def missing_ratio_report(frame: pd.DataFrame, features: Sequence[str]) -> dict[str, float]:
    """Per-feature NaN share; the "缺数显式缺失" audit the issue asks for."""

    return {
        str(f): round(
            float(np.mean(~np.isfinite(frame[f].to_numpy(dtype=float)))), 6
        )
        for f in features
        if f in frame.columns
    }


__all__ = [
    "FACTOR_MISSING_SUFFIX",
    "LEAKAGE_FORBIDDEN_EXACT",
    "LEAKAGE_FORBIDDEN_PATTERNS",
    "MISSING_SUFFIX",
    "PitAudit",
    "LeakageError",
    "assert_pit_features",
    "attach_factors",
    "find_leakage_columns",
    "is_missing_column",
    "left_date_max",
    "missing_ratio_report",
    "select_factor_features",
]
