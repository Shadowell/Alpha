"""轻量中文情绪打分（issue #33，与 #14 共享的文本抽取出口）。

约束：

* 不引入任何新依赖（无 snownlp / jieba），只用仓库已有的标准库 + pandas；
* 输出必须带 ``confidence``，低置信度不得被当作强信号；
* **解析失败 / 空文本 → ``score=None``（缺失）**，与「中性 0 分」严格区分；
* 分词退化为 n-gram 重叠，避免水军长文靠堆字数抬高权重。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Iterable

from app.services.alt_sentiment.records import is_missing

# 词表按打板场景裁剪：命中即给方向权重，可被否定词翻转。
POSITIVE_LEXICON: dict[str, float] = {
    "利好": 0.85,
    "超预期": 0.85,
    "预增": 0.8,
    "扭亏": 0.75,
    "增长": 0.5,
    "大增": 0.8,
    "中标": 0.8,
    "签订": 0.55,
    "订单": 0.6,
    "回购": 0.7,
    "增持": 0.7,
    "分红": 0.5,
    "获批": 0.8,
    "通过": 0.4,
    "量产": 0.65,
    "投产": 0.6,
    "新品": 0.5,
    "突破": 0.6,
    "涨停": 0.7,
    "重组": 0.55,
    "注入": 0.55,
    "收购": 0.45,
    "涨价": 0.6,
    "提价": 0.6,
    "景气": 0.55,
    "龙头": 0.5,
    "首发": 0.5,
    "独家": 0.45,
    "合作": 0.4,
}

NEGATIVE_LEXICON: dict[str, float] = {
    "利空": -0.85,
    "预减": -0.8,
    "亏损": -0.75,
    "下滑": -0.6,
    "减持": -0.7,
    "质押": -0.5,
    "处罚": -0.8,
    "立案": -0.85,
    "诉讼": -0.7,
    "违约": -0.8,
    "退市": -0.9,
    "停牌": -0.6,
    "终止": -0.6,
    "风险提示": -0.5,
    "减值": -0.7,
    "商誉": -0.5,
    "解禁": -0.45,
    "降价": -0.55,
    "滞销": -0.6,
    "违规": -0.8,
    "警示": -0.5,
    "问询": -0.45,
    "低于预期": -0.85,
}

NEGATION_TOKENS: tuple[str, ...] = ("未", "不", "无", "没有", "未能", "不再", "尚未")
INTENSIFIERS: dict[str, float] = {"大幅": 1.3, "显著": 1.25, "严重": 1.3, "轻微": 0.7}

_TOKEN_SPLIT = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")
_MIN_TEXT_CHARS = 4
_MAX_TEXT_CHARS = 4000
# 置信度上限：词典法本身有天花板，禁止伪装成模型级高置信度。
_MAX_CONFIDENCE = 0.85


@dataclass(slots=True)
class SentimentScore:
    """单条文本的情绪输出。``score is None`` 表示无法判定（缺失）。"""

    score: float | None
    confidence: float
    method: str = "lexicon"
    hits: tuple[str, ...] = ()
    reason: str = ""

    @property
    def available(self) -> bool:
        return not is_missing(self.score)

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "confidence": round(self.confidence, 4),
            "method": self.method,
            "hits": list(self.hits),
            "reason": self.reason,
        }


@dataclass(slots=True)
class _LexiconMatch:
    token: str
    weight: float
    start: int
    end: int
    negated: bool = False
    intensity: float = 1.0


def _match_lexicon(text: str) -> list[_LexiconMatch]:
    matches: list[_LexiconMatch] = []
    for lexicon in (POSITIVE_LEXICON, NEGATIVE_LEXICON):
        for token, weight in lexicon.items():
            start = 0
            while True:
                index = text.find(token, start)
                if index < 0:
                    break
                window = text[max(0, index - 4) : index]
                negated = any(neg in window for neg in NEGATION_TOKENS)
                intensity = 1.0
                for amp, factor in INTENSIFIERS.items():
                    if amp in window:
                        intensity = (
                            max(intensity, factor)
                            if factor > 1
                            else min(intensity, factor)
                        )
                matches.append(
                    _LexiconMatch(
                        token=token,
                        weight=weight,
                        start=index,
                        end=index + len(token),
                        negated=negated,
                        intensity=intensity,
                    )
                )
                start = index + len(token)
    # 去掉被更长命中完全包裹的子串，避免同一语义重复计权。
    matches.sort(key=lambda m: (m.start, -(m.end - m.start)))
    kept: list[_LexiconMatch] = []
    for match in matches:
        covered = any(
            other.start <= match.start and match.end <= other.end and other is not match
            for other in kept
        )
        if not covered:
            kept.append(match)
    return kept


def normalize_text(value: Any) -> str:
    """去 HTML 标签 / 空白，统一小写拉丁字母；不做截断以外的破坏性处理。"""
    if value is None or is_missing(value):
        return ""
    text = str(value)
    text = text.replace("<em>", "").replace("</em>", "")
    text = _TOKEN_SPLIT.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def score_text(title: Any, content: Any = None) -> SentimentScore:
    """对标题 + 正文打分。

    * 空文本 / 过短文本 → ``score=None``、``confidence=0``（缺失，不是 0 分）；
    * 有文本但无命中 → ``score=0.0``、低置信度（弱中性）；
    * 命中 → 归一到 ``[-1, 1]``，置信度随命中数与文本长度上升，封顶 0.85。
    """
    head = normalize_text(title)
    body = normalize_text(content)
    joined = f"{head} {body}".strip()
    if not joined:
        return SentimentScore(None, 0.0, method="none", reason="empty_text")
    working = joined[:_MAX_TEXT_CHARS]
    if len(working) < _MIN_TEXT_CHARS:
        return SentimentScore(None, 0.0, method="none", reason="text_too_short")

    matches = _match_lexicon(working)
    if not matches:
        return SentimentScore(0.0, 0.20, method="lexicon", reason="no_lexicon_hit")

    raw = 0.0
    for match in matches:
        weight = match.weight * match.intensity
        raw += -weight if match.negated else weight
    length_factor = min(1.0, math.log10(max(len(working), 10)) / math.log10(400))
    confidence = min(_MAX_CONFIDENCE, 0.30 + 0.12 * len(matches) + 0.25 * length_factor)
    score = max(-1.0, min(1.0, raw / (6.0 + 1.5 * len(matches))))
    return SentimentScore(
        round(score, 6),
        round(confidence, 6),
        method="lexicon",
        hits=tuple(sorted({match.token for match in matches}))[:8],
        reason="lexicon_hit",
    )


def aggregate_scores(
    scores: Iterable[SentimentScore],
    *,
    weights: Iterable[float] | None = None,
) -> SentimentScore:
    """按置信度（可再乘外部权重）加权平均；全部缺失时返回缺失而不是 0。"""
    items = [item for item in scores if item is not None and item.available]
    if not items:
        return SentimentScore(None, 0.0, method="aggregate", reason="no_valid_score")
    external = list(weights) if weights is not None else [1.0] * len(items)
    numerator = 0.0
    denominator = 0.0
    hits: list[str] = []
    for index, item in enumerate(items):
        mass = float(item.confidence) * float(
            external[index] if index < len(external) else 1.0
        )
        if mass <= 0:
            continue
        numerator += mass * float(item.score or 0.0)
        denominator += mass
        hits.extend(item.hits)
    if denominator <= 0:
        return SentimentScore(
            None, 0.0, method="aggregate", reason="zero_confidence_mass"
        )
    confidence = min(_MAX_CONFIDENCE, denominator / max(len(items), 1))
    return SentimentScore(
        round(numerator / denominator, 6),
        round(confidence, 6),
        method="aggregate",
        hits=tuple(sorted(set(hits)))[:8],
        reason="aggregated",
    )


def bigram_tokens(text: Any) -> frozenset[str]:
    """中文 2-gram 集合，用于新闻新颖度（不依赖分词器）。"""
    cleaned = normalize_text(text).replace(" ", "")
    if len(cleaned) < 2:
        return frozenset()
    return frozenset(cleaned[i : i + 2] for i in range(len(cleaned) - 1))
