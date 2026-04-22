from __future__ import annotations

from typing import Any

import pandas as pd

from .schema import LabelConfig


def compute_sample_labels(frame: pd.DataFrame, idx: int, label_cfg: LabelConfig) -> dict[str, Any] | None:
    max_needed = max(
        label_cfg.continuation_horizon,
        label_cfg.strong_horizon,
        label_cfg.break_horizon,
        label_cfg.evaluation_horizon,
    )
    future = frame.iloc[idx + 1 : idx + 1 + max_needed].copy()
    if len(future) < max_needed:
        return None

    current = frame.iloc[idx]
    entry = future.iloc[0]
    entry_open = float(entry["open"])
    if entry_open <= 0:
        return None

    cont_window = future.head(label_cfg.continuation_horizon)
    strong_window = future.head(label_cfg.strong_horizon)
    break_window = future.head(label_cfg.break_horizon)
    eval_window = future.head(label_cfg.evaluation_horizon)

    max_high_3d = float(strong_window["high"].max())
    min_low_2d = float(break_window["low"].min())
    close_3d = float(strong_window.iloc[-1]["close"])
    close_5d = float(eval_window.iloc[-1]["close"])
    ret_high_3d = (max_high_3d / entry_open) - 1.0
    ret_close_3d = (close_3d / entry_open) - 1.0
    ret_close_5d = (close_5d / entry_open) - 1.0
    min_low_2d_ret = (min_low_2d / entry_open) - 1.0
    next_2d_limit_up = int(bool(cont_window["is_limit_up"].astype(bool).any()))
    day1_close_ret = (float(entry["close"]) / entry_open) - 1.0
    break_risk = int(
        min_low_2d_ret <= label_cfg.break_drawdown_threshold
        or day1_close_ret < -0.01
        or float(entry["close"]) < float(current["close"])
    )

    payload: dict[str, Any] = {
        "entry_date": str(entry["trade_date"]),
        "entry_open": round(entry_open, 6),
        "label_continuation": next_2d_limit_up,
        "label_strong_3d": int(ret_high_3d >= label_cfg.strong_threshold),
        "label_break_risk": break_risk,
        "future_max_high_3d": round(max_high_3d, 6),
        "future_min_low_2d": round(min_low_2d, 6),
        "future_close_3d": round(close_3d, 6),
        "future_close_5d": round(close_5d, 6),
        "ret_high_3d": round(ret_high_3d, 6),
        "ret_close_3d": round(ret_close_3d, 6),
        "ret_close_5d": round(ret_close_5d, 6),
        "ret_min_low_2d": round(min_low_2d_ret, 6),
        "future_limit_up_count_5d": int(eval_window["is_limit_up"].astype(bool).sum()),
    }
    for offset in range(1, label_cfg.evaluation_horizon + 1):
        row = future.iloc[offset - 1]
        for field in ("open", "high", "low", "close"):
            payload[f"d{offset}_{field}"] = round(float(row[field]), 6)
    return payload
