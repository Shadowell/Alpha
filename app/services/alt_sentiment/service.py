"""另类情绪因子服务编排（issue #33）。

职责：把「抓取 → 规范化 → 落库 → 因子 → 覆盖率/评估」串成可降级的一条链路，
并把每一步的状态返回给调用方（router / 定时任务由主 agent 接线）。

失败隔离：任何一个来源超时或异常都只影响该来源的状态位，绝不抛出到主流程；
数据库异常同样降级为 ``status=degraded``，保证选股主链路不被另类数据拖垮。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence

from app.services.alt_sentiment.evaluation import (
    evaluate_alt_factors,
    research_note_markdown,
    rows_to_frame,
    attach_forward_returns,
)
from app.services.alt_sentiment.factors import AltFactorBuilder, FACTOR_NAMES
from app.services.alt_sentiment.fetchers import (
    STATUS_DISABLED,
    AltDataFetcher,
    FetchOutcome,
    package_enabled,
)
from app.services.alt_sentiment.normalize import normalize_symbol
from app.services.alt_sentiment.records import (
    SOURCE_ATTENTION,
    SOURCE_INTERACTIVE_QA,
    SOURCE_NEWS,
    AltRecord,
    AltSentimentDaily,
    snapshot_visible_time,
)
from app.services.alt_sentiment.sources import SOURCE_ASSESSMENTS
from app.services.alt_sentiment.store import AltSentimentStore
from app.services.time_utils import now_cn

logger = logging.getLogger("alpha.alt_sentiment.service")

DEFAULT_LOOKBACK_DAYS = 30


@dataclass(slots=True)
class SyncReport:
    trade_date: str
    as_of_time: str
    outcomes: list[dict[str, Any]] = field(default_factory=list)
    stored_raw: dict[str, int] = field(default_factory=dict)
    factor_rows: int = 0
    degraded: bool = False
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "trade_date": self.trade_date,
            "as_of_time": self.as_of_time,
            "outcomes": self.outcomes,
            "stored_raw": self.stored_raw,
            "factor_rows": self.factor_rows,
            "degraded": self.degraded,
            "reasons": self.reasons,
        }


class AltSentimentService:
    def __init__(
        self,
        *,
        store: AltSentimentStore | None = None,
        fetcher: AltDataFetcher | None = None,
        factor_builder: AltFactorBuilder | None = None,
        lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    ) -> None:
        self.store = store
        self.fetcher = fetcher or AltDataFetcher()
        self.factor_builder = factor_builder or AltFactorBuilder()
        self.lookback_days = max(1, int(lookback_days))

    # ─── 元信息 ───

    def source_assessment(self) -> dict[str, Any]:
        return {
            "package_enabled": package_enabled(),
            "sources": [item.to_dict() for item in SOURCE_ASSESSMENTS],
            "factors": list(FACTOR_NAMES),
            "report_markdown": self.report_markdown(),
        }

    @staticmethod
    def report_markdown() -> str:
        from app.services.alt_sentiment.sources import build_assessment_report

        return build_assessment_report()

    # ─── 抓取 + 落库 ───

    async def sync(
        self,
        symbols: Sequence[str] | None = None,
        *,
        trade_date: str | None = None,
        with_attention: bool = True,
        with_interactive: bool = True,
        with_news: bool = True,
    ) -> SyncReport:
        stamp = now_cn()
        anchor = snapshot_visible_time(trade_date or stamp.date().isoformat()) or stamp
        report = SyncReport(
            trade_date=anchor.date().isoformat(),
            as_of_time=anchor.isoformat(),
        )
        outcomes: list[FetchOutcome] = []
        clean_symbols = [s for s in (normalize_symbol(x) for x in (symbols or [])) if s]

        if with_attention:
            outcomes.append(
                await self._safe(
                    self.fetcher.fetch_attention_snapshot(
                        fetched_at=stamp, trade_date=report.trade_date
                    )
                )
            )
        if with_interactive and clean_symbols:
            outcomes.extend(
                await self._safe_batch(
                    self.fetcher.fetch_interactive_batch(
                        clean_symbols, fetched_at=stamp
                    )
                )
            )
        if with_news and clean_symbols:
            outcomes.extend(
                await self._safe_batch(
                    self.fetcher.fetch_news_batch(clean_symbols, fetched_at=stamp)
                )
            )

        records: list[AltRecord] = []
        for outcome in outcomes:
            report.outcomes.append(outcome.sanitized())
            records.extend(outcome.records)
            if outcome.status not in {STATUS_DISABLED} and outcome.status != "ok":
                report.degraded = True
                report.reasons.append(f"{outcome.source}:{outcome.status}")

        if self.store is not None and records:
            try:
                report.stored_raw = self.store.upsert_records(records)
            except Exception as exc:
                logger.warning("alt_store_write_failed error=%s", type(exc).__name__)
                report.degraded = True
                report.reasons.append(f"store:{type(exc).__name__}")
        return report

    async def _safe(self, awaitable: Any) -> FetchOutcome:
        try:
            return await awaitable
        except Exception as exc:  # 兜底：任何未预期异常都只降级
            logger.warning("alt_sync_step_failed error=%s", type(exc).__name__)
            return FetchOutcome("unknown", "failed", error_type=type(exc).__name__)

    async def _safe_batch(self, awaitable: Any) -> list[FetchOutcome]:
        try:
            return await awaitable
        except Exception as exc:
            logger.warning("alt_sync_batch_failed error=%s", type(exc).__name__)
            return [FetchOutcome("unknown", "failed", error_type=type(exc).__name__)]

    # ─── 因子 ───

    def build_factors(
        self,
        symbols: Sequence[str],
        *,
        as_of: datetime | None = None,
        limit_up_symbols: Sequence[str] | None = None,
        persist: bool = True,
    ) -> list[AltSentimentDaily]:
        """从已落库的原始记录重建因子行（只使用 ``as_of`` 之前可见的记录）。"""
        anchor = as_of or now_cn()
        limit_up = {normalize_symbol(s) for s in (limit_up_symbols or [])}
        rows: list[AltSentimentDaily] = []
        if self.store is None:
            return rows
        for raw_symbol in symbols:
            symbol = normalize_symbol(raw_symbol)
            if not symbol:
                continue
            try:
                records = self.store.load_records(
                    symbol=symbol, until_as_of=anchor, limit=4000
                )
                prior = self.store.load_daily(
                    symbol=symbol,
                    start_date=(anchor - timedelta(days=self.lookback_days))
                    .date()
                    .isoformat(),
                    end_date=anchor.date().isoformat(),
                )
            except Exception as exc:
                logger.warning(
                    "alt_record_load_failed symbol=%s error=%s",
                    symbol,
                    type(exc).__name__,
                )
                continue
            row = self.factor_builder.build(
                symbol,
                anchor,
                records,
                limit_up=(symbol in limit_up) if limit_up_symbols is not None else None,
                prior_rows=[p for p in prior if p.as_of_time < anchor],
            )
            rows.append(row)
        if persist and rows and self.store is not None:
            try:
                self.store.upsert_daily(rows)
            except Exception as exc:
                logger.warning("alt_daily_write_failed error=%s", type(exc).__name__)
        return rows

    async def run_daily(
        self,
        candidates: Sequence[str],
        *,
        trade_date: str | None = None,
        limit_up_symbols: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """每日主流程：同步 → 因子 → 覆盖率，返回结构化状态。"""
        clean = [s for s in (normalize_symbol(x) for x in candidates) if s]
        report = await self.sync(
            clean,
            trade_date=trade_date,
            with_attention=True,
            with_interactive=True,
            with_news=True,
        )
        if self.store is None:
            report.reasons.append("store:absent")
            return report.to_dict()
        rows = self.build_factors(
            clean,
            as_of=snapshot_visible_time(report.trade_date) or now_cn(),
            limit_up_symbols=limit_up_symbols,
        )
        report.factor_rows = len(rows)
        coverage = self.store.coverage(report.trade_date, clean)
        return {
            **report.to_dict(),
            "coverage": coverage,
            "factor_names": list(FACTOR_NAMES),
        }

    # ─── 研究 / 评估 ───

    def research_frame(
        self,
        *,
        symbols: Sequence[str] | None = None,
        start_date: str,
        end_date: str,
        forward_returns: Mapping[tuple[str, str], float] | Any | None = None,
        label_column: str = "fwd_ret_1d",
    ) -> Any:
        rows = (
            self.store.load_daily(symbol=None, start_date=start_date, end_date=end_date)
            if (self.store is not None)
            else []
        )
        if symbols:
            wanted = {normalize_symbol(s) for s in symbols}
            rows = [row for row in rows if normalize_symbol(row.symbol) in wanted]
        frame = rows_to_frame(rows)
        if forward_returns is not None and not frame.empty:
            frame = attach_forward_returns(frame, forward_returns, column=label_column)  # type: ignore[arg-type]
        return frame

    def offline_report(
        self,
        frame: Any,
        *,
        label_column: str,
        factors: Sequence[str] = (
            "alt_attention_score",
            "attention_index",
            "news_sentiment",
        ),
        price_columns: Sequence[str] = (),
    ) -> dict[str, Any]:
        if frame is None or frame.empty:
            return {
                "label_column": label_column,
                "ic": [],
                "layers": [],
                "correlation": [],
                "promoted_factors": [],
                "decision": "insufficient_data",
                "note": "研究帧为空：另类数据历史不足或尚未同步",
            }
        report = evaluate_alt_factors(
            frame,
            label_column=label_column,
            factors=factors,
            price_columns=price_columns,
        )
        report["research_note"] = research_note_markdown(report)
        return report


__all__ = [
    "AltSentimentService",
    "SyncReport",
    "SOURCE_ATTENTION",
    "SOURCE_INTERACTIVE_QA",
    "SOURCE_NEWS",
]
