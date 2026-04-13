from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.models import MovePoolRequest, RecomputeRequest
from app.services.data_provider import AkshareDataProvider, normalize_symbol
from app.services.funnel_service import FunnelService
from app.services.kline_cache_service import KlineCacheService
from app.services.notice_service import NoticeService
from app.services.realtime import RealtimeHub
from app.services.strategy_engine import get_last_n_trade_window
from app.services.time_utils import now_cn

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

provider = AkshareDataProvider()
kline_cache_service = KlineCacheService(provider=provider)
service = FunnelService(provider=provider, kline_cache_service=kline_cache_service)
notice_service = NoticeService(state_store=service.state_store, kline_cache_service=kline_cache_service)
hub = RealtimeHub()


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


async def _get_latest_trade_date() -> str | None:
    """获取最近（含今天）的交易日，用于确定实时数据对应哪一天。"""
    try:
        import pandas as pd
        df = await provider.get_trade_days()
        if df.empty:
            return now_cn().date().isoformat()
        today = pd.Timestamp(now_cn().date())
        df["trade_date"] = pd.to_datetime(df["trade_date"])
        valid = df[df["trade_date"] <= today]["trade_date"]
        if valid.empty:
            return now_cn().date().isoformat()
        return valid.iloc[-1].strftime("%Y-%m-%d")
    except Exception:
        return now_cn().date().isoformat()


async def _build_today_bar(symbol: str) -> dict | None:
    """从个股实时行情构造当天的 K 线柱，非交易时段或无数据返回 None。"""
    import akshare as ak

    trade_date = await _get_latest_trade_date()
    if not trade_date:
        return None

    bar = None
    try:
        raw_symbol = symbol.replace("sh", "").replace("sz", "").replace("bj", "")
        df = await asyncio.to_thread(ak.stock_bid_ask_em, symbol=raw_symbol)
        if df is not None and not df.empty:
            lookup = dict(zip(df["item"], df["value"]))
            price = float(lookup.get("最新", 0))
            open_ = float(lookup.get("今开", 0))
            if price > 0 and open_ > 0:
                volume_hands = float(lookup.get("总手", 0))
                bar = {
                    "date": trade_date,
                    "open": open_,
                    "high": float(lookup.get("最高", price)),
                    "low": float(lookup.get("最低", price)),
                    "close": price,
                    "volume": volume_hands * 100,
                    "amount": float(lookup.get("金额", 0)),
                }
    except Exception:
        pass

    if bar is None:
        try:
            snapshot = await provider.get_realtime_snapshot(cache_ttl_seconds=300)
            if not snapshot.empty:
                row = snapshot[snapshot["代码"] == symbol]
                if not row.empty:
                    r = row.iloc[0]
                    price = float(r.get("最新价", 0))
                    open_ = float(r.get("今开", 0))
                    if price > 0 and open_ > 0:
                        bar = {
                            "date": trade_date,
                            "open": open_,
                            "high": float(r.get("最高", price)),
                            "low": float(r.get("最低", price)),
                            "close": price,
                            "volume": float(r.get("成交量", 0)),
                            "amount": float(r.get("成交额", 0)),
                        }
        except Exception:
            pass

    return bar


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    app.state.ticker_task = asyncio.create_task(_ticker_loop())
    app.state.kline_cache_task = asyncio.create_task(_kline_cache_loop())
    yield
    for key in ["ticker_task", "kline_cache_task"]:
        task = getattr(app.state, key, None)
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


app = FastAPI(title="Alpha", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/notice")
async def notice_index() -> RedirectResponse:
    return RedirectResponse(url="/?tab=notice")


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
        result = await service.run_eod_screen(trade_date)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"盘后筛选执行失败: {exc}")
    await _broadcast_snapshot()
    return {"success": True, **result}


@app.post("/api/jobs/kline-cache/sync")
async def run_kline_cache_sync(trade_date: str | None = None, force: bool = False, trigger_mode: str = "manual"):
    payload = await kline_cache_service.sync_trade_date(trade_date=trade_date, force=force, trigger_mode=trigger_mode)
    if not payload.get("success"):
        raise HTTPException(status_code=503, detail=payload.get("message", "同步失败"))
    return payload


@app.get("/api/jobs/kline-cache/status")
async def get_kline_cache_status():
    return kline_cache_service.get_sync_state()


@app.get("/api/jobs/kline-cache/progress")
async def get_kline_cache_progress():
    return kline_cache_service.get_sync_progress()


@app.get("/api/jobs/kline-cache/logs")
async def get_kline_cache_logs(page: int = 1, page_size: int = 20):
    return kline_cache_service.list_sync_logs(page=page, page_size=page_size)


@app.get("/api/jobs/kline-cache/logs/{task_id}")
async def get_kline_cache_log_detail(task_id: str):
    payload = kline_cache_service.get_sync_log_detail(task_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="task not found")
    return payload


@app.get("/api/kline/{symbol}")
async def get_cached_kline(symbol: str, days: int = 30):
    clean_symbol = normalize_symbol(symbol)
    items = kline_cache_service.get_kline(symbol=clean_symbol, days=days)
    if not items:
        trade_days = await provider.get_trade_days()
        try:
            start_date, end_date = get_last_n_trade_window(trade_days, now_cn().date().isoformat(), max(10, min(days, 180)))
            hist = await provider.get_hist(clean_symbol, start_date, end_date)
        except Exception:
            hist = None
        if hist is not None and not hist.empty:
            for _, row in hist.tail(max(1, min(days, 365))).iterrows():
                items.append(
                    {
                        "date": str(row.get("日期", "")),
                        "open": float(row.get("开盘", 0)),
                        "high": float(row.get("最高", 0)),
                        "low": float(row.get("最低", 0)),
                        "close": float(row.get("收盘", 0)),
                        "volume": float(row.get("成交量", 0)),
                        "amount": float(row.get("成交额", 0)),
                    }
                )
    today_bar = await _build_today_bar(clean_symbol)
    if today_bar:
        if items and items[-1]["date"] == today_bar["date"]:
            items[-1] = today_bar
        else:
            items.append(today_bar)
    return {
        "symbol": clean_symbol,
        "days": max(1, min(days, 365)),
        "count": len(items),
        "items": items,
    }


@app.get("/api/strategy/profile")
async def get_strategy_profile():
    return await service.get_strategy_profile()


@app.get("/api/rules/engine")
async def get_rule_engine():
    return await service.get_rule_engine()


@app.put("/api/rules/engine")
async def update_rule_engine(body: dict):
    return await service.update_rule_engine(body)


@app.get("/api/notice/funnel")
async def get_notice_funnel(trade_date: str | None = None):
    return await notice_service.get_notice_funnel(trade_date)


@app.post("/api/jobs/notice-screen")
async def run_notice_screen(notice_date: str | None = None, limit: int = 10):
    try:
        payload = await notice_service.run_notice_screen(notice_date=notice_date, limit=limit)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"公告选股执行失败: {exc}")
    return payload


@app.post("/api/notice/pool/move")
async def move_notice_pool(req: MovePoolRequest):
    payload = await notice_service.move_pool(req.symbol, req.target_pool)
    if not payload.get("success"):
        raise HTTPException(status_code=400, detail=payload.get("message", "迁移失败"))
    return payload


@app.get("/api/notice/{symbol}/detail")
async def get_notice_detail(symbol: str, days: int = 30):
    try:
        return await notice_service.get_notice_detail(symbol, days=days)
    except KeyError:
        raise HTTPException(status_code=404, detail="symbol not found in notice funnel")


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
