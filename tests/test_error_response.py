"""5xx 通用错误响应与静默异常日志工具测试（#19）。"""
from __future__ import annotations

import json
import logging
import re

import pytest

from app.services import error_response
from app.services.error_response import error_detail, log_exception


def test_log_exception_logs_context_error_id_and_traceback(caplog) -> None:
    logger = logging.getLogger("tests.error_response")
    try:
        raise RuntimeError("db password=hunter2 at /srv/alpha/private.py")
    except RuntimeError as caught:
        exc = caught

    with caplog.at_level(logging.ERROR, logger="tests.error_response"):
        error_id = log_exception(logger, "预测任务失败", exc)

    assert re.fullmatch(r"err_[0-9a-f]{12}", error_id)
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelno == logging.ERROR
    assert record.name == "tests.error_response"
    message = record.getMessage()
    assert "预测任务失败" in message
    assert error_id in message
    # 服务端日志必须保留原始异常信息，便于排障。
    assert "hunter2" in message
    assert record.exc_info is not None
    assert record.exc_info[1] is exc
    # exc_info 里带有 traceback，真实 handler 会据此渲染完整堆栈。
    assert record.exc_info[2] is not None


def test_log_exception_without_exception_object(caplog) -> None:
    logger = logging.getLogger("tests.error_response")

    with caplog.at_level(logging.ERROR, logger="tests.error_response"):
        error_id = log_exception(logger, "后台任务状态未知", None)

    assert re.fullmatch(r"err_[0-9a-f]{12}", error_id)
    assert len(caplog.records) == 1
    assert caplog.records[0].exc_info is None
    assert "后台任务状态未知" in caplog.records[0].getMessage()


def test_log_exception_supports_debug_level_and_explicit_error_id(caplog) -> None:
    logger = logging.getLogger("tests.error_response")

    with caplog.at_level(logging.DEBUG, logger="tests.error_response"):
        error_id = log_exception(
            logger,
            "清理缓存失败（非关键路径）",
            ValueError("cache miss"),
            level=logging.DEBUG,
            error_id="err_0123456789ab",
        )

    assert error_id == "err_0123456789ab"
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.DEBUG
    assert "err_0123456789ab" in caplog.records[0].getMessage()


def test_log_exception_generates_unique_ids() -> None:
    logger = logging.getLogger("tests.error_response")
    first = log_exception(logger, "first", None)
    second = log_exception(logger, "second", None)
    assert first != second


# ── 对外 5xx 响应：通用消息 + error_id ───────────────────────

SECRET_EXCEPTION_TEXT = "connect failed: password=hunter2 at /srv/alpha/private.py"


def _payload(response) -> dict:
    return json.loads(response.body.decode("utf-8"))


def test_error_detail_returns_generic_message_and_error_id(caplog) -> None:
    try:
        raise RuntimeError(SECRET_EXCEPTION_TEXT)
    except RuntimeError as exc:
        with caplog.at_level(logging.ERROR, logger=error_response.LOGGER_NAME):
            response = error_detail("预测失败，请稍后重试", exc)

    body = response.body.decode("utf-8")
    assert response.status_code == 500
    assert response.media_type == "application/json"
    # 响应体（含 headers）不得回显任何内部异常文本。
    assert SECRET_EXCEPTION_TEXT not in body
    assert "hunter2" not in body
    assert "RuntimeError" not in body
    data = _payload(response)
    assert set(data) == {"detail", "error_id"}
    assert data["detail"] == "预测失败，请稍后重试"
    assert re.fullmatch(r"err_[0-9a-f]{12}", data["error_id"])
    assert response.headers["x-error-id"] == data["error_id"]
    # 服务端日志保留原始异常。
    assert "hunter2" in caplog.text
    assert data["error_id"] in caplog.text


@pytest.mark.parametrize("status_code", [500, 502, 503])
def test_error_detail_preserves_status_code(status_code: int) -> None:
    response = error_detail("上游服务暂时不可用", None, status_code=status_code)
    assert response.status_code == status_code
    assert _payload(response)["detail"] == "上游服务暂时不可用"


def test_error_detail_uses_default_message_for_empty_message() -> None:
    response = error_detail("", RuntimeError("internal detail"))
    data = _payload(response)
    assert data["detail"] == error_response.DEFAULT_ERROR_MESSAGE
    assert "internal detail" not in response.body.decode("utf-8")


def test_error_detail_drops_message_that_embeds_exception_text() -> None:
    """防止调用方误用 f"失败: {exc}" 时把内部异常带进响应。"""
    exc = RuntimeError(SECRET_EXCEPTION_TEXT)
    response = error_detail(f"预测失败: {exc}", exc)
    body = response.body.decode("utf-8")
    assert SECRET_EXCEPTION_TEXT not in body
    assert _payload(response)["detail"] == error_response.DEFAULT_ERROR_MESSAGE


def test_error_detail_generates_unique_error_ids() -> None:
    first = _payload(error_detail("失败 A", None))["error_id"]
    second = _payload(error_detail("失败 B", None))["error_id"]
    assert first != second


def test_error_detail_supports_custom_logger_context_and_headers(caplog) -> None:
    custom_logger = logging.getLogger("tests.error_response.custom")
    with caplog.at_level(logging.ERROR, logger="tests.error_response.custom"):
        response = error_detail(
            "公告选股失败",
            ValueError("boom"),
            status_code=503,
            logger=custom_logger,
            context="notice.screen",
            headers={"Retry-After": "5"},
        )

    assert "notice.screen" in caplog.text
    assert response.headers["retry-after"] == "5"
    assert response.headers["x-error-id"] == _payload(response)["error_id"]
