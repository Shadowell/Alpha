"""另类数据抓取（issue #33）。

合规与稳定性红线：

* 只调用 AkShare 已封装的公开数据渠道；**不实现任何需要登录 / 绕反爬的抓取**；
* 全部经 ``app.services.blocking_io.run_blocking``：有界线程池 + 超时 + 熔断；
* 单个来源失败只降级，不抛出到调用方主流程（失败隔离）；
* 日志与异常只包含来源名和异常类型，绝不打印上游响应体、token 或 Cookie；
* 东财个股新闻接口在 akshare 内部携带浏览器 Cookie / 指纹头，属于「有条件」来源，
  默认关闭（``ALPHA_ALT_NEWS_ENABLED``），评估结论见 ``sources.py``。
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Iterable, Sequence

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
)
from app.services.blocking_io import ExternalTimeoutError, run_blocking
from app.services.time_utils import now_cn

logger = logging.getLogger("alpha.alt_sentiment")

STATUS_OK = "ok"
STATUS_EMPTY = "empty"
STATUS_FAILED = "failed"
STATUS_TIMEOUT = "timeout"
STATUS_DISABLED = "disabled"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return max(0, int(float(str(raw).strip())))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return max(0.5, float(str(raw).strip()))
    except ValueError:
        return default


def package_enabled() -> bool:
    """总开关：另类数据默认离线研究态，未显式打开就不接生产。"""
    return _env_bool("ALPHA_ALT_SENTIMENT_ENABLED", False)


def source_enabled(source: str) -> bool:
    if not package_enabled():
        return False
    if source == SOURCE_ATTENTION:
        return _env_bool("ALPHA_ALT_ATTENTION_ENABLED", True)
    if source == SOURCE_INTERACTIVE_QA:
        return _env_bool("ALPHA_ALT_INTERACTIVE_ENABLED", True)
    if source == SOURCE_NEWS:
        # 有条件来源：默认关闭，需要显式授权开关。
        return _env_bool("ALPHA_ALT_NEWS_ENABLED", False)
    return False


def max_symbols_per_run() -> int:
    return _env_int("ALPHA_ALT_MAX_SYMBOLS", 200)


@dataclass(slots=True)
class FetchOutcome:
    """一次来源级抓取结果；失败只带类型名，不带响应内容。"""

    source: str
    status: str
    records: list[AltRecord] = field(default_factory=list)
    error_type: str | None = None
    symbol: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    def sanitized(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "status": self.status,
            "count": len(self.records),
            "error_type": self.error_type,
            "symbol": self.symbol,
            "meta": self.meta,
        }


AkLoader = Callable[[], Any]
FrameNormalizer = Callable[..., list[AltRecord]]


def _default_ak_loader() -> Any:
    import akshare as ak

    return ak


class AltDataFetcher:
    """AkShare 另类数据源适配器（可注入假模块以便离线测试）。"""

    def __init__(
        self,
        *,
        ak_loader: AkLoader | None = None,
        timeout_seconds: float | None = None,
        interactive_limit: int | None = None,
        news_limit: int | None = None,
    ) -> None:
        self._ak_loader = ak_loader or _default_ak_loader
        self._ak: Any | None = None
        self.timeout_seconds = timeout_seconds or _env_float(
            "ALPHA_ALT_TIMEOUT_SECONDS", 20.0
        )
        self.interactive_limit = (
            interactive_limit
            if interactive_limit is not None
            else _env_int("ALPHA_ALT_INTERACTIVE_ROWS", 400)
        )
        self.news_limit = (
            news_limit
            if news_limit is not None
            else _env_int("ALPHA_ALT_NEWS_ROWS", 100)
        )

    # ─── 基础设施 ───

    def _module(self) -> Any | None:
        if self._ak is None:
            try:
                self._ak = self._ak_loader()
            except Exception as exc:
                logger.warning("alt_akshare_import_failed error=%s", type(exc).__name__)
                return None
        return self._ak

    async def _call(
        self,
        fn_name: str,
        *args: Any,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> Any:
        ak = self._module()
        if ak is None:
            raise RuntimeError("akshare_unavailable")
        fn = getattr(ak, fn_name, None)
        if fn is None:
            raise AttributeError(f"akshare_missing_function:{fn_name}")
        return await run_blocking(
            fn,
            *args,
            timeout=timeout or self.timeout_seconds,
            name=f"akshare.{fn_name}",
            **kwargs,
        )

    @staticmethod
    def _failure_status(exc: BaseException) -> str:
        if isinstance(exc, (ExternalTimeoutError, asyncio.TimeoutError, TimeoutError)):
            return STATUS_TIMEOUT
        return STATUS_FAILED

    # ─── 各来源 ───

    async def fetch_attention_snapshot(
        self, *, fetched_at: datetime | None = None, trade_date: str | None = None
    ) -> FetchOutcome:
        """东财千股千评全市场关注度快照（单次调用覆盖全 A 股候选）。"""
        if not source_enabled(SOURCE_ATTENTION):
            return FetchOutcome(SOURCE_ATTENTION, STATUS_DISABLED)
        stamp = fetched_at or now_cn()
        try:
            frame = await self._call("stock_comment_em", timeout=self.timeout_seconds)
        except Exception as exc:
            logger.warning("alt_attention_fetch_failed error=%s", type(exc).__name__)
            return FetchOutcome(
                SOURCE_ATTENTION,
                self._failure_status(exc),
                error_type=type(exc).__name__,
            )
        records = normalize_attention_frame(frame, fetched_at=stamp)
        if trade_date:
            records = [r for r in records if r.trade_date == trade_date]
        status = STATUS_OK if records else STATUS_EMPTY
        return FetchOutcome(
            SOURCE_ATTENTION,
            status,
            records=records,
            meta={"raw_rows": int(len(frame)) if frame is not None else 0},
        )

    async def fetch_attention_history(
        self, symbol: str, *, fetched_at: datetime | None = None
    ) -> FetchOutcome:
        """千股千评单票关注指数历史（约 30 个交易日，用于回填 growth）。"""
        if not source_enabled(SOURCE_ATTENTION):
            return FetchOutcome(SOURCE_ATTENTION, STATUS_DISABLED, symbol=symbol)
        stamp = fetched_at or now_cn()
        try:
            frame = await self._call(
                "stock_comment_detail_scrd_focus_em",
                symbol,
                timeout=self.timeout_seconds,
            )
        except Exception as exc:
            logger.warning(
                "alt_attention_history_failed symbol=%s error=%s",
                symbol,
                type(exc).__name__,
            )
            return FetchOutcome(
                SOURCE_ATTENTION,
                self._failure_status(exc),
                error_type=type(exc).__name__,
                symbol=symbol,
            )
        records = normalize_attention_frame(frame, fetched_at=stamp)
        return FetchOutcome(
            SOURCE_ATTENTION,
            STATUS_OK if records else STATUS_EMPTY,
            records=records,
            symbol=symbol,
            meta={"raw_rows": int(len(frame)) if frame is not None else 0},
        )

    async def fetch_interactive(
        self, symbol: str, *, fetched_at: datetime | None = None
    ) -> FetchOutcome:
        """互动易提问（含公司回复正文）。官方披露渠道，默认可用。"""
        if not source_enabled(SOURCE_INTERACTIVE_QA):
            return FetchOutcome(SOURCE_INTERACTIVE_QA, STATUS_DISABLED, symbol=symbol)
        stamp = fetched_at or now_cn()
        try:
            frame = await self._call(
                "stock_irm_cninfo", symbol, timeout=self.timeout_seconds
            )
        except Exception as exc:
            logger.warning(
                "alt_interactive_fetch_failed symbol=%s error=%s",
                symbol,
                type(exc).__name__,
            )
            return FetchOutcome(
                SOURCE_INTERACTIVE_QA,
                self._failure_status(exc),
                error_type=type(exc).__name__,
                symbol=symbol,
            )
        records = normalize_interactive_frame(
            frame, fetched_at=stamp, max_rows=self.interactive_limit
        )
        return FetchOutcome(
            SOURCE_INTERACTIVE_QA,
            STATUS_OK if records else STATUS_EMPTY,
            records=records,
            symbol=symbol,
            meta={"raw_rows": int(len(frame)) if frame is not None else 0},
        )

    async def fetch_news(
        self, symbol: str, *, fetched_at: datetime | None = None
    ) -> FetchOutcome:
        """东财个股新闻（有条件来源，默认关闭）。"""
        if not source_enabled(SOURCE_NEWS):
            return FetchOutcome(SOURCE_NEWS, STATUS_DISABLED, symbol=symbol)
        stamp = fetched_at or now_cn()
        try:
            frame = await self._call(
                "stock_news_em", symbol, timeout=self.timeout_seconds
            )
        except Exception as exc:
            logger.warning(
                "alt_news_fetch_failed symbol=%s error=%s", symbol, type(exc).__name__
            )
            return FetchOutcome(
                SOURCE_NEWS,
                self._failure_status(exc),
                error_type=type(exc).__name__,
                symbol=symbol,
            )
        records = normalize_news_frame(
            frame, fetched_at=stamp, max_rows=self.news_limit
        )
        return FetchOutcome(
            SOURCE_NEWS,
            STATUS_OK if records else STATUS_EMPTY,
            records=records,
            symbol=symbol,
            meta={"raw_rows": int(len(frame)) if frame is not None else 0},
        )

    async def fetch_interactive_batch(
        self,
        symbols: Sequence[str],
        *,
        concurrency: int = 3,
        fetched_at: datetime | None = None,
    ) -> list[FetchOutcome]:
        """受限并发的批量互动易抓取；单票失败不影响其他票。"""
        capped = [str(s) for s in symbols if str(s).strip()][: max_symbols_per_run()]
        if not capped:
            return []
        semaphore = asyncio.Semaphore(max(1, min(int(concurrency), 5)))

        async def _one(symbol: str) -> FetchOutcome:
            async with semaphore:
                return await self.fetch_interactive(symbol, fetched_at=fetched_at)

        results: Iterable[FetchOutcome] = await asyncio.gather(
            *[_one(symbol) for symbol in capped], return_exceptions=False
        )
        return list(results)

    async def fetch_news_batch(
        self,
        symbols: Sequence[str],
        *,
        concurrency: int = 3,
        fetched_at: datetime | None = None,
    ) -> list[FetchOutcome]:
        capped = [str(s) for s in symbols if str(s).strip()][: max_symbols_per_run()]
        if not capped:
            return []
        semaphore = asyncio.Semaphore(max(1, min(int(concurrency), 5)))

        async def _one(symbol: str) -> FetchOutcome:
            async with semaphore:
                return await self.fetch_news(symbol, fetched_at=fetched_at)

        tasks: list[Awaitable[FetchOutcome]] = [_one(symbol) for symbol in capped]
        return list(await asyncio.gather(*tasks, return_exceptions=False))
