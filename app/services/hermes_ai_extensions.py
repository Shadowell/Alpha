"""Hermes AI 能力扩展 — 6 项增强 MVP。

设计原则：
- 共享 HermesRuntime 的 _call_llm / _call_monitor_llm / memory / funnel / notice。
- 每项能力独立接口，失败降级（无 LLM 时走规则 fallback）。
- 所有写入走 kv_store（SqliteKVStore）+ 独立表，避免污染现有业务表。

能力列表：
- [A] AutoTradeLoop          ：让 Hermes 根据 buy 池自动下模拟盘 buy，止盈止损自动 sell（配开关）
- [C] BacktestLab            ：对"缩量启动"策略在 180 天历史上批量回测
- [D] ResearchCardGenerator  ：对 buy 池个股聚合 财报+概念+龙虎榜+Kronos+形态 生成研报
- [F] NewsInsightExtractor   ：把公告/龙虎榜/概念带到 LLM 上下文，输出"消息→标的→操作"链路
- [G] WeeklyReportBuilder    ：周五盘后生成周报，自动推飞书
"""
from __future__ import annotations

import asyncio
import json
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.services.time_utils import now_cn


# ==================== [A] 自动下单闭环 ====================

@dataclass
class AutoTradeConfig:
    enabled: bool = False
    max_positions: int = 5          # 最多持仓数
    position_size: int = 1000       # 每笔股数
    tp_pct: float = 8.0             # 止盈（%）
    sl_pct: float = -5.0            # 止损（%）
    min_llm_confidence: float = 0.6 # 自动买入最低置信门槛
    dry_run: bool = True            # dry_run=True 时不真下单


class AutoTradeLoop:
    """监听 buy 池变化，自动下模拟盘 buy；盘中监控持仓触发 tp/sl 后 sell。

    触发点：
    1. 每 60 秒轮询一次 buy 池 + open_positions，执行规则引擎
    2. 完全可关（enabled=False 时什么都不做）
    3. dry_run 只记录"本应该下单"，不真调 paper_trading
    """

    def __init__(
        self,
        paper_trading: Any,
        funnel_service: Any,
        hermes_memory: Any,
        get_realtime_price,  # async (symbol) -> (price, source)
        is_market_open,      # () -> bool
    ) -> None:
        self.paper = paper_trading
        self.funnel = funnel_service
        self.memory = hermes_memory
        self._get_price = get_realtime_price
        self._is_open = is_market_open
        self.config = AutoTradeConfig()
        self._running = False
        self._actions: list[dict] = []  # 最近 50 次动作
        self._last_tick_at: str | None = None

    def set_config(self, payload: dict) -> AutoTradeConfig:
        for k, v in (payload or {}).items():
            if hasattr(self.config, k):
                setattr(self.config, k, v)
        return self.config

    def get_snapshot(self) -> dict:
        return {
            "config": self.config.__dict__,
            "running": self._running,
            "last_tick_at": self._last_tick_at,
            "recent_actions": self._actions[-50:],
        }

    async def tick(self) -> dict:
        """一次完整规则扫描 + 执行。返回 {buys, sells, skipped, errors}。"""
        self._running = True
        self._last_tick_at = now_cn().isoformat(timespec="seconds")
        result = {"buys": 0, "sells": 0, "skipped": 0, "errors": 0, "messages": []}
        try:
            if not self.config.enabled:
                result["messages"].append("未启用")
                return result
            if not self._is_open():
                result["messages"].append("非交易时段")
                return result

            await self._auto_sell(result)
            await self._auto_buy(result)
        except Exception as exc:
            result["errors"] += 1
            result["messages"].append(f"tick 异常: {exc}")
            print(f"[auto_trade] tick failed: {exc}\n{traceback.format_exc()}")
        finally:
            self._running = False
        return result

    async def _auto_sell(self, result: dict) -> None:
        opens = self.paper.get_open_positions()
        for pos in opens:
            sym = pos["symbol"]
            cost = float(pos["cost_price"])
            price, _ = await self._get_price(sym)
            if price <= 0:
                continue
            pct = (price - cost) / cost * 100.0
            hit_tp = pct >= self.config.tp_pct
            hit_sl = pct <= self.config.sl_pct
            if not (hit_tp or hit_sl):
                continue
            reason = "止盈" if hit_tp else "止损"
            action = {
                "at": now_cn().isoformat(timespec="seconds"),
                "type": "sell",
                "symbol": sym,
                "name": pos.get("name", ""),
                "price": price,
                "pnl_pct": round(pct, 2),
                "reason": f"{reason}@{pct:+.2f}%",
                "dry_run": self.config.dry_run,
            }
            if self.config.dry_run:
                action["note"] = "DRY_RUN 未真实下单"
            else:
                try:
                    self.paper.close_position(pos["id"], price, note=f"auto_trade:{reason}")
                    action["note"] = "sell 成功"
                    result["sells"] += 1
                except Exception as e:
                    action["note"] = f"sell 失败: {e}"
                    result["errors"] += 1
            self._actions.append(action)
            result["messages"].append(f"{sym} {reason} {pct:+.2f}%")

    async def _auto_buy(self, result: dict) -> None:
        opens = self.paper.get_open_positions()
        if len(opens) >= self.config.max_positions:
            result["skipped"] += 1
            result["messages"].append(f"持仓已满 ({len(opens)}/{self.config.max_positions})")
            return

        funnel = await self.funnel.get_funnel()
        pools = funnel.pools if hasattr(funnel, "pools") else (funnel.get("pools", {}) if isinstance(funnel, dict) else {})
        buy_pool = pools.get("buy", []) if isinstance(pools, dict) else getattr(pools, "buy", [])

        holding = {p["symbol"] for p in opens}
        slots = self.config.max_positions - len(opens)

        for card in buy_pool:
            if slots <= 0:
                break
            sym = card.get("symbol", "") if isinstance(card, dict) else getattr(card, "symbol", "")
            nm = card.get("name", "") if isinstance(card, dict) else getattr(card, "name", "")
            score = float(card.get("score", 0) if isinstance(card, dict) else getattr(card, "score", 0) or 0)
            if not sym or sym in holding:
                continue
            if score < 70:  # 买入池内分数过低不进
                continue
            price, _ = await self._get_price(sym)
            if price <= 0:
                continue
            action = {
                "at": now_cn().isoformat(timespec="seconds"),
                "type": "buy",
                "symbol": sym,
                "name": nm,
                "price": price,
                "qty": self.config.position_size,
                "reason": f"buy池自动买入(score={score:.0f})",
                "dry_run": self.config.dry_run,
            }
            if self.config.dry_run:
                action["note"] = "DRY_RUN 未真实下单"
            else:
                try:
                    self.paper.open_position(sym, nm, price, self.config.position_size, note="auto_trade:buy池")
                    action["note"] = "buy 成功"
                    result["buys"] += 1
                    slots -= 1
                    holding.add(sym)
                except Exception as e:
                    action["note"] = f"buy 失败: {e}"
                    result["errors"] += 1
            self._actions.append(action)
            result["messages"].append(f"{sym} 自动买入 @{price:.2f}")


# ==================== [C] 策略回测实验室 ====================

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


# ==================== [D] 深度个股研报 ====================

class ResearchCardGenerator:
    """对 buy/focus 池个股生成聚合研报（含 Kronos 预测 + 概念 + 公告 + 形态）。"""

    def __init__(
        self,
        runtime: Any,
        kronos_service: Any,
        kline_store: Any,
        data_provider: Any,
        notice_service: Any,
    ) -> None:
        self.runtime = runtime
        self.kronos = kronos_service
        self.store = kline_store
        self.provider = data_provider
        self.notice = notice_service
        self._cache: dict[str, dict] = {}  # symbol -> {generated_at, markdown, ...}

    def get_cached(self, symbol: str) -> dict | None:
        item = self._cache.get(symbol)
        if not item:
            return None
        if (datetime.now() - datetime.fromisoformat(item["generated_at"])).total_seconds() > 3600:
            return None
        return item

    async def generate(self, symbol: str, name: str = "") -> dict:
        t0 = time.time()
        bundle: dict[str, Any] = {"symbol": symbol, "name": name}

        # 1. Kronos 预测
        try:
            pred = await self.kronos.predict(symbol, lookback=180, horizon=3)
            bundle["kronos"] = {
                "predicted_kline": pred.get("predicted_kline", []),
                "max_high": max((p["high"] for p in pred.get("predicted_kline", [])), default=0),
                "avg_close": sum(p["close"] for p in pred.get("predicted_kline", [])) / max(len(pred.get("predicted_kline", [])), 1),
            }
        except Exception as e:
            bundle["kronos"] = {"error": str(e)}

        # 2. 最近 30 天 K 线摘要
        try:
            rows = self.store.get_kline(symbol, days=30)
            if rows:
                latest = rows[-1]
                first = rows[0]
                bundle["kline"] = {
                    "latest_close": float(latest["close"]),
                    "period_return_pct": (float(latest["close"]) - float(first["close"])) / float(first["close"]) * 100,
                    "volume_avg": sum(float(r["volume"]) for r in rows) / len(rows),
                    "days_count": len(rows),
                }
        except Exception:
            pass

        # 3. 所属概念
        try:
            concepts = await self.provider.get_concepts_for_symbol(symbol) if hasattr(self.provider, "get_concepts_for_symbol") else []
            bundle["concepts"] = concepts[:6] if concepts else []
        except Exception:
            bundle["concepts"] = []

        # 4. 近期公告
        try:
            if hasattr(self.notice, "get_stock_notices"):
                n = await self.notice.get_stock_notices(symbol, days=7, limit=5)
                bundle["notices"] = n if isinstance(n, list) else []
            else:
                bundle["notices"] = []
        except Exception:
            bundle["notices"] = []

        # 5. LLM 综合成研报
        prompt = f"""你是 A 股投研分析师。根据以下数据为 {name}({symbol}) 生成一份 200 字中文研报卡片：

## K线数据
{json.dumps(bundle.get('kline', {}), ensure_ascii=False)}

## Kronos AI 预测（未来 3 天）
{json.dumps(bundle.get('kronos', {}), ensure_ascii=False)}

## 所属概念
{', '.join(bundle.get('concepts', []) or ['无'])}

## 近期公告
{json.dumps(bundle.get('notices', []), ensure_ascii=False)[:600]}

请输出 JSON 格式：
{{"verdict": "买入|观望|回避", "confidence": 0-100的数字, "bullish_points": ["要点1", "要点2"], "bearish_points": ["要点1"], "action": "具体操作建议 1 句话", "summary": "120 字以内综合评述"}}"""

        sys_prompt = "你是专业的 A 股投研分析师，基于数据输出客观研报。永远返回严格 JSON。"
        llm_result: dict | None = None
        try:
            llm_result = await self.runtime._call_llm(sys_prompt, prompt)
        except Exception as e:
            bundle["llm_error"] = str(e)

        bundle["llm"] = llm_result or {
            "verdict": "观望",
            "confidence": 40,
            "bullish_points": [],
            "bearish_points": ["LLM 未配置"],
            "action": "等待 LLM 配置完成后再生成研报",
            "summary": "当前 OPENAI_API_KEY 未配置，研报只含数据层，请参考 K 线和 Kronos 预测自行判断。",
        }
        bundle["generated_at"] = datetime.now().isoformat(timespec="seconds")
        bundle["elapsed_seconds"] = round(time.time() - t0, 2)
        self._cache[symbol] = bundle
        return bundle

# ==================== [F] 消息驱动分析 ====================

class NewsInsightExtractor:
    """把公告/龙虎榜/概念融合成 LLM 上下文，输出「消息 → 标的 → 操作」链路。"""

    def __init__(self, runtime: Any, notice_service: Any, funnel_service: Any) -> None:
        self.runtime = runtime
        self.notice = notice_service
        self.funnel = funnel_service
        self._last: dict | None = None

    def get_snapshot(self) -> dict | None:
        return self._last

    async def generate(self, trade_date: str | None = None, limit_notices: int = 20) -> dict:
        t0 = time.time()

        notice_snapshot = {}
        try:
            f = await self.notice.get_notice_funnel(trade_date)
            notice_snapshot = f.model_dump() if hasattr(f, "model_dump") else dict(f)
        except Exception as e:
            notice_snapshot = {"error": str(e)}

        hot_concepts = []
        try:
            hc = await self.funnel.get_hot_concepts(trade_date)
            items = hc.items if hasattr(hc, "items") else (hc.get("items", []) if isinstance(hc, dict) else [])
            for c in items[:10]:
                hot_concepts.append({
                    "name": c.get("name") if isinstance(c, dict) else getattr(c, "name", ""),
                    "change_pct": c.get("change_pct") if isinstance(c, dict) else getattr(c, "change_pct", 0),
                    "leader": c.get("leader") if isinstance(c, dict) else getattr(c, "leader", ""),
                })
        except Exception:
            pass

        notice_pools = notice_snapshot.get("pools", {}) if isinstance(notice_snapshot, dict) else {}
        notice_cards = []
        for pool_name in ("buy", "focus", "candidate"):
            for c in (notice_pools.get(pool_name, []) or [])[:8]:
                if isinstance(c, dict):
                    notice_cards.append({
                        "symbol": c.get("symbol"),
                        "name": c.get("name"),
                        "title": c.get("notice_title") or c.get("title"),
                        "tags": c.get("tags") or [],
                        "score": c.get("score") or 0,
                    })

        prompt = f"""基于以下 A 股实时数据，输出"消息驱动分析"JSON：

## 当日热门概念 TOP 10
{json.dumps(hot_concepts, ensure_ascii=False)}

## 公告精选（含 buy/focus/candidate 池 Top {len(notice_cards)}）
{json.dumps(notice_cards[:limit_notices], ensure_ascii=False)}

请输出严格 JSON：
{{
  "insights": [
    {{"message": "消息摘要", "symbols": ["代码1"], "operation": "买入/观望/回避", "confidence": 0-100, "reason": "为什么"}},
    ...最多 8 条
  ],
  "overall_mood": "风偏|中性|避险",
  "summary": "一句话收束"
}}"""

        sys_prompt = "你是 A 股消息面研究员。基于公告+龙虎榜+概念板块联动输出操作建议，严格返回 JSON。"
        llm = None
        try:
            llm = await self.runtime._call_llm(sys_prompt, prompt)
        except Exception:
            pass

        result = {
            "hot_concepts": hot_concepts,
            "notice_cards": notice_cards,
            "llm": llm or {
                "insights": [],
                "overall_mood": "中性",
                "summary": "LLM 未配置，降级输出原始数据。",
            },
            "generated_at": now_cn().isoformat(timespec="seconds"),
            "elapsed_seconds": round(time.time() - t0, 2),
        }
        self._last = result
        return result


# ==================== [G] 周报生成器 ====================

class WeeklyReportBuilder:
    """周五盘后生成周报并推送飞书。"""

    def __init__(
        self,
        runtime: Any,
        funnel_service: Any,
        notice_service: Any,
        paper_trading: Any,
    ) -> None:
        self.runtime = runtime
        self.funnel = funnel_service
        self.notice = notice_service
        self.paper = paper_trading
        self._last: dict | None = None

    def get_snapshot(self) -> dict | None:
        return self._last

    async def generate(self) -> dict:
        t0 = time.time()
        n = now_cn()
        week_start = (n - timedelta(days=n.weekday())).date()
        week_end = n.date()

        # 1. 本周热门概念
        hot_concepts = []
        try:
            hc = await self.funnel.get_hot_concepts()
            items = hc.items if hasattr(hc, "items") else (hc.get("items", []) if isinstance(hc, dict) else [])
            for c in items[:15]:
                hot_concepts.append({
                    "name": c.get("name") if isinstance(c, dict) else getattr(c, "name", ""),
                    "change_pct": c.get("change_pct") if isinstance(c, dict) else getattr(c, "change_pct", 0),
                })
        except Exception:
            pass

        # 2. 漏斗与公告 TopN
        funnel_stats = {}
        try:
            f = await self.funnel.get_funnel()
            stats = f.stats if hasattr(f, "stats") else (f.get("stats", {}) if isinstance(f, dict) else {})
            funnel_stats = {
                "candidate": stats.get("candidate", 0) if isinstance(stats, dict) else getattr(stats, "candidate", 0),
                "focus": stats.get("focus", 0) if isinstance(stats, dict) else getattr(stats, "focus", 0),
                "buy": stats.get("buy", 0) if isinstance(stats, dict) else getattr(stats, "buy", 0),
            }
        except Exception:
            pass

        notice_stats = {}
        try:
            f = await self.notice.get_notice_funnel()
            stats = f.stats if hasattr(f, "stats") else (f.get("stats", {}) if isinstance(f, dict) else {})
            notice_stats = {
                "candidate": stats.get("candidate", 0) if isinstance(stats, dict) else 0,
                "focus": stats.get("focus", 0) if isinstance(stats, dict) else 0,
                "buy": stats.get("buy", 0) if isinstance(stats, dict) else 0,
            }
        except Exception:
            pass

        # 3. 模拟盘周业绩
        paper_summary = {}
        try:
            paper_summary = self.paper.get_summary()
        except Exception:
            pass

        # 4. LLM 总结
        prompt = f"""基于以下 A 股数据生成本周（{week_start} - {week_end}）的量化系统周报，中文，约 300 字：

## 本周热门概念 Top 15
{json.dumps(hot_concepts, ensure_ascii=False)}

## 策略漏斗当前池子规模
{json.dumps(funnel_stats, ensure_ascii=False)}

## 公告漏斗当前池子规模
{json.dumps(notice_stats, ensure_ascii=False)}

## 模拟盘累计业绩
{json.dumps(paper_summary, ensure_ascii=False)}

请输出 JSON：
{{
  "headline": "一句话标题",
  "market_overview": "本周市场主线与板块轮动，2-3 句",
  "system_performance": "漏斗 + 模拟盘表现，2-3 句",
  "next_week_focus": ["关注点 1", "关注点 2", "关注点 3"],
  "risk_alerts": ["风险项 1"]
}}"""

        sys_prompt = "你是量化研究周报撰稿人。基于数据输出结构化周报 JSON。"
        llm = None
        try:
            llm = await self.runtime._call_llm(sys_prompt, prompt)
        except Exception:
            pass

        report = {
            "week_start": week_start.isoformat(),
            "week_end": week_end.isoformat(),
            "hot_concepts": hot_concepts,
            "funnel_stats": funnel_stats,
            "notice_stats": notice_stats,
            "paper_summary": paper_summary,
            "llm": llm or {
                "headline": "本周 Alpha 周报（LLM 降级）",
                "market_overview": "LLM 未配置，只输出数据层。",
                "system_performance": "见原始数据。",
                "next_week_focus": [],
                "risk_alerts": [],
            },
            "generated_at": now_cn().isoformat(timespec="seconds"),
            "elapsed_seconds": round(time.time() - t0, 2),
        }
        self._last = report

        # 推飞书
        try:
            await self._push_feishu(report)
        except Exception as e:
            report["feishu_push_error"] = str(e)

        return report

    async def _push_feishu(self, report: dict) -> None:
        from app.services.feishu_notify import CardBuilder, send_feishu_card

        llm = report.get("llm", {}) or {}
        hot = report.get("hot_concepts", [])[:5]
        hot_str = "、".join(f"{c['name']}({c.get('change_pct', 0)}%)" for c in hot) or "-"
        focus = llm.get("next_week_focus", []) or []
        risk_alerts = llm.get("risk_alerts", []) or []

        builder = (
            CardBuilder(
                title="📊 Alpha 周报",
                subtitle=f"{report['week_start']} ~ {report['week_end']}",
                template="indigo",
            )
            .add_markdown(f"**{llm.get('headline', '本周周报')}**")
            .add_kv_grid(
                [
                    ("🌊 市场", llm.get("market_overview", "-") or "-"),
                    ("⚙ 系统", llm.get("system_performance", "-") or "-"),
                ],
                cols=1,
            )
            .add_markdown(f"🔥 **热门** {hot_str}")
        )
        if focus:
            builder.add_hr()
            builder.add_markdown("🎯 **下周关注**\n" + "\n".join(f"- {f}" for f in focus[:5]))
        if risk_alerts:
            builder.add_markdown("⚠ **风险**\n" + "\n".join(f"- {r}" for r in risk_alerts[:3]))
        builder.add_note(f"生成于 {report.get('generated_at', '')}")
        await send_feishu_card(builder.build())
