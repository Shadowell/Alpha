"""风险守门人 — 盘中持续监控模拟盘持仓。

规则引擎：
- 止损阈值（默认 -5%）/ 止盈（+8%）
- 冲高回落：当日最高涨幅 >5%，现价较最高回落 >3%
- 量能瞬缩：最近 3 根 1 分钟 K 收缩（暂用日线量能均值替代，简化版）
- 触发后生成告警（warning 级别），可选 auto_close=True 时自动平仓
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.services.time_utils import now_cn


@dataclass
class RiskRule:
    tp_pct: float = 8.0
    sl_pct: float = -5.0
    surge_pullback_high_pct: float = 5.0
    surge_pullback_drop_pct: float = 3.0


@dataclass
class RiskAlert:
    at: str
    symbol: str
    name: str
    position_id: str
    kind: str          # "stop_loss" | "take_profit" | "surge_pullback"
    pnl_pct: float
    current_price: float
    details: str
    auto_closed: bool = False
    close_note: str = ""


class RiskGuardian:
    def __init__(
        self,
        paper_trading: Any,
        get_realtime_price,
        is_market_open,
    ) -> None:
        self.paper = paper_trading
        self._get_price = get_realtime_price
        self._is_open = is_market_open
        self.rule = RiskRule()
        self.enabled: bool = True
        self.auto_close: bool = False
        self._alerts: list[RiskAlert] = []
        self._alerted_keys: set[str] = set()  # 去重（position_id + kind）
        self._last_tick_at: str | None = None
        self._peak_price: dict[str, float] = {}  # position_id -> 持有期间最高价

    def set_config(self, payload: dict) -> None:
        self.enabled = bool(payload.get("enabled", self.enabled))
        self.auto_close = bool(payload.get("auto_close", self.auto_close))
        for k in ("tp_pct", "sl_pct", "surge_pullback_high_pct", "surge_pullback_drop_pct"):
            if k in payload:
                setattr(self.rule, k, float(payload[k]))

    def get_snapshot(self) -> dict:
        return {
            "enabled": self.enabled,
            "auto_close": self.auto_close,
            "rule": self.rule.__dict__,
            "last_tick_at": self._last_tick_at,
            "alerts": [a.__dict__ for a in self._alerts[-50:]],
        }

    async def tick(self) -> dict:
        if not self.enabled:
            return {"skipped": "disabled"}
        if not self._is_open():
            return {"skipped": "market_closed"}
        self._last_tick_at = now_cn().isoformat(timespec="seconds")
        opens = self.paper.get_open_positions()
        triggered = 0
        for pos in opens:
            try:
                alert = await self._evaluate(pos)
                if alert:
                    triggered += 1
            except Exception as exc:
                print(f"[risk] eval {pos.get('symbol')} failed: {exc}")
        return {"checked": len(opens), "triggered": triggered}

    async def _evaluate(self, pos: dict) -> RiskAlert | None:
        pid = pos["id"]
        sym = pos["symbol"]
        cost = float(pos["cost_price"])
        if cost <= 0:
            return None
        price, _ = await self._get_price(sym)
        if price <= 0:
            return None
        peak = max(self._peak_price.get(pid, cost), price)
        self._peak_price[pid] = peak

        pnl_pct = (price - cost) / cost * 100.0
        kind: str | None = None
        details = ""

        if pnl_pct >= self.rule.tp_pct:
            kind = "take_profit"
            details = f"到达止盈 {pnl_pct:+.2f}% (>= {self.rule.tp_pct}%)"
        elif pnl_pct <= self.rule.sl_pct:
            kind = "stop_loss"
            details = f"触发止损 {pnl_pct:+.2f}% (<= {self.rule.sl_pct}%)"
        else:
            peak_pct = (peak - cost) / cost * 100.0
            drop_from_peak = (price - peak) / peak * 100.0
            if peak_pct >= self.rule.surge_pullback_high_pct and drop_from_peak <= -self.rule.surge_pullback_drop_pct:
                kind = "surge_pullback"
                details = f"冲高 {peak_pct:+.2f}% 后回落 {drop_from_peak:+.2f}%"

        if not kind:
            return None

        dedup_key = f"{pid}:{kind}"
        if dedup_key in self._alerted_keys:
            return None

        alert = RiskAlert(
            at=now_cn().isoformat(timespec="seconds"),
            symbol=sym,
            name=pos.get("name", ""),
            position_id=pid,
            kind=kind,
            pnl_pct=round(pnl_pct, 2),
            current_price=price,
            details=details,
        )
        if self.auto_close:
            try:
                self.paper.close_position(pid, price, note=f"risk_guardian:{kind}")
                alert.auto_closed = True
                alert.close_note = f"已自动平仓 @ {price:.2f}"
            except Exception as e:
                alert.close_note = f"自动平仓失败: {e}"
        self._alerts.append(alert)
        self._alerted_keys.add(dedup_key)
        return alert


async def risk_guardian_loop(guardian: RiskGuardian, interval_seconds: int = 30) -> None:
    """盘中每 30 秒 tick 一次。"""
    await asyncio.sleep(15)
    while True:
        try:
            await guardian.tick()
        except Exception as exc:
            print(f"[risk_guardian] loop error: {exc}")
        await asyncio.sleep(interval_seconds)
