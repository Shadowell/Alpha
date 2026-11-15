"""外部调用护栏：超时、失败隔离、日志脱敏（issue #35 工程要求）。

研究链路要接两类外部数据：公告原文抓取与 LLM 结构化抽取。两者的共同点是
"不可靠且可能长时间挂起"，所以统一走这里：

* :func:`run_with_timeout` —— 子线程 + 硬超时。超时只放弃等待，工作线程
  本身仍会跑到返回为止，因此线程池必须小且有界（与
  ``app/services/blocking_io.py`` 同一结论，这里为了不依赖 app 包而自带一份）；
* :func:`outcome` —— 把任何异常收敛成 ``FetchOutcome(status=...)``，
  调用方拿到的是显式降级标记而不是静默空结果；
* :func:`redact` —— 日志里绝不出现 api key / token / cookie / Authorization。
"""

from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar

logger = logging.getLogger("alpha.daban_events")

T = TypeVar("T")

DEFAULT_TIMEOUT_SECONDS = 20.0

STATUS_OK = "ok"
STATUS_TIMEOUT = "timeout"
STATUS_ERROR = "error"
STATUS_EMPTY = "empty"
STATUS_UNAVAILABLE = "unavailable"

#: 明确不可用的数据源必须带这个原因串，报表与前端都靠它区分"没数据"和"取不到"。
REASON_PERMISSION = "permission_denied_or_no_access"
REASON_NOT_IMPLEMENTED = "source_not_integrated"

_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|authorization|bearer|cookie|token|secret|password)"
    r"\s*[=:]\s*(\S+)"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{6,}")


def redact(text: Any, *, limit: int = 240) -> str:
    """把可能带凭据的字符串脱敏后再截断，用于日志与异常信息。"""

    raw = str(text if text is not None else "")
    masked = _SECRET_RE.sub(lambda m: f"{m.group(1)}=***", raw)
    masked = _BEARER_RE.sub("Bearer ***", masked)
    masked = " ".join(masked.split())
    if len(masked) > limit:
        masked = masked[:limit] + "…"
    return masked


@dataclass(slots=True)
class FetchOutcome:
    """一次外部调用的结构化结论（永不静默失败）。"""

    source: str
    status: str
    rows: list[Any] = field(default_factory=list)
    reason: str = ""
    detail: str = ""
    elapsed_ms: int | None = None
    degraded: bool = False

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "status": self.status,
            "row_count": len(self.rows),
            "reason": self.reason,
            "detail": self.detail,
            "elapsed_ms": self.elapsed_ms,
            "degraded": int(self.degraded),
        }


def run_with_timeout(
    fn: Callable[[], T],
    *,
    source: str,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> FetchOutcome:
    """执行无参可调用并把结果/故障收敛成 :class:`FetchOutcome`。

    ``fn`` 返回 ``None`` / 空序列 -> ``status=empty``（不算 degraded：
    "当天真的没有公告" 与 "取不到公告" 是两件事）。
    """

    started = time.monotonic()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"daban-{source}")
    future = executor.submit(fn)
    try:
        payload = future.result(timeout=max(0.001, float(timeout_seconds)))
    except TimeoutError:
        future.cancel()
        elapsed = int((time.monotonic() - started) * 1000)
        logger.warning(
            "[daban_events] source=%s timed out after %.1fs", source, timeout_seconds
        )
        return FetchOutcome(
            source=source,
            status=STATUS_TIMEOUT,
            reason="timeout",
            detail=f"exceeded {timeout_seconds:.1f}s",
            elapsed_ms=elapsed,
            degraded=True,
        )
    except Exception as exc:  # noqa: BLE001 - 外部源任何异常都必须隔离
        elapsed = int((time.monotonic() - started) * 1000)
        logger.warning(
            "[daban_events] source=%s failed: %s: %s",
            type(exc).__name__,
            source,
            redact(exc),
        )
        return FetchOutcome(
            source=source,
            status=STATUS_ERROR,
            reason=f"{type(exc).__name__}",
            detail=redact(exc),
            elapsed_ms=elapsed,
            degraded=True,
        )
    finally:
        # 超时后线程可能仍在跑；wait=False 保证调用方不被二次拖住。
        executor.shutdown(wait=False, cancel_futures=True)

    elapsed = int((time.monotonic() - started) * 1000)
    rows = _as_rows(payload)
    if not rows:
        return FetchOutcome(
            source=source,
            status=STATUS_EMPTY,
            rows=[],
            reason="empty_result",
            elapsed_ms=elapsed,
            degraded=False,
        )
    return FetchOutcome(
        source=source, status=STATUS_OK, rows=rows, elapsed_ms=elapsed
    )


def _as_rows(payload: Any) -> list[Any]:
    if payload is None:
        return []
    if isinstance(payload, (list, tuple, set)):
        return [row for row in payload if row is not None]
    try:
        import pandas as pd

        if isinstance(payload, pd.DataFrame):
            return [] if payload.empty else payload.to_dict("records")
    except Exception:  # pragma: no cover - pandas 缺席时按不可迭代处理
        pass
    if isinstance(payload, dict):
        return [payload]
    return [payload]
