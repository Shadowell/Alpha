"""分层晋级概率模型与因子评估体系 (GitHub issue #27).

把打板研究从"单样本池 + 单模型"升级为 **首板 / 1进2 / 2进3 / 高位板 / 反包** 五档
独立建模 + 统一因子评估 + 复用 #36 门槛判定的实验流程。

模块分工
--------
* :mod:`~strategy.first_limit_alpha.tiered_model.tiers` - 五档样本池注册表，
  ``(board_tier, entry_scene)`` 两维分解与互斥审计（"不混合训练"）。
* :mod:`~strategy.first_limit_alpha.tiered_model.targets` - ``p_continue`` /
  ``p_strong_3d`` / ``p_break`` / ``p_next_auction_premium`` 多任务头 +
  ``limit_up_alpha_score`` 融合；可成交性取自 #23。
* :mod:`~strategy.first_limit_alpha.tiered_model.pit` - 严格 PIT as-of 连接与
  标签泄漏黑名单。
* :mod:`~strategy.first_limit_alpha.tiered_model.dataset` - 分层数据集装配，并桥到
  #36 的 ``Panel`` 契约。
* :mod:`~strategy.first_limit_alpha.tiered_model.estimators` - numpy 基线 +
  sklearn / LightGBM 可选后端（缺失即 ``skipped``，不红 CI）。
* :mod:`~strategy.first_limit_alpha.tiered_model.calibration` - Platt / isotonic
  校准、Brier / ECE / 可靠性曲线。
* :mod:`~strategy.first_limit_alpha.tiered_model.factors` - RankIC / ICIR /
  分层收益单调性 / 相关性 / VIF / 衰减 / 拥挤度 / 失效因子识别。
* :mod:`~strategy.first_limit_alpha.tiered_model.explain` - 置换重要度（必有）+
  SHAP（可选）+ 单样本解释。
* :mod:`~strategy.first_limit_alpha.tiered_model.alpha_eval` - 分层分类 / 排序 /
  交易 / 稳定性评估 + 随机与全市场基线对比。
* :mod:`~strategy.first_limit_alpha.tiered_model.walkforward` - 每档 walk-forward
  训练评估（复用 #36 splitting / metrics / portfolio）。
* :mod:`~strategy.first_limit_alpha.tiered_model.model_card` - 模型卡。
* :mod:`~strategy.first_limit_alpha.tiered_model.pipeline` - 端到端编排 + 上线门槛。
* :mod:`~strategy.first_limit_alpha.tiered_model.synthetic` - 合成标签/因子面板
  （线上 #22 表未回填，测试与本地演示全靠它）。

本包**不接入 API / 容器 / 前端**，也不改动 #23/#24/#25/#26/#36 的任何既有模块；
接线步骤见交付报告。
"""

from __future__ import annotations

PACKAGE_NAME = "alpha-tiered-model"
ISSUE_NUMBER = 27

__all__ = ["ISSUE_NUMBER", "PACKAGE_NAME"]
