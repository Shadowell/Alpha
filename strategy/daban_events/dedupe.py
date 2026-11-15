"""同一事件的多次公告去重与"首次披露"判定（issue #35「公告噪声」风险项）。

一场重组从筹划提示性公告 → 停牌 → 预案 → 进展 → 问询回复 → 终止，会发十几份
公告。如果每份都当成一个新事件，样本会严重重复、``event_novelty`` 也全是 0；
反过来，如果按 ``(symbol, event_type)`` 一刀切合并，两笔不相关的收购又会被并成
一笔。这里的口径是：

* 先按 ``(symbol, event_type)`` 分桶；
* 桶内按可见时间排序，标题指纹（去停用词 + 分词集合）Jaccard 相似度
  ≥ ``similarity_threshold``，且金额不矛盾（相等 / 任一缺失），
  且可见时间间隔 ≤ ``cluster_window_days`` 自然日 —— 才并为一簇；
* 正文指纹完全相同（``raw_text_hash``）直接并簇，这是最硬的去重信号；
* ``progress_update`` / ``repeat_disclosure`` 这类风险标记只加强"同一事件"判断，
  不会单独成簇。

每一簇输出 ``dedupe_key``（首个成员的指纹，落库后可跨批次对齐）、
``event_seq``（第几次披露）、``is_first_disclosure``、``repeat_count``。
"是否首次披露"还必须对照**已落库历史**：本次批次内部是第一条，但库里早就有
同一事件时仍然不是首次（:func:`apply_history_first_disclosure`）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .extraction import EventExtract
from .taxonomy import EVENT_NEUTRAL

_STOPWORDS = (
    "关于",
    "的公告",
    "公告",
    "的",
    "暨",
    "以及",
    "公司",
    "本公司",
    "进行",
    "相关",
    "情况",
    "说明",
    "（",
    "）",
    "(",
    ")",
    "：",
    ":",
    "，",
    ",",
    "。",
)

_TOKEN_RE = re.compile(r"[0-9A-Za-z%EhH日一二三四五六七八九十]+|[\u4e00-\u9fff]{2,}")

CONTINUATION_FLAGS = ("progress_update", "repeat_disclosure", "supplement_notice")


def normalize_title(title: str) -> str:
    text = str(title or "")
    for token in _STOPWORDS:
        text = text.replace(token, "")
    return "".join(text.split())


def title_tokens(title: str) -> frozenset[str]:
    normalized = normalize_title(title)
    tokens: set[str] = set()
    for match in _TOKEN_RE.findall(normalized):
        if len(match) >= 2:
            tokens.add(match)
    # 二字滑动能显著提升中文标题召回（"重大资产购买" vs "购买资产"）。
    chars = [c for c in normalized if "\u4e00" <= c <= "\u9fff"]
    for i in range(len(chars) - 1):
        tokens.add(chars[i] + chars[i + 1])
    return frozenset(tokens)


def title_similarity(a: str, b: str) -> float:
    left, right = title_tokens(a), title_tokens(b)
    if not left or not right:
        return 0.0
    inter = len(left & right)
    union = len(left | right)
    return inter / union if union else 0.0


@dataclass(slots=True)
class EventCluster:
    """同一事件的一簇公告。"""

    key: str
    symbol: str
    event_type: str
    members: list[EventExtract] = field(default_factory=list)

    @property
    def first(self) -> EventExtract:
        return self.members[0]

    @property
    def repeat_count(self) -> int:
        return len(self.members)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dedupe_key": self.key,
            "symbol": self.symbol,
            "event_type": self.event_type,
            "repeat_count": self.repeat_count,
            "titles": [m.title for m in self.members],
            "hashes": [m.raw_text_hash for m in self.members],
        }


def _cluster_key(member: EventExtract) -> str:
    return f"{member.symbol}:{member.event_type}:{member.raw_text_hash[:16]}"


def _same_event(candidate: EventExtract, anchor: EventExtract, cfg: DedupeConfig) -> bool:
    if candidate.raw_text_hash == anchor.raw_text_hash:
        return True
    if not _within_window(candidate, anchor, cfg):
        return False
    if not _amount_compatible(candidate, anchor):
        return False
    similarity = title_similarity(candidate.title, anchor.title)
    boosted = bool(set(candidate.risk_flags) & set(CONTINUATION_FLAGS))
    threshold = cfg.similarity_threshold * (0.8 if boosted else 1.0)
    return similarity >= threshold


def _within_window(candidate: EventExtract, anchor: EventExtract, cfg: DedupeConfig) -> bool:
    left = _date_of(candidate)
    right = _date_of(anchor)
    if left is None or right is None:
        # 任一侧日期缺失时不合并：宁可就多不就重复。
        return False
    return abs(_days_between(left, right)) <= cfg.cluster_window_days


def _date_of(member: EventExtract) -> str | None:
    if member.timing is not None and member.timing.visible_at:
        return str(member.timing.visible_at)[:10]
    return member.announce_date


def _days_between(left: str, right: str) -> int:
    from datetime import date

    def _parse(value: str) -> date | None:
        try:
            parts = [int(p) for p in value[:10].split("-")]
        except (ValueError, IndexError):
            return None
        if len(parts) != 3:
            return None
        return date(*parts)

    a, b = _parse(left), _parse(right)
    if a is None or b is None:
        return 0
    return (b - a).days


def _amount_compatible(candidate: EventExtract, anchor: EventExtract) -> bool:
    left, right = candidate.amount, anchor.amount
    if left is None or right is None:
        return True
    if left == right:
        return True
    denominator = max(abs(left), abs(right))
    return denominator > 0 and abs(left - right) / denominator <= 0.01


@dataclass(frozen=True, slots=True)
class DedupeConfig:
    similarity_threshold: float = 0.55
    cluster_window_days: int = 30

    def as_dict(self) -> dict[str, Any]:
        return {
            "similarity_threshold": self.similarity_threshold,
            "cluster_window_days": self.cluster_window_days,
        }


def cluster_events(
    extracts: Iterable[EventExtract],
    *,
    config: DedupeConfig = DedupeConfig(),
) -> list[EventCluster]:
    """按标的 + 事件类型分桶聚类；兜底类（中性）不聚类。"""

    buckets: dict[tuple[str, str], list[EventExtract]] = {}
    singles: list[EventExtract] = []
    for member in extracts:
        if member.event_type == EVENT_NEUTRAL:
            singles.append(member)
            continue
        buckets.setdefault((member.symbol, member.event_type), []).append(member)

    clusters: list[EventCluster] = []
    for members in buckets.values():
        members.sort(key=lambda m: (_date_of(m) or "9999", m.raw_text_hash))
        current: list[EventExtract] = []
        for member in members:
            if current and _same_event(member, current[0], config):
                current.append(member)
                continue
            if current:
                clusters.append(_make_cluster(current))
            current = [member]
        if current:
            clusters.append(_make_cluster(current))

    for member in singles:
        clusters.append(
            EventCluster(key=_cluster_key(member), symbol=member.symbol, event_type=member.event_type, members=[member])
        )

    clusters.sort(key=lambda c: (_date_of(c.first) or "9999", c.symbol, c.event_type))
    return clusters


def _make_cluster(members: Sequence[EventExtract]) -> EventCluster:
    anchor = members[0]
    return EventCluster(
        key=f"{anchor.symbol}:{anchor.event_type}:{anchor.raw_text_hash[:20]}",
        symbol=anchor.symbol,
        event_type=anchor.event_type,
        members=list(members),
    )


def annotate_clusters(
    clusters: Sequence[EventCluster],
    *,
    history: Mapping[str, int] | Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """把聚类结果写回每条抽取（``dedupe_key`` / ``event_seq`` / ``is_first_disclosure``）。

    ``history`` 支持两种形态：已见 ``dedupe_key`` 集合，或 ``{key: 已披露次数}``。
    库里已有同一事件时，本批次的"第一条"也不是首次披露。
    """

    seen_counts: dict[str, int] = {}
    if isinstance(history, Mapping):
        for key, value in history.items():
            try:
                seen_counts[str(key)] = int(value)
            except (TypeError, ValueError):
                seen_counts[str(key)] = 1
    elif history is not None:
        for key in history:
            seen_counts[str(key)] = seen_counts.get(str(key), 0) + 1

    rows: list[dict[str, Any]] = []
    for cluster in clusters:
        prior = int(seen_counts.get(cluster.key, 0))
        for index, member in enumerate(cluster.members, start=1):
            seq = prior + index
            member.dedupe_key = cluster.key
            member.event_seq = seq
            member.is_first_disclosure = seq == 1
            member.repeat_count = len(cluster.members)
            rows.append(
                {
                    "dedupe_key": cluster.key,
                    "symbol": member.symbol,
                    "event_type": member.event_type,
                    "event_seq": seq,
                    "is_first_disclosure": int(member.is_first_disclosure or 0),
                    "repeat_count": len(cluster.members),
                    "raw_text_hash": member.raw_text_hash,
                }
            )
        seen_counts[cluster.key] = prior + len(cluster.members)
    return rows
