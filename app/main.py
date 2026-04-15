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
from app.services.hermes_memory import HermesMemory
from app.services.hermes_runtime import HermesRuntime, hermes_scheduler_loop, monitor_loop
from app.services.hermes_memory_bridge import record_feedback_to_hermes_memory
from app.services.kronos_predict_service import KronosPredictService
from app.services.paper_trading import PaperTradingService

from app.routers.kline import init_kline_router, kline_cache_loop

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

from app.services.kline_store import KlineSQLiteStore as _KlineSQLiteStore

_kline_store = _KlineSQLiteStore()
provider = AkshareDataProvider(kline_store=_kline_store)
kline_cache_service = KlineCacheService(provider=provider, store=_kline_store)
service = FunnelService(provider=provider, kline_cache_service=kline_cache_service)
notice_service = NoticeService(state_store=service.state_store, kline_cache_service=kline_cache_service, provider=provider)
hub = RealtimeHub()

kronos_service = KronosPredictService(
    kline_store=kline_cache_service.store,
    provider=provider,
)

paper_trading = PaperTradingService()
hermes_memory = HermesMemory()
hermes_runtime = HermesRuntime(
    memory=hermes_memory,
    funnel_service=service,
    notice_service=notice_service,
    kline_cache_service=kline_cache_service,
)


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


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    fixed = await service.backfill_names()
    if fixed:
        print(f"[startup] backfilled {fixed} stock names")
    app.state.ticker_task = asyncio.create_task(_ticker_loop())
    app.state.kline_cache_task = asyncio.create_task(kline_cache_loop(kline_cache_service))
    app.state.hermes_task = asyncio.create_task(hermes_scheduler_loop(hermes_runtime))
    app.state.monitor_task = asyncio.create_task(monitor_loop(hermes_runtime, hub))
    yield
    for key in ["ticker_task", "kline_cache_task", "hermes_task", "monitor_task"]:
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
app.include_router(init_kline_router(provider, kline_cache_service), prefix="/api")
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


@app.get("/api/stock/{symbol}/realtime")
async def get_stock_realtime(symbol: str):
    """盘中实时行情（当天 OHLCV），从全市场 snapshot 中过滤。"""
    clean = normalize_symbol(symbol)
    df = await provider.get_realtime_snapshot(cache_ttl_seconds=60)
    if df.empty:
        return {"symbol": clean, "found": False}
    row = df[df["代码"] == clean]
    if row.empty:
        return {"symbol": clean, "found": False}
    r = row.iloc[0]
    from datetime import date as _date
    return {
        "symbol": clean,
        "found": True,
        "date": _date.today().isoformat(),
        "open": float(r.get("今开", 0)),
        "high": float(r.get("最高", 0)),
        "low": float(r.get("最低", 0)),
        "close": float(r.get("最新价", 0)),
        "volume": float(r.get("成交量", 0)),
        "amount": float(r.get("成交额", 0)),
        "prev_close": float(r.get("昨收", 0)),
        "change_pct": float(r.get("涨跌幅", 0)),
    }


@app.get("/api/predict/{symbol}/kronos")
async def predict_kronos(symbol: str, lookback: int = 30, horizon: int = 3):
    clean = normalize_symbol(symbol)
    try:
        return await kronos_service.predict(clean, lookback, horizon)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"预测失败: {exc}")


@app.get("/api/strategy/profile")
async def get_strategy_profile():
    return await service.get_strategy_profile()


@app.get("/api/notice/funnel")
async def get_notice_funnel(trade_date: str | None = None):
    return await notice_service.get_notice_funnel(trade_date)


@app.get("/api/notice/keywords")
async def get_notice_keywords():
    from app.services.notice_service import BULLISH_RULES
    return {"keywords": [{"tag": rule[0], "weight": rule[1]} for rule in BULLISH_RULES]}


@app.post("/api/jobs/notice-screen")
async def run_notice_screen(notice_date: str | None = None, limit: int = 10, keywords: str | None = None):
    kw_list = [k.strip() for k in keywords.split(",") if k.strip()] if keywords else None
    try:
        payload = await notice_service.run_notice_screen(notice_date=notice_date, limit=limit, keywords=kw_list)
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


# ── Hermes Agent API ──


@app.get("/api/agent/status")
async def get_agent_status():
    return await hermes_runtime.get_status_async()


@app.post("/api/agent/run")
async def run_agent_task(body: dict):
    task_type = body.get("task_type", "daily_review")
    params = body.get("params", {})
    valid_types = {"daily_review", "notice_review", "full_diagnosis"}
    if task_type not in valid_types:
        raise HTTPException(status_code=400, detail=f"无效任务类型，可选: {valid_types}")
    result = await hermes_runtime.run_task(task_type, trigger="manual", params=params)
    if not result.get("success"):
        raise HTTPException(status_code=503, detail=result.get("message", "任务执行失败"))
    return result


@app.get("/api/agent/proposals")
async def list_agent_proposals(status: str | None = None, type: str | None = None, limit: int = 20, offset: int = 0):
    items, total = hermes_memory.list_proposals(status=status, proposal_type=type, limit=limit, offset=offset)
    return {"items": items, "total": total}


@app.get("/api/agent/proposals/{proposal_id}")
async def get_agent_proposal(proposal_id: int):
    p = hermes_memory.get_proposal(proposal_id)
    if not p:
        raise HTTPException(status_code=404, detail="提案不存在")
    feedbacks = hermes_memory.get_feedback_for_proposal(proposal_id)
    p["feedbacks"] = feedbacks
    return p


@app.post("/api/agent/proposals/{proposal_id}/approve")
async def approve_agent_proposal(proposal_id: int, body: dict | None = None):
    body = body or {}
    note = body.get("note", "")
    proposal = hermes_memory.get_proposal(proposal_id)
    if not proposal:
        raise HTTPException(status_code=404, detail="提案不存在")
    if proposal["status"] != "pending":
        raise HTTPException(status_code=400, detail=f"提案状态为 {proposal['status']}，无法审批")

    hermes_memory.update_proposal_status(proposal_id, "approved", approved_by="user")
    hermes_memory.record_feedback(proposal_id, "approve", note)

    record_feedback_to_hermes_memory(
        proposal_title=proposal["title"],
        proposal_type=proposal["type"],
        action="approve",
        note=note,
        diff_payload=proposal.get("diff_payload"),
    )

    try:
        funnel = await service.get_funnel()
        baseline = {
            "candidate_count": funnel.stats.get("candidate", 0) if hasattr(funnel, "stats") else 0,
            "focus_count": funnel.stats.get("focus", 0) if hasattr(funnel, "stats") else 0,
            "buy_count": funnel.stats.get("buy", 0) if hasattr(funnel, "stats") else 0,
            "approved_at": proposal.get("approved_at", ""),
        }
        hermes_memory.create_outcome_tracking(proposal_id, baseline, check_after_days=3)
    except Exception as e:
        print(f"[hermes] outcome tracking setup failed: {e}")

    return {"success": True, "message": "提案已批准并应用"}


@app.post("/api/agent/proposals/{proposal_id}/reject")
async def reject_agent_proposal(proposal_id: int, body: dict | None = None):
    body = body or {}
    note = body.get("note", "")
    proposal = hermes_memory.get_proposal(proposal_id)
    if not proposal:
        raise HTTPException(status_code=404, detail="提案不存在")
    if proposal["status"] != "pending":
        raise HTTPException(status_code=400, detail=f"提案状态为 {proposal['status']}，无法操作")

    hermes_memory.update_proposal_status(proposal_id, "rejected")
    hermes_memory.record_feedback(proposal_id, "reject", note)

    record_feedback_to_hermes_memory(
        proposal_title=proposal["title"],
        proposal_type=proposal["type"],
        action="reject",
        note=note,
        diff_payload=proposal.get("diff_payload"),
    )

    return {"success": True, "message": "提案已驳回"}


@app.post("/api/agent/proposals/create")
async def create_agent_proposal(body: dict):
    """直接创建提案（供 MCP 工具调用，跳过完整诊断流程）。"""
    required = {"type", "title", "reasoning"}
    if not required.issubset(body.keys()):
        raise HTTPException(status_code=400, detail=f"缺少必填字段: {required - body.keys()}")

    task_id = hermes_memory.create_task(
        task_type=f"mcp_{body['type']}",
        trigger="mcp",
        input_summary=body,
    )
    hermes_memory.finish_task(task_id, status="success", output_summary={"source": "mcp"}, elapsed_ms=0)

    proposal_id = hermes_memory.create_proposal(
        task_id,
        proposal_type=body["type"],
        title=body["title"],
        risk_level=body.get("risk_level", "medium"),
        reasoning=body["reasoning"],
        diff_payload=body.get("diff"),
        expected_impact=body.get("expected_impact", ""),
        confidence=float(body.get("confidence", 0.5)),
        evidence=body.get("evidence", []),
    )
    return {"success": True, "proposal_id": proposal_id, "task_id": task_id}


@app.get("/api/agent/tasks")
async def list_agent_tasks(limit: int = 10):
    tasks = hermes_memory.get_recent_tasks(limit)
    return {"items": tasks}


# ── 盘中监控 API ──


@app.get("/api/agent/monitor/config")
async def get_monitor_config():
    return hermes_memory.get_monitor_config()


@app.post("/api/agent/monitor/config")
async def save_monitor_config(body: dict):
    prompt = body.get("system_prompt")
    if "system_prompt" in body and prompt is None:
        prompt = hermes_memory._DEFAULT_MONITOR_PROMPT
    return hermes_memory.save_monitor_config(
        system_prompt=prompt,
        interval_minutes=body.get("interval_minutes"),
        enabled=body.get("enabled"),
    )


@app.get("/api/agent/monitor/messages")
async def list_monitor_messages(limit: int = 50, offset: int = 0, today_only: bool = True):
    items, total = hermes_memory.list_monitor_messages(limit=limit, offset=offset, today_only=today_only)
    return {"items": items, "total": total}


@app.post("/api/agent/monitor/trigger")
async def trigger_monitor():
    result = await hermes_runtime.run_monitor_tick(trigger="manual")
    if result.get("success"):
        await hub.broadcast("monitor_update", {
            "message_id": result["message_id"],
            "content": result["content"],
            "created_at": result.get("created_at", ""),
            "trigger": "manual",
        })
    return result


@app.post("/api/agent/monitor/stop")
async def stop_monitor():
    hermes_memory.save_monitor_config(enabled=False)
    return {"success": True, "message": "盘中监控已停止"}


# ── 模拟盘 ──────────────────────────────────────────


@app.post("/api/paper/buy")
async def paper_buy(req: dict):
    if not _is_a_market_open():
        raise HTTPException(400, "当前非交易时段，无法模拟买入")
    symbol = normalize_symbol(req.get("symbol", ""))
    name = req.get("name", symbol)
    qty = int(req.get("qty", 100))
    if not symbol:
        raise HTTPException(400, "缺少 symbol")
    if qty <= 0:
        qty = 100
    price = await _get_realtime_price(symbol)
    if price <= 0:
        raise HTTPException(400, f"无法获取 {symbol} 实时价格")
    pos = paper_trading.open_position(symbol, name, price, qty, note=req.get("note", ""))
    return {"success": True, "position": pos, "realtime_price": price}


@app.post("/api/paper/sell")
async def paper_sell(req: dict):
    if not _is_a_market_open():
        raise HTTPException(400, "当前非交易时段，无法模拟卖出")
    position_id = req.get("position_id", "")
    if not position_id:
        raise HTTPException(400, "缺少 position_id")
    opens = paper_trading.get_open_positions()
    target = next((p for p in opens if p["id"] == position_id), None)
    if not target:
        raise HTTPException(404, "持仓不存在或已平仓")
    price = await _get_realtime_price(target["symbol"])
    if price <= 0:
        raise HTTPException(400, f"无法获取 {target['symbol']} 实时价格")
    pos = paper_trading.close_position(position_id, price, note=req.get("note", ""))
    if not pos:
        raise HTTPException(404, "持仓不存在或已平仓")
    return {"success": True, "position": pos, "realtime_price": price}


def _is_a_market_open() -> bool:
    from app.services.time_utils import now_cn
    now = now_cn()
    if now.weekday() >= 5:
        return False
    t = now.hour * 60 + now.minute
    return (570 <= t < 690) or (780 <= t < 900)


async def _get_realtime_price(symbol: str) -> float:
    """从实时快照获取个股最新价，cache TTL 最短以保证时效。"""
    df = await provider.get_realtime_snapshot(cache_ttl_seconds=5)
    if df.empty:
        return 0.0
    match = df[df["代码"] == symbol]
    if match.empty:
        return 0.0
    val = match.iloc[0].get("最新价", 0)
    return float(val) if val else 0.0


@app.get("/api/paper/positions")
async def paper_positions():
    opens = paper_trading.get_open_positions()
    price_map = {}
    if opens:
        ttl = 8 if _is_a_market_open() else 60
        df = await provider.get_realtime_snapshot(cache_ttl_seconds=ttl)
        if not df.empty:
            for _, r in df.iterrows():
                price_map[r["代码"]] = float(r["最新价"]) if r["最新价"] else 0
            paper_trading.update_prices(price_map)
            opens = paper_trading.get_open_positions()
    return {"positions": opens}


@app.get("/api/paper/history")
async def paper_history(limit: int = 50):
    return {"positions": paper_trading.get_closed_positions(limit)}


@app.get("/api/paper/summary")
async def paper_summary():
    opens = paper_trading.get_open_positions()
    if opens:
        ttl = 8 if _is_a_market_open() else 60
        df = await provider.get_realtime_snapshot(cache_ttl_seconds=ttl)
        if not df.empty:
            price_map = {}
            for _, r in df.iterrows():
                price_map[r["代码"]] = float(r["最新价"]) if r["最新价"] else 0
            paper_trading.update_prices(price_map)
    return paper_trading.get_summary()


@app.get("/api/paper/trades")
async def paper_trades(limit: int = 100):
    return {"trades": paper_trading.get_trades(limit)}


@app.get("/api/paper/settings")
async def paper_settings_get():
    return paper_trading.get_settings()


@app.post("/api/paper/settings")
async def paper_settings_post(req: dict):
    paper_trading.update_settings(**req)
    return paper_trading.get_settings()


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
    except Exception as exc:
        print(f"[ws] error: {exc}")
        hub.disconnect(websocket)
