"""另类关注度 / 情绪因子计算（issue #33）。

硬性纪律：

* 每个因子行都带 ``as_of_time``，可见性只由 ``as_of_time`` 决定；
* 窗口一律是「截至 ``as_of_time`` 之前（含）已可见」的记录，禁止用
  ``event_time`` 之外的字段偷跑；
* 窗口基期为 0 / 样本不足时返回 ``None``（缺失），不做 ``fillna(0)``；
* 因子清单与 issue 正文对齐，暂不可得的因子在 ``FACTOR_SPECS`` 里标
  ``available=False``，落库后仍然是显式缺失。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Sequence

from app.services.alt_sentiment.records import (
    SOURCE_ATTENTION,
    SOURCE_INTERACTIVE_QA,
    SOURCE_NEWS,
    AltRecord,
    AltSentimentDaily,
    is_missing,
    trade_date_of,
)
from app.services.alt_sentiment.sentiment import (
    SentimentScore,
    aggregate_scores,
    bigram_tokens,
    score_text,
)

WINDOW_DAYS = (1, 3, 7)


@dataclass(frozen=True)
class FactorSpec:
    name: str
    family: str
    unit: str
    source: str | None
    available: bool
    note: str


FACTOR_SPECS: tuple[FactorSpec, ...] = (
    FactorSpec(
        "interactive_qa_count_1d",
        "interactive",
        "count",
        SOURCE_INTERACTIVE_QA,
        True,
        "",
    ),
    FactorSpec(
        "interactive_qa_count_3d",
        "interactive",
        "count",
        SOURCE_INTERACTIVE_QA,
        True,
        "",
    ),
    FactorSpec(
        "interactive_qa_count_7d",
        "interactive",
        "count",
        SOURCE_INTERACTIVE_QA,
        True,
        "",
    ),
    FactorSpec(
        "interactive_reply_rate_7d",
        "interactive",
        "ratio",
        SOURCE_INTERACTIVE_QA,
        True,
        "已回答比例",
    ),
    FactorSpec(
        "interactive_reply_speed_hours_7d",
        "interactive",
        "hours",
        SOURCE_INTERACTIVE_QA,
        True,
        "中位回复时长；无已回答样本则缺失",
    ),
    FactorSpec(
        "social_mention_count_1d",
        "social",
        "count",
        SOURCE_NEWS,
        True,
        "新闻口径提及量",
    ),
    FactorSpec(
        "social_mention_count_3d",
        "social",
        "count",
        SOURCE_NEWS,
        True,
        "新闻口径提及量",
    ),
    FactorSpec(
        "social_mention_count_7d",
        "social",
        "count",
        SOURCE_NEWS,
        True,
        "新闻口径提及量",
    ),
    FactorSpec(
        "social_mention_growth", "social", "ratio", SOURCE_NEWS, True, "7d/前 7d"
    ),
    FactorSpec("social_sentiment", "social", "score[-1,1]", SOURCE_NEWS, True, ""),
    FactorSpec("social_sentiment_change", "social", "score", SOURCE_NEWS, True, ""),
    FactorSpec("news_count_1d", "news", "count", SOURCE_NEWS, True, ""),
    FactorSpec("news_sentiment", "news", "score[-1,1]", SOURCE_NEWS, True, ""),
    FactorSpec("news_sentiment_confidence", "news", "ratio", SOURCE_NEWS, True, ""),
    FactorSpec(
        "news_novelty", "news", "ratio", SOURCE_NEWS, True, "1 - 与历史标题重叠度"
    ),
    FactorSpec(
        "attention_index",
        "attention",
        "index",
        SOURCE_ATTENTION,
        True,
        "千股千评关注指数",
    ),
    FactorSpec(
        "attention_score",
        "attention",
        "score",
        SOURCE_ATTENTION,
        True,
        "千股千评综合得分",
    ),
    FactorSpec("attention_rank", "attention", "rank", SOURCE_ATTENTION, True, ""),
    FactorSpec(
        "attention_rank_delta",
        "attention",
        "count",
        SOURCE_ATTENTION,
        True,
        "排名上升名次",
    ),
    FactorSpec(
        "attention_index_growth_7d",
        "attention",
        "ratio",
        SOURCE_ATTENTION,
        True,
        "关注指数 7 日环比",
    ),
    FactorSpec(
        "search_index", "search", "index", None, False, "无合规免费授权来源，不接入"
    ),
    FactorSpec(
        "search_index_growth",
        "search",
        "ratio",
        None,
        False,
        "依赖 search_index，不接入",
    ),
    FactorSpec(
        "analyst_rating_change", "analyst", "count", None, False, "研报评级需商业授权"
    ),
    FactorSpec(
        "target_price_upside", "analyst", "ratio", None, False, "目标价需商业授权"
    ),
    FactorSpec("alt_attention_score", "composite", "score", None, True, "综合关注度"),
    FactorSpec(
        "alt_attention_x_limit_up",
        "composite",
        "interaction",
        None,
        True,
        "综合关注度 × 涨停",
    ),
)

FACTOR_NAMES: tuple[str, ...] = tuple(spec.name for spec in FACTOR_SPECS)
UNAVAILABLE_FACTORS: tuple[str, ...] = tuple(
    spec.name for spec in FACTOR_SPECS if not spec.available
)

# 综合关注度的成分权重（缺失成分按比例重新归一）。
COMPOSITE_WEIGHTS: dict[str, float] = {
    "attention_index": 0.35,
    "attention_score": 0.20,
    "interactive_qa_count_7d": 0.20,
    "social_mention_count_7d": 0.15,
    "news_sentiment": 0.10,
}

# 各成分的粗略满分标尺，用于把不同量纲压到 [0,1]；只做相对排序，不做绝对定价。
COMPOSITE_SCALES: dict[str, float] = {
    "attention_index": 200.0,
    "attention_score": 5.0,
    "interactive_qa_count_7d": 10.0,
    "social_mention_count_7d": 10.0,
    "news_sentiment": 1.0,
}


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None:
        return None
    if denominator <= 0:
        return None
    return round(float(numerator) / float(denominator), 6)


def _mean(values: Sequence[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 6)


def _median(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return round(float(ordered[middle]), 6)
    return round((ordered[middle - 1] + ordered[middle]) / 2.0, 6)


@dataclass(slots=True)
class AltFactorBuilder:
    """把规范记录聚合成 ``(symbol, trade_date)`` 因子行。"""

    min_news_for_sentiment: int = 1
    novelty_history_days: int = 30
    computed: dict[str, int] = field(default_factory=dict)

    # ─── 内部工具 ───

    @staticmethod
    def _visible(records: Iterable[AltRecord], as_of: datetime) -> list[AltRecord]:
        return [
            record
            for record in records
            if record.as_of_time is not None and record.as_of_time <= as_of
        ]

    def _window(
        self,
        records: Sequence[AltRecord],
        as_of: datetime,
        days: int,
        source: str,
    ) -> list[AltRecord]:
        start = as_of - timedelta(days=days)
        return [
            record
            for record in records
            if record.source == source
            and record.as_of_time is not None
            and start <= record.as_of_time <= as_of
        ]

    # ─── 主入口 ───

    def build(
        self,
        symbol: str,
        as_of: datetime,
        records: Iterable[AltRecord],
        *,
        limit_up: bool | None = None,
        prior_rows: Sequence[AltSentimentDaily] = (),
    ) -> AltSentimentDaily:
        visible = self._visible(records, as_of)
        by_source = {
            SOURCE_INTERACTIVE_QA: [
                r for r in visible if r.source == SOURCE_INTERACTIVE_QA
            ],
            SOURCE_ATTENTION: [r for r in visible if r.source == SOURCE_ATTENTION],
            SOURCE_NEWS: [r for r in visible if r.source == SOURCE_NEWS],
        }
        trade_date = trade_date_of(as_of)
        values: dict[str, float | None] = {name: None for name in FACTOR_NAMES}
        notes: dict[str, Any] = {}

        values.update(
            self._interactive_factors(by_source[SOURCE_INTERACTIVE_QA], as_of)
        )
        values.update(self._attention_factors(by_source[SOURCE_ATTENTION], as_of))
        news_values, news_notes = self._news_factors(by_source[SOURCE_NEWS], as_of)
        values.update(news_values)
        notes.update(news_notes)
        values["social_sentiment_change"] = self._sentiment_change(
            values.get("social_sentiment"), prior_rows
        )

        composite = self._composite(values)
        values["alt_attention_score"] = composite
        if composite is None or limit_up is None:
            values["alt_attention_x_limit_up"] = None
        else:
            values["alt_attention_x_limit_up"] = round(
                composite * (1.0 if limit_up else 0.0), 6
            )

        availability = {name: not is_missing(value) for name, value in values.items()}
        sources = sorted(source for source, items in by_source.items() if items)
        status = "ok" if sources else "no_data"
        return AltSentimentDaily(
            symbol=symbol,
            trade_date=trade_date,
            as_of_time=as_of,
            values=values,
            availability=availability,
            sources=sources,
            record_count=sum(len(items) for items in by_source.values()),
            status=status,
            notes=notes,
        )

    # ─── 分组因子 ───

    def _interactive_factors(
        self, records: Sequence[AltRecord], as_of: datetime
    ) -> dict[str, float | None]:
        """互动易窗口因子。

        「该标的有可见问答」是覆盖前提；有覆盖时窗口内 0 条是真实的 0，
        没有覆盖（一条都看不到）则整族因子为 ``None``，绝不写成 0 分。
        """
        out: dict[str, float | None] = {
            f"interactive_qa_count_{days}d": None for days in WINDOW_DAYS
        }
        out["interactive_reply_rate_7d"] = None
        out["interactive_reply_speed_hours_7d"] = None
        if not records:
            return out
        window_counts: dict[int, int] = {}
        for days in WINDOW_DAYS:
            window = self._window(records, as_of, days, SOURCE_INTERACTIVE_QA)
            window_counts[days] = len(window)
            out[f"interactive_qa_count_{days}d"] = float(len(window))
        window7 = self._window(records, as_of, 7, SOURCE_INTERACTIVE_QA)
        if not window7:
            return out
        out["interactive_reply_rate_7d"] = round(
            sum(1 for r in window7 if float(r.metrics.get("answered") or 0.0) > 0)
            / len(window7),
            6,
        )
        speeds = [
            float(r.metrics["reply_hours"])
            for r in window7
            if not is_missing(r.metrics.get("reply_hours"))
        ]
        out["interactive_reply_speed_hours_7d"] = _median(speeds)
        return out

    def _attention_factors(
        self, records: Sequence[AltRecord], as_of: datetime
    ) -> dict[str, float | None]:
        out: dict[str, float | None] = {
            "attention_index": None,
            "attention_score": None,
            "attention_rank": None,
            "attention_rank_delta": None,
            "attention_index_growth_7d": None,
        }
        if not records:
            return out
        ordered = sorted(
            records,
            key=lambda r: r.as_of_time or datetime.min,
        )
        latest = ordered[-1]
        for metric, name in (
            ("attention_index", "attention_index"),
            ("attention_score", "attention_score"),
            ("attention_rank", "attention_rank"),
            ("attention_rank_delta", "attention_rank_delta"),
        ):
            value = latest.metrics.get(metric)
            out[name] = None if is_missing(value) else float(value)
        current = out["attention_index"]
        if current is None:
            return out
        week_ago_anchor = as_of - timedelta(days=7)
        history = [
            r
            for r in ordered
            if r.as_of_time is not None and r.as_of_time <= week_ago_anchor
        ]
        baseline: float | None = None
        if history:
            baseline_value = history[-1].metrics.get("attention_index")
            baseline = None if is_missing(baseline_value) else float(baseline_value)
        if baseline is None:
            earlier = [
                r.metrics.get("attention_index")
                for r in ordered
                if not is_missing(r.metrics.get("attention_index"))
            ]
            baseline = _mean([float(v) for v in earlier]) if len(earlier) >= 3 else None
        out["attention_index_growth_7d"] = _ratio(current, baseline)
        return out

    def _news_factors(
        self, records: Sequence[AltRecord], as_of: datetime
    ) -> tuple[dict[str, float | None], dict[str, Any]]:
        out: dict[str, float | None] = {
            "social_mention_count_1d": None,
            "social_mention_count_3d": None,
            "social_mention_count_7d": None,
            "social_mention_growth": None,
            "social_sentiment": None,
            "social_sentiment_change": None,
            "news_count_1d": None,
            "news_sentiment": None,
            "news_sentiment_confidence": None,
            "news_novelty": None,
        }
        notes: dict[str, Any] = {}
        if not records:
            notes["news_status"] = "no_data"
            return out, notes

        counts: dict[int, int] = {}
        for days in WINDOW_DAYS:
            window = self._window(records, as_of, days, SOURCE_NEWS)
            counts[days] = len(window)
            # 与互动易同规则：来源对该标的有可见记录时，窗口内 0 条是真实观测值；
            # 完全没有可见记录才在函数开头返回缺失。
            out[f"social_mention_count_{days}d"] = float(len(window))

        out["news_count_1d"] = float(counts[1])

        recent7 = self._window(records, as_of, 7, SOURCE_NEWS)
        prior7 = [
            r
            for r in records
            if r.as_of_time is not None
            and as_of - timedelta(days=14) <= r.as_of_time < as_of - timedelta(days=7)
        ]
        out["social_mention_growth"] = _ratio(float(len(recent7)), float(len(prior7)))

        scored: list[tuple[SentimentScore, AltRecord]] = []
        invalid = 0
        for record in recent7:
            result = score_text(record.title, record.text)
            if result.available:
                scored.append((result, record))
            else:
                invalid += 1
        if len(scored) >= max(1, int(self.min_news_for_sentiment)):
            aggregate = aggregate_scores([item[0] for item in scored])
            out["news_sentiment"] = aggregate.score
            out["news_sentiment_confidence"] = aggregate.confidence
            out["social_sentiment"] = aggregate.score
        notes["news_scored"] = len(scored)
        notes["news_unparsable"] = invalid

        prior_anchors = sorted(
            (r for r in records if r.as_of_time is not None and r.as_of_time < as_of),
            key=lambda r: r.as_of_time or datetime.min,
        )[-20:]
        history_tokens: set[str] = set()
        for record in prior_anchors:
            history_tokens |= bigram_tokens(record.title)
        current_tokens: set[str] = set()
        for record in recent7:
            current_tokens |= bigram_tokens(record.title)
        if current_tokens and history_tokens:
            overlap = len(current_tokens & history_tokens) / len(current_tokens)
            out["news_novelty"] = round(max(0.0, min(1.0, 1.0 - overlap)), 6)

        if counts[1] and counts[3]:
            notes["mention_daily_ratio"] = round(counts[1] / counts[3], 6)
        return out, notes

    @staticmethod
    def _sentiment_change(
        current: float | None, prior_rows: Sequence[AltSentimentDaily]
    ) -> float | None:
        """相对上一交易日的情绪变化；任一端缺失即缺失。"""
        if is_missing(current) or not prior_rows:
            return None
        ordered = sorted(prior_rows, key=lambda row: row.as_of_time)
        for row in reversed(ordered):
            previous = row.factor("social_sentiment")
            if previous is not None:
                return round(float(current) - previous, 6)  # type: ignore[arg-type]
        return None

    def _composite(
        self,
        values: dict[str, float | None],
    ) -> float | None:
        numerator = 0.0
        mass = 0.0
        for name, weight in COMPOSITE_WEIGHTS.items():
            value = values.get(name)
            if is_missing(value):
                continue
            scale = COMPOSITE_SCALES.get(name, 1.0)
            if scale <= 0:
                continue
            if name in {"news_sentiment", "social_sentiment"}:
                # 情绪本身已是有界 [-1,1] 的强度值，保留符号。
                normalized = max(-1.0, min(1.0, float(value)))
            else:
                normalized = max(0.0, min(1.0, float(value) / scale))
            numerator += weight * normalized
            mass += weight
        if mass <= 0 or mass < sum(COMPOSITE_WEIGHTS.values()) * 0.4:
            # 可用成分不足 40% 权重时，综合分本身不可信 → 显式缺失。
            return None
        return round(numerator / mass, 6)


def factor_frame_from_rows(rows: Iterable[AltSentimentDaily]) -> Any:
    """因子行 → DataFrame（缺失保持 NaN，另配 ``*_available`` 掩码列）。"""
    import pandas as pd

    records: list[dict[str, Any]] = []
    for row in rows:
        item: dict[str, Any] = {
            "symbol": row.symbol,
            "trade_date": row.trade_date,
            "as_of_time": row.as_of_time.isoformat(),
            "sources": ",".join(row.sources),
            "record_count": row.record_count,
            "status": row.status,
        }
        for name in FACTOR_NAMES:
            value = row.values.get(name)
            item[name] = float("nan") if is_missing(value) else float(value)
            item[f"{name}__available"] = bool(row.availability.get(name, False))
        records.append(item)
    if not records:
        return pd.DataFrame()
    frame = pd.DataFrame.from_records(records)
    return frame.sort_values(["trade_date", "symbol"]).reset_index(drop=True)
