"""统一任务查询路由 + 任务响应辅助。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from app.container import ServiceContainer
from app.dependencies import get_container
from app.models import JobListOut, JobOut
from app.services.jobs import JobConflict

router = APIRouter(tags=["后台任务"])


def poll_url(job_id: str) -> str:
    return f"/api/jobs/{job_id}"


def accepted_payload(job: dict[str, Any]) -> dict[str, Any]:
    """202 响应体：{job_id, kind, status, poll_url}。"""
    return {
        "job_id": job["id"],
        "kind": job["kind"],
        "status": job["status"],
        "poll_url": poll_url(job["id"]),
    }


def accepted_response(job: dict[str, Any]) -> JSONResponse:
    return JSONResponse(status_code=202, content=accepted_payload(job))


def conflict_response(exc: JobConflict) -> JSONResponse:
    """409 响应体显式携带现有 job_id，便于前端直接接管轮询。"""
    job = exc.job
    return JSONResponse(
        status_code=409,
        content={
            "detail": "同类任务正在运行",
            "job_id": job["id"],
            "kind": job["kind"],
            "status": job["status"],
            "poll_url": poll_url(job["id"]),
        },
    )


@router.get("/jobs", response_model=JobListOut)
async def list_jobs(
    kind: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=50, ge=1, le=200),
    container: ServiceContainer = Depends(get_container),
):
    items = container.job_manager.list_jobs(kind=kind, limit=limit)
    return {"items": items, "total": len(items)}


@router.get("/jobs/{job_id}", response_model=JobOut)
async def get_job(job_id: str, container: ServiceContainer = Depends(get_container)):
    job = container.job_manager.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job
