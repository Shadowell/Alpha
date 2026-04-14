"""Kronos 模型系列 benchmark — 加载时间、推理时间、预测质量对比。

用法: python -m tests.benchmark_kronos
"""
from __future__ import annotations

import gc
import json
import sys
import time

import numpy as np
import pandas as pd
import torch

sys.path.insert(0, ".")

from app.services.kronos_model import Kronos, KronosTokenizer, KronosPredictor
from app.services.kline_store import KlineSQLiteStore

MODELS = [
    {
        "name": "Kronos-mini",
        "model_id": "NeoQuasar/Kronos-mini",
        "tokenizer_id": "NeoQuasar/Kronos-Tokenizer-2k",
        "max_context": 2048,
        "params": "4.1M",
    },
    {
        "name": "Kronos-small",
        "model_id": "NeoQuasar/Kronos-small",
        "tokenizer_id": "NeoQuasar/Kronos-Tokenizer-base",
        "max_context": 512,
        "params": "24.7M",
    },
    {
        "name": "Kronos-base",
        "model_id": "NeoQuasar/Kronos-base",
        "tokenizer_id": "NeoQuasar/Kronos-Tokenizer-base",
        "max_context": 512,
        "params": "102.3M",
    },
]

LOOKBACK = 30
HORIZON = 5
SYMBOL = "000001"
SAMPLE_COUNTS = [1, 10, 20, 50, 100]


def get_history(lookback: int) -> list[dict]:
    store = KlineSQLiteStore()
    items = store.get_kline(SYMBOL, days=lookback + 10)
    if len(items) < lookback + HORIZON:
        print(f"[WARN] K线数据不足: 需要 {lookback + HORIZON} 根，实际 {len(items)} 根")
    return items


def build_future_dates(history: list[dict], horizon: int) -> list[str]:
    last = pd.Timestamp(history[-1]["date"])
    dates = pd.bdate_range(start=last + pd.Timedelta(days=1), periods=horizon)
    return [d.strftime("%Y-%m-%d") for d in dates]


def run_inference(
    predictor: KronosPredictor,
    history: list[dict],
    future_dates: list[str],
    horizon: int,
    sample_count: int = 1,
) -> pd.DataFrame:
    df = pd.DataFrame(history)
    x_df = df[["open", "high", "low", "close", "volume", "amount"]].copy()
    hist_dates = pd.to_datetime(df["date"])
    x_timestamp = pd.Series(hist_dates).reset_index(drop=True)
    y_timestamp = pd.Series(pd.to_datetime(future_dates))
    pred_df = predictor.predict(
        df=x_df.reset_index(drop=True),
        x_timestamp=x_timestamp,
        y_timestamp=y_timestamp,
        pred_len=horizon,
        T=1.0,
        top_k=0,
        top_p=0.9,
        sample_count=sample_count,
        verbose=False,
    )
    return pred_df


def calc_metrics(history: list[dict], pred_df: pd.DataFrame) -> dict:
    last = history[-1]
    last_close = last["close"]
    pred_closes = pred_df["close"].values
    pred_day1_close = float(pred_closes[0])
    pred_day5_close = float(pred_closes[-1])
    day1_chg = (pred_day1_close - last_close) / last_close * 100
    day5_chg = (pred_day5_close - last_close) / last_close * 100

    pred_highs = pred_df["high"].values
    pred_lows = pred_df["low"].values
    volatility = float(np.mean((pred_highs - pred_lows) / pred_lows * 100))

    return {
        "pred_day1_close": round(pred_day1_close, 4),
        "pred_day5_close": round(pred_day5_close, 4),
        "day1_chg_pct": round(day1_chg, 2),
        "day5_chg_pct": round(day5_chg, 2),
        "avg_volatility_pct": round(volatility, 2),
    }


def benchmark_model_sample_counts(
    cfg: dict, history: list[dict], future_dates: list[str]
) -> dict:
    """对一个模型跑全部 sample_count 档位的 benchmark。"""
    name = cfg["name"]
    print(f"\n{'='*60}")
    print(f"  Benchmarking: {name} ({cfg['params']})")
    print(f"{'='*60}")

    # --- Load ---
    print(f"  Loading tokenizer: {cfg['tokenizer_id']} ...")
    t0 = time.perf_counter()
    tokenizer = KronosTokenizer.from_pretrained(cfg["tokenizer_id"])
    t_tok = time.perf_counter() - t0

    print(f"  Loading model: {cfg['model_id']} ...")
    t0 = time.perf_counter()
    model = Kronos.from_pretrained(cfg["model_id"])
    t_model = time.perf_counter() - t0

    t0 = time.perf_counter()
    predictor = KronosPredictor(model, tokenizer, device=None, max_context=cfg["max_context"])
    t_init = time.perf_counter() - t0
    device = predictor.device
    load_total = t_tok + t_model + t_init
    print(f"  Loaded on {device} in {load_total:.2f}s")

    # --- Warmup ---
    print("  Warmup inference (sample_count=1)...")
    try:
        _ = run_inference(predictor, history, future_dates, HORIZON, sample_count=1)
    except Exception as e:
        print(f"  [ERROR] warmup failed: {e}")

    # --- Test each sample_count ---
    sc_results = []
    n_runs = 3
    for sc in SAMPLE_COUNTS:
        print(f"\n  --- sample_count={sc} ({n_runs} runs) ---")
        infer_times = []
        pred_df = None
        for i in range(n_runs):
            t0 = time.perf_counter()
            pred_df = run_inference(predictor, history, future_dates, HORIZON, sample_count=sc)
            elapsed = time.perf_counter() - t0
            infer_times.append(elapsed)
            print(f"    run {i+1}: {elapsed:.2f}s")

        avg_infer = float(np.mean(infer_times))
        metrics = calc_metrics(history, pred_df) if pred_df is not None else {}
        sc_results.append({
            "sample_count": sc,
            "avg_infer_sec": round(avg_infer, 2),
            "infer_times": [round(t, 2) for t in infer_times],
            **metrics,
        })
        print(f"    avg={avg_infer:.2f}s  D1={metrics.get('day1_chg_pct','?')}%  D5={metrics.get('day5_chg_pct','?')}%  vol={metrics.get('avg_volatility_pct','?')}%")

    # --- Cleanup ---
    del predictor, model, tokenizer
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()

    return {
        "model": name,
        "params": cfg["params"],
        "device": str(device),
        "max_context": cfg["max_context"],
        "load_time_sec": round(load_total, 2),
        "sample_count_results": sc_results,
    }


def main():
    print(f"Kronos Benchmark: lookback={LOOKBACK}, horizon={HORIZON}, symbol={SYMBOL}")
    print(f"Sample counts: {SAMPLE_COUNTS}")
    print(f"PyTorch: {torch.__version__}, Device: ", end="")
    if torch.cuda.is_available():
        print(f"CUDA ({torch.cuda.get_device_name(0)})")
    elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        print("MPS (Apple Silicon)")
    else:
        print("CPU")

    all_items = get_history(LOOKBACK)
    history = all_items[:LOOKBACK]
    future_dates = build_future_dates(history, HORIZON)

    print(f"\nHistory: {len(history)} bars, {history[0]['date']} ~ {history[-1]['date']}")
    print(f"Future dates: {future_dates}")

    results = []
    for cfg in MODELS:
        try:
            r = benchmark_model_sample_counts(cfg, history, future_dates)
            results.append(r)
        except Exception as e:
            print(f"  [FATAL] {cfg['name']} benchmark failed: {e}")
            results.append({"model": cfg["name"], "params": cfg["params"], "error": str(e)})

    # --- Summary table ---
    print(f"\n{'='*80}")
    print("  BENCHMARK RESULTS — sample_count scaling")
    print(f"{'='*80}")
    header = f"{'Model':<16} {'SC':<6} {'Infer(s)':<10} {'D1 Chg%':<10} {'D5 Chg%':<10} {'Vol%':<8}"
    print(header)
    print("-" * len(header))
    for r in results:
        if "error" in r:
            print(f"{r['model']:<16} ERROR: {r['error']}")
            continue
        for sc_r in r["sample_count_results"]:
            print(
                f"{r['model']:<16} {sc_r['sample_count']:<6} "
                f"{sc_r['avg_infer_sec']:<10} "
                f"{sc_r.get('day1_chg_pct','N/A'):<10} "
                f"{sc_r.get('day5_chg_pct','N/A'):<10} "
                f"{sc_r.get('avg_volatility_pct','N/A'):<8}"
            )

    out_path = "tests/benchmark_kronos_results.json"
    with open(out_path, "w") as f:
        json.dump({
            "lookback": LOOKBACK,
            "horizon": HORIZON,
            "symbol": SYMBOL,
            "sample_counts": SAMPLE_COUNTS,
            "results": results,
        }, f, indent=2, ensure_ascii=False)
    print(f"\nResults saved to {out_path}")


if __name__ == "__main__":
    main()
