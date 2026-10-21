"""对外安全的错误响应与异常日志工具（#19）。

原则：
  1) 5xx 响应只返回通用消息 + error_id，绝不拼接内部异常文本；
  2) 异常类型、消息与堆栈只写服务端日志，用 error_id 关联两次记录；
  3) 原先 `except Exception: pass` 的静默分支可用 log_exception(..., level=DEBUG)
     留下可检索的痕迹。
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping

from fastapi.responses import JSONResponse

ERROR_ID_PREFIX = "err_"
DEFAULT_ERROR_MESSAGE = "服务暂时不可用，请稍后重试"
LOGGER_NAME = "alpha.error"

_logger = logging.getLogger(LOGGER_NAME)


def new_error_id() -> str:
    """生成短错误 ID（48 bit 随机十六进制，便于日志检索）。"""
    return f"{ERROR_ID_PREFIX}{uuid.uuid4().hex[:12]}"


def log_exception(
    logger: logging.Logger,
    context: str,
    exc: BaseException | None = None,
    *,
    level: int = logging.ERROR,
    error_id: str | None = None,
) -> str:
    """记录异常（含堆栈）并返回错误 ID。

    context 应为稳定的静态描述（如 "kronos predict failed"），不要拼接用户输入；
    原始异常信息只进入日志，不进入 HTTP 响应。
    """
    eid = error_id or new_error_id()
    detail = f"{type(exc).__name__}: {exc}" if exc is not None else "no exception object"
    logger.log(
        level,
        "%s [error_id=%s] %s",
        context or "unexpected error",
        eid,
        detail,
        exc_info=exc,
    )
    return eid


def _safe_detail_message(message: str, exc: BaseException | None) -> str:
    text = (message or "").strip()
    if not text:
        return DEFAULT_ERROR_MESSAGE
    # 防御误用：调用方若拼了 f"...: {exc}"，整体回退为通用消息。
    if exc is not None:
        raw = str(exc).strip()
        if raw and raw in text:
            return DEFAULT_ERROR_MESSAGE
    return text


def error_detail(
    message: str,
    exc: BaseException | None = None,
    status_code: int = 500,
    *,
    logger: logging.Logger | None = None,
    context: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """构造对外安全的错误响应。

    message 必须是静态通用文案（不要拼接用户输入或 str(exc)）；异常细节只写入
    服务端日志，并通过 error_id 关联。返回体固定为
    ``{"detail": <通用消息>, "error_id": <id>}``，同时带 X-Error-ID 响应头。

    路由内可直接 ``return error_detail("预测失败，请稍后重试", exc)``。
    """
    safe_message = _safe_detail_message(message, exc)
    active_logger = logger or _logger
    level = logging.ERROR if status_code >= 500 else logging.WARNING
    eid = log_exception(active_logger, context or safe_message, exc, level=level)
    response_headers = {"X-Error-ID": eid}
    if headers:
        response_headers.update(headers)
    return JSONResponse(
        status_code=status_code,
        content={"detail": safe_message, "error_id": eid},
        headers=response_headers,
    )
