"""自定义策略 · 原子规则引擎。

把"形态 / 指标 / 过滤条件"抽象成一批**原子规则**，每条规则只做一件事：
输入 K 线序列 + 参数，返回 (passed, metric, detail)。

上层 CustomStrategyRunner 按用户选择组合一批规则（AND 模式），
对全 A 股评估每只股票。前端通过 /api/strategy/rules 拉取 "规则目录"
动态渲染参数表单。

所有规则仅消费已有 KlineSQLiteStore 提供的字段：date/open/high/low/close/volume，
不引入新的数据源。规则如果对参数无效（例如 K 线数据不足），返回 passed=False。
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any, Callable


# ─────────────────────────────── 基础类型 ───────────────────────────────


@dataclass
class RuleContext:
    """评估一只股票时传入的上下文。"""

    symbol: str
    name: str
    kline: list[dict[str, Any]]  # 按日期升序；最后一条 = 今日
    trade_date: str = ""


@dataclass
class RuleResult:
    passed: bool
    label: str = ""           # 人类可读摘要，例如 "放量 3.2x"
    metric: float | None = None  # 关键数值（排序/展示用）
    detail: str = ""          # 长说明（可选）

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "label": self.label,
            "metric": self.metric,
            "detail": self.detail,
        }


@dataclass
class RuleParam:
    key: str
    label: str
    type: str                       # int / float / pct / bool / text
    default: Any
    min: float | None = None
    max: float | None = None
    step: float | None = None
    hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "type": self.type,
            "default": self.default,
            "min": self.min,
            "max": self.max,
            "step": self.step,
            "hint": self.hint,
        }


@dataclass
class RuleSpec:
    code: str
    title: str
    category: str                       # price / volume / pattern / trend / filter
    description: str
    params: list[RuleParam]
    evaluator: Callable[[RuleContext, dict[str, Any]], RuleResult]
    min_kline_days: int = 1             # 对 K 线长度的最低要求

    def default_params(self) -> dict[str, Any]:
        return {p.key: p.default for p in self.params}

    def evaluate(self, ctx: RuleContext, params: dict[str, Any]) -> RuleResult:
        if len(ctx.kline) < self.min_kline_days:
            return RuleResult(passed=False, label="K线不足", detail=f"需至少 {self.min_kline_days} 日 K 线")
        merged = self.default_params()
        merged.update({k: v for k, v in (params or {}).items() if k in merged})
        try:
            return self.evaluator(ctx, merged)
        except Exception as exc:  # noqa: BLE001
            return RuleResult(passed=False, label="评估失败", detail=str(exc))

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "title": self.title,
            "category": self.category,
            "description": self.description,
            "params": [p.to_dict() for p in self.params],
            "min_kline_days": self.min_kline_days,
        }


# ─────────────────────────────── 工具函数 ───────────────────────────────


def _col(rows: list[dict], key: str) -> list[float]:
    out: list[float] = []
    for r in rows:
        v = r.get(key)
        if v is None:
            continue
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            continue
    return out


def _limit_pct(symbol: str, name: str) -> float:
    """A 股涨停阈值（小数）。"""
    s = (symbol or "").strip()
    nm = (name or "").upper()
    if "ST" in nm:
        return 0.05
    if s.startswith("30") or s.startswith("688"):
        return 0.20
    return 0.10


# ─────────────────────────────── 规则实现 ───────────────────────────────


def _rule_price_range(ctx: RuleContext, p: dict[str, Any]) -> RuleResult:
    close = float(ctx.kline[-1].get("close") or 0)
    lo = float(p.get("min", 0) or 0)
    hi = float(p.get("max", 1e9) or 1e9)
    ok = close > 0 and lo <= close <= hi
    return RuleResult(
        passed=ok,
        label=f"收盘 {close:.2f}",
        metric=round(close, 3),
        detail=f"区间 [{lo:.2f}, {hi:.2f}]",
    )


def _rule_change_pct_today(ctx: RuleContext, p: dict[str, Any]) -> RuleResult:
    rows = ctx.kline
    if len(rows) < 2:
        return RuleResult(passed=False, label="K线不足")
    prev = float(rows[-2].get("close") or 0)
    today = float(rows[-1].get("close") or 0)
    if prev <= 0 or today <= 0:
        return RuleResult(passed=False, label="无效价格")
    chg = (today / prev - 1.0) * 100.0
    lo = float(p.get("min", -20) or -20)
    hi = float(p.get("max", 20) or 20)
    ok = lo <= chg <= hi
    return RuleResult(
        passed=ok,
        label=f"涨跌 {chg:+.2f}%",
        metric=round(chg, 2),
        detail=f"阈值 [{lo:+.1f}%, {hi:+.1f}%]",
    )


def _rule_limit_up_today(ctx: RuleContext, p: dict[str, Any]) -> RuleResult:
    rows = ctx.kline
    if len(rows) < 2:
        return RuleResult(passed=False, label="K线不足")
    prev = float(rows[-2].get("close") or 0)
    today = float(rows[-1].get("close") or 0)
    if prev <= 0 or today <= 0:
        return RuleResult(passed=False, label="无效价格")
    chg = (today / prev - 1.0) * 100.0
    tol = float(p.get("tolerance", 0.3) or 0.3)  # 允许略低于涨停 0.3%
    lim = _limit_pct(ctx.symbol, ctx.name) * 100.0
    ok = chg >= (lim - tol)
    return RuleResult(
        passed=ok,
        label=f"涨幅 {chg:+.2f}% / 涨停 {lim:.0f}%",
        metric=round(chg, 2),
        detail=f"容忍 {tol:.1f}%",
    )


def _rule_box_consolidation(ctx: RuleContext, p: dict[str, Any]) -> RuleResult:
    n = int(p.get("lookback", 20) or 20)
    max_amp = float(p.get("max_amp_pct", 18) or 18)
    rows = ctx.kline
    if len(rows) < n + 1:
        return RuleResult(passed=False, label="K线不足")
    window = rows[-(n + 1):-1]   # 不含今日
    highs = _col(window, "high")
    lows = _col(window, "low")
    closes = _col(window, "close")
    if len(closes) < n or not closes or min(closes) <= 0:
        return RuleResult(passed=False, label="无效窗口")
    mean_c = sum(closes) / len(closes)
    amp = (max(highs) - min(lows)) / mean_c * 100.0
    ok = amp <= max_amp
    return RuleResult(
        passed=ok,
        label=f"箱体{n}天 振幅{amp:.1f}%",
        metric=round(amp, 2),
        detail=f"阈值 ≤ {max_amp:.1f}%",
    )


def _rule_volume_shrink(ctx: RuleContext, p: dict[str, Any]) -> RuleResult:
    n = int(p.get("lookback", 20) or 20)
    max_cv = float(p.get("max_cv", 0.4) or 0.4)
    rows = ctx.kline
    if len(rows) < n + 1:
        return RuleResult(passed=False, label="K线不足")
    vols = _col(rows[-(n + 1):-1], "volume")
    if len(vols) < n:
        return RuleResult(passed=False, label="无效窗口")
    mean_v = sum(vols) / len(vols)
    if mean_v <= 0:
        return RuleResult(passed=False, label="均量为0")
    cv = statistics.pstdev(vols) / mean_v
    ok = cv <= max_cv
    return RuleResult(
        passed=ok,
        label=f"量能CV {cv:.2f}",
        metric=round(cv, 3),
        detail=f"阈值 ≤ {max_cv:.2f}",
    )


def _rule_volume_spike_today(ctx: RuleContext, p: dict[str, Any]) -> RuleResult:
    n = int(p.get("lookback", 20) or 20)
    min_ratio = float(p.get("min_ratio", 2.0) or 2.0)
    rows = ctx.kline
    if len(rows) < n + 1:
        return RuleResult(passed=False, label="K线不足")
    window = rows[-(n + 1):-1]
    today = rows[-1]
    vols = _col(window, "volume")
    if not vols:
        return RuleResult(passed=False, label="无效窗口")
    mean_v = sum(vols) / len(vols)
    today_v = float(today.get("volume") or 0)
    if mean_v <= 0 or today_v <= 0:
        return RuleResult(passed=False, label="量能无效")
    ratio = today_v / mean_v
    ok = ratio >= min_ratio
    return RuleResult(
        passed=ok,
        label=f"放量 {ratio:.2f}x",
        metric=round(ratio, 2),
        detail=f"近{n}日均量对比，阈值 ≥ {min_ratio:.1f}x",
    )


def _rule_break_prior_high(ctx: RuleContext, p: dict[str, Any]) -> RuleResult:
    n = int(p.get("lookback", 30) or 30)
    buffer = float(p.get("buffer", 1.0) or 1.0)  # 倍数：1.0=等于前高，1.003=突破0.3%
    rows = ctx.kline
    if len(rows) < n + 1:
        return RuleResult(passed=False, label="K线不足")
    window = rows[-(n + 1):-1]
    today = rows[-1]
    highs = _col(window, "high")
    close_today = float(today.get("close") or 0)
    if not highs or close_today <= 0:
        return RuleResult(passed=False, label="无效数据")
    prior_high = max(highs)
    threshold = prior_high * buffer
    ok = close_today >= threshold
    ratio = close_today / prior_high if prior_high > 0 else 0
    return RuleResult(
        passed=ok,
        label=f"收盘/前高 {ratio:.3f}",
        metric=round(ratio, 4),
        detail=f"前{n}日最高 {prior_high:.2f}，需 ≥ ×{buffer:.3f}",
    )


def _rule_below_prior_high(ctx: RuleContext, p: dict[str, Any]) -> RuleResult:
    """与 break_prior_high 对称：用于"调整期未突破"。"""
    n = int(p.get("lookback", 20) or 20)
    buffer = float(p.get("buffer", 0.995) or 0.995)  # 收盘 ≤ 前高 × buffer
    rows = ctx.kline
    if len(rows) < n + 1:
        return RuleResult(passed=False, label="K线不足")
    window = rows[-(n + 1):-1]
    today = rows[-1]
    highs = _col(window, "high")
    close_today = float(today.get("close") or 0)
    if not highs or close_today <= 0:
        return RuleResult(passed=False, label="无效数据")
    prior_high = max(highs)
    threshold = prior_high * buffer
    ok = close_today <= threshold
    ratio = close_today / prior_high if prior_high > 0 else 0
    return RuleResult(
        passed=ok,
        label=f"收盘/前高 {ratio:.3f}",
        metric=round(ratio, 4),
        detail=f"前{n}日最高 {prior_high:.2f}，需 ≤ ×{buffer:.3f}",
    )


def _rule_ma_above(ctx: RuleContext, p: dict[str, Any]) -> RuleResult:
    n = int(p.get("ma_n", 20) or 20)
    rows = ctx.kline
    if len(rows) < n:
        return RuleResult(passed=False, label="K线不足")
    closes = _col(rows[-n:], "close")
    if len(closes) < n:
        return RuleResult(passed=False, label="无效数据")
    ma = sum(closes) / n
    close_today = float(rows[-1].get("close") or 0)
    ok = close_today > ma
    return RuleResult(
        passed=ok,
        label=f"收盘 {close_today:.2f} / MA{n} {ma:.2f}",
        metric=round(close_today / ma - 1, 4) if ma > 0 else 0,
        detail=f"需 收盘 > MA({n})",
    )


def _rule_ma_bull_stack(ctx: RuleContext, p: dict[str, Any]) -> RuleResult:
    raw = p.get("periods") or [5, 10, 20]
    try:
        periods = sorted({int(x) for x in raw if int(x) > 0})
    except Exception:
        periods = [5, 10, 20]
    if len(periods) < 2:
        return RuleResult(passed=False, label="均线数<2")
    rows = ctx.kline
    need = max(periods)
    if len(rows) < need:
        return RuleResult(passed=False, label="K线不足")
    closes = _col(rows, "close")
    mas: list[tuple[int, float]] = []
    for n in periods:
        seg = closes[-n:]
        if len(seg) < n:
            return RuleResult(passed=False, label=f"MA{n}不足")
        mas.append((n, sum(seg) / n))
    ok = all(mas[i][1] > mas[i + 1][1] for i in range(len(mas) - 1))
    label = " > ".join([f"MA{n}{v:.2f}" for n, v in mas])
    return RuleResult(
        passed=ok,
        label=label,
        metric=1.0 if ok else 0.0,
        detail=f"均线多头排列（{', '.join(str(n) for n, _ in mas)}）",
    )


def _rule_drawdown_from_high(ctx: RuleContext, p: dict[str, Any]) -> RuleResult:
    n = int(p.get("lookback", 30) or 30)
    min_dd = float(p.get("min_pct", 0) or 0)
    max_dd = float(p.get("max_pct", 30) or 30)
    rows = ctx.kline
    if len(rows) < n + 1:
        return RuleResult(passed=False, label="K线不足")
    window = rows[-(n + 1):]
    highs = _col(window, "high")
    close_today = float(rows[-1].get("close") or 0)
    if not highs or close_today <= 0:
        return RuleResult(passed=False, label="无效数据")
    peak = max(highs)
    dd = (peak - close_today) / peak * 100.0 if peak > 0 else 0
    ok = min_dd <= dd <= max_dd
    return RuleResult(
        passed=ok,
        label=f"回撤 {dd:.1f}%",
        metric=round(dd, 2),
        detail=f"近{n}日最高 {peak:.2f} → 当前，阈值 [{min_dd:.1f}%, {max_dd:.1f}%]",
    )


def _rule_exclude_boards(ctx: RuleContext, p: dict[str, Any]) -> RuleResult:
    sym = (ctx.symbol or "").strip()
    nm = (ctx.name or "").upper()
    exclude_st = bool(p.get("exclude_st", True))
    exclude_gem = bool(p.get("exclude_gem", False))
    exclude_star = bool(p.get("exclude_star", False))
    exclude_bse = bool(p.get("exclude_bse", True))
    reasons: list[str] = []
    if exclude_st and "ST" in nm:
        reasons.append("ST")
    if exclude_gem and sym.startswith("30"):
        reasons.append("创业板")
    if exclude_star and sym.startswith("688"):
        reasons.append("科创板")
    if exclude_bse and sym.startswith(("43", "8", "9")):
        reasons.append("北交所")
    ok = not reasons
    label = "保留" if ok else f"排除({','.join(reasons)})"
    return RuleResult(passed=ok, label=label, metric=1.0 if ok else 0.0, detail=label)


# ─────────────────────────────── 注册表 ───────────────────────────────


def _build_registry() -> dict[str, RuleSpec]:
    specs: list[RuleSpec] = [
        RuleSpec(
            code="price_range",
            title="股价区间",
            category="filter",
            description="最新收盘价位于 [min, max] 区间",
            params=[
                RuleParam("min", "最低价", "float", default=5.0, min=0, max=1000, step=0.5),
                RuleParam("max", "最高价", "float", default=60.0, min=0, max=1000, step=0.5),
            ],
            evaluator=_rule_price_range,
            min_kline_days=1,
        ),
        RuleSpec(
            code="exclude_boards",
            title="板块过滤",
            category="filter",
            description="排除 ST / 创业板 / 科创板 / 北交所",
            params=[
                RuleParam("exclude_st", "排除 ST", "bool", default=True),
                RuleParam("exclude_gem", "排除 创业板(30x)", "bool", default=False),
                RuleParam("exclude_star", "排除 科创板(688x)", "bool", default=False),
                RuleParam("exclude_bse", "排除 北交所", "bool", default=True),
            ],
            evaluator=_rule_exclude_boards,
            min_kline_days=1,
        ),
        RuleSpec(
            code="change_pct_today_range",
            title="当日涨跌幅区间",
            category="price",
            description="今日涨跌幅 ∈ [min%, max%]",
            params=[
                RuleParam("min", "最低(%)", "float", default=0.0, min=-20, max=20, step=0.1),
                RuleParam("max", "最高(%)", "float", default=10.0, min=-20, max=20, step=0.1),
            ],
            evaluator=_rule_change_pct_today,
            min_kline_days=2,
        ),
        RuleSpec(
            code="limit_up_today",
            title="当日涨停",
            category="pattern",
            description="今日涨停（主板 10% / 创业科创 20% / ST 5%；含容忍度）",
            params=[
                RuleParam("tolerance", "容忍(低于涨停%)", "float", default=0.3, min=0, max=2, step=0.1),
            ],
            evaluator=_rule_limit_up_today,
            min_kline_days=2,
        ),
        RuleSpec(
            code="box_consolidation",
            title="箱体横盘",
            category="pattern",
            description="近 N 天箱体振幅 ≤ max_amp_pct（不含今日）",
            params=[
                RuleParam("lookback", "回溯天数 N", "int", default=20, min=5, max=60, step=1),
                RuleParam("max_amp_pct", "最大振幅(%)", "float", default=20.0, min=3, max=60, step=1),
            ],
            evaluator=_rule_box_consolidation,
            min_kline_days=6,
        ),
        RuleSpec(
            code="volume_shrink",
            title="量能缩量",
            category="volume",
            description="近 N 天成交量 CV（std/mean）≤ max_cv（不含今日）",
            params=[
                RuleParam("lookback", "回溯天数 N", "int", default=20, min=5, max=60, step=1),
                RuleParam("max_cv", "最大变异系数", "float", default=0.4, min=0.1, max=1.5, step=0.05),
            ],
            evaluator=_rule_volume_shrink,
            min_kline_days=6,
        ),
        RuleSpec(
            code="volume_spike_today",
            title="当日放量",
            category="volume",
            description="今日成交量 / 近 N 日均量 ≥ min_ratio",
            params=[
                RuleParam("lookback", "均量回溯 N", "int", default=20, min=5, max=60, step=1),
                RuleParam("min_ratio", "最小放量倍数", "float", default=2.0, min=1.0, max=20, step=0.1),
            ],
            evaluator=_rule_volume_spike_today,
            min_kline_days=6,
        ),
        RuleSpec(
            code="break_prior_high",
            title="突破前高",
            category="pattern",
            description="今日收盘 ≥ 前 N 日最高 × buffer",
            params=[
                RuleParam("lookback", "前高回溯 N", "int", default=30, min=5, max=250, step=1),
                RuleParam("buffer", "突破倍数", "float", default=1.0, min=0.9, max=1.2, step=0.001),
            ],
            evaluator=_rule_break_prior_high,
            min_kline_days=6,
        ),
        RuleSpec(
            code="below_prior_high",
            title="未破前高（调整期）",
            category="pattern",
            description="今日收盘 ≤ 前 N 日最高 × buffer（通常 0.99~1.0）",
            params=[
                RuleParam("lookback", "前高回溯 N", "int", default=20, min=5, max=250, step=1),
                RuleParam("buffer", "未破倍数", "float", default=0.995, min=0.9, max=1.0, step=0.001),
            ],
            evaluator=_rule_below_prior_high,
            min_kline_days=6,
        ),
        RuleSpec(
            code="ma_above",
            title="收盘 > MA(N)",
            category="trend",
            description="今日收盘价站上 N 日均线",
            params=[
                RuleParam("ma_n", "均线周期", "int", default=20, min=3, max=250, step=1),
            ],
            evaluator=_rule_ma_above,
            min_kline_days=3,
        ),
        RuleSpec(
            code="ma_bull_stack",
            title="均线多头排列",
            category="trend",
            description="MA(N1) > MA(N2) > MA(N3)（从快到慢）",
            params=[
                RuleParam("periods", "均线周期(逗号分隔)", "text", default="5,10,20"),
            ],
            evaluator=_rule_ma_bull_stack,
            min_kline_days=20,
        ),
        RuleSpec(
            code="drawdown_from_high",
            title="距前高回撤",
            category="price",
            description="今日收盘距前 N 日最高的回撤幅度 ∈ [min_pct, max_pct]",
            params=[
                RuleParam("lookback", "回溯天数 N", "int", default=30, min=5, max=250, step=1),
                RuleParam("min_pct", "最小回撤(%)", "float", default=0, min=0, max=50, step=0.5),
                RuleParam("max_pct", "最大回撤(%)", "float", default=15, min=0, max=90, step=0.5),
            ],
            evaluator=_rule_drawdown_from_high,
            min_kline_days=6,
        ),
    ]
    return {s.code: s for s in specs}


RULE_REGISTRY: dict[str, RuleSpec] = _build_registry()


def list_rules() -> list[dict[str, Any]]:
    return [s.to_dict() for s in RULE_REGISTRY.values()]


def get_rule(code: str) -> RuleSpec | None:
    return RULE_REGISTRY.get(code)
