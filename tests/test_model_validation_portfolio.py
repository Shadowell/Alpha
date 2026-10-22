"""Tradability, cost sensitivity and turnover of the TopK portfolio (issue #36)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from strategy.model_validation.contracts import ColumnNames, Panel, validate_panel
from strategy.model_validation.portfolio import PortfolioConfig, run_portfolio


def _panel(rows: list[dict], *, with_executability: bool = True) -> Panel:
    frame = pd.DataFrame(rows)
    if not with_executability:
        frame = frame.drop(
            columns=[c for c in ("can_buy", "can_sell") if c in frame.columns]
        )
    return validate_panel(Panel(frame=frame, features=("f1",)), label_horizon=2)


def _row(
    symbol: str,
    date: str,
    ret: float,
    *,
    buyable: bool = True,
    sellable: bool = True,
    premium: float = 0.01,
) -> dict:
    return {
        "symbol": symbol,
        "event_date": date,
        "label": 1.0 if ret > 0 else 0.0,
        "f1": float(ret),
        "outcome_return": ret,
        "next_day_premium": premium,
        "can_buy": buyable,
        "can_sell": sellable,
    }


ALL_ROWS = np.arange(0, 1)


def test_topk_selection_picks_the_highest_scores_and_charges_costs() -> None:
    rows = [
        _row("000001", "2026-01-05", 0.05),
        _row("000002", "2026-01-05", -0.02),
        _row("000003", "2026-01-05", 0.03),
        _row("000004", "2026-01-06", 0.01),
        _row("000005", "2026-01-06", -0.04),
    ]
    panel = _panel(rows)
    scores = np.array([0.9, 0.1, 0.5, 0.7, 0.2])
    cfg = PortfolioConfig(top_k=1, fee_bps=8.0, slippage_bps=10.0)
    result = run_portfolio(panel, np.arange(len(rows)), scores, cfg)

    assert result.n_selected == 2  # one pick per date
    assert result.gross_mean == pytest.approx((0.05 + 0.01) / 2)
    assert result.net_mean == pytest.approx((0.05 + 0.01) / 2 - 0.0018)
    assert result.executability_source == "upstream"
    assert result.excluded_not_buyable == 0
    assert result.hit_rate == pytest.approx(1.0)


def test_one_word_limit_up_rows_can_never_be_selected() -> None:
    """一字板买不到: the best-looking row is untradeable and must be skipped."""

    rows = [
        _row("000001", "2026-01-05", 0.30, buyable=False),  # gap-up, cannot fill
        _row("000002", "2026-01-05", 0.04),
        _row("000003", "2026-01-05", -0.01),
    ]
    panel = _panel(rows)
    scores = np.array([1.0, 0.5, 0.1])  # the model loves the unbuyable row
    cfg = PortfolioConfig(top_k=2)
    result = run_portfolio(panel, np.arange(len(rows)), scores, cfg)

    assert result.excluded_not_buyable == 1
    assert result.n_eligible == 2
    assert result.n_selected == 2
    assert result.gross_mean == pytest.approx((0.04 - 0.01) / 2)
    assert result.coverage_ratio == pytest.approx(2 / 3)


def test_limit_down_exit_is_excluded_because_the_position_cannot_be_closed() -> None:
    rows = [
        _row("000001", "2026-01-05", 0.10, sellable=False),
        _row("000002", "2026-01-05", 0.02),
    ]
    panel = _panel(rows)
    result = run_portfolio(
        panel, np.arange(len(rows)), np.array([0.9, 0.1]), PortfolioConfig(top_k=2)
    )
    assert result.excluded_not_sellable == 1
    assert result.n_eligible == 1


def test_dates_without_any_eligible_candidate_are_counted_not_hidden() -> None:
    rows = [
        _row("000001", "2026-01-05", 0.10, buyable=False),
        _row("000002", "2026-01-05", 0.05, buyable=False),
        _row("000003", "2026-01-06", 0.02),
    ]
    panel = _panel(rows)
    result = run_portfolio(
        panel, np.arange(len(rows)), np.array([0.9, 0.8, 0.1]), PortfolioConfig(top_k=1)
    )
    assert result.n_dates == 2
    assert result.dates_without_eligible == 1
    assert result.n_selected == 1


def test_missing_executability_columns_are_flagged_as_assumed() -> None:
    rows = [_row("000001", "2026-01-05", 0.02), _row("000002", "2026-01-06", 0.03)]
    panel = _panel(rows, with_executability=False)
    result = run_portfolio(
        panel, np.arange(2), np.array([0.4, 0.6]), PortfolioConfig(top_k=1)
    )
    assert result.executability_source == "assumed"


def test_cost_sensitivity_degrades_monotonically_with_costs() -> None:
    rows = []
    scores = []
    for day in range(1, 7):
        for i, ret in enumerate([0.04, 0.02, -0.01]):
            rows.append(_row(f"{600000 + i:06d}", f"2026-01-{day:02d}", ret))
            scores.append(1.0 - 0.1 * i)
    panel = _panel(rows)
    cfg = PortfolioConfig(
        top_k=2, fee_bps=8.0, slippage_bps=10.0, cost_grid_bps=(0.0, 10.0, 30.0, 80.0)
    )
    result = run_portfolio(panel, np.arange(len(rows)), np.asarray(scores), cfg)

    values = [result.cost_sensitivity[key] for key in ("0.0", "10.0", "30.0", "80.0")]
    assert all(v is not None for v in values)
    assert values == sorted(values, reverse=True), (
        "cumulative net return must fall as costs rise"
    )
    zero = result.cost_sensitivity["0.0"]
    eighty = result.cost_sensitivity["80.0"]
    assert eighty < zero - 0.05  # 6 dates * ~0.8% per date drag


def test_turnover_is_zero_for_a_stable_basket_and_high_for_a_churning_one() -> None:
    def build(order: list[str]) -> tuple[Panel, np.ndarray]:
        rows = [
            _row("000001", "2026-01-05", 0.01),
            _row("000002", "2026-01-05", 0.02),
            _row("000001", "2026-01-06", 0.01),
            _row("000002", "2026-01-06", 0.02),
        ]
        panel = _panel(rows)
        scores = (
            np.array([0.2, 0.9, 0.2, 0.9])
            if order == ["000002"]
            else np.array([0.9, 0.2, 0.2, 0.9])
        )
        return panel, scores

    stable_panel, stable_scores = build(["000002"])
    stable = run_portfolio(
        stable_panel, np.arange(4), stable_scores, PortfolioConfig(top_k=1)
    )
    churn_panel, churn_scores = build(["000001"])
    churn = run_portfolio(
        churn_panel, np.arange(4), churn_scores, PortfolioConfig(top_k=1)
    )

    assert stable.turnover == pytest.approx(0.0)
    # one-sided turnover = |today \ yesterday| / |union| -> a full swap of a 1-name
    # basket is 1/2, not 1; the definition is documented in metrics.one_sided_turnover
    assert churn.turnover == pytest.approx(0.5)


def test_next_day_premium_is_reported_for_selected_names() -> None:
    rows = [
        _row("000001", "2026-01-05", 0.02, premium=0.011),
        _row("000002", "2026-01-05", 0.01, premium=0.002),
    ]
    panel = _panel(rows)
    result = run_portfolio(
        panel, np.arange(2), np.array([0.9, 0.1]), PortfolioConfig(top_k=1)
    )
    assert result.next_day_premium_mean == pytest.approx(0.011)


def test_non_finite_returns_are_dropped_before_selection() -> None:
    rows = [
        _row("000001", "2026-01-05", float("nan")),
        _row("000002", "2026-01-05", 0.02),
    ]
    panel = _panel(rows)
    result = run_portfolio(
        panel, np.arange(2), np.array([0.9, 0.1]), PortfolioConfig(top_k=1)
    )
    assert result.n_eligible == 1
    assert result.n_selected == 1


def test_custom_column_names_are_respected() -> None:
    frame = pd.DataFrame(
        {
            "code": ["000001", "000002"],
            "signal_date": ["2026-01-05", "2026-01-05"],
            "target": [1.0, 0.0],
            "f1": [0.5, 0.1],
            "exec_gross_return": [0.03, -0.01],
        }
    )
    columns = ColumnNames(
        symbol="code",
        event_date="signal_date",
        label="target",
        gross_return="exec_gross_return",
    )
    panel = validate_panel(
        Panel(frame=frame, features=("f1",), columns=columns), label_horizon=2
    )
    result = run_portfolio(
        panel, np.arange(2), np.array([0.9, 0.2]), PortfolioConfig(top_k=1)
    )
    assert result.n_selected == 1
    assert result.gross_mean == pytest.approx(0.03)
