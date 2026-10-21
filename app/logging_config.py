"""统一日志配置 — 结构化文本 / JSON，附带 request_id / task / symbol / duration_ms。

使用方式：
    from app.logging_config import configure_logging, log_context, set_request_id
    configure_logging()
    with log_context(task="kline-sync", symbol="600519"):
        logging.getLogger(__name__).info("synced")
"""
from __future__ import annotations

import json
import logging
import os
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

_REQUEST_ID: ContextVar[str] = ContextVar("request_id", default="-")
_TASK: ContextVar[str] = ContextVar("task", default="-")
_SYMBOL: ContextVar[str] = ContextVar("symbol", default="-")

_STANDARD_ATTRS = set(vars(logging.LogRecord("x", 0, "", 0, "", (), None)).keys()) | {
    "message",
    "asctime",
    "taskName",
}

_TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s [req=%(request_id)s task=%(task)s symbol=%(symbol)s] %(message)s"


def set_request_id(value: str) -> Any:
    return _REQUEST_ID.set(value)


def reset_request_id(token: Any) -> None:
    _REQUEST_ID.reset(token)


def current_request_id() -> str:
    return _REQUEST_ID.get()


def set_task(value: str) -> Any:
    return _TASK.set(value)


def reset_task(token: Any) -> None:
    _TASK.reset(token)


@contextmanager
def log_context(*, task: str | None = None, symbol: str | None = None) -> Iterator[None]:
    tokens: list[tuple[ContextVar[str], Any]] = []
    if task is not None:
        tokens.append((_TASK, _TASK.set(task)))
    if symbol is not None:
        tokens.append((_SYMBOL, _SYMBOL.set(symbol)))
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        record.request_id = getattr(record, "request_id", None) or _REQUEST_ID.get()
        record.task = getattr(record, "task", None) or _TASK.get()
        record.symbol = getattr(record, "symbol", None) or _SYMBOL.get()
        return True


class JsonFormatter(logging.Formatter):
    """每行一个 JSON 对象，便于容器日志采集。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
            "task": getattr(record, "task", "-"),
            "symbol": getattr(record, "symbol", "-"),
        }
        for key, value in vars(record).items():
            if key in _STANDARD_ATTRS or key in payload:
                continue
            if key.startswith("_"):
                continue
            payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(
    *,
    level: str | None = None,
    fmt: str | None = None,
    force: bool = False,
) -> None:
    """配置根 logger。幂等：重复调用不会叠加 handler（同一进程只生效一次）。"""
    root = logging.getLogger()
    if getattr(root, "_alpha_configured", False) and not force:
        return
    resolved_level = (level or os.getenv("ALPHA_LOG_LEVEL", "INFO")).upper()
    resolved_fmt = (fmt or os.getenv("ALPHA_LOG_FORMAT", "text")).lower()
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(_ContextFilter())
    if resolved_fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(_TEXT_FORMAT))
    root.handlers = [handler]
    root.setLevel(getattr(logging, resolved_level, logging.INFO))
    root._alpha_configured = True  # type: ignore[attr-defined]
    # 第三方库降噪
    for noisy in ("httpx", "httpcore", "urllib3", "akshare", "matplotlib"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
