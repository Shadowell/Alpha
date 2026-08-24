"""写操作访问控制 — 纯函数实现，便于独立单测。

规则：
  1) 配置了 ALPHA_WRITE_TOKEN → 所有写请求必须携带匹配的 X-Alpha-Token 头；
  2) 未配置 token → 仅允许本机回环来源（127.0.0.1 / ::1）发起写请求。
读请求不受影响。"testclient" 为 Starlette 测试客户端来源标记，视为回环。
"""
from __future__ import annotations

import os

WRITE_METHODS = {"POST", "PUT", "DELETE", "PATCH"}
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


def write_access_decision(
    *,
    method: str,
    client_host: str | None,
    header_token: str | None,
    configured_token: str | None = None,
) -> str | None:
    """判定写请求是否放行。返回 None 放行，返回字符串为拒绝原因。"""
    if method not in WRITE_METHODS:
        return None
    token = (
        configured_token
        if configured_token is not None
        else os.getenv("ALPHA_WRITE_TOKEN", "")
    ).strip()
    host = (client_host or "").strip()
    if token:
        if (header_token or "").strip() == token:
            return None
        return "token_mismatch"
    if host in LOOPBACK_HOSTS:
        return None
    return "non_loopback_without_token"
