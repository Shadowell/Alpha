from __future__ import annotations

from dataclasses import asdict
from typing import Any

import pandas as pd

from .schema import BacktestConfig


class FirstLimitBacktester:
    def run(self, predictions: pd.DataFrame, config: BacktestConfig | None = None) -> dict[str, Any]:
        cfg = config or BacktestConfig()
        if predictions.empty:
            return {
                "config": asdict(cfg),
                "summary": {"trade_count": 0, "win_rate": 0.0, "avg_return": 0.0, "cum_return": 0.0, "max_drawdown": 0.0},
                "trades": [],
                "equity_curve": [],
            }

        candidates = predictions[predictions["first_limit_score"] >= cfg.score_threshold].copy()
        if candidates.empty:
            return {
                "config": asdict(cfg),
                "summary": {"trade_count": 0, "win_rate": 0.0, "avg_return": 0.0, "cum_return": 0.0, "max_drawdown": 0.0},
                "trades": [],
                "equity_curve": [],
            }

        candidates = candidates.sort_values(["trade_date", "first_limit_score"], ascending=[True, False])
        picks = candidates.groupby("trade_date", sort=True).head(cfg.top_k).copy()
        trades: list[dict[str, Any]] = []
        for _, row in picks.iterrows():
            entry = float(row["entry_open"])
            if entry <= 0:
                continue
            exit_ret = None
            exit_day = cfg.hold_days
            exit_reason = f"hold_{cfg.hold_days}d"
            for day in range(1, cfg.hold_days + 1):
                high = float(row.get(f"d{day}_high", entry))
                low = float(row.get(f"d{day}_low", entry))
                close = float(row.get(f"d{day}_close", entry))
                if high / entry - 1.0 >= cfg.take_profit:
                    exit_ret = cfg.take_profit
                    exit_day = day
                    exit_reason = "take_profit"
                    break
                if low / entry - 1.0 <= cfg.stop_loss:
                    exit_ret = cfg.stop_loss
                    exit_day = day
                    exit_reason = "stop_loss"
                    break
                if day == cfg.hold_days:
                    exit_ret = close / entry - 1.0
            if exit_ret is None:
                continue
            cost = (cfg.fee_bps + cfg.slippage_bps) / 10_000.0
            net_ret = exit_ret - cost
            trades.append(
                {
                    "trade_date": row["trade_date"],
                    "symbol": row["symbol"],
                    "name": row.get("name", ""),
                    "score": round(float(row["first_limit_score"]), 4),
                    "entry_open": round(entry, 6),
                    "exit_day": exit_day,
                    "exit_reason": exit_reason,
                    "gross_return": round(exit_ret, 6),
                    "net_return": round(net_ret, 6),
                }
            )
        trades_df = pd.DataFrame(trades)
        if trades_df.empty:
            return {
                "config": asdict(cfg),
                "summary": {"trade_count": 0, "win_rate": 0.0, "avg_return": 0.0, "cum_return": 0.0, "max_drawdown": 0.0},
                "trades": [],
                "equity_curve": [],
            }
        daily = trades_df.groupby("trade_date", sort=True)["net_return"].mean().reset_index()
        daily["equity"] = (1.0 + daily["net_return"]).cumprod()
        daily["peak"] = daily["equity"].cummax()
        daily["drawdown"] = daily["equity"] / daily["peak"] - 1.0
        summary = {
            "trade_count": int(len(trades_df)),
            "win_rate": round(float((trades_df["net_return"] > 0).mean()), 4),
            "avg_return": round(float(trades_df["net_return"].mean()), 6),
            "cum_return": round(float(daily["equity"].iloc[-1] - 1.0), 6),
            "max_drawdown": round(float(daily["drawdown"].min()), 6),
        }
        return {
            "config": asdict(cfg),
            "summary": summary,
            "trades": trades_df.to_dict(orient="records"),
            "equity_curve": daily[["trade_date", "equity", "drawdown"]].to_dict(orient="records"),
        }
