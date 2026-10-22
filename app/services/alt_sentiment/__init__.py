"""另类情绪 / 关注度因子（issue #33）。

本包只依赖仓库已有依赖（akshare / pandas / numpy / httpx），用于：

* 评估可合规获取的另类数据源（互动易、东财千股千评关注度、个股新闻）；
* 把原始条目规范化成带 ``event_time`` / ``as_of_time`` 的可追溯记录；
* 计算打板相关的关注度 / 情绪因子（严格 Point-in-Time）；
* 离线单因子 IC / 分层收益与泄漏校验。

生产接线（router / container / 定时任务 / 前端卡片）由主 agent 在合并时统一落，
本包不修改任何既有公共文件。
"""

from __future__ import annotations

from app.services.alt_sentiment.factors import (
    AltFactorBuilder,
    FACTOR_NAMES,
    FACTOR_SPECS,
)
from app.services.alt_sentiment.normalize import (
    normalize_attention_frame,
    normalize_interactive_frame,
    normalize_news_frame,
)
from app.services.alt_sentiment.records import (
    SOURCE_ATTENTION,
    SOURCE_INTERACTIVE_QA,
    SOURCE_NEWS,
    AltRecord,
    AltSentimentDaily,
    is_missing,
)
from app.services.alt_sentiment.sentiment import SentimentScore, score_text
from app.services.alt_sentiment.service import AltSentimentService
from app.services.alt_sentiment.store import AltSentimentStore

__all__ = [
    "AltFactorBuilder",
    "AltRecord",
    "AltSentimentDaily",
    "AltSentimentService",
    "AltSentimentStore",
    "FACTOR_NAMES",
    "FACTOR_SPECS",
    "SentimentScore",
    "SOURCE_ATTENTION",
    "SOURCE_INTERACTIVE_QA",
    "SOURCE_NEWS",
    "is_missing",
    "normalize_attention_frame",
    "normalize_interactive_frame",
    "normalize_news_frame",
    "score_text",
]
