"""K 线同步模块 — 缓存同步、定时任务。

路由前缀: /api  (由 main.py include 时指定)
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException

from app.services.data_provider import AkshareDataProvider, normalize_symbol
from app.services.kline_cache_service import KlineCacheService

router = APIRouter(tags=["K线同步"])

# 由 init_kline_router() 注入
_provider: AkshareDataProvider | None = None
_kline_cache_service: KlineCacheService | None = None


def init_kline_router(
    provider: AkshareDataProvider,
    kline_cache_service: KlineCacheService,
) -> APIRouter:
    """注入依赖并返回 router，供 main.py 调用。"""
    global _provider, _kline_cache_service
    _provider = provider
    _kline_cache_service = kline_cache_service
    return router


# ── 同步任务路由 ──────────────────────────────────────────


@router.post("/jobs/kline-cache/sync")
async def run_kline_cache_sync(
    trade_date: str | None = None,
    force: bool = False,
    trigger_mode: str = "manual",
    window_days: int | None = None,
):
    payload = await _kline_cache_service.sync_trade_date(
        trade_date=trade_date, force=force, trigger_mode=trigger_mode, window_days=window_days,
    )
    if not payload.get("success"):
        raise HTTPException(status_code=503, detail=payload.get("message", "同步失败"))
    return payload


@router.post("/jobs/kline-cache/incremental-sync")
async def run_kline_incremental_sync(
    trade_date: str | None = None,
    trigger_mode: str = "manual",
):
    payload = await _kline_cache_service.incremental_sync(
        trade_date=trade_date, trigger_mode=trigger_mode,
    )
    if not payload.get("success"):
        raise HTTPException(status_code=503, detail=payload.get("message", "增量同步失败"))
    return payload


@router.get("/jobs/kline-cache/status")
async def get_kline_cache_status():
    return _kline_cache_service.get_sync_state()


@router.get("/jobs/kline-cache/progress")
async def get_kline_cache_progress():
    return _kline_cache_service.get_sync_progress()


@router.get("/jobs/kline-cache/logs")
async def get_kline_cache_logs(page: int = 1, page_size: int = 20):
    return _kline_cache_service.list_sync_logs(page=page, page_size=page_size)


@router.get("/jobs/kline-cache/logs/{task_id}")
async def get_kline_cache_log_detail(task_id: str):
    payload = _kline_cache_service.get_sync_log_detail(task_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="task not found")
    return payload


@router.get("/jobs/kline-cache/stats")
async def get_kline_cache_stats():
    return _kline_cache_service.get_stats()


@router.post("/jobs/kline-cache/check")
async def run_kline_data_check():
    report = await _kline_cache_service.check_data_integrity(days=30)
    return report


@router.get("/jobs/kline-cache/report")
async def get_kline_check_report():
    report = _kline_cache_service.get_latest_check_report()
    if report is None:
        return {"status": "none", "message": "暂无检查报告"}
    return report


# ── K 线查询路由 ──────────────────────────────────────────


@router.get("/kline/{symbol}")
async def get_cached_kline(symbol: str, days: int = 30):
    clean_symbol = normalize_symbol(symbol)
    items = _kline_cache_service.get_kline(symbol=clean_symbol, days=days)
    return {
        "symbol": clean_symbol,
        "days": max(1, min(days, 365)),
        "count": len(items),
        "items": items,
    }


# ── 后台定时同步任务 ──────────────────────────────────────


async def kline_cache_loop(kline_cache_service: KlineCacheService) -> None:
    """后台循环：每 10 分钟检测是否需要自动同步 K 线，同步后自动检查数据完整性。"""
    await asyncio.sleep(10)
    while True:
        try:
            result = await kline_cache_service.run_if_due()
            if result is not None:
                print(f"[kline-cache] daily sync completed: {result.get('message')}")

                report = None
                try:
                    report = await kline_cache_service.check_data_integrity(days=30)
                    print(f"[kline-cache] integrity check: {report.get('status')} coverage={report.get('coverage_pct')}%")
                except Exception as chk_exc:
                    print(f"[kline-cache] integrity check failed: {chk_exc}")

                print("[kline-cache] sync completion notification skipped")
        except Exception as exc:
            print(f"[kline-cache] error: {exc}")
        await asyncio.sleep(600)
