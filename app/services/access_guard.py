"""写操作访问控制 — 纯函数实现，便于独立单测。

规则：
  1) 配置了 ALPHA_WRITE_TOKEN → 所有写请求必须携带匹配的 X-Alpha-Token 头；
  2) 未配置 token → 仅允许本机回环来源（127.0.0.1 / ::1）发起写请求；
  3) ALPHA_TRUST_EDGE=1（Docker/K8s 等边界代理部署）：请求经端口发布 NAT 后
     容器内看到的来源是网桥网关而非真实客户端，回环判断失效。此时认为
     "谁能访问我"由宿主机端口绑定/边界层决定（compose 默认只绑 127.0.0.1），
     无 token 时放行。⚠️ 若同时把端口暴露到局域网，必须改用 token 模式。
读请求不受影响。"testclient" 为 Starlette 测试客户端来源标记，视为回环。
"""
from __future__ import annotations

import os

WRITE_METHODS = {"POST", "PUT", "DELETE", "PATCH"}
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


def _env_flag(name: str, default: str = "") -> str:
    return (os.getenv(name, "") or default).strip().lower()


def write_access_decision(
    *,
    method: str,
    client_host: str | None,
    header_token: str | None,
    configured_token: str | None = None,
    trust_edge: bool | None = None,
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
    if trust_edge is None:
        trust_edge = _env_flag("ALPHA_TRUST_EDGE") in {"1", "true", "yes", "on"}
    if trust_edge:
        return None
    if host in LOOPBACK_HOSTS:
        return None
    return "non_loopback_without_token"
