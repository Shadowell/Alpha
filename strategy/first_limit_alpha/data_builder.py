from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from .feature_store import ensure_dir, write_dataframe, write_json
from .labeling import compute_sample_labels
from .schema import LabelConfig, SampleBuildConfig


def board_limit_pct(symbol: str) -> float:
    if symbol.startswith(("30", "68")):
        return 0.20
    return 0.10


def normalize_amount(frame: pd.DataFrame) -> pd.Series:
    amount = pd.to_numeric(frame.get("amount", 0.0), errors="coerce").fillna(0.0)
    if float(amount.max()) > 0:
        return amount
    close = pd.to_numeric(frame["close"], errors="coerce").fillna(0.0)
    volume = pd.to_numeric(frame["volume"], errors="coerce").fillna(0.0)
    return close * volume


class FirstLimitAlphaDataBuilder:
    def __init__(self, db_path: str | Path, name_map: dict[str, str] | None = None) -> None:
        self.db_path = Path(db_path)
        self.name_map = name_map or {}

    def _load_kline_frame(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        symbols: Iterable[str] | None = None,
    ) -> pd.DataFrame:
        conn = sqlite3.connect(str(self.db_path))
        query = """
            SELECT symbol, trade_date, open, high, low, close, volume, amount
            FROM kline_daily
            WHERE 1=1
        """
        params: list[Any] = []
        if start_date:
            query += " AND trade_date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND trade_date <= ?"
            params.append(end_date)
        if symbols:
            symbols = list(symbols)
            placeholders = ",".join("?" for _ in symbols)
            query += f" AND symbol IN ({placeholders})"
            params.extend(symbols)
        query += " ORDER BY symbol ASC, trade_date ASC"
        frame = pd.read_sql_query(query, conn, params=params)
        conn.close()
        if frame.empty:
            return frame
        numeric_cols = ["open", "high", "low", "close", "volume", "amount"]
        for col in numeric_cols:
            frame[col] = pd.to_numeric(frame[col], errors="coerce").fillna(0.0)
        frame["trade_date"] = frame["trade_date"].astype(str)
        return frame

    def _prepare_symbol_frame(self, raw: pd.DataFrame, cfg: SampleBuildConfig) -> pd.DataFrame:
        frame = raw.copy().reset_index(drop=True)
        frame["prev_close"] = frame["close"].shift(1)
        frame["pct_change"] = (frame["close"] / frame["prev_close"]) - 1.0
        frame["amount"] = normalize_amount(frame)
        limit_pct = board_limit_pct(str(frame.iloc[0]["symbol"]))
        frame["limit_pct"] = limit_pct
        frame["is_limit_up"] = frame["pct_change"] >= (limit_pct - 0.0025)
        frame["prior_limitup_count"] = (
            frame["is_limit_up"].shift(1).rolling(cfg.prior_limitup_window, min_periods=cfg.prior_limitup_window).sum()
        )
        frame["prior_high"] = frame["high"].shift(1).rolling(cfg.lookback_days, min_periods=cfg.lookback_days).max()
        frame["prior_low"] = frame["low"].shift(1).rolling(cfg.lookback_days, min_periods=cfg.lookback_days).min()
        frame["consolidation_amp"] = (frame["prior_high"] / frame["prior_low"]) - 1.0
        frame["consolidation_volatility"] = (
            frame["pct_change"].shift(1).rolling(cfg.lookback_days, min_periods=cfg.lookback_days).std()
        )
        vol_avg = frame["volume"].shift(1).rolling(cfg.lookback_days, min_periods=cfg.lookback_days).mean()
        vol_std = frame["volume"].shift(1).rolling(cfg.lookback_days, min_periods=cfg.lookback_days).std()
        frame["volume_cv"] = vol_std / vol_avg.replace(0, np.nan)
        frame["recent_spike"] = frame["pct_change"].shift(1).rolling(cfg.lookback_days, min_periods=cfg.lookback_days).max()
        frame["avg_amount"] = frame["amount"].shift(1).rolling(cfg.lookback_days, min_periods=cfg.lookback_days).mean()
        frame["distance_to_recent_high"] = (frame["close"] / frame["prior_high"]) - 1.0
        return frame

    def _passes_sample_filter(self, row: pd.Series, cfg: SampleBuildConfig) -> bool:
        if not bool(row["is_limit_up"]):
            return False
        if float(row.get("prior_limitup_count", 0.0) or 0.0) > 0:
            return False
        if pd.isna(row["consolidation_amp"]) or pd.isna(row["consolidation_volatility"]):
            return False
        if float(row["consolidation_amp"]) > cfg.max_consolidation_amp:
            return False
        if float(row["consolidation_volatility"]) > cfg.max_consolidation_volatility:
            return False
        if float(row.get("recent_spike", 0.0) or 0.0) > cfg.max_recent_spike:
            return False
        if float(row.get("volume_cv", 0.0) or 0.0) > cfg.max_volume_cv:
            return False
        if float(row.get("avg_amount", 0.0) or 0.0) < cfg.min_avg_amount:
            return False
        return True

    def _base_sample_payload(self, symbol: str, name: str, row: pd.Series) -> dict[str, Any]:
        return {
            "symbol": str(symbol),
            "name": name,
            "trade_date": str(row["trade_date"]),
            "sample_type": "first_limit_after_consolidation",
            "is_first_limit": 1,
            "close": round(float(row["close"]), 6),
            "open": round(float(row["open"]), 6),
            "high": round(float(row["high"]), 6),
            "low": round(float(row["low"]), 6),
            "volume": round(float(row["volume"]), 6),
            "amount": round(float(row["amount"]), 6),
            "pct_change_today": round(float(row["pct_change"]), 6),
            "limit_pct": float(row["limit_pct"]),
            "prior_limitup_count": int(row["prior_limitup_count"] or 0),
            "consolidation_amp": round(float(row["consolidation_amp"]), 6),
            "consolidation_volatility": round(float(row["consolidation_volatility"]), 6),
            "volume_cv": round(float(row["volume_cv"]), 6),
            "recent_spike": round(float(row["recent_spike"]), 6),
            "distance_to_recent_high": round(float(row["distance_to_recent_high"]), 6),
        }

    def build_dataset(
        self,
        output_dir: str | Path,
        build_cfg: SampleBuildConfig | None = None,
        label_cfg: LabelConfig | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        symbols: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        build_cfg = build_cfg or SampleBuildConfig()
        label_cfg = label_cfg or LabelConfig()
        frame = self._load_kline_frame(start_date=start_date, end_date=end_date, symbols=symbols)
        out_dir = ensure_dir(Path(output_dir))
        if frame.empty:
            empty_path = write_dataframe(pd.DataFrame(), out_dir / "samples.csv")
            meta = {
                "sample_count": 0,
                "start_date": start_date,
                "end_date": end_date,
                "paths": {"samples": str(empty_path)},
                "build_config": build_cfg.to_dict(),
                "label_config": label_cfg.to_dict(),
            }
            write_json(meta, out_dir / "meta.json")
            return meta

        samples: list[dict[str, Any]] = []
        for symbol, group in frame.groupby("symbol", sort=False):
            name = self.name_map.get(symbol, "")
            if build_cfg.allowed_prefixes and not str(symbol).startswith(build_cfg.allowed_prefixes):
                continue
            if build_cfg.exclude_st and "ST" in name.upper():
                continue
            prepared = self._prepare_symbol_frame(group, build_cfg)
            if len(prepared) < build_cfg.min_history_days + label_cfg.evaluation_horizon:
                continue
            for idx in range(build_cfg.min_history_days, len(prepared) - label_cfg.evaluation_horizon):
                row = prepared.iloc[idx]
                if not self._passes_sample_filter(row, build_cfg):
                    continue
                labels = compute_sample_labels(prepared, idx, label_cfg)
                if labels is None:
                    continue
                samples.append({**self._base_sample_payload(symbol, name, row), **labels})

        samples_df = pd.DataFrame(samples).sort_values(["trade_date", "symbol"]).reset_index(drop=True)
        samples_path = write_dataframe(samples_df, out_dir / "samples.csv")
        meta = {
            "sample_count": int(len(samples_df)),
            "symbol_count": int(samples_df["symbol"].nunique()) if not samples_df.empty else 0,
            "date_range": {
                "start": str(samples_df["trade_date"].min()) if not samples_df.empty else start_date,
                "end": str(samples_df["trade_date"].max()) if not samples_df.empty else end_date,
            },
            "paths": {"samples": str(samples_path)},
            "build_config": build_cfg.to_dict(),
            "label_config": label_cfg.to_dict(),
            "future_information_fields": [
                "entry_date",
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
                "label_continuation",
                "label_strong_3d",
                "label_break_risk",
                "d1_open",
                "d1_high",
                "d1_low",
                "d1_close",
                "d2_open",
                "d2_high",
                "d2_low",
                "d2_close",
                "d3_open",
                "d3_high",
                "d3_low",
                "d3_close",
                "d4_open",
                "d4_high",
                "d4_low",
                "d4_close",
                "d5_open",
                "d5_high",
                "d5_low",
                "d5_close",
            ],
            "time_split_recommendation": {
                "train": "最早 70% 交易日",
                "valid": "中间 15% 交易日",
                "test": "最后 15% 交易日",
                "rule": "严格按 trade_date 升序切分，禁止随机打乱",
            },
        }
        write_json(meta, out_dir / "meta.json")
        return meta

    def build_candidate_frame(
        self,
        trade_date: str | None = None,
        build_cfg: SampleBuildConfig | None = None,
        symbols: Iterable[str] | None = None,
    ) -> pd.DataFrame:
        build_cfg = build_cfg or SampleBuildConfig()
        frame = self._load_kline_frame(end_date=trade_date, symbols=symbols)
        if frame.empty:
            return pd.DataFrame()
        target_trade_date = str(trade_date or frame["trade_date"].max())
        candidates: list[dict[str, Any]] = []
        for symbol, group in frame.groupby("symbol", sort=False):
            name = self.name_map.get(symbol, "")
            if build_cfg.allowed_prefixes and not str(symbol).startswith(build_cfg.allowed_prefixes):
                continue
            if build_cfg.exclude_st and "ST" in name.upper():
                continue
            prepared = self._prepare_symbol_frame(group, build_cfg)
            if len(prepared) < build_cfg.min_history_days:
                continue
            matched = prepared.index[prepared["trade_date"].astype(str) == target_trade_date].tolist()
            if not matched:
                continue
            idx = matched[-1]
            if idx < build_cfg.min_history_days:
                continue
            row = prepared.iloc[idx]
            if not self._passes_sample_filter(row, build_cfg):
                continue
            candidates.append(self._base_sample_payload(symbol, name, row))
        if not candidates:
            return pd.DataFrame()
        return pd.DataFrame(candidates).sort_values(["trade_date", "symbol"]).reset_index(drop=True)
