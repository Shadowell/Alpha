from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.models import MovePoolRequest, RecomputeRequest
from app.services.data_provider import AkshareDataProvider
from app.services.funnel_service import FunnelService
from app.services.kline_cache_service import KlineCacheService
from app.services.realtime import RealtimeHub

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

provider = AkshareDataProvider()
kline_cache_service = KlineCacheService(provider=provider)
service = FunnelService(provider=provider, kline_cache_service=kline_cache_service)
hub = RealtimeHub()

app = FastAPI(title="漏斗选股系统", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


async def _broadcast_snapshot() -> None:
    funnel = await service.get_funnel()
    hot = await service.get_hot_concepts()
    hot_stocks = await service.get_hot_stocks()
    await hub.broadcast(
        "snapshot",
        {
            "funnel": funnel.model_dump(),
            "hot_concepts": hot.model_dump(),
            "hot_stocks": hot_stocks.model_dump(),
        },
    )


async def _ticker_loop() -> None:
    # Avoid blocking startup with heavy first refresh.
    await asyncio.sleep(5)
    while True:
        try:
            changed = await service.tick()
            if changed:
                await _broadcast_snapshot()
        except Exception as exc:
            print(f"[ticker] error: {exc}")
        await asyncio.sleep(60)


async def _kline_cache_loop() -> None:
    await asyncio.sleep(10)
    while True:
        try:
            changed = await kline_cache_service.run_if_due()
            if changed:
                print("[kline-cache] daily sync completed")
        except Exception as exc:
            print(f"[kline-cache] error: {exc}")
        await asyncio.sleep(600)


@app.on_event("startup")
async def on_startup() -> None:
    app.state.ticker_task = asyncio.create_task(_ticker_loop())
    app.state.kline_cache_task = asyncio.create_task(_kline_cache_loop())


@app.on_event("shutdown")
async def on_shutdown() -> None:
    for key in ["ticker_task", "kline_cache_task"]:
        task = getattr(app.state, key, None)
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/funnel")
async def get_funnel(trade_date: str | None = None):
    payload = await service.get_funnel(trade_date)
    return payload


@app.get("/api/market/hot-concepts")
async def get_hot_concepts(trade_date: str | None = None):
    payload = await service.get_hot_concepts(trade_date)
    return payload


@app.get("/api/market/hot-stocks")
async def get_hot_stocks(trade_date: str | None = None):
    payload = await service.get_hot_stocks(trade_date)
    return payload


@app.get("/api/stock/{symbol}/detail")
async def get_stock_detail(symbol: str, trade_date: str | None = None, kline_days: int = 30):
    try:
        payload = await service.get_stock_detail(symbol, trade_date, kline_days)
    except KeyError:
        raise HTTPException(status_code=404, detail="symbol not found in funnel")
    return payload


@app.post("/api/pool/move")
async def move_pool(req: MovePoolRequest):
    resp = await service.move_pool(req.symbol, req.target_pool, req.note)
    await _broadcast_snapshot()
    return resp


@app.post("/api/score/recompute")
async def recompute(req: RecomputeRequest):
    await service.recompute(req.symbol)
    await _broadcast_snapshot()
    return {"success": True}


@app.post("/api/jobs/eod-screen")
async def run_eod_screen(trade_date: str | None = None):
    try:
        count = await service.run_eod_screen(trade_date)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"盘后筛选执行失败: {exc}")
    await _broadcast_snapshot()
    return {"success": True, "candidate_count": count}


@app.post("/api/jobs/kline-cache/sync")
async def run_kline_cache_sync(trade_date: str | None = None, force: bool = False):
    payload = await kline_cache_service.sync_trade_date(trade_date=trade_date, force=force)
    if not payload.get("success"):
        raise HTTPException(status_code=503, detail=payload.get("message", "同步失败"))
    return payload


@app.get("/api/jobs/kline-cache/status")
async def get_kline_cache_status():
    return kline_cache_service.get_sync_state()


@app.get("/api/kline/{symbol}")
async def get_cached_kline(symbol: str, days: int = 30):
    items = kline_cache_service.get_kline(symbol=symbol, days=days)
    return {
        "symbol": symbol,
        "days": max(1, min(days, 365)),
        "count": len(items),
        "items": items,
    }


@app.websocket("/ws/realtime")
async def realtime_socket(websocket: WebSocket):
    await hub.connect(websocket)
    try:
        funnel = await service.get_funnel()
        hot = await service.get_hot_concepts()
        hot_stocks = await service.get_hot_stocks()
        await websocket.send_json(
            {
                "event": "snapshot",
                "data": {
                    "funnel": funnel.model_dump(),
                    "hot_concepts": hot.model_dump(),
                    "hot_stocks": hot_stocks.model_dump(),
                },
            }
        )
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        hub.disconnect(websocket)
    except Exception:
        hub.disconnect(websocket)
