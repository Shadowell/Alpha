"""Custom strategy engine unit tests."""
from __future__ import annotations

from app.services.custom_strategy import (
    BUILTIN_STRATEGIES,
    CustomStrategy,
    StrategyRuleRef,
    _compute_score,
    _is_a_main,
)
from app.services.strategy_rules import (
    RULE_REGISTRY,
    RuleContext,
    list_rules,
)


# ─────────────────────────────── helpers ───────────────────────────────


def _flat_kline(days: int, close: float = 10.0, vol: float = 1_000_000.0, start="2026-01-01"):
    """生成完全平静的 K 线：横盘 + 均量。"""
    import datetime as dt
    d = dt.date.fromisoformat(start)
    out = []
    for _ in range(days):
        out.append({
            "date": d.isoformat(), "open": close, "high": close * 1.01,
            "low": close * 0.99, "close": close, "volume": vol,
        })
        d = d + dt.timedelta(days=1)
    return out


# ─────────────────────────────── registry ───────────────────────────────


def test_registry_has_12_rules():
    codes = set(RULE_REGISTRY.keys())
    expected = {
        "price_range", "exclude_boards", "change_pct_today_range",
        "limit_up_today", "box_consolidation", "volume_shrink",
        "volume_spike_today", "break_prior_high", "below_prior_high",
        "ma_above", "ma_bull_stack", "drawdown_from_high",
    }
    assert expected.issubset(codes)
    listed = list_rules()
    assert len(listed) >= 12
    for r in listed:
        assert r["code"] and r["title"] and r["category"]
        assert isinstance(r["params"], list)


def test_builtin_strategies_are_three():
    assert len(BUILTIN_STRATEGIES) == 3
    ids = {s.id for s in BUILTIN_STRATEGIES}
    assert ids == {"builtin_quiet_breakout", "builtin_adjustment_box", "builtin_breakout_volume"}
    # 默认策略存在且为 quiet_breakout
    default = [s for s in BUILTIN_STRATEGIES if s.is_default]
    assert len(default) == 1 and default[0].id == "builtin_quiet_breakout"
    # 每条策略的规则 code 都在注册表里
    for s in BUILTIN_STRATEGIES:
        for ref in s.rules:
            assert ref.rule_code in RULE_REGISTRY, f"unknown rule {ref.rule_code} in {s.id}"


# ─────────────────────────────── rules behavior ───────────────────────────────


def test_price_range_rule():
    rows = _flat_kline(5, close=20.0)
    ctx = RuleContext(symbol="000001", name="T", kline=rows)
    rule = RULE_REGISTRY["price_range"]
    assert rule.evaluate(ctx, {"min": 10, "max": 30}).passed
    assert not rule.evaluate(ctx, {"min": 30, "max": 40}).passed


def test_volume_spike_today():
    rows = _flat_kline(30, close=10.0, vol=1_000_000)
    rows[-1]["volume"] = 5_000_000
    rows[-1]["close"] = 11.0
    rows[-1]["high"] = 11.0
    ctx = RuleContext(symbol="000001", name="T", kline=rows)
    res = RULE_REGISTRY["volume_spike_today"].evaluate(ctx, {"lookback": 20, "min_ratio": 3.0})
    assert res.passed
    assert res.metric and res.metric >= 3.0


def test_limit_up_today_mainboard():
    rows = _flat_kline(5, close=10.0)
    rows[-2]["close"] = 10.0
    rows[-1]["close"] = 11.00  # +10.00%
    ctx_mb = RuleContext(symbol="600001", name="工商银行", kline=rows)
    assert RULE_REGISTRY["limit_up_today"].evaluate(ctx_mb, {}).passed
    # 创业板 10% 不算涨停
    ctx_gem = RuleContext(symbol="300001", name="测试", kline=rows)
    assert not RULE_REGISTRY["limit_up_today"].evaluate(ctx_gem, {}).passed


def test_exclude_boards_flags():
    rows = _flat_kline(3)
    rule = RULE_REGISTRY["exclude_boards"]
    assert rule.evaluate(RuleContext("600001", "工商银行", rows), {"exclude_st": True}).passed
    assert not rule.evaluate(RuleContext("600001", "ST 中弘", rows), {"exclude_st": True}).passed
    assert not rule.evaluate(RuleContext("300001", "测试", rows), {"exclude_gem": True}).passed
    assert rule.evaluate(RuleContext("300001", "测试", rows), {"exclude_gem": False}).passed


def test_box_consolidation():
    # 横盘箱体：振幅很小
    rows = _flat_kline(25, close=10.0)
    ctx = RuleContext(symbol="000001", name="T", kline=rows)
    res = RULE_REGISTRY["box_consolidation"].evaluate(ctx, {"lookback": 20, "max_amp_pct": 20})
    assert res.passed
    # 振幅过大：人为放宽最高价
    rows2 = _flat_kline(25, close=10.0)
    for r in rows2[:-1]:
        r["high"] = 15.0
        r["low"] = 8.0
    ctx2 = RuleContext(symbol="000001", name="T", kline=rows2)
    assert not RULE_REGISTRY["box_consolidation"].evaluate(ctx2, {"lookback": 20, "max_amp_pct": 20}).passed


def test_ma_bull_stack():
    # 构造上升趋势
    import datetime as dt
    d = dt.date.fromisoformat("2026-01-01")
    rows = []
    for i in range(30):
        c = 10.0 + i * 0.1
        rows.append({"date": d.isoformat(), "open": c, "high": c, "low": c, "close": c, "volume": 100})
        d = d + dt.timedelta(days=1)
    ctx = RuleContext(symbol="000001", name="T", kline=rows)
    res = RULE_REGISTRY["ma_bull_stack"].evaluate(ctx, {"periods": "5,10,20"})
    assert res.passed


def test_break_prior_high():
    import datetime as dt
    d = dt.date.fromisoformat("2026-01-01")
    rows = []
    for _ in range(29):
        rows.append({"date": d.isoformat(), "open": 10, "high": 10.5, "low": 9.5, "close": 10, "volume": 100})
        d = d + dt.timedelta(days=1)
    # 今日突破
    rows.append({"date": d.isoformat(), "open": 10.5, "high": 11.2, "low": 10.5, "close": 11.0, "volume": 500})
    ctx = RuleContext(symbol="000001", name="T", kline=rows)
    res = RULE_REGISTRY["break_prior_high"].evaluate(ctx, {"lookback": 28, "buffer": 1.0})
    assert res.passed


def test_short_kline_early_returns():
    rows = _flat_kline(3, close=10.0)
    ctx = RuleContext(symbol="000001", name="T", kline=rows)
    # 要求 lookback=25 的规则，数据不足应返回 passed=False 而非报错
    res = RULE_REGISTRY["box_consolidation"].evaluate(ctx, {"lookback": 25, "max_amp_pct": 20})
    assert res.passed is False


# ─────────────────────────────── scorer ───────────────────────────────


def test_compute_score_monotonic():
    hits_basic = [{"code": "price_range", "passed": True, "metric": 10.0}]
    hits_extended = hits_basic + [{"code": "volume_spike_today", "passed": True, "metric": 3.0}]
    assert _compute_score(hits_extended) > _compute_score(hits_basic)


# ─────────────────────────────── model roundtrip ───────────────────────────────


def test_custom_strategy_roundtrip():
    s = CustomStrategy(
        id="abc",
        name="测试",
        description="desc",
        rules=[StrategyRuleRef("price_range", True, {"min": 5, "max": 60})],
    )
    d = s.to_dict()
    s2 = CustomStrategy.from_dict(d)
    assert s2.id == "abc"
    assert s2.rules[0].rule_code == "price_range"
    assert s2.rules[0].params == {"min": 5, "max": 60}


def test_is_a_main():
    assert _is_a_main("000001")
    assert _is_a_main("600001")
    assert _is_a_main("300001")
    assert _is_a_main("688001")
    assert not _is_a_main("430001")
    assert not _is_a_main("12345")
