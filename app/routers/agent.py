"""Hermes Agent / 智能监控路由。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from app.container import ServiceContainer
from app.dependencies import get_container
from app.models import AgentRunRequest, MonitorConfigUpdate
from app.routers.jobs import accepted_response, conflict_response
from app.services.error_response import error_detail
from app.services.jobs import JobConflict

router = APIRouter(tags=["Agent"])


@router.get("/agent/status")
async def get_agent_status(container: ServiceContainer = Depends(get_container)):
    return await container.hermes_runtime.get_status_async()


@router.post("/agent/run")
async def run_agent_task(
    body: AgentRunRequest,
    sync: bool = False,
    container: ServiceContainer = Depends(get_container),
):
    """提交 Agent 任务；默认异步返回 202，`?sync=true` 保留旧同步响应。"""
    runtime = container.hermes_runtime

    if sync:
        result = await runtime.run_task(body.task_type, trigger="manual", params=body.params)
        if not result.get("success"):
            return error_detail(
                "Agent 任务执行失败，请稍后重试",
                RuntimeError(str(result.get("message", "任务执行失败"))),
                status_code=503,
                context="agent task failed (sync)",
            )
        return result

    async def runner(ctx) -> dict:
        ctx.progress(10.0, "Agent 任务已启动")
        result = await runtime.run_task(body.task_type, trigger="manual", params=body.params)
        if not result.get("success"):
            raise RuntimeError(result.get("message", "任务执行失败"))
        ctx.progress(90.0, "整理任务结果")
        return result

    try:
        job = await container.job_manager.submit(
            kind="agent_run",
            runner=runner,
            dedupe_key=body.task_type,
            params={"task_type": body.task_type, "params": body.params},
            timeout_seconds=180,
            request_id="",
        )
    except JobConflict as exc:
        return conflict_response(exc)
    return accepted_response(job)


@router.get("/agent/tasks")
async def list_agent_tasks(
    limit: int = Query(default=10, ge=1, le=100),
    container: ServiceContainer = Depends(get_container),
):
    tasks = container.hermes_memory.get_recent_tasks(limit)
    return {"items": tasks}


# ── 智能监控 ──


@router.get("/agent/monitor/config")
async def get_monitor_config(container: ServiceContainer = Depends(get_container)):
    return container.hermes_memory.get_monitor_config()


@router.post("/agent/monitor/config")
async def save_monitor_config(
    body: MonitorConfigUpdate,
    container: ServiceContainer = Depends(get_container),
):
    memory = container.hermes_memory
    provided = body.model_fields_set
    system_prompt = body.system_prompt
    if "system_prompt" in provided and system_prompt is None:
        system_prompt = memory._DEFAULT_MONITOR_PROMPT  # noqa: SLF001 — 与旧接口语义一致
    kwargs: dict = {}
    if "system_prompt" in provided:
        kwargs["system_prompt"] = system_prompt
    if body.interval_minutes is not None:
        kwargs["interval_minutes"] = body.interval_minutes
    if body.enabled is not None:
        kwargs["enabled"] = body.enabled
    return memory.save_monitor_config(**kwargs)


@router.get("/agent/monitor/messages")
async def list_monitor_messages(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    today_only: bool = True,
    container: ServiceContainer = Depends(get_container),
):
    items, total = container.hermes_memory.list_monitor_messages(
        limit=limit, offset=offset, today_only=today_only
    )
    return {"items": items, "total": total}


@router.post("/agent/monitor/trigger")
async def trigger_monitor(container: ServiceContainer = Depends(get_container)):
    result = await container.hermes_runtime.run_monitor_tick(trigger="manual")
    if result.get("success"):
        await container.hub.broadcast(
            "monitor_update",
            {
                "message_id": result["message_id"],
                "content": result["content"],
                "created_at": result.get("created_at", ""),
                "trigger": "manual",
            },
        )
    return result


@router.post("/agent/monitor/stop")
async def stop_monitor(container: ServiceContainer = Depends(get_container)):
    container.hermes_memory.save_monitor_config(enabled=False)
    return {"success": True, "message": "智能监控已停止"}
