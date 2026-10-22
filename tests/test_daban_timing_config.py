"""#32 择时/仓位规则参数化（strategy.daban_timing.config）单元测试。

关注点：默认口径必须与 issue 原文一致、可从外部 JSON 注入、可整体关闭、
非法阈值必须报错（而不是静默降级）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from strategy.daban_timing.config import (
    DEFAULT_REGIME_RULES,
    TIMING_REGIMES,
    ConfigError,
    PortfolioLimits,
    RegimeTimingRule,
    RiskLimits,
    TimingConfig,
    load_timing_config,
    parse_timing_config,
)


def test_default_rules_match_issue_32_wording():
    by_regime = {rule.regime: rule for rule in DEFAULT_REGIME_RULES}
    assert set(by_regime) == set(TIMING_REGIMES)
    assert (
        by_regime["冰点"].position_multiplier,
        by_regime["冰点"].max_board_height,
    ) == (
        0.2,
        1,
    )
    assert by_regime["修复"].position_multiplier == 0.5
    assert by_regime["发酵"].position_multiplier == 1.0
    assert by_regime["发酵"].max_board_height == 3  # 允许 1进2 / 2进3
    assert by_regime["高潮"].position_multiplier == 0.7
    assert by_regime["高潮"].ban_high_board is True
    assert "分批止盈" in by_regime["高潮"].actions
    assert by_regime["分歧"].position_multiplier == 0.5
    assert by_regime["分歧"].required_condition == "弱转强"
    ebb = by_regime["退潮"]
    assert ebb.position_multiplier == 0.0
    assert ebb.allow_new_positions is False
    assert ebb.max_new_positions == 0
    assert "禁止 3 板以上" in ebb.forbidden


def test_portfolio_defaults():
    limits = PortfolioLimits()
    assert limits.max_position_per_stock == 0.20
    assert limits.max_theme_concentration == 0.40
    assert limits.max_hold_positions == 5
    assert limits.lot_size == 100
    assert 0 < limits.liquidity_participation_rate <= 1


def test_config_rejects_out_of_range_values():
    with pytest.raises(ConfigError):
        PortfolioLimits(max_position_per_stock=1.5)
    with pytest.raises(ConfigError):
        PortfolioLimits(lot_size=0)
    with pytest.raises(ConfigError):
        RiskLimits(drawdown_half_threshold=0.2, drawdown_halt_threshold=0.1)
    with pytest.raises(ConfigError):
        RegimeTimingRule(
            regime="发酵",
            allow_new_positions=True,
            position_multiplier=float("nan"),
            max_new_positions=1,
            max_position_per_stock=0.2,
            max_theme_concentration=0.4,
        )


def test_config_round_trip_and_partial_override():
    config = TimingConfig()
    payload = config.to_dict()
    assert json.loads(json.dumps(payload, ensure_ascii=False)) == json.loads(
        json.dumps(payload, ensure_ascii=False)
    )
    rebuilt = load_timing_config(payload)
    assert rebuilt.rules_by_regime["高潮"] == config.rules_by_regime["高潮"]
    assert rebuilt.portfolio == config.portfolio

    override = load_timing_config(
        {"rules_by_regime": {"发酵": {"position_multiplier": 0.8}}}
    )
    assert override.rules_by_regime["发酵"].position_multiplier == 0.8
    # 未覆盖字段沿用默认值，不能被静默置 0（缺失 != 空仓信号）
    assert override.rules_by_regime["发酵"].max_board_height == 3
    assert override.rules_by_regime["冰点"] == config.rules_by_regime["冰点"]


def test_load_timing_config_from_json_file_and_string(tmp_path: Path):
    payload = {
        "risk": {"drawdown_half_threshold": 0.05, "drawdown_halt_threshold": 0.09}
    }
    path = tmp_path / "timing.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    assert load_timing_config(path).risk.drawdown_half_threshold == 0.05
    assert load_timing_config(json.dumps(payload)).risk.drawdown_halt_threshold == 0.09
    with pytest.raises(ConfigError):
        load_timing_config("{not json")
    with pytest.raises(ConfigError):
        load_timing_config("[1, 2, 3]")
    with pytest.raises(ConfigError):
        load_timing_config({"unknown_key": 1})


def test_config_rejects_missing_or_unknown_regime_rules():
    base = TimingConfig()
    broken = dict(base.rules_by_regime)
    broken.pop("退潮")
    with pytest.raises(ConfigError):
        TimingConfig(rules_by_regime=broken)
    with pytest.raises(ConfigError):
        parse_timing_config({"rules_by_regime": {"熊市": {"position_multiplier": 0.1}}})
    with pytest.raises(ConfigError):
        parse_timing_config({"unknown_regime_mode": "guess"})


def test_disabled_config_is_neutral_but_keeps_portfolio_caps():
    disabled = TimingConfig().disabled()
    assert disabled.enable_timing is False
    for rule in disabled.rules_by_regime.values():
        assert rule.position_multiplier == 1.0
        assert rule.allow_new_positions is True
        assert rule.max_board_height is None
    # 默认配置（择时开启）下退潮仍然禁买
    assert TimingConfig().rule("退潮").allow_new_positions is False
