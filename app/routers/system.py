"""系统路由 — /api/health 与 /api/metrics。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.container import ServiceContainer
from app.dependencies import get_container
from app.services.observability import METRICS, collect_health

router = APIRouter(tags=["系统"])

_HEALTH_TASK_KEYS = (
    "ticker_task",
    "kline_cache_task",
    "hermes_task",
    "monitor_task",
    "predict_funnel_task",
    "hot_stock_ai_task",
)


def _task_states(app_state) -> dict[str, bool | None]:
    """True=已退出，False=运行中，None=缺失（供健康检查降级）。"""
    states: dict[str, bool | None] = {}
    for key in _HEALTH_TASK_KEYS:
        task = getattr(app_state, key, None)
        states[key] = None if task is None else bool(task.done())
    return states


@router.get("/health")
async def health(request: Request, container: ServiceContainer = Depends(get_container)):
    payload, http_status = collect_health(
        db_paths=container.db_paths,
        kline_db_path=container.kline_db_path,
        kronos_status=container.kronos_service.status(),
        job_stats=container.job_manager.stats,
        task_states=_task_states(request.app.state),
    )
    return JSONResponse(status_code=http_status, content=payload)


@router.get("/metrics")
async def metrics(container: ServiceContainer = Depends(get_container)):
    try:
        jobs = container.job_manager.stats()
    except Exception as exc:  # noqa: BLE001 — 指标端点保持 200，暴露错误字段
        jobs = {"error": f"{type(exc).__name__}: {exc}"}
    return {"metrics": METRICS.snapshot(), "jobs": jobs}
