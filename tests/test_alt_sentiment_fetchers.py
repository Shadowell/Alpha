"""另类数据抓取单测（issue #33）：开关、超时降级、失败隔离、脱敏。"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pandas as pd
import pytest

from app.services.alt_sentiment.fetchers import (
    STATUS_DISABLED,
    STATUS_EMPTY,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_TIMEOUT,
    AltDataFetcher,
    max_symbols_per_run,
    package_enabled,
    source_enabled,
)
from app.services.alt_sentiment.records import (
    SOURCE_ATTENTION,
    SOURCE_INTERACTIVE_QA,
    SOURCE_NEWS,
)
from app.services.blocking_io import reset_external_io_state


class FakeAk:
    """假 akshare 模块：只暴露被测函数，绝不发起真实网络请求。"""

    def __init__(
        self,
        *,
        comment: Any | None = None,
        irm: Any | None = None,
        news: Any | None = None,
        delay: float = 0.0,
        error: Exception | None = None,
    ) -> None:
        self.comment = comment
        self.irm = irm
        self.news = news
        self.delay = delay
        self.error = error
        self.calls: list[str] = []

    def _probe(self, name: str, payload: Any) -> Any:
        self.calls.append(name)
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        if payload is None:
            raise AttributeError(f"akshare_missing_function:{name}")
        return payload

    def stock_comment_em(self) -> Any:
        return self._probe("stock_comment_em", self.comment)

    def stock_comment_detail_scrd_focus_em(self, symbol: str) -> Any:
        return self._probe("stock_comment_detail_scrd_focus_em", self.comment)

    def stock_irm_cninfo(self, symbol: str) -> Any:
        return self._probe("stock_irm_cninfo", self.irm)

    def stock_news_em(self, symbol: str) -> Any:
        return self._probe("stock_news_em", self.news)


def _enable(monkeypatch: pytest.MonkeyPatch, *, news: bool = False) -> None:
    monkeypatch.setenv("ALPHA_ALT_SENTIMENT_ENABLED", "true")
    monkeypatch.setenv("ALPHA_ALT_NEWS_ENABLED", "true" if news else "false")
    monkeypatch.delenv("ALPHA_ALT_ATTENTION_ENABLED", raising=False)
    monkeypatch.delenv("ALPHA_ALT_INTERACTIVE_ENABLED", raising=False)


@pytest.fixture(autouse=True)
def _clean_breaker() -> Any:
    reset_external_io_state()
    yield
    reset_external_io_state()


def test_default_off_for_production_wiring(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALPHA_ALT_SENTIMENT_ENABLED", raising=False)
    assert package_enabled() is False
    assert source_enabled(SOURCE_ATTENTION) is False


def test_news_source_is_conditional(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    assert source_enabled(SOURCE_ATTENTION) is True
    assert source_enabled(SOURCE_INTERACTIVE_QA) is True
    assert source_enabled(SOURCE_NEWS) is False  # 需显式授权开关


def test_disabled_fetcher_returns_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALPHA_ALT_SENTIMENT_ENABLED", raising=False)
    fetcher = AltDataFetcher(ak_loader=FakeAk)
    outcome = asyncio.run(fetcher.fetch_attention_snapshot())
    assert outcome.status == STATUS_DISABLED
    assert outcome.records == []


def test_attention_snapshot_success(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    frame = pd.DataFrame(
        [
            {
                "代码": "600000",
                "名称": "浦发银行",
                "交易日": "2026-03-06",
                "关注指数": 120.0,
                "综合得分": 3.0,
                "目前排名": 10,
                "上升": 2,
            },
            {"代码": "", "交易日": "2026-03-06", "关注指数": 1.0},
        ]
    )
    fake = FakeAk(comment=frame)
    fetcher = AltDataFetcher(ak_loader=lambda: fake)
    outcome = asyncio.run(fetcher.fetch_attention_snapshot())
    assert outcome.status == STATUS_OK
    assert len(outcome.records) == 1
    assert outcome.records[0].symbol == "600000"
    assert "stock_comment_em" in fake.calls


def test_timeout_degrades_instead_of_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    fake = FakeAk(comment=pd.DataFrame(), delay=0.4)
    fetcher = AltDataFetcher(ak_loader=lambda: fake, timeout_seconds=0.05)
    outcome = asyncio.run(fetcher.fetch_attention_snapshot())
    assert outcome.status == STATUS_TIMEOUT
    assert outcome.error_type in {"TimeoutError", "ExternalTimeoutError"}
    assert outcome.records == []


def test_upstream_error_is_isolated_and_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    fake = FakeAk(
        irm=pd.DataFrame(), error=RuntimeError("cookie=abc token=xyz 上游 500")
    )
    fetcher = AltDataFetcher(ak_loader=lambda: fake)
    outcome = asyncio.run(fetcher.fetch_interactive("002594"))
    assert outcome.status == STATUS_FAILED
    assert outcome.error_type == "RuntimeError"
    blob = str(outcome.sanitized())
    assert "token=xyz" not in blob and "cookie=abc" not in blob


def test_missing_akshare_function_reports_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    fake = FakeAk(irm=None)
    fetcher = AltDataFetcher(ak_loader=lambda: fake)
    outcome = asyncio.run(fetcher.fetch_interactive("002594"))
    assert outcome.status == STATUS_FAILED
    assert outcome.error_type == "AttributeError"


def test_unloadable_akshare_module(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)

    def _boom() -> Any:
        raise ImportError("akshare not installed")

    fetcher = AltDataFetcher(ak_loader=_boom)
    outcome = asyncio.run(fetcher.fetch_attention_snapshot())
    assert outcome.status == STATUS_FAILED
    assert outcome.error_type == "RuntimeError"


def test_empty_frame_reports_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch, news=True)
    fake = FakeAk(news=pd.DataFrame())
    fetcher = AltDataFetcher(ak_loader=lambda: fake)
    outcome = asyncio.run(fetcher.fetch_news("600000"))
    assert outcome.status == STATUS_EMPTY


def test_interactive_batch_is_limited_and_isolated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    monkeypatch.setenv("ALPHA_ALT_MAX_SYMBOLS", "2")
    frame = pd.DataFrame(
        [
            {
                "股票代码": "002594",
                "问题": "出海进展如何？",
                "提问时间": "2026-03-06 09:00:00",
                "回答内容": "出口持续增长",
                "问题编号": "1",
            }
        ]
    )
    fake = FakeAk(irm=frame)
    fetcher = AltDataFetcher(ak_loader=lambda: fake)
    outcomes = asyncio.run(
        fetcher.fetch_interactive_batch(["002594", "600000", "600001", ""])
    )
    assert len(outcomes) == max_symbols_per_run() == 2
    assert all(o.status == STATUS_OK for o in outcomes)
    assert outcomes[0].records[0].metrics["answered"] == 1.0


def test_news_batch_requires_env_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch, news=False)
    fake = FakeAk(
        news=pd.DataFrame(
            [
                {
                    "关键词": "600000",
                    "新闻标题": "标题",
                    "发布时间": "2026-03-06 09:00:00",
                }
            ]
        )
    )
    fetcher = AltDataFetcher(ak_loader=lambda: fake)
    outcomes = asyncio.run(fetcher.fetch_news_batch(["600000"]))
    assert outcomes[0].status == STATUS_DISABLED
    assert fake.calls == []


def test_news_batch_success_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch, news=True)
    frame = pd.DataFrame(
        [
            {
                "关键词": "600000",
                "新闻标题": "公司中标重大合同",
                "新闻内容": "金额较大",
                "发布时间": "2026-03-06 09:00:00",
                "文章来源": "证券时报",
                "新闻链接": "http://finance.eastmoney.com/a/1.html",
            }
        ]
    )
    fetcher = AltDataFetcher(ak_loader=lambda: FakeAk(news=frame))
    outcomes = asyncio.run(fetcher.fetch_news_batch(["600000"]))
    assert outcomes[0].status == STATUS_OK
    assert outcomes[0].records[0].source == SOURCE_NEWS
