"""Cost-aware TopK portfolio view of a fold's predictions (issue #36).

What this module does *and does not* do about tradability
--------------------------------------------------------
#26 (limit-up aware backtest) owns the ground truth for "一字板买不到 / 跌停卖不出".
This scaffold only **consumes** the two boolean flags ``can_buy`` / ``can_sell``
plus the gross realised return produced upstream, and it makes the missing case
loud rather than silent:

* rows flagged ``can_buy == False`` are dropped from *selection* (they can never
  be picked) and are counted in ``excluded_not_buyable``;
* rows flagged ``can_sell == False`` are additionally dropped because a position
  that cannot be exited cannot enter a horizon-bounded comparison;
* when the panel has no executability columns at all, ``executability_source`` is
  ``"assumed"`` and :class:`strategy.model_validation.gate.GateConfig`'s
  ``require_executability`` check blocks any ``keep`` decision.

Survivorship bias: the sample universe must already be delisting-safe (#22/#23).
Here we report ``coverage_ratio`` (selected / eligible) and the number of dates
with zero eligible candidates so an artificially "clean" universe is visible in
the report instead of quietly inflating the metrics.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .contracts import Panel
from .metrics import max_drawdown, one_sided_turnover


@dataclass(frozen=True, slots=True)
class PortfolioConfig:
    top_k: int = 5
    fee_bps: float = 8.0  # round-trip commission in basis points
    slippage_bps: float = 10.0
    cost_grid_bps: tuple[float, ...] = (0.0, 5.0, 10.0, 20.0, 30.0, 50.0)

    def as_dict(self) -> dict[str, object]:
        return {
            "top_k": self.top_k,
            "fee_bps": self.fee_bps,
            "slippage_bps": self.slippage_bps,
            "base_cost_bps": self.fee_bps + self.slippage_bps,
            "cost_grid_bps": list(self.cost_grid_bps),
        }

    def cost_fraction(self, total_bps: float | None = None) -> float:
        bps = (
            self.fee_bps + self.slippage_bps if total_bps is None else float(total_bps)
        )
        return bps / 10_000.0


@dataclass(slots=True)
class PortfolioResult:
    n_dates: int = 0
    n_selected: int = 0
    n_candidates: int = 0
    n_eligible: int = 0
    excluded_not_buyable: int = 0
    excluded_not_sellable: int = 0
    dates_without_eligible: int = 0
    coverage_ratio: float | None = None
    executability_source: str = "unknown"
    gross_mean: float | None = None
    net_mean: float | None = None
    hit_rate: float | None = None
    cum_net_return: float | None = None
    max_drawdown: float | None = None
    turnover: float | None = None
    next_day_premium_mean: float | None = None
    cost_sensitivity: dict[str, float | None] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "n_dates": self.n_dates,
            "n_selected": self.n_selected,
            "n_candidates": self.n_candidates,
            "n_eligible": self.n_eligible,
            "excluded_not_buyable": self.excluded_not_buyable,
            "excluded_not_sellable": self.excluded_not_sellable,
            "dates_without_eligible": self.dates_without_eligible,
            "coverage_ratio": self.coverage_ratio,
            "executability_source": self.executability_source,
            "gross_mean": self.gross_mean,
            "net_mean": self.net_mean,
            "hit_rate": self.hit_rate,
            "cum_net_return": self.cum_net_return,
            "max_drawdown": self.max_drawdown,
            "turnover": self.turnover,
            "next_day_premium_mean": self.next_day_premium_mean,
            "cost_sensitivity": dict(self.cost_sensitivity),
        }


def _panel_slice(panel: Panel, rows: np.ndarray) -> pd.DataFrame:
    columns = panel.columns
    frame = panel.frame.loc[rows]
    out = pd.DataFrame(
        {
            "event_date": frame[columns.event_date].to_numpy(dtype=str),
            "symbol": frame[columns.symbol].to_numpy(dtype=str),
            "gross_return": panel.gross_returns()[rows],
            "can_buy": frame[columns.can_buy].to_numpy(dtype=bool),
            "can_sell": frame[columns.can_sell].to_numpy(dtype=bool),
        }
    )
    if columns.next_day_premium in frame.columns:
        out["premium"] = pd.to_numeric(
            frame[columns.next_day_premium], errors="coerce"
        ).to_numpy(dtype=float)
    else:
        out["premium"] = np.nan
    return out


def _cum_net_return(daily: np.ndarray) -> float | None:
    if daily.size == 0:
        return None
    return float(np.prod(1.0 + daily) - 1.0)


def run_portfolio(
    panel: Panel, rows: np.ndarray, scores: np.ndarray, config: PortfolioConfig
) -> PortfolioResult:
    """Select the daily TopK basket and translate it into cost-aware returns."""

    scores = np.asarray(scores, dtype=np.float64)
    table = _panel_slice(panel, rows)
    table["score"] = scores
    source = "upstream" if panel.has_executability() else "assumed"

    has_return = np.isfinite(table["gross_return"].to_numpy(dtype=float))
    can_buy = table["can_buy"].to_numpy(dtype=bool)
    can_sell = table["can_sell"].to_numpy(dtype=bool)

    eligible = table[has_return & can_buy & can_sell].copy()
    result = PortfolioResult(
        n_dates=int(table["event_date"].nunique()),
        n_candidates=int(table.shape[0]),
        n_eligible=int(eligible.shape[0]),
        excluded_not_buyable=int((~can_buy).sum()),
        excluded_not_sellable=int((can_buy & ~can_sell).sum()),
        executability_source=source,
    )

    cost = config.cost_fraction()
    selections: list[list[str]] = []
    daily_net: list[float] = []
    per_cost_daily: dict[float, list[float]] = {bps: [] for bps in config.cost_grid_bps}
    net_pick_returns: list[float] = []
    gross_pick_returns: list[float] = []
    premium_values: list[float] = []
    dates_without = 0

    for date, group in eligible.groupby("event_date", sort=True):
        if config.top_k <= 0:
            break
        picked = group.sort_values("score", ascending=False, kind="mergesort").head(
            config.top_k
        )
        if picked.empty:  # pragma: no cover - defensive
            dates_without += 1
            continue
        picks = picked["gross_return"].to_numpy(dtype=float)
        selections.append([str(s) for s in picked["symbol"].to_numpy()])
        net = picks - cost
        net_pick_returns.extend(net.tolist())
        gross_pick_returns.extend(picks.tolist())
        daily_net.append(float(net.mean()))
        for bps in config.cost_grid_bps:
            per_cost_daily[float(bps)].append(
                float((picks - config.cost_fraction(bps)).mean())
            )
        prem = picked["premium"].to_numpy(dtype=float)
        premium_values.extend([float(v) for v in prem if np.isfinite(v)])

    eligible_dates = int(eligible["event_date"].nunique())
    result.dates_without_eligible = max(0, result.n_dates - eligible_dates)
    result.n_selected = len(net_pick_returns)
    result.coverage_ratio = (
        None
        if not has_return.any()
        else round(result.n_eligible / max(1, int(has_return.sum())), 6)
    )

    if not net_pick_returns:
        result.cost_sensitivity = {f"{bps:.1f}": None for bps in config.cost_grid_bps}
        return result

    daily_arr = np.asarray(daily_net, dtype=np.float64)
    net_arr = np.asarray(net_pick_returns, dtype=np.float64)
    gross_arr = np.asarray(gross_pick_returns, dtype=np.float64)
    result.gross_mean = round(float(gross_arr.mean()), 8)
    result.net_mean = round(float(net_arr.mean()), 8)
    result.hit_rate = round(float((net_arr > 0).mean()), 8)
    result.cum_net_return = round(float(_cum_net_return(daily_arr) or 0.0), 8)
    result.max_drawdown = round(float(max_drawdown(daily_arr) or 0.0), 8)
    result.turnover = (
        None if (t := one_sided_turnover(selections)) is None else round(t, 6)
    )
    result.next_day_premium_mean = (
        None if not premium_values else round(float(np.mean(premium_values)), 8)
    )
    for bps in config.cost_grid_bps:
        series = np.asarray(per_cost_daily[float(bps)], dtype=np.float64)
        cum = _cum_net_return(series)
        result.cost_sensitivity[f"{float(bps):.1f}"] = (
            None if cum is None else round(cum, 8)
        )
    return result
