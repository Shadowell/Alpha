"""行情 / 漏斗 / 个股路由。"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query

from app.container import ServiceContainer
from app.dependencies import get_container
from app.models import MovePoolRequest, RecomputeRequest
from app.services.data_provider import normalize_symbol

router = APIRouter(tags=["行情"])


@router.get("/funnel")
async def get_funnel(
    trade_date: str | None = None,
    container: ServiceContainer = Depends(get_container),
):
    return await container.funnel_service.get_funnel(trade_date)


@router.get("/market/hot-concepts")
async def get_hot_concepts(
    trade_date: str | None = None,
    container: ServiceContainer = Depends(get_container),
):
    return await container.funnel_service.get_hot_concepts(trade_date)


@router.get("/market/hot-stocks")
async def get_hot_stocks(
    trade_date: str | None = None,
    container: ServiceContainer = Depends(get_container),
):
    return await container.funnel_service.get_hot_stocks(trade_date)


@router.get("/stock/{symbol}/detail")
async def get_stock_detail(
    symbol: str,
    trade_date: str | None = None,
    kline_days: int = Query(default=30, ge=1, le=365),
    container: ServiceContainer = Depends(get_container),
):
    try:
        return await container.funnel_service.get_stock_detail(symbol, trade_date, kline_days)
    except KeyError:
        raise HTTPException(status_code=404, detail="symbol not found in funnel")


@router.post("/pool/move")
async def move_pool(
    req: MovePoolRequest,
    container: ServiceContainer = Depends(get_container),
):
    resp = await container.funnel_service.move_pool(req.symbol, req.target_pool, req.note)
    if not resp.success:
        # Issue #56：与 /api/notice/pool/move 一致的 4xx 语义（非法层级/池满/不存在）。
        raise HTTPException(status_code=400, detail=resp.message)
    await container.broadcast_snapshot()
    return resp


@router.post("/score/recompute")
async def recompute(
    req: RecomputeRequest,
    container: ServiceContainer = Depends(get_container),
):
    await container.funnel_service.recompute(req.symbol)
    await container.broadcast_snapshot()
    return {"success": True}


@router.get("/stock/{symbol}/realtime")
async def get_stock_realtime(
    symbol: str,
    container: ServiceContainer = Depends(get_container),
):
    """盘中实时行情（当天 OHLCV），从全市场 snapshot 中过滤。"""
    clean = normalize_symbol(symbol)
    df = await container.provider.get_realtime_snapshot(cache_ttl_seconds=60)
    if df.empty:
        return {"symbol": clean, "found": False}
    row = df[df["代码"] == clean]
    if row.empty:
        return {"symbol": clean, "found": False}
    r = row.iloc[0]
    return {
        "symbol": clean,
        "found": True,
        "date": date.today().isoformat(),
        "open": float(r.get("今开", 0)),
        "high": float(r.get("最高", 0)),
        "low": float(r.get("最低", 0)),
        "close": float(r.get("最新价", 0)),
        "volume": float(r.get("成交量", 0)),
        "amount": float(r.get("成交额", 0)),
        "prev_close": float(r.get("昨收", 0)),
        "change_pct": float(r.get("涨跌幅", 0)),
    }


@router.get("/strategy/profile")
async def get_strategy_profile(container: ServiceContainer = Depends(get_container)):
    return await container.funnel_service.get_strategy_profile()
