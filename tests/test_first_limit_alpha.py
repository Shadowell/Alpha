from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.services.kline_store import KlineSQLiteStore
from strategy.first_limit_alpha.data_builder import FirstLimitAlphaDataBuilder
from strategy.first_limit_alpha.features import FirstLimitFeatureBuilder
from strategy.first_limit_alpha.modeling import FirstLimitBaselineTrainer
from strategy.first_limit_alpha.schema import LabelConfig, SampleBuildConfig, SequenceConfig, TrainingConfig
from strategy.first_limit_alpha.train_sequence import FirstLimitSequenceTrainer


def _symbol_rows(base_close: float, event_mode: str, start: str = "2026-01-01") -> list[dict]:
    dates = pd.bdate_range(start, periods=40)
    rows: list[dict] = []
    close = base_close
    for idx, ts in enumerate(dates, start=1):
        date = ts.date().isoformat()
        if idx < 25:
            drift = 0.002 if idx % 2 == 0 else -0.001
            open_p = close * (1.0 - 0.001)
            close = round(close * (1.0 + drift), 4)
            high = round(max(open_p, close) * 1.01, 4)
            low = round(min(open_p, close) * 0.99, 4)
        elif idx == 25:
            prev_close = close
            open_p = round(prev_close * 1.01, 4)
            close = round(prev_close * 1.1, 4)
            high = close
            low = open_p
        elif event_mode == "strong" and idx in {26, 27, 28, 29, 30}:
            prev_close = close
            open_p = round(prev_close * 1.015, 4)
            close = round(prev_close * 1.06, 4)
            high = round(close * 1.03, 4)
            low = round(open_p * 0.995, 4)
        elif event_mode == "weak" and idx in {26, 27, 28, 29, 30}:
            prev_close = close
            if idx == 26:
                open_p = round(prev_close * 1.01, 4)
                close = round(prev_close * 0.95, 4)
                high = round(open_p * 1.01, 4)
                low = round(close * 0.96, 4)
            else:
                open_p = round(close * 0.995, 4)
                close = round(close * 0.99, 4)
                high = round(open_p * 1.005, 4)
                low = round(close * 0.99, 4)
        else:
            open_p = round(close * 0.998, 4)
            close = round(close * 1.002, 4)
            high = round(close * 1.01, 4)
            low = round(open_p * 0.99, 4)
        rows.append(
            {
                "trade_date": date,
                "open": round(open_p, 4),
                "high": round(high, 4),
                "low": round(low, 4),
                "close": round(close, 4),
                "volume": 1_000_000 + idx * 20_000,
                "amount": (1_000_000 + idx * 20_000) * round(close, 4),
            }
        )
    return rows


def test_data_builder_and_feature_builder(tmp_path: Path):
    store = KlineSQLiteStore(str(tmp_path / "market_kline.db"))
    store.upsert_symbol_klines("600001", _symbol_rows(10.0, "strong"), "2026-04-22T11:00:00+08:00")
    store.upsert_symbol_klines("600002", _symbol_rows(12.0, "weak"), "2026-04-22T11:00:00+08:00")

    dataset_dir = tmp_path / "dataset"
    builder = FirstLimitAlphaDataBuilder(store.db_path, name_map={"600001": "强势样本", "600002": "弱势样本"})
    ds_meta = builder.build_dataset(
        dataset_dir,
        build_cfg=SampleBuildConfig(
            lookback_days=15,
            prior_limitup_window=15,
            min_history_days=20,
            max_consolidation_amp=0.18,
            max_consolidation_volatility=0.03,
            max_recent_spike=0.05,
            min_avg_amount=1000.0,
        ),
        label_cfg=LabelConfig(),
    )
    samples = pd.read_csv(dataset_dir / "samples.csv")
    assert ds_meta["sample_count"] >= 2
    assert {"label_continuation", "label_strong_3d", "label_break_risk"}.issubset(samples.columns)
    assert set(samples["symbol"].astype(str)) == {"600001", "600002"}

    feature_dir = tmp_path / "features"
    ft_meta = FirstLimitFeatureBuilder(store.db_path).build_features(samples, feature_dir)
    features = pd.read_csv(feature_dir / "features.csv")
    assert ft_meta["feature_count"] >= 30
    assert "market_mean_return" in features.columns
    assert "interaction_market_x_limit" in features.columns


def test_baseline_trainer_excludes_future_columns(tmp_path: Path):
    dates = pd.bdate_range("2026-02-01", periods=12)
    rows = []
    for idx, ts in enumerate(dates, start=1):
        pos = idx % 2
        rows.append(
            {
                "symbol": f"600{idx:03d}",
                "name": f"S{idx}",
                "trade_date": ts.date().isoformat(),
                "sample_type": "first_limit_after_consolidation",
                "entry_date": ts.date().isoformat(),
                "entry_open": 10.0,
                "future_max_high_3d": 12.0,
                "future_min_low_2d": 9.0,
                "future_close_3d": 11.0,
                "future_close_5d": 11.5,
                "ret_high_3d": 0.2,
                "ret_close_3d": 0.1 if pos else -0.05,
                "ret_close_5d": 0.15 if pos else -0.03,
                "ret_min_low_2d": -0.08 if not pos else -0.02,
                "future_limit_up_count_5d": pos,
                "label_continuation": pos,
                "label_strong_3d": pos,
                "label_break_risk": 1 - pos,
                "d1_open": 10.0,
                "d1_high": 11.5,
                "d1_low": 9.8,
                "d1_close": 10.8,
                "d2_open": 10.8,
                "d2_high": 11.8,
                "d2_low": 10.5,
                "d2_close": 11.3,
                "d3_open": 11.3,
                "d3_high": 12.1,
                "d3_low": 10.9,
                "d3_close": 11.5,
                "feature_a": float(idx),
                "feature_b": float(pos) * 2.0 + 0.1,
                "feature_c": float(idx % 3),
                "feature_d": float(idx) / 10.0,
                "feature_e": float(pos) + 0.3,
                "feature_f": float(idx % 5) / 10.0,
            }
        )
    frame = pd.DataFrame(rows)
    out_dir = tmp_path / "model"
    result = FirstLimitBaselineTrainer().train(frame, out_dir, config=TrainingConfig(n_estimators=40, score_threshold=0.0))
    assert result["ok"] is True
    assert Path(result["paths"]["model"]).exists()
    assert "entry_open" not in result["features"]
    assert "d1_high" not in result["features"]
    assert result["backtest"]["summary"]["trade_count"] >= 1


def test_sequence_trainer_small_sample_returns_graceful_result(tmp_path: Path):
    store = KlineSQLiteStore(str(tmp_path / "market_kline.db"))
    store.upsert_symbol_klines("600001", _symbol_rows(10.0, "strong"), "2026-04-22T11:00:00+08:00")
    samples = pd.DataFrame(
        [
            {
                "symbol": "600001",
                "trade_date": "2026-02-04",
                "label_continuation": 1,
                "label_strong_3d": 1,
                "label_break_risk": 0,
            }
        ]
    )
    result = FirstLimitSequenceTrainer(store.db_path).train(samples, tmp_path / "sequence", cfg=SequenceConfig(seq_len=10, epochs=1))
    assert result["ok"] is False
    assert result["reason"] == "sequence samples too small"


def test_sequence_trainer_outputs_metrics_and_predictions(tmp_path: Path):
    store = KlineSQLiteStore(str(tmp_path / "market_kline.db"))
    name_map = {}
    for idx in range(40):
        symbol = f"600{idx+100:03d}"
        mode = "strong" if idx % 2 == 0 else "weak"
        name_map[symbol] = f"N{idx}"
        store.upsert_symbol_klines(symbol, _symbol_rows(10.0 + idx * 0.1, mode), "2026-04-22T11:00:00+08:00")

    dataset_dir = tmp_path / "dataset_seq"
    samples_meta = FirstLimitAlphaDataBuilder(store.db_path, name_map=name_map).build_dataset(
        dataset_dir,
        build_cfg=SampleBuildConfig(
            lookback_days=15,
            prior_limitup_window=15,
            min_history_days=20,
            max_consolidation_amp=0.18,
            max_consolidation_volatility=0.03,
            max_recent_spike=0.05,
            min_avg_amount=1000.0,
        ),
        label_cfg=LabelConfig(),
    )
    assert samples_meta["sample_count"] >= 32
    samples = pd.read_csv(dataset_dir / "samples.csv")
    baseline_reference = {
        "split": {
            "train": sorted(samples["trade_date"].astype(str).unique().tolist())[:-5],
            "test": sorted(samples["trade_date"].astype(str).unique().tolist())[-5:],
        },
        "metrics": {
            "continuation": {"test_auc": 0.5, "test_ap": 0.5},
            "strong_3d": {"test_auc": 0.5, "test_ap": 0.5},
            "break_risk": {"test_auc": 0.5, "test_ap": 0.5},
        },
        "backtest": {"summary": {"cum_return": 0.01, "win_rate": 0.5, "max_drawdown": -0.05}},
    }
    result = FirstLimitSequenceTrainer(store.db_path).train(
        samples,
        tmp_path / "sequence_full",
        cfg=SequenceConfig(seq_len=20, epochs=1, batch_size=128, hidden_size=16),
        baseline_metrics=baseline_reference,
    )
    assert result["ok"] is True
    assert Path(result["paths"]["model"]).exists()
    assert Path(result["paths"]["test_predictions"]).exists()
    assert "metrics" in result
    assert "comparison_vs_baseline" in result
    assert "backtest" in result
