"""#34 龙虎榜游资席位画像与跟随 / 反跟策略（离线研究层）。

本包是 **只读研究模块**：只读取 ``market_kline.db`` 里 #22 打板数据底座的
``dragon_tiger_daily`` / ``dragon_tiger_seat_daily`` / ``hot_money_seat`` /
``limit_board_daily`` / ``kline_daily`` 等表，不注册 API、不写库、不接 HTTP。
主 agent 若要把结果暴露成 ``/api/hot-money/*``，只需调用本包公开入口。

核心纪律
--------
* **严格 PIT**：龙虎榜是盘后披露数据。``as_of_date`` 收盘后只能看到
  ``trade_date <= as_of_date`` 的榜单；而"次日溢价 / 3 日收益"这类结果字段，
  要求**观察日本身也 <= as_of_date**，因此 T 日榜单最早只能在 T+1 竞价/开盘
  被使用（见 :mod:`strategy.daban_hotmoney.follow`，T 日排队入场直接拒绝）。
* **小样本不上线**：席位统计使用滚动窗口 + 贝叶斯收缩，样本量低于
  ``min_events`` 的席位给出 ``is_reliable=0`` 且 ``seat_alpha_score=None``，
  绝不输出"1 次上榜 = 100% 胜率"这类误导值。
* **可审计**：席位名称标准化只做机械规则（去公司全称噪声、营业部后缀、
  券商简称映射）并记录 ``method``；游资人名标签由人工名录 ``HOT_MONEY_LABELS``
  维护，来源字段必填，**不明确的一律不标注**。
* **降级显式**：``hot_money_seat`` 若来自 #22 的 ``proxy.top_inst`` 降级路径
  （``hm_detail`` / ``hm_list`` 无权限），所有依赖名录的字段打
  ``hot_money_label_is_proxy=1``。

模块划分
--------
``seat_names``  席位名称标准化 / 别名合并 / 人工名录（可审计）
``source``      只读数据装载 + PIT 价格账本（含除权剔除）
``profile``     席位画像（样本量、次日溢价、胜率、砸盘率、风格、协同、偏好）
``factors``     个股聚合游资因子（``*_is_missing`` / ``*_is_proxy`` 约定同 #24）
``follow``      跟随 / 反跟信号与研究口径解释
``validation``  增量验证与上线（保留 / 不保留）决策 + 研究 CLI
``sample_data`` 确定性合成行情 / 榜单 fixture（单测与研究 CLI 共用）
"""

from __future__ import annotations

from typing import Any

# 懒加载（PEP 562）：`import strategy.daban_hotmoney` 不该拖进 pandas/numpy，
# 也让单测可以只加载自己需要的子模块。
_LAZY_EXPORTS: dict[str, str] = {
    "FollowConfig": "follow",
    "build_follow_signals": "follow",
    "explain_stock_hot_money": "follow",
    "HotMoneyFactorConfig": "factors",
    "build_hot_money_factors": "factors",
    "hot_money_factor_specs": "factors",
    "hot_money_output_columns": "factors",
    "SeatProfileConfig": "profile",
    "build_seat_profiles": "profile",
    "profiles_frame": "profile",
    "HOT_MONEY_LABELS": "seat_names",
    "LABEL_DISCLAIMER": "seat_names",
    "normalization_report": "seat_names",
    "normalize_seat_name": "seat_names",
    "data_availability": "source",
    "open_readonly": "source",
    "IncrementThresholds": "validation",
    "ReleaseDecision": "validation",
    "evaluate_hot_money_release": "validation",
}

__all__ = sorted(_LAZY_EXPORTS)


def __getattr__(name: str) -> Any:
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    module = import_module(f"{__name__}.{module_name}")
    return getattr(module, name)
