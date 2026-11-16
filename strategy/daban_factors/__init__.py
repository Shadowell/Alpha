"""打板 P2 因子族（issues #28 / #29 / #30 / #31）。

* :mod:`strategy.daban_factors.capital_flow` —— #28 资金流向与龙虎榜因子族
* :mod:`strategy.daban_factors.theme_lifecycle` —— #29 题材板块联动、身位与生命周期
* :mod:`strategy.daban_factors.auction_intraday` —— #30 集合竞价与盘中分时强度
* :mod:`strategy.daban_factors.chip_event_risk` —— #31 筹码、解禁与事件风险

公共件：``_base``（口径 / 单位 / PIT / 指示列）、``_correlation``（IC / 分层 / 共线性）。
本包**只读**现有数据底座，不注册 API、不建表、不触发同步；建表与路由由集成方落地
（见各模块 ``*_capability()`` 与交付说明）。
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

#: issue 编号 → 模块名（逐条 issue 交付时追加）。
FACTOR_FAMILY_MODULES: dict[int, str] = {
    28: "capital_flow",
    29: "theme_lifecycle",
    30: "auction_intraday",
    31: "chip_event_risk",
}


def family_capability() -> dict[str, Any]:
    """各因子族的能力标记（含未接入数据源的显式 PARTIAL 说明）。"""
    payload: dict[str, Any] = {}
    for issue, module_name in FACTOR_FAMILY_MODULES.items():
        module = import_module(f"strategy.daban_factors.{module_name}")
        payload[str(issue)] = {
            "module": f"strategy.daban_factors.{module_name}",
            "capability": getattr(module, f"{module_name}_capability")(),
            "factor_count": len(getattr(module, f"{module_name}_factor_specs")()),
        }
    return payload


__all__ = ["FACTOR_FAMILY_MODULES", "family_capability"]
