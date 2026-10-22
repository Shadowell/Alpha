"""Alpha FastAPI 入口 — 只保留装配、中间件、lifespan 与静态页面。

业务路由按领域拆分到 app/routers/；服务装配见 app/container.py。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.container import build_container, resolve_compat_attr
from app.logging_config import configure_logging, reset_request_id, set_request_id
from app.routers import (
    agent,
    jobs,
    kline,
    limit_board,
    market,
    notice,
    paper,
    predict,
    realtime_ws,
    strategy,
    system,
)
from app.routers.first_limit_alpha import router as first_limit_alpha_router
from app.routers.paper import (  # noqa: F401 — 兼容旧脚本 re-export
    PAPER_DB_FALLBACK_TIMEOUT_SECONDS,
    PAPER_SNAPSHOT_TIMEOUT_SECONDS,
    _is_a_market_open,
)
from app.services.access_guard import write_access_decision
from app.services.hermes_runtime import hermes_scheduler_loop, monitor_loop
from app.services.deployment_guard import enforce_deployment_security
from app.services.observability import METRICS

log = logging.getLogger("api")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    configure_logging()
    enforce_deployment_security()
    container = getattr(app.state, "container", None) or build_container()
    app.state.container = container
    await container.startup()
    app.state.backfill_task = asyncio.create_task(
        container.backfill_names(), name="backfill-names"
    )
    app.state.ticker_task = asyncio.create_task(container.ticker_loop(), name="ticker")
    app.state.kline_cache_task = asyncio.create_task(
        kline.kline_cache_loop(container.kline_cache_service), name="kline-cache"
    )
    app.state.minute_sync_task = asyncio.create_task(
        kline.minute_sync_loop(container.kline_cache_service, container.funnel_service),
        name="minute-sync",
    )
    app.state.hermes_task = asyncio.create_task(
        hermes_scheduler_loop(container.hermes_runtime), name="hermes-scheduler"
    )
    app.state.monitor_task = asyncio.create_task(
        monitor_loop(container.hermes_runtime, container.hub), name="hermes-monitor"
    )
    app.state.predict_funnel_task = asyncio.create_task(
        container.predict_funnel_scheduler_loop(), name="predict-funnel-scheduler"
    )
    app.state.hot_stock_ai_task = asyncio.create_task(
        container.hot_stock_ai_scheduler_loop(), name="hot-stock-ai-scheduler"
    )
    log.info("Alpha started (data_dir=%s)", container.data_dir)
    try:
        yield
    finally:
        await container.shutdown(app.state)
        log.info("Alpha stopped")


app = FastAPI(title="Alpha", version="1.0.0", lifespan=lifespan)


# ── 请求上下文（request_id / 时延 / 指标）─────────────────────────
@app.middleware("http")
async def request_context(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
    token = set_request_id(request_id)
    started = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        response.headers["X-Request-ID"] = request_id
        return response
    except Exception:
        log.exception("unhandled request error")
        raise
    finally:
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        METRICS.record_request(
            method=request.method,
            path=request.url.path,
            status_code=status_code,
            duration_ms=duration_ms,
        )
        log.info(
            "request",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status": status_code,
                "duration_ms": duration_ms,
            },
        )
        reset_request_id(token)


# ── 写操作防护（S2）────────────────────────────────────────────
@app.middleware("http")
async def write_access_guard(request: Request, call_next):
    decision = write_access_decision(
        method=request.method,
        client_host=request.client.host if request.client else None,
        header_token=request.headers.get("X-Alpha-Token"),
    )
    if decision == "token_mismatch":
        return JSONResponse(
            status_code=403,
            content={"detail": "写操作被拒绝：X-Alpha-Token 缺失或不匹配"},
        )
    if decision == "non_loopback_without_token":
        return JSONResponse(
            status_code=403,
            content={
                "detail": "写操作仅允许本机访问；如需远程写入请配置 ALPHA_WRITE_TOKEN 并携带 X-Alpha-Token 头"
            },
        )
    return await call_next(request)


def _cors_origins() -> list[str]:
    raw = os.getenv("ALPHA_CORS_ORIGINS", "").strip()
    if raw:
        return [origin.strip() for origin in raw.split(",") if origin.strip()]
    return [
        "http://localhost:18888",
        "http://127.0.0.1:18888",
        "http://localhost:18890",
        "http://127.0.0.1:18890",
    ]


app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── 路由装配 ──────────────────────────────────────────────────
for domain_router in (
    market.router,
    predict.router,
    strategy.router,
    notice.router,
    agent.router,
    paper.router,
    system.router,
    jobs.router,
    kline.router,
    limit_board.router,
    first_limit_alpha_router,
):
    app.include_router(domain_router, prefix="/api")
app.include_router(realtime_ws.router)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/notice")
async def notice_index() -> RedirectResponse:
    return RedirectResponse(url="/?tab=notice")


def __getattr__(name: str) -> Any:
    """兼容旧脚本 import app.main.paper_trading 等（容器启动后可用）。"""
    return resolve_compat_attr(app, name)


def _spawn_tracked(coro) -> asyncio.Task:
    """兼容旧调用：统一走 container 的受控后台任务注册表。"""
    return app.state.container.background_tasks.spawn(coro)
