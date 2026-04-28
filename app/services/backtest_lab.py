"""Backtest helpers used by the custom strategy center."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from app.services.time_utils import now_cn


@dataclass
class BacktestResult:
    strategy: str
    params: dict
    total_signals: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    avg_hold_days: float = 0.0
    hit_rate: float = 0.0
    samples: list[dict] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    generated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "params": self.params,
            "total_signals": self.total_signals,
            "wins": self.wins,
            "losses": self.losses,
            "total_pnl_pct": round(self.total_pnl_pct, 2),
            "max_drawdown_pct": round(self.max_drawdown_pct, 2),
            "avg_hold_days": round(self.avg_hold_days, 2),
            "hit_rate": round(self.hit_rate, 2),
            "samples": self.samples[:50],  # 前端只展示 50 个样本
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            "generated_at": self.generated_at,
        }


class BacktestLab:
    """策略回测实验室 — 当前实现"缩量启动"策略。

    规则：
    - 每天把 T 日作为锚点，取 T 前 25 日横盘窗口 + T 日判定突破
    - 若满足"缩量启动 + 放量涨停"形态，作为一次买入信号
    - 按 hold_days (默认 3 天) 或 tp/sl 优先触发，计算收益
    """

    def __init__(self, kline_store: Any, name_lookup=None) -> None:
        self.store = kline_store
        self._name_lookup = name_lookup
        self._running = False
        self._last_result: dict | None = None

    def get_snapshot(self) -> dict | None:
        return self._last_result

    async def run(
        self,
        lookback_days: int = 25,
        hold_days: int = 3,
        tp_pct: float = 8.0,
        sl_pct: float = -5.0,
        amp_threshold: float = 0.20,
        vol_cv_threshold: float = 0.40,
        vol_spike_ratio: float = 3.0,
        require_limit_up: bool = True,
        limit: int | None = None,
    ) -> dict:
        from app.services.quiet_breakout_scanner import _limit_pct, _is_a_main
        self._running = True
        t0 = time.time()
        try:
            symbols = await asyncio.to_thread(self.store.get_all_symbols)
            symbols = [s for s in symbols if _is_a_main(s)]
            if limit:
                symbols = symbols[:limit]

            samples: list[dict] = []
            sem = asyncio.Semaphore(16)

            def _eval_symbol(sym: str) -> list[dict]:
                try:
                    rows = self.store.get_kline(sym, days=365)
                    if len(rows) < lookback_days + hold_days + 2:
                        return []
                    hits: list[dict] = []
                    import statistics as _st
                    N = lookback_days
                    for anchor in range(N, len(rows) - hold_days):
                        window = rows[anchor - N:anchor]
                        today = rows[anchor]
                        closes = [float(r["close"]) for r in window]
                        highs = [float(r["high"]) for r in window]
                        lows = [float(r["low"]) for r in window]
                        vols = [float(r["volume"]) for r in window]
                        if not closes or min(closes) <= 0:
                            continue
                        mean_c = sum(closes) / len(closes)
                        amp = (max(highs) - min(lows)) / mean_c
                        mean_v = sum(vols) / len(vols)
                        if mean_v <= 0:
                            continue
                        vol_cv = _st.pstdev(vols) / mean_v
                        prev_c = float(window[-1]["close"])
                        tc = float(today["close"])
                        tv = float(today["volume"])
                        if prev_c <= 0 or tc <= 0 or tv <= 0:
                            continue
                        chg = (tc / prev_c - 1.0) * 100.0
                        spike = tv / mean_v
                        name = self._name_lookup(sym) if self._name_lookup else ""
                        lim = _limit_pct(sym, name)
                        is_lu = chg >= (lim * 100.0 - 0.3)
                        if amp > amp_threshold:
                            continue
                        if vol_cv > vol_cv_threshold:
                            continue
                        if spike < vol_spike_ratio:
                            continue
                        if require_limit_up and not is_lu:
                            continue

                        # 模拟：次日开盘买入，hold_days 后收盘卖出，期间先触发 tp/sl
                        entry_price = float(rows[anchor + 1]["open"])  # 次日开盘进场
                        exit_price = None
                        exit_day = None
                        exit_reason = "hold_end"
                        for h in range(1, hold_days + 1):
                            if anchor + h >= len(rows):
                                break
                            bar = rows[anchor + h]
                            high = float(bar["high"])
                            low = float(bar["low"])
                            close_b = float(bar["close"])
                            # 最高价触发止盈
                            if (high - entry_price) / entry_price * 100 >= tp_pct:
                                exit_price = entry_price * (1 + tp_pct / 100)
                                exit_day = h
                                exit_reason = "tp"
                                break
                            # 最低价触发止损
                            if (low - entry_price) / entry_price * 100 <= sl_pct:
                                exit_price = entry_price * (1 + sl_pct / 100)
                                exit_day = h
                                exit_reason = "sl"
                                break
                            if h == hold_days:
                                exit_price = close_b
                                exit_day = h
                                exit_reason = "hold_end"
                        if exit_price is None or exit_day is None:
                            continue
                        pnl_pct = (exit_price - entry_price) / entry_price * 100.0
                        hits.append({
                            "symbol": sym,
                            "name": name,
                            "signal_date": str(today.get("date", "")),
                            "entry_date": str(rows[anchor + 1].get("date", "")),
                            "exit_date": str(rows[anchor + exit_day].get("date", "")),
                            "entry_price": round(entry_price, 3),
                            "exit_price": round(exit_price, 3),
                            "hold_days": exit_day,
                            "pnl_pct": round(pnl_pct, 2),
                            "reason": exit_reason,
                            "vol_spike": round(spike, 2),
                            "vol_cv": round(vol_cv, 3),
                            "amp_pct": round(amp * 100, 2),
                            "is_limit_up": bool(is_lu),
                        })
                    return hits
                except Exception as exc:
                    return []

            async def _bounded(sym: str):
                async with sem:
                    return await asyncio.to_thread(_eval_symbol, sym)

            tasks = [asyncio.create_task(_bounded(s)) for s in symbols]
            for fut in asyncio.as_completed(tasks):
                samples.extend(await fut)

            # 汇总统计
            wins = [s for s in samples if s["pnl_pct"] > 0]
            losses = [s for s in samples if s["pnl_pct"] <= 0]
            total_pnl = sum(s["pnl_pct"] for s in samples)
            avg_hold = sum(s["hold_days"] for s in samples) / max(len(samples), 1)
            hit_rate = len(wins) / max(len(samples), 1) * 100
            # 最大回撤（按 cumulative pnl）
            sorted_samples = sorted(samples, key=lambda x: x["signal_date"])
            cum = 0.0
            peak = 0.0
            mdd = 0.0
            for s in sorted_samples:
                cum += s["pnl_pct"]
                peak = max(peak, cum)
                mdd = min(mdd, cum - peak)

            sorted_samples.sort(key=lambda x: x["pnl_pct"], reverse=True)

            result = BacktestResult(
                strategy="quiet_breakout",
                params={
                    "lookback_days": lookback_days,
                    "hold_days": hold_days,
                    "tp_pct": tp_pct,
                    "sl_pct": sl_pct,
                    "amp_threshold": amp_threshold,
                    "vol_cv_threshold": vol_cv_threshold,
                    "vol_spike_ratio": vol_spike_ratio,
                    "require_limit_up": require_limit_up,
                },
                total_signals=len(samples),
                wins=len(wins),
                losses=len(losses),
                total_pnl_pct=total_pnl,
                max_drawdown_pct=mdd,
                avg_hold_days=avg_hold,
                hit_rate=hit_rate,
                samples=sorted_samples,
                elapsed_seconds=time.time() - t0,
                generated_at=now_cn().isoformat(timespec="seconds"),
            )
            self._last_result = result.to_dict()
            return self._last_result
        finally:
            self._running = False

    async def run_custom_strategy(
        self,
        strategy: Any,  # app.services.custom_strategy.CustomStrategy
        *,
        lookback_anchor_margin: int = 30,
        hold_days: int = 3,
        tp_pct: float = 8.0,
        sl_pct: float = -5.0,
        history_days: int = 220,
        limit: int | None = None,
    ) -> dict:
        """用自定义策略规则做历史 T 日信号回测。

        对每只股票：在最近 `history_days` 天内，把每个可取的 T 日当作锚点，用策略的 enabled 规则
        在 T 日及之前的 K 线（截断到 T 日为止）评估；全通过视为一次买入信号；次日开盘进场，
        按 hold_days / tp / sl 退出。
        """
        from app.services.custom_strategy import _is_a_main as _is_a_main_fn
        from app.services.strategy_rules import RULE_REGISTRY as _REG, RuleContext as _RC

        self._running = True
        t0 = time.time()
        try:
            enabled_refs = [r for r in strategy.rules if r.enabled and r.rule_code in _REG]
            if not enabled_refs:
                result = BacktestResult(
                    strategy=f"custom:{strategy.id}",
                    params={"name": strategy.name, "hold_days": hold_days, "tp_pct": tp_pct, "sl_pct": sl_pct},
                    generated_at=now_cn().isoformat(timespec="seconds"),
                )
                self._last_result = result.to_dict()
                return self._last_result

            symbols = await asyncio.to_thread(self.store.get_all_symbols)
            symbols = [s for s in symbols if _is_a_main_fn(s)]
            if limit and limit > 0:
                symbols = symbols[:limit]

            # 需要的最小 K 线窗口 = max(rule.min_kline_days) + hold_days + 1（次日进场）+ 余量
            min_needed = max((_REG[r.rule_code].min_kline_days for r in enabled_refs), default=30)
            window = max(min_needed, lookback_anchor_margin) + 5
            need_days = history_days + window + hold_days + 3

            sem = asyncio.Semaphore(16)
            samples: list[dict] = []

            def _eval_symbol(sym: str) -> list[dict]:
                try:
                    rows = self.store.get_kline(sym, days=need_days)
                    if len(rows) < window + hold_days + 2:
                        return []
                    name = ""
                    if self._name_lookup is not None:
                        try:
                            name = self._name_lookup(sym) or ""
                        except Exception:
                            name = ""
                    hits: list[dict] = []
                    start_anchor = max(window, len(rows) - history_days)
                    for anchor in range(start_anchor, len(rows) - hold_days - 1):
                        ctx = _RC(
                            symbol=sym,
                            name=name,
                            kline=rows[: anchor + 1],  # 包含 T 日
                            trade_date=str(rows[anchor].get("date") or ""),
                        )
                        all_pass = True
                        for ref in enabled_refs:
                            spec = _REG[ref.rule_code]
                            if not spec.evaluate(ctx, ref.params).passed:
                                all_pass = False
                                break
                        if not all_pass:
                            continue

                        entry_bar = rows[anchor + 1]
                        entry_price = float(entry_bar.get("open") or 0)
                        if entry_price <= 0:
                            continue
                        exit_price: float | None = None
                        exit_day: int | None = None
                        exit_reason = "hold_end"
                        for h in range(1, hold_days + 1):
                            idx = anchor + h
                            if idx >= len(rows):
                                break
                            bar = rows[idx]
                            high = float(bar.get("high") or 0)
                            low = float(bar.get("low") or 0)
                            close_b = float(bar.get("close") or 0)
                            if high > 0 and (high - entry_price) / entry_price * 100 >= tp_pct:
                                exit_price = entry_price * (1 + tp_pct / 100)
                                exit_day = h
                                exit_reason = "tp"
                                break
                            if low > 0 and (low - entry_price) / entry_price * 100 <= sl_pct:
                                exit_price = entry_price * (1 + sl_pct / 100)
                                exit_day = h
                                exit_reason = "sl"
                                break
                            if h == hold_days and close_b > 0:
                                exit_price = close_b
                                exit_day = h
                                exit_reason = "hold_end"
                        if exit_price is None or exit_day is None:
                            continue
                        pnl_pct = (exit_price - entry_price) / entry_price * 100.0
                        hits.append({
                            "symbol": sym,
                            "name": name,
                            "signal_date": str(rows[anchor].get("date", "")),
                            "entry_date": str(rows[anchor + 1].get("date", "")),
                            "exit_date": str(rows[anchor + exit_day].get("date", "")),
                            "entry_price": round(entry_price, 3),
                            "exit_price": round(exit_price, 3),
                            "hold_days": exit_day,
                            "pnl_pct": round(pnl_pct, 2),
                            "reason": exit_reason,
                        })
                    return hits
                except Exception:
                    return []

            async def _bounded(sym: str):
                async with sem:
                    return await asyncio.to_thread(_eval_symbol, sym)

            tasks = [asyncio.create_task(_bounded(s)) for s in symbols]
            for fut in asyncio.as_completed(tasks):
                samples.extend(await fut)

            wins = [s for s in samples if s["pnl_pct"] > 0]
            losses = [s for s in samples if s["pnl_pct"] <= 0]
            total_pnl = sum(s["pnl_pct"] for s in samples)
            avg_hold = sum(s["hold_days"] for s in samples) / max(len(samples), 1)
            hit_rate = len(wins) / max(len(samples), 1) * 100
            sorted_samples = sorted(samples, key=lambda x: x["signal_date"])
            cum = 0.0
            peak = 0.0
            mdd = 0.0
            for s in sorted_samples:
                cum += s["pnl_pct"]
                peak = max(peak, cum)
                mdd = min(mdd, cum - peak)
            sorted_samples.sort(key=lambda x: x["pnl_pct"], reverse=True)

            result = BacktestResult(
                strategy=f"custom:{strategy.id}",
                params={
                    "strategy_id": strategy.id,
                    "strategy_name": strategy.name,
                    "rules_count": len(enabled_refs),
                    "history_days": history_days,
                    "hold_days": hold_days,
                    "tp_pct": tp_pct,
                    "sl_pct": sl_pct,
                },
                total_signals=len(samples),
                wins=len(wins),
                losses=len(losses),
                total_pnl_pct=total_pnl,
                max_drawdown_pct=mdd,
                avg_hold_days=avg_hold,
                hit_rate=hit_rate,
                samples=sorted_samples,
                elapsed_seconds=time.time() - t0,
                generated_at=now_cn().isoformat(timespec="seconds"),
            )
            return result.to_dict()
        finally:
            self._running = False
