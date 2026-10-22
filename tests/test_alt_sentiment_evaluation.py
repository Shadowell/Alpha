"""另类因子离线评估单测（issue #33）：PIT 泄漏拦截、IC、分层、相关性。"""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from app.services.alt_sentiment.evaluation import (
    FutureDataError,
    attach_forward_returns,
    cross_factor_correlation,
    enforce_point_in_time,
    evaluate_alt_factors,
    factor_ic_report,
    quantile_layer_report,
    research_note_markdown,
    rows_to_frame,
    walk_forward_folds,
)
from app.services.alt_sentiment.factors import FACTOR_NAMES, UNAVAILABLE_FACTORS
from app.services.alt_sentiment.records import AltSentimentDaily, snapshot_visible_time

DATES = [f"2026-03-{day:02d}" for day in range(2, 14)]


def _synthetic_frame(
    *,
    symbols: int = 24,
    dates: list[str] | None = None,
    informative: bool = True,
    shift_hours: float = 0.0,
    missing_share: float = 0.0,
    seed: int = 7,
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    days = dates or DATES
    rows: list[dict[str, object]] = []
    noise = rng.normal(0.0, 0.02, size=(len(days), symbols))
    for day_index, day in enumerate(days):
        as_of = snapshot_visible_time(day) + timedelta(hours=shift_hours)
        for index in range(symbols):
            symbol = f"{600000 + index}"
            signal = float(rng.normal(0.0, 1.0))
            values = {name: np.nan for name in FACTOR_NAMES}
            values["attention_index"] = 100.0 + signal * 10.0
            values["attention_score"] = 3.0 + signal * 0.2
            values["alt_attention_score"] = signal
            ret = float(noise[day_index, index])
            if informative:
                ret += signal * 0.004
            if missing_share and rng.random() < missing_share:
                values["alt_attention_score"] = np.nan
            rows.append(
                {
                    "symbol": symbol,
                    "trade_date": day,
                    "as_of_time": as_of,
                    "sources": "eastmoney_attention",
                    **values,
                    "fwd_ret_1d": ret,
                    "ret_1d": -ret * 0.5,
                    "amount_z": signal * 0.6 + rng.normal(0.0, 0.5),
                }
            )
    return pd.DataFrame.from_records(rows)


def test_rows_to_frame_keeps_missing_as_nan() -> None:
    as_of = snapshot_visible_time("2026-03-06")
    row = AltSentimentDaily(
        symbol="600000",
        trade_date="2026-03-06",
        as_of_time=as_of,
        values={name: None for name in FACTOR_NAMES},
    )
    row.values["attention_index"] = 12.0
    frame = rows_to_frame([row])
    assert pd.isna(frame["alt_attention_score"].iloc[0])
    assert float(frame["attention_index"].iloc[0]) == pytest.approx(12.0)


def test_attach_forward_returns_leaves_unknown_as_nan() -> None:
    frame = _synthetic_frame(symbols=3, dates=["2026-03-06"])
    mapping = {("600000", "2026-03-06"): 0.03}
    joined = attach_forward_returns(frame, mapping, column="fwd_ret_3d")
    assert joined["fwd_ret_3d"].notna().sum() == 1
    assert joined["fwd_ret_3d"].isna().sum() == 2


def test_enforce_point_in_time_drops_leaked_rows() -> None:
    frame = _synthetic_frame(symbols=6, dates=["2026-03-06"])
    ok = enforce_point_in_time(frame, label_column="fwd_ret_1d")
    assert len(ok) == 6  # as_of=15:30 < 次日 09:30

    leaked = _synthetic_frame(symbols=6, dates=["2026-03-06"], shift_hours=24.0)
    with pytest.raises(FutureDataError):
        enforce_point_in_time(leaked, label_column="fwd_ret_1d")


def test_enforce_point_in_time_rejects_missing_columns() -> None:
    frame = _synthetic_frame(symbols=4, dates=["2026-03-06"]).drop(
        columns=["as_of_time"]
    )
    with pytest.raises(FutureDataError):
        enforce_point_in_time(frame, label_column="fwd_ret_1d")


def test_ic_report_detects_signal() -> None:
    frame = _synthetic_frame(informative=True)
    report = factor_ic_report(
        frame, factor="alt_attention_score", label_column="fwd_ret_1d"
    )
    assert report.verdict == "pass"
    assert report.ic_mean is not None and report.ic_mean > 0.1
    assert report.icir is not None and report.icir > 0.25
    assert report.periods >= 8
    assert report.coverage == pytest.approx(1.0)


def test_ic_report_rejects_uninformative_noise() -> None:
    frame = _synthetic_frame(informative=False, seed=11)
    report = factor_ic_report(
        frame, factor="alt_attention_score", label_column="fwd_ret_1d"
    )
    assert report.ic_mean is not None
    assert abs(report.ic_mean) < 0.02
    assert report.verdict == "no_increment"


def test_ic_report_low_coverage_is_flagged() -> None:
    frame = _synthetic_frame(informative=True, missing_share=0.8, seed=3)
    report = factor_ic_report(
        frame, factor="alt_attention_score", label_column="fwd_ret_1d"
    )
    assert report.coverage < 0.5
    assert report.verdict in {"low_coverage", "insufficient"}


def test_ic_report_never_fills_missing_with_zero() -> None:
    frame = _synthetic_frame(informative=True, missing_share=0.5, seed=5)
    before = frame["fwd_ret_1d"].mean()
    report = factor_ic_report(
        frame, factor="alt_attention_score", label_column="fwd_ret_1d"
    )
    assert report.observations == len(frame)
    assert frame["fwd_ret_1d"].mean() == pytest.approx(before)


def test_quantile_layers_are_monotone_for_signal() -> None:
    frame = _synthetic_frame(informative=True, symbols=50, seed=9)
    report = quantile_layer_report(
        frame, factor="alt_attention_score", label_column="fwd_ret_1d", layers=5
    )
    assert report.usable
    assert report.long_short_spread is not None and report.long_short_spread > 0
    assert report.monotonicity is not None and report.monotonicity > 0.5


def test_quantile_layers_report_unknown_factor_columns() -> None:
    frame = _synthetic_frame(symbols=20, seed=2)
    report = quantile_layer_report(
        frame, factor="search_index", label_column="fwd_ret_1d"
    )
    assert report.layer_returns[0] is None
    assert report.usable is False


def test_cross_factor_correlation_detects_collinearity() -> None:
    frame = _synthetic_frame(informative=True, symbols=30, seed=4)
    result = cross_factor_correlation(
        frame,
        factor="alt_attention_score",
        price_columns=["amount_z", "ret_1d", "missing_col"],
    )
    assert result["by_column"]["missing_col"] is None
    assert result["max_abs_corr"] is not None
    assert result["controlled"] is False  # amount_z 与信号共线
    assert result["collinear_with"] == "amount_z"


def test_walk_forward_folds_respect_embargo() -> None:
    folds = walk_forward_folds(DATES, train_days=5, test_days=2, embargo_days=1)
    assert folds
    for fold in folds:
        assert len(fold["train"]) == 5
        assert len(fold["test"]) == 2
        last_train = max(fold["train"])
        embargo_gap = (
            datetime.fromisoformat(min(fold["test"]))
            - datetime.fromisoformat(last_train)
        ).days
        assert embargo_gap >= 2  # embargo=1 天 + 至少 1 个测试日间隔
        assert set(fold["train"]).isdisjoint(fold["test"])


def test_walk_forward_folds_validates_args() -> None:
    with pytest.raises(ValueError):
        walk_forward_folds(DATES, train_days=0, test_days=2)


def test_evaluate_alt_factors_stops_when_no_increment() -> None:
    frame = _synthetic_frame(informative=False, seed=13)
    report = evaluate_alt_factors(
        frame,
        label_column="fwd_ret_1d",
        factors=("alt_attention_score",),
        price_columns=("ret_1d",),
    )
    assert report["decision"] == "stop_and_record"
    assert report["promoted_factors"] == []
    note = research_note_markdown(report)
    assert "另类因子离线验证结论" in note
    assert "不接入生产打分链路" in note


def test_evaluate_alt_factors_promotes_effective_factor() -> None:
    frame = _synthetic_frame(informative=True, symbols=50, seed=17)
    report = evaluate_alt_factors(
        frame,
        label_column="fwd_ret_1d",
        factors=("alt_attention_score",),
        price_columns=("ret_1d",),
    )
    assert report["decision"] == "adopt"
    assert "alt_attention_score" in report["promoted_factors"]
    assert "数据源评估报告" in report["source_assessment"]


def test_unavailable_factors_stay_in_registry() -> None:
    assert set(UNAVAILABLE_FACTORS).issubset(set(FACTOR_NAMES))
    frame = _synthetic_frame(symbols=10, dates=["2026-03-06"])
    for name in UNAVAILABLE_FACTORS:
        assert frame[name].isna().all()
