"""事件驱动打板研究链路（issue #35，P3 研究性质）。

本包把公告侧已有的关键词/LLM 打分结果**结构化为可追溯的事件样本**，再按
"投资者实际可见时间"对齐到打板决策时点，产出事件因子与事件首板标签，并复用
issue #36 的样本外验证脚手架判断是否有增量。

设计红线（与 issue #35 原文一致）：

1. **可见时间优先**：公告披露日期 ≠ 可得时间。没有精确时间戳时一律按
   "盘后 15:00 之后才可见"处理（见 :mod:`strategy.daban_events.disclosure`），
   盘后公告绝不进入当日因子。
2. **可追溯**：LLM 抽取必须回指原文片段，校验不过就退回规则抽取并显式标记，
   绝不允许 LLM 编造事件（见 :mod:`strategy.daban_events.extraction`）。
3. **缺数显式**：所有数值因子缺失时返回 ``None`` 并在 ``missing`` 中登记，
   禁止 ``fillna(0)``。
4. **可成交性**：事件首板常见一字板买不到，标签必须带 ``entry_unfilled``
   口径（复用 #23 常量），不达标不进收益统计。

本包**不依赖 app 包**（与 ``strategy/first_limit_alpha``、
``strategy/model_validation`` 保持一致），也不访问网络：外部抓取通过
:mod:`strategy.daban_events.sources` 的注入式 fetcher 完成，便于离线单测。
"""

from .taxonomy import (
    EVENT_CODES,
    EVENT_TAXONOMY,
    KeywordHit,
    classify_event,
    event_type_doc,
    match_keywords,
)

__all__ = [
    "EVENT_CODES",
    "EVENT_TAXONOMY",
    "KeywordHit",
    "classify_event",
    "event_type_doc",
    "match_keywords",
]
