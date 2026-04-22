from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .feature_store import ensure_dir, write_dataframe, write_json
from .schema import FeatureConfig


def _read_kline(db_path: str | Path) -> pd.DataFrame:
    conn = sqlite3.connect(str(db_path))
    frame = pd.read_sql_query(
        """
        SELECT symbol, trade_date, open, high, low, close, volume, amount
        FROM kline_daily
        ORDER BY symbol ASC, trade_date ASC
        """,
        conn,
    )
    conn.close()
    if frame.empty:
        return frame
    for col in ["open", "high", "low", "close", "volume", "amount"]:
        frame[col] = pd.to_numeric(frame[col], errors="coerce").fillna(0.0)
    frame["trade_date"] = frame["trade_date"].astype(str)
    return frame


def _board_limit_pct(symbol: str) -> float:
    if symbol.startswith(("30", "68")):
        return 0.20
    return 0.10


def _prepare_market_context(frame: pd.DataFrame) -> pd.DataFrame:
    market = frame.copy()
    market["prev_close"] = market.groupby("symbol")["close"].shift(1)
    market["pct_change"] = (market["close"] / market["prev_close"]) - 1.0
    market["limit_pct"] = market["symbol"].astype(str).map(_board_limit_pct)
    market["is_limit_up"] = market["pct_change"] >= (market["limit_pct"] - 0.0025)
    grouped = market.groupby("trade_date", sort=True)
    ctx = grouped.agg(
        market_mean_return=("pct_change", "mean"),
        market_median_return=("pct_change", "median"),
        market_up_ratio=("pct_change", lambda s: float((s > 0).mean())),
        market_down_ratio=("pct_change", lambda s: float((s < 0).mean())),
        market_strong_ratio=("pct_change", lambda s: float((s >= 0.05).mean())),
        market_limit_up_ratio=("is_limit_up", lambda s: float(pd.Series(s).astype(bool).mean())),
        market_avg_amount=("amount", "mean"),
        market_symbol_count=("symbol", "count"),
    ).reset_index()
    ctx["market_adv_dec_ratio"] = ctx["market_up_ratio"] / ctx["market_down_ratio"].replace(0, np.nan)
    ctx["market_adv_dec_ratio"] = ctx["market_adv_dec_ratio"].replace([np.inf, -np.inf], np.nan).fillna(99.0)
    return ctx


def _build_symbol_feature_frame(group: pd.DataFrame) -> pd.DataFrame:
    frame = group.copy().reset_index(drop=True)
    frame["amount"] = np.where(frame["amount"] > 0, frame["amount"], frame["close"] * frame["volume"])
    frame["prev_close"] = frame["close"].shift(1)
    frame["pct_change"] = (frame["close"] / frame["prev_close"]) - 1.0
    frame["ret_1d_prev"] = frame["close"].shift(1) / frame["close"].shift(2) - 1.0
    for window in (3, 5, 10, 20):
        frame[f"ret_{window}d"] = frame["close"] / frame["close"].shift(window) - 1.0
        frame[f"amp_{window}d"] = frame["high"].rolling(window).max() / frame["low"].rolling(window).min() - 1.0
        frame[f"volatility_{window}d"] = frame["pct_change"].rolling(window).std()
    for window in (5, 10, 20, 60):
        frame[f"ma_{window}"] = frame["close"].rolling(window).mean()
        frame[f"avg_volume_{window}"] = frame["volume"].shift(1).rolling(window).mean()
        frame[f"avg_amount_{window}"] = frame["amount"].shift(1).rolling(window).mean()
    frame["close_vs_ma5"] = frame["close"] / frame["ma_5"] - 1.0
    frame["close_vs_ma10"] = frame["close"] / frame["ma_10"] - 1.0
    frame["close_vs_ma20"] = frame["close"] / frame["ma_20"] - 1.0
    frame["close_vs_ma60"] = frame["close"] / frame["ma_60"] - 1.0
    frame["ma5_vs_ma10"] = frame["ma_5"] / frame["ma_10"] - 1.0
    frame["ma10_vs_ma20"] = frame["ma_10"] / frame["ma_20"] - 1.0
    frame["ma20_slope_5d"] = frame["ma_20"] / frame["ma_20"].shift(5) - 1.0
    frame["distance_to_20d_high"] = frame["close"] / frame["high"].rolling(20).max() - 1.0
    frame["distance_to_20d_low"] = frame["close"] / frame["low"].rolling(20).min() - 1.0
    frame["distance_to_60d_high"] = frame["close"] / frame["high"].rolling(60).max() - 1.0
    frame["open_gap_pct"] = frame["open"] / frame["prev_close"] - 1.0
    frame["body_pct"] = (frame["close"] - frame["open"]) / frame["prev_close"]
    frame["upper_shadow_pct"] = (frame["high"] - frame[["open", "close"]].max(axis=1)) / frame["prev_close"]
    frame["lower_shadow_pct"] = (frame[["open", "close"]].min(axis=1) - frame["low"]) / frame["prev_close"]
    frame["intraday_range_pct"] = (frame["high"] - frame["low"]) / frame["prev_close"]
    frame["close_near_high"] = ((frame["high"] - frame["close"]) / frame["prev_close"] <= 0.003).astype(int)
    frame["one_word_limit"] = ((frame["open"] == frame["low"]) & (frame["low"] == frame["high"])).astype(int)
    frame["volume_ratio_5d"] = frame["volume"] / frame["avg_volume_5"]
    frame["volume_ratio_20d"] = frame["volume"] / frame["avg_volume_20"]
    frame["amount_ratio_5d"] = frame["amount"] / frame["avg_amount_5"]
    frame["amount_ratio_20d"] = frame["amount"] / frame["avg_amount_20"]
    frame["volume_cv_20d"] = frame["volume"].shift(1).rolling(20).std() / frame["volume"].shift(1).rolling(20).mean()
    frame["amount_cv_20d"] = frame["amount"].shift(1).rolling(20).std() / frame["amount"].shift(1).rolling(20).mean()
    frame["compression_ratio_20_60"] = frame["amp_20d"] / frame["amp_20d"].rolling(3).mean()
    frame["turnover_proxy_log"] = np.log1p(frame["amount"])
    frame["avg_amount_20d_log"] = np.log1p(frame["avg_amount_20"])
    frame["avg_volume_20d_log"] = np.log1p(frame["avg_volume_20"])
    frame["limit_quality"] = (frame["body_pct"].fillna(0.0) + frame["close_near_high"] * 0.02) - frame["upper_shadow_pct"].fillna(0.0)
    frame["interaction_compress_x_volume"] = frame["amp_20d"] * frame["volume_ratio_20d"]
    frame["interaction_trend_x_amount"] = frame["ret_10d"] * frame["amount_ratio_20d"]
    frame["prefix_00"] = frame["symbol"].astype(str).str.startswith("00").astype(int)
    frame["prefix_60"] = frame["symbol"].astype(str).str.startswith("60").astype(int)
    frame["price_bucket_mid"] = ((frame["close"] >= 10.0) & (frame["close"] <= 30.0)).astype(int)
    keep = [
        "symbol",
        "trade_date",
        "ret_1d_prev",
        "ret_3d",
        "ret_5d",
        "ret_10d",
        "ret_20d",
        "amp_3d",
        "amp_5d",
        "amp_10d",
        "amp_20d",
        "volatility_3d",
        "volatility_5d",
        "volatility_10d",
        "volatility_20d",
        "close_vs_ma5",
        "close_vs_ma10",
        "close_vs_ma20",
        "close_vs_ma60",
        "ma5_vs_ma10",
        "ma10_vs_ma20",
        "ma20_slope_5d",
        "distance_to_20d_high",
        "distance_to_20d_low",
        "distance_to_60d_high",
        "open_gap_pct",
        "body_pct",
        "upper_shadow_pct",
        "lower_shadow_pct",
        "intraday_range_pct",
        "close_near_high",
        "one_word_limit",
        "volume_ratio_5d",
        "volume_ratio_20d",
        "amount_ratio_5d",
        "amount_ratio_20d",
        "volume_cv_20d",
        "amount_cv_20d",
        "compression_ratio_20_60",
        "turnover_proxy_log",
        "avg_amount_20d_log",
        "avg_volume_20d_log",
        "limit_quality",
        "interaction_compress_x_volume",
        "interaction_trend_x_amount",
        "prefix_00",
        "prefix_60",
        "price_bucket_mid",
    ]
    return frame[keep]


class FirstLimitFeatureBuilder:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    def transform_samples(
        self,
        samples: pd.DataFrame,
        feature_cfg: FeatureConfig | None = None,
    ) -> tuple[pd.DataFrame, dict[str, Any]]:
        feature_cfg = feature_cfg or FeatureConfig()
        if samples.empty:
            return pd.DataFrame(), {"feature_count": 0, "feature_config": feature_cfg.to_dict()}

        samples = samples.copy()
        samples["symbol"] = samples["symbol"].astype(str)
        samples["trade_date"] = samples["trade_date"].astype(str)
        kline = _read_kline(self.db_path)
        if kline.empty:
            return samples.copy(), {"feature_count": 0, "feature_config": feature_cfg.to_dict()}

        symbol_features = []
        symbols = set(samples["symbol"].astype(str).tolist())
        for symbol, group in kline[kline["symbol"].astype(str).isin(symbols)].groupby("symbol", sort=False):
            symbol_features.append(_build_symbol_feature_frame(group))
        feature_frame = pd.concat(symbol_features, ignore_index=True) if symbol_features else pd.DataFrame()
        merged = samples.merge(feature_frame, on=["symbol", "trade_date"], how="left")
        if feature_cfg.include_market_features:
            market_ctx = _prepare_market_context(kline)
            merged = merged.merge(market_ctx, on="trade_date", how="left")
            merged["interaction_market_x_limit"] = merged["market_limit_up_ratio"] * merged["limit_quality"]
        else:
            merged["interaction_market_x_limit"] = np.nan

        merged = merged.replace([np.inf, -np.inf], np.nan)
        feature_cols = [
            col for col in merged.columns
            if col
            not in {
                "symbol",
                "name",
                "trade_date",
                "sample_type",
                "entry_date",
            }
        ]
        for col in feature_cols:
            if merged[col].dtype.kind in {"f", "i"}:
                merged[col] = pd.to_numeric(merged[col], errors="coerce").fillna(feature_cfg.fill_value)

        meta = {
            "feature_count": int(
                len(
                    [
                        col for col in merged.columns
                        if col not in {
                            "symbol",
                            "name",
                            "trade_date",
                            "sample_type",
                            "entry_date",
                            "label_continuation",
                            "label_strong_3d",
                            "label_break_risk",
                            "entry_open",
                            "future_max_high_3d",
                            "future_min_low_2d",
                            "future_close_3d",
                            "future_close_5d",
                            "ret_high_3d",
                            "ret_close_3d",
                            "ret_close_5d",
                            "ret_min_low_2d",
                            "future_limit_up_count_5d",
                            *[f"d{day}_{field}" for day in range(1, 6) for field in ("open", "high", "low", "close")],
                        }
                    ]
                )
            ),
            "feature_config": feature_cfg.to_dict(),
            "feature_groups": {
                "price_behavior": [
                    "ret_3d",
                    "ret_5d",
                    "ret_10d",
                    "ret_20d",
                    "amp_10d",
                    "amp_20d",
                    "volatility_10d",
                    "volatility_20d",
                    "distance_to_20d_high",
                    "distance_to_60d_high",
                ],
                "volume_liquidity": [
                    "volume_ratio_5d",
                    "volume_ratio_20d",
                    "amount_ratio_5d",
                    "amount_ratio_20d",
                    "volume_cv_20d",
                    "amount_cv_20d",
                    "avg_amount_20d_log",
                    "avg_volume_20d_log",
                ],
                "candle_quality": [
                    "open_gap_pct",
                    "body_pct",
                    "upper_shadow_pct",
                    "lower_shadow_pct",
                    "intraday_range_pct",
                    "close_near_high",
                    "one_word_limit",
                    "limit_quality",
                ],
                "market_context": [
                    "market_mean_return",
                    "market_median_return",
                    "market_up_ratio",
                    "market_down_ratio",
                    "market_strong_ratio",
                    "market_limit_up_ratio",
                    "market_adv_dec_ratio",
                ],
                "interactions": [
                    "interaction_compress_x_volume",
                    "interaction_trend_x_amount",
                    "interaction_market_x_limit",
                ],
            },
        }
        return merged, meta

    def build_features(
        self,
        samples: pd.DataFrame,
        output_dir: str | Path,
        feature_cfg: FeatureConfig | None = None,
    ) -> dict[str, Any]:
        out_dir = ensure_dir(Path(output_dir))
        merged, meta = self.transform_samples(samples, feature_cfg=feature_cfg)
        features_path = write_dataframe(merged, out_dir / "features.csv")
        meta["paths"] = {"features": str(features_path)}
        write_json(meta, out_dir / "meta.json")
        return meta
