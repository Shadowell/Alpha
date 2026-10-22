"""另类情绪服务编排单测（issue #33）：日频链路、降级、覆盖率、离线报告。"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pandas as pd
import pytest

from app.services.alt_sentiment.factors import AltFactorBuilder
from app.services.alt_sentiment.fetchers import (
    STATUS_DISABLED,
    STATUS_FAILED,
    STATUS_OK,
    FetchOutcome,
)
from app.services.alt_sentiment.normalize import (
    normalize_attention_frame,
    normalize_interactive_frame,
    normalize_symbol,
)
from app.services.alt_sentiment.records import (
    SOURCE_ATTENTION,
    SOURCE_INTERACTIVE_QA,
    SOURCE_NEWS,
    snapshot_visible_time,
)
from app.services.alt_sentiment.service import AltSentimentService
from app.services.alt_sentiment.store import AltSentimentStore

TRADE_DATE = "2026-03-06"
AS_OF = snapshot_visible_time(TRADE_DATE)
DEFAULT_CANDIDATES = [f"6000{i:02d}" for i in range(20)]


class StubFetcher:
    """返回固定帧的假抓取器；可指定某个来源失败以验证失败隔离。"""

    def __init__(
        self,
        *,
        fail_attention: bool = False,
        fail_interactive: bool = False,
        news_enabled: bool = False,
    ) -> None:
        self.fail_attention = fail_attention
        self.fail_interactive = fail_interactive
        self.news_enabled = news_enabled

    async def fetch_attention_snapshot(
        self, *, fetched_at=None, trade_date=None
    ) -> FetchOutcome:
        if self.fail_attention:
            return FetchOutcome(
                SOURCE_ATTENTION, STATUS_FAILED, error_type="TimeoutError"
            )
        rows = [
            {
                "代码": symbol,
                "名称": f"标的{symbol}",
                "交易日": trade_date or TRADE_DATE,
                "关注指数": 100.0 + index * 5,
                "综合得分": 3.0 + index * 0.1,
                "目前排名": 50 - index,
                "上升": float(index),
                "机构参与度": 0.2,
            }
            for index, symbol in enumerate(DEFAULT_CANDIDATES)
        ]
        frame = pd.DataFrame(rows)
        return FetchOutcome(
            SOURCE_ATTENTION,
            STATUS_OK,
            records=normalize_attention_frame(frame, fetched_at=fetched_at),
            meta={"raw_rows": len(rows)},
        )

    async def fetch_attention_history(self, symbol, *, fetched_at=None) -> FetchOutcome:
        return FetchOutcome(SOURCE_ATTENTION, STATUS_OK, symbol=symbol)

    async def fetch_interactive_batch(
        self, symbols, *, concurrency: int = 3, fetched_at=None
    ) -> list[FetchOutcome]:
        if self.fail_interactive:
            return [
                FetchOutcome(
                    SOURCE_INTERACTIVE_QA,
                    STATUS_FAILED,
                    symbol=symbol,
                    error_type="RuntimeError",
                )
                for symbol in symbols
            ]
        frame = pd.DataFrame(
            [
                {
                    "股票代码": symbol,
                    "问题": "公司订单饱满吗？",
                    "提问时间": (AS_OF - timedelta(hours=3)).isoformat(),
                    "回答内容": "订单充足",
                    "问题编号": f"q-{symbol}",
                }
                for symbol in symbols
            ]
        )
        return [
            FetchOutcome(
                SOURCE_INTERACTIVE_QA,
                STATUS_OK,
                records=normalize_interactive_frame(frame, fetched_at=fetched_at),
                symbol=symbol,
            )
            for symbol in symbols
        ]

    async def fetch_news_batch(
        self, symbols, *, concurrency: int = 3, fetched_at=None
    ) -> list[FetchOutcome]:
        if not self.news_enabled:
            return [
                FetchOutcome(SOURCE_NEWS, STATUS_DISABLED, symbol=s) for s in symbols
            ]
        frame = pd.DataFrame(
            [
                {
                    "关键词": symbol,
                    "新闻标题": "公司中标重大合同",
                    "新闻内容": "订单金额较大",
                    "发布时间": (AS_OF - timedelta(hours=2)).isoformat(),
                    "文章来源": "证券时报",
                    "新闻链接": "http://finance.eastmoney.com/a/1.html",
                }
                for symbol in symbols
            ]
        )
        from app.services.alt_sentiment.normalize import normalize_news_frame

        return [
            FetchOutcome(
                SOURCE_NEWS,
                STATUS_OK,
                records=normalize_news_frame(frame, fetched_at=fetched_at),
                symbol=symbol,
            )
            for symbol in symbols
        ]


@pytest.fixture()
def service(tmp_path) -> AltSentimentService:
    store = AltSentimentStore(tmp_path / "alt.db")
    return AltSentimentService(store=store, fetcher=StubFetcher())


def test_run_daily_builds_rows_and_coverage(service: AltSentimentService) -> None:
    result = asyncio.run(service.run_daily(DEFAULT_CANDIDATES, trade_date=TRADE_DATE))
    assert result["trade_date"] == TRADE_DATE
    assert result["as_of_time"].startswith(TRADE_DATE)
    coverage = result["coverage"]
    assert coverage["total"] == len(DEFAULT_CANDIDATES)
    assert coverage["ratio"] == pytest.approx(1.0)
    assert coverage["ratio"] >= 0.8  # 验收：至少一个来源覆盖 ≥80% 涨停候选
    assert result["factor_rows"] == len(DEFAULT_CANDIDATES)
    assert result["degraded"] is False  # 未启用的新闻源不算故障降级
    statuses = {item["source"]: item["status"] for item in result["outcomes"]}
    assert statuses[SOURCE_NEWS] == STATUS_DISABLED
    assert result["stored_raw"]["inserted"] > 0


def test_run_daily_is_idempotent_on_rerun(service: AltSentimentService) -> None:
    asyncio.run(service.run_daily(DEFAULT_CANDIDATES, trade_date=TRADE_DATE))
    second = asyncio.run(service.run_daily(DEFAULT_CANDIDATES, trade_date=TRADE_DATE))
    stats = service.store.stats()
    assert stats["daily_rows"] == len(DEFAULT_CANDIDATES)
    assert second["stored_raw"] == {
        "inserted": 0,
        "skipped": second["stored_raw"]["skipped"],
    }
    assert stats["raw_by_source"][0]["count"] == len(DEFAULT_CANDIDATES)


def test_attention_failure_degrades_without_breaking(
    service: AltSentimentService,
) -> None:
    service.fetcher = StubFetcher(fail_attention=True)
    result = asyncio.run(service.run_daily(DEFAULT_CANDIDATES, trade_date=TRADE_DATE))
    assert result["degraded"] is True
    assert f"{SOURCE_ATTENTION}:{STATUS_FAILED}" in result["reasons"]
    statuses = {item["source"]: item["status"] for item in result["outcomes"]}
    assert statuses[SOURCE_ATTENTION] == STATUS_FAILED
    assert result["factor_rows"] == len(DEFAULT_CANDIDATES)
    rows = service.store.load_daily()
    assert len(rows) == len(DEFAULT_CANDIDATES)
    for row in rows:
        assert row.factor("interactive_qa_count_1d") == pytest.approx(1.0)
        # 关注度成分缺失后综合分可用权重不足 → 显式缺失，绝不退回 0 分
        assert row.factor("alt_attention_score") is None


def test_build_factors_only_uses_visible_records(service: AltSentimentService) -> None:
    asyncio.run(service.run_daily(DEFAULT_CANDIDATES, trade_date=TRADE_DATE))
    rows = service.build_factors(
        DEFAULT_CANDIDATES[:3], as_of=AS_OF - timedelta(hours=6), persist=False
    )
    assert len(rows) == 3
    for row in rows:
        assert row.status == "no_data"
        assert row.factor("attention_index") is None
        assert row.is_available("attention_index") is False
        assert row.record_count == 0


def test_news_source_feeds_mention_and_sentiment(tmp_path) -> None:
    store = AltSentimentStore(tmp_path / "alt2.db")
    service = AltSentimentService(store=store, fetcher=StubFetcher(news_enabled=True))
    result = asyncio.run(service.run_daily(["600000", "600001"], trade_date=TRADE_DATE))
    assert result["degraded"] is False
    rows = service.store.load_daily()
    assert len(rows) == 2
    for row in rows:
        assert row.factor("social_mention_count_1d") == pytest.approx(1.0)
        assert row.factor("news_sentiment") is not None
        assert row.factor("news_sentiment") > 0
        assert row.factor("news_sentiment_confidence") > 0
        assert "eastmoney_news" in row.sources


def test_source_assessment_reports_all_candidates(service: AltSentimentService) -> None:
    payload = service.source_assessment()
    decisions = {item["key"]: item["decision"] for item in payload["sources"]}
    assert decisions["eastmoney_attention"] == "adopt"
    assert decisions["interactive_qa"] == "adopt"
    assert decisions["eastmoney_news"] == "conditional"
    assert decisions["guba_posts"] == "reject"
    assert decisions["search_index"] == "reject"
    assert decisions["analyst_reports"] == "reject"
    assert "数据源评估报告" in payload["report_markdown"]
    assert "ALPHA_ALT_NEWS_ENABLED" in payload["report_markdown"]


def test_offline_report_shape_and_decision(tmp_path) -> None:
    store = AltSentimentStore(tmp_path / "alt3.db")
    dates = ["2026-03-02", "2026-03-03", "2026-03-04"]
    service = AltSentimentService(store=store, fetcher=MultiDayFetcher(dates))
    for day in dates:
        asyncio.run(service.run_daily(MultiDayFetcher.CANDIDATES, trade_date=day))
    frame = service.research_frame(
        start_date=dates[0],
        end_date=dates[-1],
        forward_returns=MultiDayFetcher.returns(dates),
        label_column="fwd_ret_1d",
    )
    assert not frame.empty
    assert frame["fwd_ret_1d"].notna().all()
    report = service.offline_report(
        frame, label_column="fwd_ret_1d", factors=("alt_attention_score",)
    )
    assert report["decision"] in {"adopt", "stop_and_record"}
    assert report["ic"][0]["factor"] == "alt_attention_score"
    assert "另类因子离线验证结论" in report["research_note"]


def test_offline_report_handles_empty_frame(service: AltSentimentService) -> None:
    report = service.offline_report(pd.DataFrame(), label_column="fwd_ret_1d")
    assert report["decision"] == "insufficient_data"


def test_service_without_store_is_safe() -> None:
    service = AltSentimentService(store=None, fetcher=StubFetcher())
    result = asyncio.run(service.run_daily(["600000"], trade_date=TRADE_DATE))
    assert result["reasons"] == ["store:absent"]
    assert result["factor_rows"] == 0
    assert service.build_factors(["600000"]) == []


def test_normalize_candidate_symbols_before_sync(service: AltSentimentService) -> None:
    asyncio.run(
        service.run_daily(["SH600000", "600001.SZ", "bad"], trade_date=TRADE_DATE)
    )
    rows = service.store.load_daily()
    assert {row.symbol for row in rows} == {"600000", "600001"}
    assert normalize_symbol("bad") == ""


def test_store_read_failure_is_isolated(service: AltSentimentService) -> None:
    asyncio.run(service.run_daily(["600000", "600001"], trade_date=TRADE_DATE))

    def _boom(**_: object) -> list[object]:
        raise RuntimeError("db locked")

    service.store.load_records = _boom  # type: ignore[method-assign]
    rows = service.build_factors(["600000", "600001"], persist=False)
    assert rows == []  # 读库异常只跳过该标的，不冒泡到选股主链路


def test_factor_builder_is_reused(service: AltSentimentService) -> None:
    assert isinstance(service.factor_builder, AltFactorBuilder)


class MultiDayFetcher:
    """多天关注度快照，用于跑通离线 IC 链路。"""

    CANDIDATES = [f"6001{i:02d}" for i in range(30)]

    def __init__(self, dates: list[str]) -> None:
        self.dates = dates

    @staticmethod
    def returns(dates: list[str]) -> dict[tuple[str, str], float]:
        out: dict[tuple[str, str], float] = {}
        for day_index, day in enumerate(dates):
            for index, symbol in enumerate(MultiDayFetcher.CANDIDATES):
                out[(symbol, day)] = 0.001 * index + 0.01 * day_index
        return out

    async def fetch_attention_snapshot(
        self, *, fetched_at=None, trade_date=None
    ) -> FetchOutcome:
        day = trade_date or self.dates[-1]
        rows = [
            {
                "代码": symbol,
                "名称": f"标的{symbol}",
                "交易日": day,
                "关注指数": 80.0 + index * 3,
                "综合得分": 2.0 + index * 0.05,
                "目前排名": 100 - index,
                "上升": 1.0,
                "机构参与度": 0.1,
            }
            for index, symbol in enumerate(self.CANDIDATES)
        ]
        return FetchOutcome(
            SOURCE_ATTENTION,
            STATUS_OK,
            records=normalize_attention_frame(
                pd.DataFrame(rows), fetched_at=fetched_at
            ),
        )

    async def fetch_interactive_batch(
        self, symbols, *, concurrency: int = 3, fetched_at=None
    ) -> list[FetchOutcome]:
        return [
            FetchOutcome(SOURCE_INTERACTIVE_QA, STATUS_OK, symbol=s) for s in symbols
        ]

    async def fetch_news_batch(
        self, symbols, *, concurrency: int = 3, fetched_at=None
    ) -> list[FetchOutcome]:
        return [FetchOutcome(SOURCE_NEWS, STATUS_DISABLED, symbol=s) for s in symbols]
