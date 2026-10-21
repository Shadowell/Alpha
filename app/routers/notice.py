"""公告漏斗路由。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from app.container import ServiceContainer
from app.dependencies import get_container
from app.models import MovePoolRequest
from app.services.error_response import error_detail

router = APIRouter(tags=["公告"])


@router.get("/notice/funnel")
async def get_notice_funnel(
    trade_date: str | None = None,
    container: ServiceContainer = Depends(get_container),
):
    return await container.notice_service.get_notice_funnel(trade_date)


@router.get("/notice/keywords")
async def get_notice_keywords():
    from app.services.notice_service import BULLISH_RULES

    return {"keywords": [{"tag": rule[0], "weight": rule[1]} for rule in BULLISH_RULES]}


@router.post("/jobs/notice-screen")
async def run_notice_screen(
    notice_date: str | None = None,
    limit: int = Query(default=10, ge=1, le=100),
    keywords: str | None = None,
    container: ServiceContainer = Depends(get_container),
):
    kw_list = [k.strip() for k in keywords.split(",") if k.strip()] if keywords else None
    try:
        return await container.notice_service.run_notice_screen(
            notice_date=notice_date, limit=limit, keywords=kw_list
        )
    except Exception as exc:  # noqa: BLE001 — 上游公告源失败映射 503
        return error_detail(
            "公告选股执行失败，请稍后重试",
            exc,
            status_code=503,
            context="notice screen failed",
        )


@router.post("/notice/pool/move")
async def move_notice_pool(
    req: MovePoolRequest,
    container: ServiceContainer = Depends(get_container),
):
    payload = await container.notice_service.move_pool(req.symbol, req.target_pool)
    if not payload.get("success"):
        raise HTTPException(status_code=400, detail=payload.get("message", "迁移失败"))
    return payload


@router.get("/notice/{symbol}/detail")
async def get_notice_detail(
    symbol: str,
    days: int = Query(default=30, ge=1, le=365),
    container: ServiceContainer = Depends(get_container),
):
    try:
        return await container.notice_service.get_notice_detail(symbol, days=days)
    except KeyError:
        raise HTTPException(status_code=404, detail="symbol not found in notice funnel")
