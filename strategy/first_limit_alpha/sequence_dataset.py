from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .schema import SequenceConfig


class FirstLimitSequenceDatasetBuilder:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    def _load_kline(self, symbols: list[str]) -> pd.DataFrame:
        conn = sqlite3.connect(str(self.db_path))
        placeholders = ",".join("?" for _ in symbols)
        frame = pd.read_sql_query(
            f"""
            SELECT symbol, trade_date, open, high, low, close, volume, amount
            FROM kline_daily
            WHERE symbol IN ({placeholders})
            ORDER BY symbol ASC, trade_date ASC
            """,
            conn,
            params=symbols,
        )
        conn.close()
        for col in ["open", "high", "low", "close", "volume", "amount"]:
            frame[col] = pd.to_numeric(frame[col], errors="coerce").fillna(0.0)
        frame["trade_date"] = frame["trade_date"].astype(str)
        return frame

    def build(self, samples: pd.DataFrame, cfg: SequenceConfig | None = None) -> dict[str, Any]:
        cfg = cfg or SequenceConfig()
        if samples.empty:
            return {"x": np.zeros((0, cfg.seq_len, 6), dtype=np.float32), "y": np.zeros((0, 3), dtype=np.float32), "meta": pd.DataFrame()}
        symbols = sorted(samples["symbol"].astype(str).unique().tolist())
        kline = self._load_kline(symbols)
        feature_names = ["ret", "range", "body", "upper", "lower", "volume_ratio"]
        seqs: list[np.ndarray] = []
        labels: list[np.ndarray] = []
        meta_rows: list[dict[str, Any]] = []
        for symbol, group in kline.groupby("symbol", sort=False):
            frame = group.copy().reset_index(drop=True)
            frame["prev_close"] = frame["close"].shift(1)
            frame["ret"] = (frame["close"] / frame["prev_close"] - 1.0).fillna(0.0)
            frame["range"] = ((frame["high"] - frame["low"]) / frame["prev_close"]).replace([np.inf, -np.inf], 0.0).fillna(0.0)
            frame["body"] = ((frame["close"] - frame["open"]) / frame["prev_close"]).replace([np.inf, -np.inf], 0.0).fillna(0.0)
            frame["upper"] = ((frame["high"] - frame[["open", "close"]].max(axis=1)) / frame["prev_close"]).replace([np.inf, -np.inf], 0.0).fillna(0.0)
            frame["lower"] = ((frame[["open", "close"]].min(axis=1) - frame["low"]) / frame["prev_close"]).replace([np.inf, -np.inf], 0.0).fillna(0.0)
            frame["volume_ratio"] = (frame["volume"] / frame["volume"].shift(1).rolling(5).mean()).replace([np.inf, -np.inf], 0.0).fillna(0.0)
            sample_rows = samples[samples["symbol"] == symbol]
            index_map = {d: i for i, d in enumerate(frame["trade_date"].astype(str).tolist())}
            values = frame[feature_names].to_numpy(dtype=np.float32)
            for _, row in sample_rows.iterrows():
                idx = index_map.get(str(row["trade_date"]))
                if idx is None or idx + 1 < cfg.seq_len:
                    continue
                seq = values[idx + 1 - cfg.seq_len : idx + 1]
                seqs.append(seq)
                labels.append(
                    np.array(
                        [
                            float(row["label_continuation"]),
                            float(row["label_strong_3d"]),
                            float(row["label_break_risk"]),
                        ],
                        dtype=np.float32,
                    )
                )
                meta_rows.append({"symbol": symbol, "trade_date": str(row["trade_date"])})
        x = np.stack(seqs) if seqs else np.zeros((0, cfg.seq_len, len(feature_names)), dtype=np.float32)
        y = np.stack(labels) if labels else np.zeros((0, 3), dtype=np.float32)
        return {"x": x, "y": y, "meta": pd.DataFrame(meta_rows), "feature_names": feature_names}
