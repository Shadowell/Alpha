"""#27 分层模型卡（Model Card）生成器。"""

from __future__ import annotations

from typing import Any


def build_tier_model_card(
    tier: str,
    *,
    primary_head: str,
    train_range: tuple[str, str],
    features: list[str] | tuple[str, ...],
    oos_metrics: dict[str, Any],
    top_factors: list[dict[str, Any]] | None = None,
    gate_passed: bool = False,
) -> dict[str, Any]:
    """生成标准化模型卡（适用市场、区间、指标、失效场景与风险提示）。"""
    return {
        "model_id": f"first_limit_alpha_tiered_{tier}",
        "tier": tier,
        "primary_head": primary_head,
        "market": "CN A-Shares (Limit-Up Tiered Universe)",
        "train_range": {"start": train_range[0], "end": train_range[1]},
        "feature_count": len(features),
        "features": list(features),
        "oos_metrics": dict(oos_metrics),
        "top_important_features": list(top_factors or [])[:5],
        "gate_passed": bool(gate_passed),
        "known_failure_modes": [
            "极端退潮期涨停样本稀疏导致校准偏移",
            "一字板/买不到标的在 strict 口径下不可成交",
            "外部权限受限字段（limit_step/hm_detail）处于降级口径",
        ],
        "risk_notice": "仅供量化研究与人工辅助决策，不构成自动下单建议。",
    }


__all__ = ["build_tier_model_card"]
