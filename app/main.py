from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

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
from app.services.kronos_predict_service import KronosPredictService
from app.services.paper_trading import PaperTradingService
from app.services.predict_funnel_service import PredictFunnelService
from app.services.hot_stock_ai_service import HotStockAIService
from app.services.tradingagents_adapter import TradingAgentsAdapter
from app.services.quiet_breakout_scanner import QuietBreakoutConfig, QuietBreakoutScanner
from app.services.first_limit_alpha_service import FirstLimitAlphaService
from app.services.custom_strategy import (
    BUILTIN_STRATEGIES,
    CustomStrategy,
    CustomStrategyScanner,
    StrategyRuleRef,
)
from app.services.strategy_rules import RULE_REGISTRY, list_rules as list_strategy_rules
from app.services.hermes_ai_extensions import (
    AutoTradeLoop,
    BacktestLab,
    NewsInsightExtractor,
    ResearchCardGenerator,
    WeeklyReportBuilder,
)
from app.services.risk_guardian import RiskGuardian, risk_guardian_loop

from app.routers.kline import init_kline_router, kline_cache_loop
from app.routers.first_limit_alpha import init_first_limit_alpha_router

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
PAPER_SNAPSHOT_TIMEOUT_SECONDS = 2.5
PAPER_DB_FALLBACK_TIMEOUT_SECONDS = 1.0

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
predict_funnel_service = PredictFunnelService(
    provider=provider,
    kronos_service=kronos_service,
    state_store=service.state_store,
)
tradingagents_adapter = TradingAgentsAdapter()
hot_stock_ai_service = HotStockAIService(
    provider=provider,
    kline_store=_kline_store,
    kronos_service=kronos_service,
    state_store=service.state_store,
    tradingagents_adapter=tradingagents_adapter,
)
first_limit_alpha_service = FirstLimitAlphaService(
    kline_store=_kline_store,
    provider=provider,
    state_store=service.state_store,
)


def _qb_name_lookup(symbol: str) -> str:
    try:
        if provider.symbol_name_cache is not None:
            _, name_map = provider.symbol_name_cache
            if name_map and symbol in name_map:
                return name_map[symbol]
    except Exception:
        pass
    try:
        names = _kline_store.load_symbol_names()
        return names.get(symbol, "")
    except Exception:
        return ""


quiet_breakout_scanner = QuietBreakoutScanner(
    kline_store=_kline_store,
    name_lookup=_qb_name_lookup,
)

custom_strategy_scanner = CustomStrategyScanner(
    kline_store=_kline_store,
    name_lookup=_qb_name_lookup,
)

try:
    _ins = service.state_store.ensure_builtin_custom_strategies([s.to_dict() for s in BUILTIN_STRATEGIES])
    if _ins:
        print(f"[startup] inserted {_ins} builtin custom strategies")
except Exception as _exc:
    print(f"[startup] ensure builtin strategies failed: {_exc}")


def _load_custom_strategy(strategy_id: str) -> CustomStrategy:
    data = service.state_store.get_custom_strategy(strategy_id)
    if not data:
        raise HTTPException(status_code=404, detail="strategy not found")
    return CustomStrategy.from_dict(data)

hermes_memory = HermesMemory()
hermes_runtime = HermesRuntime(
    memory=hermes_memory,
    funnel_service=service,
    notice_service=notice_service,
    kline_cache_service=kline_cache_service,
)

risk_guardian = RiskGuardian(
    paper_trading=paper_trading,
    get_realtime_price=lambda sym: _get_realtime_price(sym),
    is_market_open=lambda: _is_a_market_open(),
)

auto_trade_loop = AutoTradeLoop(
    paper_trading=paper_trading,
    funnel_service=service,
    hermes_memory=hermes_memory,
    get_realtime_price=lambda sym: _get_realtime_price(sym),
    is_market_open=lambda: _is_a_market_open(),
)

backtest_lab = BacktestLab(
    kline_store=_kline_store,
    name_lookup=_qb_name_lookup,
)

research_card_gen = ResearchCardGenerator(
    runtime=hermes_runtime,
    kronos_service=kronos_service,
    kline_store=_kline_store,
    data_provider=provider,
    notice_service=notice_service,
)

news_insight = NewsInsightExtractor(
    runtime=hermes_runtime,
    notice_service=notice_service,
    funnel_service=service,
)

weekly_report = WeeklyReportBuilder(
    runtime=hermes_runtime,
    funnel_service=service,
    notice_service=notice_service,
    paper_trading=paper_trading,
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


async def _auto_trade_loop() -> None:
    """盘中每 60s 执行一次 auto_trade.tick（enabled=False 时快速返回）。"""
    await asyncio.sleep(30)
    while True:
        try:
            await auto_trade_loop.tick()
        except Exception as exc:
            print(f"[auto_trade] loop error: {exc}")
        await asyncio.sleep(60)


async def _weekly_report_scheduler() -> None:
    """周五 15:30 后自动生成周报并推飞书（每周一次）。"""
    from app.services.time_utils import now_cn
    await asyncio.sleep(120)
    last_run_week: str | None = None
    while True:
        try:
            n = now_cn()
            if n.weekday() == 4 and n.hour == 15 and n.minute >= 30:
                wk = n.strftime("%Y-W%W")
                if last_run_week != wk:
                    print(f"[weekly_report] scheduled run at {n.isoformat()}")
                    try:
                        await weekly_report.generate()
                        last_run_week = wk
                    except Exception as exc:
                        print(f"[weekly_report] run failed: {exc}")
        except Exception as exc:
            print(f"[weekly_report] scheduler error: {exc}")
        await asyncio.sleep(600)


async def _predict_funnel_scheduler_loop() -> None:
    """收盘后 16:15 自动触发预测选股扫描（每日一次）。"""
    from app.services.time_utils import now_cn
    await asyncio.sleep(60)
    last_run_date: str | None = None
    while True:
        try:
            cfg = predict_funnel_service.get_config()
            if cfg.get("auto_after_close"):
                n = now_cn()
                today = n.date().isoformat()
                if n.hour == 16 and 15 <= n.minute < 30 and last_run_date != today:
                    snap = predict_funnel_service.get_snapshot()
                    if (snap.get("trade_date") != today) or not snap.get("entries"):
                        print(f"[predict_funnel] scheduled auto run at {n.isoformat()}")
                        try:
                            await predict_funnel_service.run(trigger="auto")
                            last_run_date = today
                        except Exception as exc:
                            print(f"[predict_funnel] scheduled run failed: {exc}")
                    else:
                        last_run_date = today
        except Exception as exc:
            print(f"[predict_funnel] scheduler error: {exc}")
        await asyncio.sleep(300)


async def _hot_stock_ai_scheduler_loop() -> None:
    """热门股票智能分析自动刷新，每 60s 检查一次是否到达配置刷新窗口。"""
    from app.services.time_utils import now_cn
    await asyncio.sleep(90)
    while True:
        try:
            cfg = hot_stock_ai_service.get_config()
            if cfg.get("auto_refresh_enabled") and not hot_stock_ai_service.running and hot_stock_ai_service.is_stale():
                print(f"[hot_stock_ai] scheduled run at {now_cn().isoformat()}")
                async def _run_hot_stock_ai_auto() -> None:
                    try:
                        await hot_stock_ai_service.run(trigger="auto")
                    except Exception as exc:
                        print(f"[hot_stock_ai] scheduled run failed: {exc}")

                asyncio.create_task(_run_hot_stock_ai_auto())
        except Exception as exc:
            print(f"[hot_stock_ai] scheduler error: {exc}")
        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    async def _startup_backfill() -> None:
        try:
            fixed = await service.backfill_names()
            if fixed:
                print(f"[startup] backfilled {fixed} stock names")
        except Exception as exc:
            print(f"[startup] backfill_names failed: {exc}")

    app.state.backfill_task = asyncio.create_task(_startup_backfill())
    app.state.ticker_task = asyncio.create_task(_ticker_loop())
    app.state.kline_cache_task = asyncio.create_task(kline_cache_loop(kline_cache_service))
    app.state.hermes_task = asyncio.create_task(hermes_scheduler_loop(hermes_runtime))
    app.state.monitor_task = asyncio.create_task(monitor_loop(hermes_runtime, hub))
    app.state.predict_funnel_task = asyncio.create_task(_predict_funnel_scheduler_loop())
    app.state.hot_stock_ai_task = asyncio.create_task(_hot_stock_ai_scheduler_loop())
    app.state.risk_guardian_task = asyncio.create_task(risk_guardian_loop(risk_guardian, interval_seconds=30))
    app.state.auto_trade_task = asyncio.create_task(_auto_trade_loop())
    app.state.weekly_report_task = asyncio.create_task(_weekly_report_scheduler())
    yield
    for key in [
        "backfill_task", "ticker_task", "kline_cache_task", "hermes_task", "monitor_task",
        "predict_funnel_task", "hot_stock_ai_task", "risk_guardian_task", "auto_trade_task", "weekly_report_task",
    ]:
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
app.include_router(init_first_limit_alpha_router(first_limit_alpha_service), prefix="/api")
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
async def predict_kronos(symbol: str, lookback: int = 180, horizon: int = 3):
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


@app.get("/api/predict-funnel")
async def get_predict_funnel():
    return predict_funnel_service.get_snapshot()


@app.post("/api/predict-funnel/trigger")
async def trigger_predict_funnel():
    if predict_funnel_service.running:
        raise HTTPException(status_code=409, detail="预测任务已在执行中")
    asyncio.create_task(predict_funnel_service.run(trigger="manual"))
    return {"success": True, "message": "预测任务已启动，请查看进度", "snapshot": predict_funnel_service.get_snapshot()}


@app.get("/api/predict-funnel/config")
async def get_predict_funnel_config():
    return predict_funnel_service.get_config()


@app.post("/api/predict-funnel/config")
async def update_predict_funnel_config(payload: dict):
    cfg = predict_funnel_service.update_config(payload or {})
    return {"success": True, "config": cfg}


@app.get("/api/strategy/hot-stock-ai")
async def get_hot_stock_ai_snapshot():
    return hot_stock_ai_service.get_snapshot()


@app.post("/api/strategy/hot-stock-ai/run")
async def run_hot_stock_ai():
    if hot_stock_ai_service.running:
        raise HTTPException(status_code=409, detail="热门股票智能分析任务已在执行中")
    asyncio.create_task(hot_stock_ai_service.run(trigger="manual"))
    return {"success": True, "message": "热门股票智能分析任务已启动", "snapshot": hot_stock_ai_service.get_snapshot()}


@app.post("/api/strategy/hot-stock-ai/pool/move")
async def move_hot_stock_ai_pool(req: MovePoolRequest):
    try:
        return hot_stock_ai_service.move_pool(req.symbol, req.target_pool)
    except KeyError:
        raise HTTPException(status_code=404, detail="symbol not found in hot stock ai pools")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/strategy/hot-stock-ai/config")
async def get_hot_stock_ai_config():
    return hot_stock_ai_service.get_config()


@app.post("/api/strategy/hot-stock-ai/config")
async def update_hot_stock_ai_config(payload: dict):
    cfg = hot_stock_ai_service.update_config(payload or {})
    return {"success": True, "config": cfg}


@app.get("/api/strategy/quiet-breakout")
async def get_quiet_breakout():
    """返回上次扫描的"缩量启动"策略结果快照（兼容接口）。"""
    return quiet_breakout_scanner.get_snapshot()


@app.post("/api/strategy/quiet-breakout/scan")
async def scan_quiet_breakout(
    lookback_days: int = 25,
    amp_threshold: float = 0.20,
    vol_cv_threshold: float = 0.40,
    vol_spike_ratio: float = 3.0,
    require_limit_up: bool = True,
    limit: int | None = None,
):
    """扫描全库，返回缩量横盘 + 首板放量涨停形态的候选股（兼容接口）。"""
    if quiet_breakout_scanner.get_snapshot().get("running"):
        raise HTTPException(status_code=409, detail="扫描正在进行中")
    cfg = QuietBreakoutConfig(
        lookback_days=max(5, min(lookback_days, 60)),
        amp_threshold=max(0.05, min(amp_threshold, 0.5)),
        vol_cv_threshold=max(0.1, min(vol_cv_threshold, 1.0)),
        vol_spike_ratio=max(1.5, min(vol_spike_ratio, 20.0)),
        require_limit_up=require_limit_up,
    )
    try:
        return await quiet_breakout_scanner.scan(cfg, limit=limit)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"扫描失败: {exc}")


# ============ 自定义策略中心 ============

@app.get("/api/strategy/rules")
async def get_strategy_rules():
    """规则目录 — 供前端动态渲染参数表单。"""
    return {"rules": list_strategy_rules()}


@app.get("/api/strategy/custom")
async def list_custom_strategies():
    items = service.state_store.list_custom_strategies()
    default_item = next((x for x in items if x.get("is_default")), None)
    return {
        "items": items,
        "default_id": default_item.get("id") if default_item else (items[0]["id"] if items else None),
    }


@app.get("/api/strategy/custom/{strategy_id}")
async def get_custom_strategy_detail(strategy_id: str):
    data = service.state_store.get_custom_strategy(strategy_id)
    if not data:
        raise HTTPException(status_code=404, detail="strategy not found")
    return data


@app.post("/api/strategy/custom")
async def upsert_custom_strategy(payload: dict):
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="invalid payload")
    name = str(payload.get("name", "")).strip()
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    rules = payload.get("rules") or []
    if not isinstance(rules, list):
        raise HTTPException(status_code=400, detail="rules must be list")
    # 校验规则 code
    clean_rules = []
    for r in rules:
        if not isinstance(r, dict):
            continue
        code = str(r.get("rule_code", "")).strip()
        if code not in RULE_REGISTRY:
            continue
        clean_rules.append({
            "rule_code": code,
            "enabled": bool(r.get("enabled", True)),
            "params": dict(r.get("params", {}) or {}),
        })
    if not clean_rules:
        raise HTTPException(status_code=400, detail="至少需要一条有效规则")
    data = {
        "id": payload.get("id"),
        "name": name,
        "description": str(payload.get("description", "")),
        "rules": clean_rules,
        "is_default": bool(payload.get("is_default", False)),
    }
    saved = service.state_store.upsert_custom_strategy(data)
    return {"success": True, "strategy": saved}


@app.delete("/api/strategy/custom/{strategy_id}")
async def delete_custom_strategy(strategy_id: str):
    try:
        ok = service.state_store.delete_custom_strategy(strategy_id)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    if not ok:
        raise HTTPException(status_code=404, detail="strategy not found")
    return {"success": True}


@app.post("/api/strategy/custom/{strategy_id}/default")
async def set_default_custom_strategy(strategy_id: str):
    if not service.state_store.set_default_custom_strategy(strategy_id):
        raise HTTPException(status_code=404, detail="strategy not found")
    return {"success": True}


@app.get("/api/strategy/custom/{strategy_id}/scan")
async def get_custom_strategy_scan(strategy_id: str):
    snap = custom_strategy_scanner.get_last_snapshot(strategy_id)
    if snap is None:
        return {"strategy_id": strategy_id, "generated_at": None, "hits": [], "total_hits": 0, "total_scanned": 0}
    return snap


@app.post("/api/strategy/custom/{strategy_id}/scan")
async def scan_custom_strategy(strategy_id: str, limit: int | None = None):
    if custom_strategy_scanner.is_running(strategy_id):
        raise HTTPException(status_code=409, detail="该策略扫描正在进行中")
    strategy = _load_custom_strategy(strategy_id)
    try:
        return await custom_strategy_scanner.scan(strategy, limit=limit)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"扫描失败: {exc}")


@app.post("/api/strategy/custom/{strategy_id}/backtest")
async def backtest_custom_strategy(
    strategy_id: str,
    hold_days: int = 3,
    tp_pct: float = 8.0,
    sl_pct: float = -5.0,
    history_days: int = 180,
    limit: int | None = None,
):
    strategy = _load_custom_strategy(strategy_id)
    try:
        return await backtest_lab.run_custom_strategy(
            strategy,
            hold_days=max(1, min(hold_days, 20)),
            tp_pct=max(1.0, min(tp_pct, 50.0)),
            sl_pct=min(-0.5, max(sl_pct, -30.0)),
            history_days=max(30, min(history_days, 365)),
            limit=limit,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"回测失败: {exc}")


# ============ Hermes AI 扩展能力 ============

@app.get("/api/hermes-ai/risk")
async def get_risk_snapshot():
    return risk_guardian.get_snapshot()


@app.post("/api/hermes-ai/risk/config")
async def update_risk_config(payload: dict):
    risk_guardian.set_config(payload or {})
    return {"success": True, "snapshot": risk_guardian.get_snapshot()}


@app.post("/api/hermes-ai/risk/tick")
async def trigger_risk_tick():
    result = await risk_guardian.tick()
    return {"success": True, "result": result, "snapshot": risk_guardian.get_snapshot()}


@app.get("/api/hermes-ai/auto-trade")
async def get_auto_trade():
    return auto_trade_loop.get_snapshot()


@app.post("/api/hermes-ai/auto-trade/config")
async def update_auto_trade_config(payload: dict):
    auto_trade_loop.set_config(payload or {})
    return {"success": True, "snapshot": auto_trade_loop.get_snapshot()}


@app.post("/api/hermes-ai/auto-trade/tick")
async def trigger_auto_trade_tick():
    result = await auto_trade_loop.tick()
    return {"success": True, "result": result, "snapshot": auto_trade_loop.get_snapshot()}


@app.get("/api/hermes-ai/backtest")
async def get_backtest():
    snap = backtest_lab.get_snapshot()
    if not snap:
        return {"generated_at": None, "message": "尚未执行过回测"}
    return snap


@app.post("/api/hermes-ai/backtest/run")
async def run_backtest(
    lookback_days: int = 25,
    hold_days: int = 3,
    tp_pct: float = 8.0,
    sl_pct: float = -5.0,
    amp_threshold: float = 0.20,
    vol_cv_threshold: float = 0.40,
    vol_spike_ratio: float = 3.0,
    require_limit_up: bool = True,
    limit: int | None = None,
):
    if backtest_lab._running:
        raise HTTPException(409, "回测正在运行中")
    try:
        return await backtest_lab.run(
            lookback_days=lookback_days,
            hold_days=hold_days,
            tp_pct=tp_pct,
            sl_pct=sl_pct,
            amp_threshold=amp_threshold,
            vol_cv_threshold=vol_cv_threshold,
            vol_spike_ratio=vol_spike_ratio,
            require_limit_up=require_limit_up,
            limit=limit,
        )
    except Exception as exc:
        raise HTTPException(500, f"回测失败: {exc}")


@app.post("/api/hermes-ai/research/{symbol}")
async def gen_research_card(symbol: str, name: str = ""):
    cached = research_card_gen.get_cached(symbol)
    if cached:
        return cached
    try:
        return await research_card_gen.generate(symbol, name)
    except Exception as exc:
        raise HTTPException(500, f"研报生成失败: {exc}")


@app.get("/api/hermes-ai/news-insight")
async def get_news_insight():
    snap = news_insight.get_snapshot()
    if not snap:
        return {"message": "尚未生成", "generated_at": None}
    return snap


@app.post("/api/hermes-ai/news-insight/run")
async def run_news_insight(trade_date: str | None = None):
    try:
        return await news_insight.generate(trade_date=trade_date)
    except Exception as exc:
        raise HTTPException(500, f"消息分析失败: {exc}")


@app.get("/api/hermes-ai/weekly-report")
async def get_weekly_report():
    snap = weekly_report.get_snapshot()
    if not snap:
        return {"message": "尚未生成", "generated_at": None}
    return snap


@app.post("/api/hermes-ai/weekly-report/run")
async def run_weekly_report():
    try:
        return await weekly_report.generate()
    except Exception as exc:
        raise HTTPException(500, f"周报生成失败: {exc}")


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


@app.get("/api/agent/tasks")
async def list_agent_tasks(limit: int = 10):
    tasks = hermes_memory.get_recent_tasks(limit)
    return {"items": tasks}


# ── 智能监控 API ──


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
    return {"success": True, "message": "智能监控已停止"}


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
    price, price_source = await _get_realtime_price(symbol)
    if price <= 0:
        raise HTTPException(400, f"无法获取 {symbol} 实时价格（source={price_source}）")
    pos = paper_trading.open_position(symbol, name, price, qty, note=req.get("note", ""))
    return {
        "success": True,
        "position": pos,
        "realtime_price": price,
        "price_source": price_source,
    }


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
    price, price_source = await _get_realtime_price(target["symbol"])
    if price <= 0:
        raise HTTPException(400, f"无法获取 {target['symbol']} 实时价格（source={price_source}）")
    pos = paper_trading.close_position(position_id, price, note=req.get("note", ""))
    if not pos:
        raise HTTPException(404, "持仓不存在或已平仓")
    return {
        "success": True,
        "position": pos,
        "realtime_price": price,
        "price_source": price_source,
    }


def _is_a_market_open() -> bool:
    from app.services.time_utils import now_cn
    now = now_cn()
    if now.weekday() >= 5:
        return False
    t = now.hour * 60 + now.minute
    return (570 <= t < 690) or (780 <= t < 900)


async def _get_realtime_price(symbol: str) -> tuple[float, str]:
    """从实时快照获取个股最新价（下单专用，强制走 live）。

    返回 (price, source)，source ∈ em_live | db_fallback | stale_cache | none
    """
    df, source = await _get_paper_realtime_snapshot(cache_ttl_seconds=5, prefer_live=True)
    if df is None or df.empty:
        return 0.0, source
    match = df[df["代码"] == symbol]
    if match.empty:
        return 0.0, source
    val = match.iloc[0].get("最新价", 0)
    return (float(val) if val else 0.0), source


async def _get_paper_realtime_snapshot(
    cache_ttl_seconds: int,
    prefer_live: bool,
) -> tuple[Any | None, str]:
    try:
        df = await asyncio.wait_for(
            provider.get_realtime_snapshot(
                retries=0,
                retry_wait_seconds=0,
                cache_ttl_seconds=cache_ttl_seconds,
                prefer_live=prefer_live,
            ),
            timeout=PAPER_SNAPSHOT_TIMEOUT_SECONDS,
        )
        return df, provider.realtime_snapshot_source or "none"
    except Exception as exc:
        source = f"snapshot_timeout:{type(exc).__name__}"

    try:
        df = await asyncio.wait_for(
            asyncio.to_thread(provider._snapshot_from_db),
            timeout=PAPER_DB_FALLBACK_TIMEOUT_SECONDS,
        )
        if df is not None and not df.empty:
            return df, "db_fallback_after_timeout"
    except Exception as exc:
        source = f"{source};db_fallback_failed:{type(exc).__name__}"
    return None, source


@app.get("/api/paper/positions")
async def paper_positions():
    opens = paper_trading.get_open_positions()
    price_map = {}
    source = "none"
    if opens:
        open_market = _is_a_market_open()
        ttl = 10 if open_market else 300
        df, source = await _get_paper_realtime_snapshot(
            cache_ttl_seconds=ttl, prefer_live=open_market
        )
        if df is not None and not df.empty:
            for _, r in df.iterrows():
                price_map[r["代码"]] = float(r["最新价"]) if r["最新价"] else 0
            paper_trading.update_prices(price_map)
            opens = paper_trading.get_open_positions()
    return {"positions": opens, "price_source": source}


@app.get("/api/paper/history")
async def paper_history(limit: int = 50):
    return {"positions": paper_trading.get_closed_positions(limit)}


@app.get("/api/paper/summary")
async def paper_summary():
    opens = paper_trading.get_open_positions()
    summary = paper_trading.get_summary()
    source = "none"
    if opens:
        open_market = _is_a_market_open()
        ttl = 10 if open_market else 300
        df, source = await _get_paper_realtime_snapshot(
            cache_ttl_seconds=ttl, prefer_live=open_market
        )
        if df is not None and not df.empty:
            price_map = {}
            for _, r in df.iterrows():
                price_map[r["代码"]] = float(r["最新价"]) if r["最新价"] else 0
            paper_trading.update_prices(price_map)
            summary = paper_trading.get_summary()
    summary["price_source"] = source
    return summary


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
