from app.config import POOL_BUY, POOL_CANDIDATE, POOL_FOCUS, StrategyConfig
from app.services.strategy_engine import apply_transition_rules


def make_entry(pool=POOL_CANDIDATE, score=0.0, price=10.0, breakout=10.0, volume_ratio=1.0, above_vwap=True):
    return {
        "pool": pool,
        "score": score,
        "metrics": {
            "price": price,
            "breakout_level": breakout,
            "volume_ratio": volume_ratio,
            "above_vwap": above_vwap,
        },
        "transitions": {
            "above60_count": 0,
            "breakout_confirm_count": 0,
            "below65_count": 0,
        },
    }


def test_focus_trigger_by_consecutive_scores():
    cfg = StrategyConfig()
    e = make_entry(pool=POOL_CANDIDATE, score=61)
    apply_transition_rules(e, cfg)
    apply_transition_rules(e, cfg)
    out = apply_transition_rules(e, cfg)
    assert out["recommended_pool"] == POOL_FOCUS


def test_focus_trigger_by_single_high_score():
    cfg = StrategyConfig()
    e = make_entry(pool=POOL_CANDIDATE, score=70)
    out = apply_transition_rules(e, cfg)
    assert out["recommended_pool"] == POOL_FOCUS


def test_buy_trigger_by_breakout_confirm():
    cfg = StrategyConfig()
    e = make_entry(pool=POOL_FOCUS, score=80, price=10.05, breakout=10.0, volume_ratio=1.6, above_vwap=True)
    first = apply_transition_rules(e, cfg)
    assert first["recommended_pool"] is None
    second = apply_transition_rules(e, cfg)
    assert second["recommended_pool"] == POOL_BUY


def test_buy_pool_downgrade_after_5_minutes():
    cfg = StrategyConfig()
    e = make_entry(pool=POOL_BUY, score=64, price=9.8, breakout=10, volume_ratio=0.8, above_vwap=False)
    out = None
    for _ in range(5):
        out = apply_transition_rules(e, cfg)
    assert out is not None
    assert out["auto_move_to"] == POOL_FOCUS
