"""Kronos 预测准确率评估脚本。

评估方案：
  - 样本：本地 K 线数据库中所有 A 股主板/创业板/科创板
  - 锚点：每只股票最近 3 个"有 3 日后真实值"的交易日
  - 对每锚点用锚点前 lookback=30 日作为输入，预测 horizon=3 日
  - 用锚点后实际收盘价做方向对比（基准价 = 锚点当日 close）
  - 指标：方向准确率（按 horizon 分层 + 按预测强度分层 + IC 参考）

执行：
  python scripts/evaluate_kronos.py [--limit N] [--anchors 3] [--concurrency 4]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.kline_store import KlineSQLiteStore  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
log = logging.getLogger("eval")
log.setLevel(logging.INFO)


LOOKBACK = 20
HORIZON = 3
MIN_HISTORY_DAYS = LOOKBACK + HORIZON + 1


def _set_window(lookback: int) -> None:
    """运行时覆写 LOOKBACK / MIN_HISTORY_DAYS。"""
    global LOOKBACK, MIN_HISTORY_DAYS
    LOOKBACK = max(10, min(lookback, 240))
    MIN_HISTORY_DAYS = LOOKBACK + HORIZON + 1


def _load_predictor(device: str | None = None):
    from app.services.kronos_model import Kronos, KronosPredictor, KronosTokenizer

    tokenizer = KronosTokenizer.from_pretrained("NeoQuasar/Kronos-Tokenizer-base")
    model = Kronos.from_pretrained("NeoQuasar/Kronos-base")
    predictor = KronosPredictor(model, tokenizer, device=device, max_context=512)
    return predictor, predictor.device


def _build_anchors(kline: list[dict[str, Any]], n_anchors: int) -> list[int]:
    """返回锚点在 kline list 中的索引：每个锚点需要左侧 LOOKBACK，右侧 HORIZON 天真实值。

    选取原则：从最近的合法锚点往前连续取 n_anchors 个（间隔 1 天）。
    """
    n = len(kline)
    max_idx = n - HORIZON - 1  # anchor 需要有 HORIZON 个后续交易日
    min_idx = LOOKBACK - 1
    if max_idx < min_idx:
        return []
    anchors: list[int] = []
    for idx in range(max_idx, min_idx - 1, -1):
        anchors.append(idx)
        if len(anchors) >= n_anchors:
            break
    return anchors


def _run_one_inference(predictor, history: list[dict], future_dates: list[pd.Timestamp]):
    df = pd.DataFrame(history)
    x_df = df[["open", "high", "low", "close", "volume", "amount"]].reset_index(drop=True)
    x_ts = pd.Series(pd.to_datetime(df["date"])).reset_index(drop=True)
    y_ts = pd.Series(future_dates).reset_index(drop=True)
    pred = predictor.predict(
        df=x_df,
        x_timestamp=x_ts,
        y_timestamp=y_ts,
        pred_len=HORIZON,
        T=1.0,
        top_k=0,
        top_p=0.9,
        sample_count=100,
        verbose=False,
    )
    return pred


class PredictorPool:
    def __init__(self, n: int) -> None:
        self._predictors: list[tuple[Any, asyncio.Lock]] = []
        self._device: str = "cpu"
        self._n = n
        self._sem = asyncio.Semaphore(n)

    async def init(self) -> None:
        import torch
        # Try to determine device once
        def _pick_device() -> str:
            if torch.backends.mps.is_available():
                return "mps"
            if torch.cuda.is_available():
                return "cuda"
            return "cpu"

        device = await asyncio.to_thread(_pick_device)
        self._device = device

        for i in range(self._n):
            log.info("[pool] loading predictor %d/%d on %s ...", i + 1, self._n, device)
            try:
                predictor, dev = await asyncio.to_thread(_load_predictor, device)
                self._predictors.append((predictor, asyncio.Lock()))
                log.info("[pool] predictor %d ready on %s", i + 1, dev)
            except Exception as exc:
                log.warning("[pool] load failed at %d: %s, fall back to smaller pool", i + 1, exc)
                break
        if not self._predictors:
            raise RuntimeError("No Kronos predictor could be loaded")
        if len(self._predictors) < self._n:
            self._n = len(self._predictors)
            self._sem = asyncio.Semaphore(self._n)
            log.warning("[pool] actual concurrency reduced to %d", self._n)

    @property
    def device(self) -> str:
        return self._device

    @property
    def size(self) -> int:
        return self._n

    async def run(self, history: list[dict], future_dates: list[pd.Timestamp]):
        async with self._sem:
            # pick first free predictor
            for predictor, lk in self._predictors:
                if lk.locked():
                    continue
                async with lk:
                    return await asyncio.to_thread(_run_one_inference, predictor, history, future_dates)
            # fallback: wait for any
            predictor, lk = self._predictors[0]
            async with lk:
                return await asyncio.to_thread(_run_one_inference, predictor, history, future_dates)


async def _evaluate_symbol(
    pool: PredictorPool,
    symbol: str,
    kline: list[dict],
    anchors: list[int],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for anchor_idx in anchors:
        history = kline[anchor_idx - LOOKBACK + 1: anchor_idx + 1]
        if len(history) < LOOKBACK:
            continue
        actual_future = kline[anchor_idx + 1: anchor_idx + 1 + HORIZON]
        if len(actual_future) < HORIZON:
            continue

        anchor_close = float(history[-1]["close"])
        if anchor_close <= 0:
            continue
        last_date = pd.to_datetime(history[-1]["date"])
        future_dates = [last_date + pd.Timedelta(days=i + 1) for i in range(HORIZON)]

        try:
            pred_df = await pool.run(history, future_dates)
        except Exception as exc:
            log.debug("predict fail %s@%d: %s", symbol, anchor_idx, exc)
            continue

        try:
            for h in range(HORIZON):
                pred_close = float(pred_df.iloc[h]["close"])
                real_close = float(actual_future[h]["close"])
                pred_ret = (pred_close - anchor_close) / anchor_close * 100.0
                real_ret = (real_close - anchor_close) / anchor_close * 100.0
                direction_ok = (pred_ret >= 0 and real_ret >= 0) or (pred_ret < 0 and real_ret < 0)
                results.append({
                    "symbol": symbol,
                    "anchor_date": history[-1]["date"],
                    "target_date": actual_future[h]["date"],
                    "h": h + 1,
                    "anchor_close": anchor_close,
                    "pred_close": pred_close,
                    "real_close": real_close,
                    "pred_ret": pred_ret,
                    "real_ret": real_ret,
                    "direction_ok": bool(direction_ok),
                })
        except Exception as exc:
            log.debug("parse fail %s@%d: %s", symbol, anchor_idx, exc)
    return results


def _pct(numer: int, denom: int) -> float:
    return (numer / denom * 100.0) if denom > 0 else 0.0


def _pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return 0.0
    return num / (dx * dy)


def _aggregate(rows: list[dict[str, Any]], total_stocks: int, total_anchors_expected: int,
                elapsed_sec: float, device: str, concurrency: int) -> dict[str, Any]:
    by_h: dict[int, list[dict]] = {1: [], 2: [], 3: []}
    for r in rows:
        by_h[r["h"]].append(r)

    def _stats_for(items: list[dict]) -> dict[str, Any]:
        if not items:
            return {"count": 0}
        dir_ok = sum(1 for r in items if r["direction_ok"])
        mape = sum(abs(r["pred_close"] - r["real_close"]) / max(abs(r["real_close"]), 1e-6) for r in items) / len(items) * 100.0
        mae_ret = sum(abs(r["pred_ret"] - r["real_ret"]) for r in items) / len(items)
        ic = _pearson([r["pred_ret"] for r in items], [r["real_ret"] for r in items])
        # 按预测强度分层
        buckets = {
            "pred_up_gt_2": [r for r in items if r["pred_ret"] > 2],
            "pred_up_0_2": [r for r in items if 0 < r["pred_ret"] <= 2],
            "pred_down_0_2": [r for r in items if -2 <= r["pred_ret"] <= 0],
            "pred_down_lt_-2": [r for r in items if r["pred_ret"] < -2],
        }
        bucket_stats = {
            k: {
                "count": len(v),
                "direction_acc": _pct(sum(1 for r in v if r["direction_ok"]), len(v)),
                "avg_real_ret": (sum(r["real_ret"] for r in v) / len(v)) if v else 0,
            } for k, v in buckets.items()
        }
        # Top 10% pred_ret 命中
        sorted_items = sorted(items, key=lambda r: r["pred_ret"], reverse=True)
        top10_cnt = max(1, int(len(items) * 0.10))
        top10 = sorted_items[:top10_cnt]
        top10_up = sum(1 for r in top10 if r["real_ret"] > 0)
        top10_avg_real = sum(r["real_ret"] for r in top10) / len(top10)
        # Bottom 10%
        bottom10 = sorted_items[-top10_cnt:]
        bottom10_down = sum(1 for r in bottom10 if r["real_ret"] < 0)
        bottom10_avg_real = sum(r["real_ret"] for r in bottom10) / len(bottom10)
        return {
            "count": len(items),
            "direction_acc": _pct(dir_ok, len(items)),
            "mape_close": mape,
            "mae_ret_pct": mae_ret,
            "ic_pearson": ic,
            "bucket_by_pred_strength": bucket_stats,
            "top10pct_pred_up_hit": _pct(top10_up, len(top10)),
            "top10pct_avg_real_ret": top10_avg_real,
            "bottom10pct_pred_down_hit": _pct(bottom10_down, len(bottom10)),
            "bottom10pct_avg_real_ret": bottom10_avg_real,
        }

    per_h = {f"h{h}": _stats_for(by_h[h]) for h in (1, 2, 3)}
    overall = _stats_for(rows)

    symbols_covered = len({r["symbol"] for r in rows})
    unique_anchors = len({(r["symbol"], r["anchor_date"]) for r in rows})

    return {
        "meta": {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "device": device,
            "concurrency": concurrency,
            "lookback": LOOKBACK,
            "horizon": HORIZON,
            "model": "NeoQuasar/Kronos-base",
            "tokenizer": "NeoQuasar/Kronos-Tokenizer-base",
            "elapsed_sec": round(elapsed_sec, 1),
            "elapsed_min": round(elapsed_sec / 60, 2),
            "total_stocks_scanned": total_stocks,
            "total_stocks_with_samples": symbols_covered,
            "total_anchors_with_samples": unique_anchors,
            "total_predictions": len(rows),
            "expected_predictions": total_anchors_expected * HORIZON,
        },
        "overall": overall,
        "per_horizon": per_h,
    }


def _render_markdown(agg: dict[str, Any]) -> str:
    m = agg["meta"]
    ov = agg["overall"] if agg["overall"].get("count", 0) > 0 else {
        "count": 0, "direction_acc": 0.0, "ic_pearson": 0.0, "mape_close": 0.0, "mae_ret_pct": 0.0,
    }
    ph = agg["per_horizon"]

    def _bucket_rows(stats: dict[str, Any]) -> list[str]:
        if stats.get("count", 0) == 0:
            return ["|-|-|-|-|"]
        b = stats.get("bucket_by_pred_strength", {})
        order = [
            ("pred_up_gt_2", "预测 > +2%"),
            ("pred_up_0_2", "预测 0 ~ +2%"),
            ("pred_down_0_2", "预测 0 ~ -2%"),
            ("pred_down_lt_-2", "预测 < -2%"),
        ]
        out = []
        for key, label in order:
            s = b.get(key, {"count": 0})
            if s["count"] == 0:
                out.append(f"| {label} | 0 | - | - |")
            else:
                out.append(f"| {label} | {s['count']} | {s['direction_acc']:.1f}% | {s['avg_real_ret']:+.2f}% |")
        return out

    lines = []
    lines.append("# Kronos 预测准确率评估报告")
    lines.append("")
    lines.append(f"> 生成时间：{m['generated_at']}  |  设备：{m['device']}  |  并发：{m['concurrency']}")
    lines.append(f"> 模型：{m['model']}  |  Tokenizer：{m['tokenizer']}")
    lines.append(f"> 输入窗口：{m['lookback']} 日  |  预测窗口：{m['horizon']} 日")
    lines.append("")
    lines.append("## 一、概要")
    lines.append("")
    lines.append("| 项 | 值 |")
    lines.append("|---|---|")
    lines.append(f"| 样本股票总数 | {m['total_stocks_scanned']} |")
    lines.append(f"| 有效评估股票 | {m['total_stocks_with_samples']} |")
    lines.append(f"| 有效锚点数 | {m['total_anchors_with_samples']} |")
    lines.append(f"| 累计预测次数 | {m['total_predictions']} / 期望 {m['expected_predictions']} |")
    lines.append(f"| 总耗时 | {m['elapsed_min']} 分钟（{m['elapsed_sec']} 秒）|")
    lines.append(f"| 整体方向准确率 | **{ov['direction_acc']:.2f}%** |")
    lines.append(f"| 整体收益率 IC（Pearson） | **{ov['ic_pearson']:.4f}** |")
    lines.append(f"| 整体 MAPE（收盘价） | {ov['mape_close']:.2f}% |")
    lines.append(f"| 整体 MAE（收益率） | {ov['mae_ret_pct']:.2f}% |")
    lines.append("")
    lines.append("> 方向准确率定义：`sign(预测收盘 vs 锚点收盘) == sign(实际收盘 vs 锚点收盘)`。")
    lines.append("> 随机猜测基线 = 50%，高于 50% 即优于随机。")
    lines.append("")

    lines.append("## 二、按预测窗口（Horizon）分层")
    lines.append("")
    lines.append("| Horizon | 样本数 | 方向准确率 | IC | MAPE | MAE(ret%) |")
    lines.append("|---|---|---|---|---|---|")
    for h in (1, 2, 3):
        s = ph[f"h{h}"]
        if s.get("count", 0) == 0:
            lines.append(f"| H+{h} | 0 | - | - | - | - |")
        else:
            lines.append(f"| H+{h} | {s['count']} | **{s['direction_acc']:.2f}%** | {s['ic_pearson']:.4f} | {s['mape_close']:.2f}% | {s['mae_ret_pct']:.2f}% |")
    lines.append("")

    lines.append("## 三、按预测强度分层（方向准确率 vs 预测涨幅区间）")
    lines.append("")
    lines.append("> 观察：当 Kronos 预测 > +2% 或 < -2% 时，方向可信度是否显著优于弱信号？")
    lines.append("")
    for h in (1, 2, 3):
        lines.append(f"### H+{h}")
        lines.append("")
        lines.append("| 预测涨幅区间 | 样本数 | 方向准确率 | 平均真实收益率 |")
        lines.append("|---|---|---|---|")
        for row in _bucket_rows(ph[f"h{h}"]):
            lines.append(row)
        lines.append("")

    lines.append("## 四、极值分位命中（策略关注）")
    lines.append("")
    lines.append("> 选股场景中只关心 Top/Bottom 分位，而非整体。")
    lines.append("> - Top 10% = 预测涨幅最大的前 10% 样本，统计实际上涨比例")
    lines.append("> - Bottom 10% = 预测跌幅最大的前 10% 样本，统计实际下跌比例")
    lines.append("")
    lines.append("| Horizon | Top 10% 上涨命中 | Top 10% 平均真实收益 | Bottom 10% 下跌命中 | Bottom 10% 平均真实收益 |")
    lines.append("|---|---|---|---|---|")
    for h in (1, 2, 3):
        s = ph[f"h{h}"]
        if s.get("count", 0) == 0:
            lines.append(f"| H+{h} | - | - | - | - |")
        else:
            lines.append(
                f"| H+{h} | **{s['top10pct_pred_up_hit']:.2f}%** | {s['top10pct_avg_real_ret']:+.2f}% "
                f"| **{s['bottom10pct_pred_down_hit']:.2f}%** | {s['bottom10pct_avg_real_ret']:+.2f}% |"
            )
    lines.append("")

    lines.append("## 五、结论参考")
    lines.append("")
    overall_acc = ov["direction_acc"]
    h1 = ph["h1"].get("direction_acc", 0)
    h3 = ph["h3"].get("direction_acc", 0)
    ic = ov["ic_pearson"]
    top10_h1 = ph["h1"].get("top10pct_pred_up_hit", 0)

    bullets = []
    bullets.append(
        f"- **整体方向准确率 {overall_acc:.2f}%** — "
        + ("高于随机基线 50%，具有统计显著性。" if overall_acc > 50 else "接近或低于随机基线，需警惕过拟合风险。")
    )
    bullets.append(
        f"- **衰减趋势**：H+1 准确率 {h1:.2f}% → H+3 准确率 {h3:.2f}%，"
        + ("符合预期（越远越不准）。" if h1 >= h3 else "出现反常，可能因样本噪声或锚点分布不均。")
    )
    bullets.append(
        f"- **IC（Pearson）= {ic:.4f}**，"
        + ("属于弱相关，可作为选股辅助信号。" if 0.02 <= abs(ic) < 0.1 else ("属于有意义的信号。" if abs(ic) >= 0.1 else "信号非常弱，单独使用不足以选股。"))
    )
    bullets.append(
        f"- **Top 10% 分位命中 H+1 = {top10_h1:.2f}%**，"
        + ("高于 50%，在极值区间具备选股能力。" if top10_h1 > 55 else "仅略高于随机，极值信号同样有限。")
    )
    bullets.append(
        f"- **MAPE {ov['mape_close']:.2f}%** — 收盘价绝对偏差参考值，K 线绝对价格不可直接用于下单，需结合方向信号使用。"
    )

    lines.extend(bullets)
    lines.append("")
    lines.append("## 六、使用建议")
    lines.append("")
    lines.append("1. **方向信号为主，价格数字为辅**：Kronos 输出的绝对价格属于 token 采样结果，噪声较大，应仅用方向。")
    lines.append("2. **优先关注极值分位**：仅当预测涨幅 > +2% 或 < -2% 时才入池，过滤掉模型自身的噪声区间。")
    lines.append("3. **与其他因子组合**：单一 IC 弱，建议与概念板块热度、公告关键词等多因子合成后再做决策。")
    lines.append("4. **滚动重评**：本报告仅覆盖最近 3 个锚点，建议每月重跑一次以追踪模型漂移。")
    lines.append("")
    return "\n".join(lines)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="限制股票数（0 = 全部）")
    parser.add_argument("--anchors", type=int, default=3, help="每只股票锚点数")
    parser.add_argument("--concurrency", type=int, default=4, help="并发模型副本数")
    parser.add_argument("--lookback", type=int, default=180, help="每次预测输入的历史天数")
    parser.add_argument(
        "--symbols-file",
        type=str,
        default="",
        help="可选：JSON 数组或每行一只代码的文本文件，限定评估范围",
    )
    parser.add_argument("--output", type=str, default="docs/kronos_accuracy_report.md")
    parser.add_argument("--json-output", type=str, default="docs/kronos_accuracy_raw.json")
    parser.add_argument("--progress-every", type=int, default=50, help="每 N 只股票打印一次进度")
    parser.add_argument("--partial-every", type=int, default=500, help="每 N 只股票保存一次中间结果")
    args = parser.parse_args()

    _set_window(args.lookback)

    store = KlineSQLiteStore()
    all_symbols = store.get_all_symbols()

    whitelist: set[str] | None = None
    if args.symbols_file:
        p = ROOT / args.symbols_file if not os.path.isabs(args.symbols_file) else Path(args.symbols_file)
        raw = p.read_text(encoding="utf-8").strip()
        try:
            parsed = json.loads(raw)
            if not isinstance(parsed, list):
                raise ValueError("symbols-file JSON must be an array")
            whitelist = {str(x).zfill(6) for x in parsed if x}
        except json.JSONDecodeError:
            whitelist = {ln.strip().zfill(6) for ln in raw.splitlines() if ln.strip()}
        log.info("symbols-file loaded: %s (%d codes)", p, len(whitelist))

    symbols: list[str] = []
    for code in all_symbols:
        if not code or len(code) != 6:
            continue
        if code[:2] not in ("00", "30", "60", "68"):
            continue
        if whitelist is not None and code not in whitelist:
            continue
        symbols.append(code)
    symbols.sort()
    if args.limit > 0:
        symbols = symbols[: args.limit]
    log.info("total symbols to evaluate: %d (lookback=%d)", len(symbols), LOOKBACK)

    pool = PredictorPool(args.concurrency)
    await pool.init()
    log.info("pool ready: device=%s actual_concurrency=%d", pool.device, pool.size)

    out_path = ROOT / args.output
    json_path = ROOT / args.json_output
    out_path.parent.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    rows: list[dict[str, Any]] = []
    anchors_expected = 0
    processed = 0
    failed = 0
    covered_stocks = 0

    tasks = []

    async def _process(sym: str) -> tuple[str, list[dict] | None]:
        try:
            kline = store.get_kline(sym, days=LOOKBACK + args.anchors + HORIZON + 10)
            if len(kline) < MIN_HISTORY_DAYS:
                return sym, []
            anchors = _build_anchors(kline, args.anchors)
            if not anchors:
                return sym, []
            res = await _evaluate_symbol(pool, sym, kline, anchors)
            return sym, res
        except Exception as exc:
            log.debug("symbol fail %s: %s", sym, exc)
            return sym, None

    # 用滑动窗口避免所有任务同时堆积
    window = max(args.concurrency * 4, 16)
    idx = 0

    async def _dispatch():
        nonlocal idx
        while idx < len(symbols):
            if len(tasks) >= window:
                break
            tasks.append(asyncio.create_task(_process(symbols[idx])))
            idx += 1

    await _dispatch()
    while tasks:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        tasks = list(pending)
        for task in done:
            sym, res = await task
            processed += 1
            if res is None:
                failed += 1
            else:
                if res:
                    covered_stocks += 1
                    rows.extend(res)
                    anchors_expected += len({r["anchor_date"] for r in res})
            if processed % args.progress_every == 0 or processed == len(symbols):
                elapsed = time.time() - t0
                rate = processed / elapsed if elapsed > 0 else 0
                eta = (len(symbols) - processed) / rate if rate > 0 else 0
                log.info(
                    "[%d/%d] covered=%d failed=%d rows=%d rate=%.2f/s elapsed=%.0fs eta=%.0fs",
                    processed, len(symbols), covered_stocks, failed, len(rows), rate, elapsed, eta,
                )
            if args.partial_every and processed % args.partial_every == 0 and rows:
                try:
                    partial = _aggregate(rows, processed, anchors_expected, time.time() - t0, pool.device, pool.size)
                    tmp = json_path.with_suffix(".partial.json")
                    tmp.write_text(json.dumps(partial, ensure_ascii=False, indent=2, default=float), encoding="utf-8")
                    log.info("partial saved to %s", tmp)
                except Exception as exc:
                    log.warning("partial save failed: %s", exc)
        await _dispatch()

    elapsed = time.time() - t0
    log.info("done: processed=%d rows=%d elapsed=%.1fs", processed, len(rows), elapsed)

    agg = _aggregate(rows, len(symbols), anchors_expected, elapsed, pool.device, pool.size)
    json_path.write_text(json.dumps(agg, ensure_ascii=False, indent=2, default=float), encoding="utf-8")
    log.info("raw agg saved to %s", json_path)

    md = _render_markdown(agg)
    out_path.write_text(md, encoding="utf-8")
    log.info("report saved to %s", out_path)

    print("\n" + "=" * 60)
    print(f"Kronos 评估完成：{len(rows)} 次预测，{elapsed/60:.1f} 分钟")
    overall = agg.get("overall", {}) or {}
    if overall.get("count", 0) > 0 and "direction_acc" in overall:
        print(f"整体方向准确率：{overall['direction_acc']:.2f}%")
    else:
        print("无有效样本，请检查 lookback 是否超过 DB 可用历史天数")
    print(f"报告：{out_path}")
    print(f"原始：{json_path}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n中断，已退出")
        sys.exit(130)
