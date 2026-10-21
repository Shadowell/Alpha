"""策略中心路由 — 规则目录、自定义策略 CRUD、扫描 / 回测、缩量启动。"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from app.container import ServiceContainer
from app.dependencies import get_container
from app.models import CustomStrategyUpsert
from app.routers.jobs import accepted_response, conflict_response
from app.services.custom_strategy import CustomStrategy
from app.services.error_response import error_detail
from app.services.jobs import JobConflict
from app.services.quiet_breakout_scanner import QuietBreakoutConfig
from app.services.strategy_rules import list_rules as list_strategy_rules

log = logging.getLogger("routers.strategy")

router = APIRouter(tags=["策略"])


def _load_custom_strategy(container: ServiceContainer, strategy_id: str) -> CustomStrategy:
    data = container.funnel_service.state_store.get_custom_strategy(strategy_id)
    if not data:
        raise HTTPException(status_code=404, detail="strategy not found")
    return CustomStrategy.from_dict(data)


# ── 规则目录 ──


@router.get("/strategy/rules")
async def get_strategy_rules():
    """规则目录 — 供前端动态渲染参数表单。"""
    return {"rules": list_strategy_rules()}


# ── 自定义策略 CRUD ──


@router.get("/strategy/custom")
async def list_custom_strategies(container: ServiceContainer = Depends(get_container)):
    items = container.funnel_service.state_store.list_custom_strategies()
    default_item = next((x for x in items if x.get("is_default")), None)
    return {
        "items": items,
        "default_id": default_item.get("id") if default_item else (items[0]["id"] if items else None),
    }


@router.get("/strategy/custom/{strategy_id}")
async def get_custom_strategy_detail(
    strategy_id: str,
    container: ServiceContainer = Depends(get_container),
):
    data = container.funnel_service.state_store.get_custom_strategy(strategy_id)
    if not data:
        raise HTTPException(status_code=404, detail="strategy not found")
    return data


@router.post("/strategy/custom")
async def upsert_custom_strategy(
    payload: CustomStrategyUpsert,
    container: ServiceContainer = Depends(get_container),
):
    data = {
        "id": payload.id,
        "name": payload.name,
        "description": payload.description,
        "rules": [rule.model_dump() for rule in payload.rules],
        "is_default": payload.is_default,
    }
    saved = container.funnel_service.state_store.upsert_custom_strategy(data)
    return {"success": True, "strategy": saved}


@router.delete("/strategy/custom/{strategy_id}")
async def delete_custom_strategy(
    strategy_id: str,
    container: ServiceContainer = Depends(get_container),
):
    try:
        ok = container.funnel_service.state_store.delete_custom_strategy(strategy_id)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    if not ok:
        raise HTTPException(status_code=404, detail="strategy not found")
    return {"success": True}


@router.post("/strategy/custom/{strategy_id}/default")
async def set_default_custom_strategy(
    strategy_id: str,
    container: ServiceContainer = Depends(get_container),
):
    if not container.funnel_service.state_store.set_default_custom_strategy(strategy_id):
        raise HTTPException(status_code=404, detail="strategy not found")
    return {"success": True}


# ── 自定义策略扫描 ──


@router.get("/strategy/custom/{strategy_id}/scan")
async def get_custom_strategy_scan(
    strategy_id: str,
    container: ServiceContainer = Depends(get_container),
):
    snap = container.custom_strategy_scanner.get_last_snapshot(strategy_id)
    if snap is None:
        return {
            "strategy_id": strategy_id,
            "generated_at": None,
            "hits": [],
            "total_hits": 0,
            "total_scanned": 0,
        }
    return snap


@router.post("/strategy/custom/{strategy_id}/scan")
async def scan_custom_strategy(
    strategy_id: str,
    limit: int | None = Query(default=None, ge=1, le=10_000),
    sync: bool = False,
    container: ServiceContainer = Depends(get_container),
):
    """提交扫描任务；默认异步返回 202，`?sync=true` 保留旧同步响应。"""
    strategy = _load_custom_strategy(container, strategy_id)
    scanner = container.custom_strategy_scanner
    if sync:
        if scanner.is_running(strategy_id):
            raise HTTPException(status_code=409, detail="该策略扫描正在进行中")
        try:
            return await scanner.scan(strategy, limit=limit)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except Exception as exc:  # noqa: BLE001 — 旧同步接口保持 500 语义
            return error_detail(
                "扫描失败，请稍后重试",
                exc,
                context="custom strategy scan failed",
            )

    async def runner(ctx) -> dict:
        ctx.progress(10.0, "准备扫描")
        ctx.progress(50.0, "扫描中")
        result = await scanner.scan(strategy, limit=limit)
        ctx.progress(90.0, "整理结果")
        return result

    try:
        job = await container.job_manager.submit(
            kind="strategy_scan",
            runner=runner,
            dedupe_key=strategy_id,
            params={"strategy_id": strategy_id, "limit": limit},
        )
    except JobConflict as exc:
        return conflict_response(exc)
    return accepted_response(job)


# ── 自定义策略回测 ──


@router.post("/strategy/custom/{strategy_id}/backtest")
async def backtest_custom_strategy(
    strategy_id: str,
    hold_days: int = Query(default=3, ge=1, le=20),
    tp_pct: float = Query(default=8.0, ge=1, le=50),
    sl_pct: float = Query(default=-5.0, ge=-30, le=-0.5),
    history_days: int = Query(default=180, ge=30, le=365),
    limit: int | None = Query(default=None, ge=1, le=10_000),
    sync: bool = False,
    container: ServiceContainer = Depends(get_container),
):
    """提交回测任务；默认异步返回 202，`?sync=true` 保留旧同步响应。"""
    strategy = _load_custom_strategy(container, strategy_id)
    lab = container.backtest_lab

    async def run_backtest(ctx=None) -> dict:
        if ctx is not None:
            ctx.progress(10.0, "准备历史数据")
            ctx.progress(50.0, "回测中")
        result = await lab.run_custom_strategy(
            strategy,
            hold_days=hold_days,
            tp_pct=tp_pct,
            sl_pct=sl_pct,
            history_days=history_days,
            limit=limit,
        )
        if ctx is not None:
            ctx.progress(90.0, "汇总统计")
        return result

    if sync:
        try:
            return await run_backtest()
        except Exception as exc:  # noqa: BLE001 — 旧同步接口保持 500 语义
            return error_detail(
                "回测失败，请稍后重试",
                exc,
                context="custom strategy backtest failed",
            )

    try:
        job = await container.job_manager.submit(
            kind="strategy_backtest",
            runner=run_backtest,
            dedupe_key=strategy_id,
            params={
                "strategy_id": strategy_id,
                "hold_days": hold_days,
                "tp_pct": tp_pct,
                "sl_pct": sl_pct,
                "history_days": history_days,
                "limit": limit,
            },
        )
    except JobConflict as exc:
        return conflict_response(exc)
    return accepted_response(job)


# ── 缩量启动（兼容接口）──


@router.get("/strategy/quiet-breakout")
async def get_quiet_breakout(container: ServiceContainer = Depends(get_container)):
    """返回上次扫描的"缩量启动"策略结果快照（兼容接口）。"""
    return container.quiet_breakout_scanner.get_snapshot()


@router.post("/strategy/quiet-breakout/scan")
async def scan_quiet_breakout(
    lookback_days: int = Query(default=25, ge=5, le=60),
    amp_threshold: float = Query(default=0.20, ge=0.05, le=0.5),
    vol_cv_threshold: float = Query(default=0.40, ge=0.1, le=1.0),
    vol_spike_ratio: float = Query(default=3.0, ge=1.5, le=20.0),
    require_limit_up: bool = True,
    limit: int | None = Query(default=None, ge=1, le=10_000),
    container: ServiceContainer = Depends(get_container),
):
    """扫描全库，返回缩量横盘 + 首板放量涨停形态的候选股（兼容接口）。"""
    scanner = container.quiet_breakout_scanner
    if scanner.get_snapshot().get("running"):
        raise HTTPException(status_code=409, detail="扫描正在进行中")
    cfg = QuietBreakoutConfig(
        lookback_days=lookback_days,
        amp_threshold=amp_threshold,
        vol_cv_threshold=vol_cv_threshold,
        vol_spike_ratio=vol_spike_ratio,
        require_limit_up=require_limit_up,
    )
    try:
        return await scanner.scan(cfg, limit=limit)
    except Exception as exc:  # noqa: BLE001 — 兼容接口保持 500 语义
        log.exception("quiet breakout scan failed")
        return error_detail(
            "扫描失败，请稍后重试",
            exc,
            context="quiet breakout scan failed",
        )
