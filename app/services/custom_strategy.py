"""自定义策略 · 数据模型 / 扫描器 / 内置预设。

设计原则：
- 策略 = (元数据) + (规则列表)，其中每条规则引用 `strategy_rules.RULE_REGISTRY` 中的 code 并带独立参数覆盖。
- 扫描：AND 模式。所有 enabled=True 的规则必须通过；任何一条不过则丢弃。
- 综合分 composite_score：命中规则数 × 10 + 辅助分（放量倍数 / 箱体紧密度 / 涨停加分）。
- 与现有 `QuietBreakoutScanner` 并存：前端"策略中心"走本模块；`/api/strategy/quiet-breakout` 老接口后续通过适配器转发。
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.services.strategy_rules import (
    RULE_REGISTRY,
    RuleContext,
    RuleResult,
    RuleSpec,
    get_rule,
)

log = logging.getLogger("custom_strategy")


# ─────────────────────────────── 数据模型 ───────────────────────────────


@dataclass
class StrategyRuleRef:
    """策略内一条规则引用。"""

    rule_code: str
    enabled: bool = True
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"rule_code": self.rule_code, "enabled": self.enabled, "params": dict(self.params)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StrategyRuleRef:
        return cls(
            rule_code=str(data.get("rule_code", "")),
            enabled=bool(data.get("enabled", True)),
            params=dict(data.get("params", {}) or {}),
        )


@dataclass
class CustomStrategy:
    """用户定义的选股策略。"""

    id: str
    name: str
    description: str
    rules: list[StrategyRuleRef]
    is_builtin: bool = False
    is_default: bool = False
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "is_builtin": self.is_builtin,
            "is_default": self.is_default,
            "rules": [r.to_dict() for r in self.rules],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CustomStrategy:
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex),
            name=str(data.get("name", "未命名策略")),
            description=str(data.get("description", "")),
            is_builtin=bool(data.get("is_builtin", False)),
            is_default=bool(data.get("is_default", False)),
            rules=[StrategyRuleRef.from_dict(r) for r in (data.get("rules") or [])],
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
        )

    def enabled_rules(self) -> list[StrategyRuleRef]:
        return [r for r in self.rules if r.enabled and r.rule_code in RULE_REGISTRY]


# ─────────────────────────────── 扫描结果 ───────────────────────────────


@dataclass
class StrategyHit:
    symbol: str
    name: str
    trade_date: str
    close: float
    prev_close: float
    change_pct: float
    composite_score: float
    rule_hits: list[dict[str, Any]]   # [{code, title, passed, label, metric, detail}]

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "trade_date": self.trade_date,
            "close": round(self.close, 2),
            "prev_close": round(self.prev_close, 2),
            "change_pct": round(self.change_pct, 2),
            "composite_score": round(self.composite_score, 2),
            "rule_hits": self.rule_hits,
        }


# ─────────────────────────────── 内置策略 ───────────────────────────────


def _builtin_strategies_seed() -> list[CustomStrategy]:
    """系统预置的 3 条策略（id 固定，方便幂等写入）。"""
    ts = datetime.now().isoformat(timespec="seconds")
    return [
        CustomStrategy(
            id="builtin_quiet_breakout",
            name="缩量启动（内置）",
            description="前 N 天缩量横盘 + 今日放量涨停。适合捕捉底部首板启动。",
            is_builtin=True,
            is_default=True,
            created_at=ts,
            updated_at=ts,
            rules=[
                StrategyRuleRef("exclude_boards", True, {"exclude_st": True, "exclude_bse": True}),
                StrategyRuleRef("price_range", True, {"min": 3.0, "max": 60.0}),
                StrategyRuleRef("box_consolidation", True, {"lookback": 25, "max_amp_pct": 20.0}),
                StrategyRuleRef("volume_shrink", True, {"lookback": 25, "max_cv": 0.40}),
                StrategyRuleRef("volume_spike_today", True, {"lookback": 25, "min_ratio": 3.0}),
                StrategyRuleRef("limit_up_today", True, {"tolerance": 0.3}),
            ],
        ),
        CustomStrategy(
            id="builtin_adjustment_box",
            name="调整期横盘（内置）",
            description="缩量 + 箱体窄幅 + 未破前高，用于发现调整末期。",
            is_builtin=True,
            created_at=ts,
            updated_at=ts,
            rules=[
                StrategyRuleRef("exclude_boards", True, {"exclude_st": True, "exclude_gem": True, "exclude_star": True, "exclude_bse": True}),
                StrategyRuleRef("price_range", True, {"min": 5.0, "max": 80.0}),
                StrategyRuleRef("box_consolidation", True, {"lookback": 20, "max_amp_pct": 18.0}),
                StrategyRuleRef("volume_shrink", True, {"lookback": 20, "max_cv": 0.55}),
                StrategyRuleRef("below_prior_high", True, {"lookback": 20, "buffer": 0.995}),
                StrategyRuleRef("drawdown_from_high", True, {"lookback": 60, "min_pct": 5.0, "max_pct": 25.0}),
            ],
        ),
        CustomStrategy(
            id="builtin_breakout_volume",
            name="放量突破前高（内置）",
            description="均线多头 + 收盘突破 N 日前高 + 放量放大。适合强势趋势跟进。",
            is_builtin=True,
            created_at=ts,
            updated_at=ts,
            rules=[
                StrategyRuleRef("exclude_boards", True, {"exclude_st": True, "exclude_bse": True}),
                StrategyRuleRef("price_range", True, {"min": 5.0, "max": 80.0}),
                StrategyRuleRef("ma_bull_stack", True, {"periods": "5,10,20"}),
                StrategyRuleRef("break_prior_high", True, {"lookback": 30, "buffer": 1.000}),
                StrategyRuleRef("volume_spike_today", True, {"lookback": 20, "min_ratio": 1.8}),
                StrategyRuleRef("change_pct_today_range", True, {"min": 3.0, "max": 9.5}),
            ],
        ),
    ]


BUILTIN_STRATEGIES: list[CustomStrategy] = _builtin_strategies_seed()


# ─────────────────────────────── 扫描器 ───────────────────────────────


def _is_a_main(symbol: str) -> bool:
    s = (symbol or "").strip()
    if len(s) != 6:
        return False
    return s[:2] in ("00", "30", "60", "68")


def _compute_score(rule_hits: list[dict[str, Any]]) -> float:
    """综合分：命中规则数 × 10 + 辅助加分（放量/箱体紧凑/涨停）。"""
    passed = [h for h in rule_hits if h.get("passed")]
    base = len(passed) * 10.0
    bonus = 0.0
    for h in passed:
        code = h.get("code", "")
        m = h.get("metric")
        if m is None:
            continue
        if code == "volume_spike_today":
            bonus += min(float(m) * 1.5, 30.0)
        elif code == "box_consolidation":
            bonus += max(0.0, 20.0 - float(m)) * 0.3
        elif code == "volume_shrink":
            bonus += max(0.0, (0.6 - float(m))) * 20.0
        elif code == "limit_up_today":
            bonus += 8.0
        elif code == "break_prior_high":
            bonus += (float(m) - 1.0) * 100.0 if float(m) > 1.0 else 0.0
    return base + bonus


class CustomStrategyScanner:
    """对全 A 股 K 线库按策略规则筛选。"""

    def __init__(self, kline_store: Any, name_lookup=None) -> None:
        self.store = kline_store
        self._name_lookup = name_lookup
        self._lock = asyncio.Lock()
        self._last_snapshots: dict[str, dict[str, Any]] = {}  # strategy_id -> payload
        self._running_strategies: set[str] = set()

    def get_last_snapshot(self, strategy_id: str) -> dict[str, Any] | None:
        return self._last_snapshots.get(strategy_id)

    def is_running(self, strategy_id: str) -> bool:
        return strategy_id in self._running_strategies

    async def scan(self, strategy: CustomStrategy, *, limit: int | None = None) -> dict[str, Any]:
        if strategy.id in self._running_strategies:
            raise RuntimeError("该策略扫描正在进行中")
        self._running_strategies.add(strategy.id)
        try:
            async with self._lock:
                return await self._scan_impl(strategy, limit)
        finally:
            self._running_strategies.discard(strategy.id)

    async def _scan_impl(self, strategy: CustomStrategy, limit: int | None) -> dict[str, Any]:
        t0 = time.time()
        enabled_refs = strategy.enabled_rules()
        if not enabled_refs:
            payload = {
                "strategy_id": strategy.id,
                "strategy_name": strategy.name,
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "total_scanned": 0,
                "total_hits": 0,
                "elapsed_seconds": 0.0,
                "hits": [],
                "rules_count": 0,
                "running": False,
                "message": "未勾选任何规则",
            }
            self._last_snapshots[strategy.id] = payload
            return payload

        # 计算需要的 K 线天数
        need_days = max((RULE_REGISTRY[r.rule_code].min_kline_days for r in enabled_refs), default=30) + 5
        need_days = max(need_days, 30)

        symbols = await asyncio.to_thread(self.store.get_all_symbols)
        symbols = [s for s in symbols if _is_a_main(s)]
        if limit and limit > 0:
            symbols = symbols[:limit]

        sem = asyncio.Semaphore(16)
        hits: list[StrategyHit] = []

        def _eval_symbol(sym: str) -> StrategyHit | None:
            try:
                rows = self.store.get_kline(sym, days=need_days)
                if not rows:
                    return None
                name = ""
                if self._name_lookup is not None:
                    try:
                        name = self._name_lookup(sym) or ""
                    except Exception:
                        name = ""
                ctx = RuleContext(
                    symbol=sym,
                    name=name,
                    kline=rows,
                    trade_date=str(rows[-1].get("date") or ""),
                )
                rule_hits: list[dict[str, Any]] = []
                for ref in enabled_refs:
                    spec: RuleSpec | None = RULE_REGISTRY.get(ref.rule_code)
                    if spec is None:
                        return None
                    res = spec.evaluate(ctx, ref.params)
                    if not res.passed:
                        return None  # AND 模式：任何不过则丢弃
                    rule_hits.append({
                        "code": spec.code,
                        "title": spec.title,
                        "passed": True,
                        "label": res.label,
                        "metric": res.metric,
                        "detail": res.detail,
                    })
                if not rule_hits:
                    return None
                today = rows[-1]
                prev = rows[-2] if len(rows) >= 2 else rows[-1]
                prev_close = float(prev.get("close") or 0)
                close = float(today.get("close") or 0)
                chg = ((close / prev_close) - 1.0) * 100.0 if prev_close > 0 else 0.0
                return StrategyHit(
                    symbol=sym,
                    name=name,
                    trade_date=str(today.get("date") or ""),
                    close=close,
                    prev_close=prev_close,
                    change_pct=chg,
                    composite_score=_compute_score(rule_hits),
                    rule_hits=rule_hits,
                )
            except Exception as exc:  # noqa: BLE001
                log.debug("scan %s failed: %s", sym, exc)
                return None

        async def _bounded(sym: str) -> StrategyHit | None:
            async with sem:
                return await asyncio.to_thread(_eval_symbol, sym)

        tasks = [asyncio.create_task(_bounded(s)) for s in symbols]
        for fut in asyncio.as_completed(tasks):
            result = await fut
            if result is not None:
                hits.append(result)

        hits.sort(key=lambda h: (-h.composite_score, -abs(h.change_pct)))
        payload = {
            "strategy_id": strategy.id,
            "strategy_name": strategy.name,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "total_scanned": len(symbols),
            "total_hits": len(hits),
            "elapsed_seconds": round(time.time() - t0, 2),
            "rules_count": len(enabled_refs),
            "hits": [h.to_dict() for h in hits],
            "running": False,
        }
        self._last_snapshots[strategy.id] = payload
        log.info(
            "custom_strategy scan id=%s name=%s scanned=%d hits=%d elapsed=%.2fs",
            strategy.id, strategy.name, len(symbols), len(hits), payload["elapsed_seconds"],
        )
        return payload
