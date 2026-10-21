"""FastAPI 依赖 — 从 app.state 读取 lifespan 构建的服务容器。

路由不持有全局服务实例：测试可构造最小 app 并直接设置
`app.state.container`，实现不启动完整应用的路由级单测。
"""
from __future__ import annotations

from fastapi import Request

from app.container import ServiceContainer


def get_container(request: Request) -> ServiceContainer:
    container = getattr(request.app.state, "container", None)
    if container is None:
        raise RuntimeError("service container is not initialized (lifespan not started)")
    return container
