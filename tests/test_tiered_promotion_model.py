"""#27 分层晋级概率模型骨架（tiers/targets/pit/dataset/synthetic）单元测试。"""

from __future__ import annotations

import pytest

from strategy.first_limit_alpha.tiered_model.dataset import (
    build_all_tier_datasets,
    to_validation_panel,
)
from strategy.first_limit_alpha.tiered_model.pit import (
    LeakageError,
    assert_pit_features,
    find_leakage_columns,
)
from strategy.first_limit_alpha.tiered_model.synthetic import (
    SyntheticTierConfig,
    make_synthetic_labels,
)
from strategy.first_limit_alpha.tiered_model.tiers import (
    TIER_ORDER,
    assert_tiers_disjoint,
    assign_board_tiers,
)


def test_synthetic_and_all_tier_datasets():
    labels, factors, meta = make_synthetic_labels(SyntheticTierConfig(n_dates=15, n_symbols=12))
    assert not labels.empty
    assert not factors.empty
    assert isinstance(meta, dict)

    tiered, _ = assign_board_tiers(labels)
    assert_tiers_disjoint(tiered)

    datasets = build_all_tier_datasets(labels, factors)
    assert set(datasets.keys()).issubset(set(TIER_ORDER))
    assert len(datasets) > 0

    first_tier = next(iter(datasets.values()))
    assert not first_tier.frame.empty
    panel = to_validation_panel(
        first_tier.frame,
        first_tier.tier,
        features=first_tier.features,
        primary_head=first_tier.primary_head,
    )
    assert panel.frame is not None
    assert not panel.frame.empty


def test_pit_leakage_guard():
    assert "auction_price" in find_leakage_columns(["seal_strength", "auction_price"])
    with pytest.raises(LeakageError):
        assert_pit_features(["seal_strength", "auction_price"])
