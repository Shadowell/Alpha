"""5xx 通用错误响应与静默异常日志工具测试（#19）。"""
from __future__ import annotations

import logging
import re

from app.services.error_response import log_exception


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
