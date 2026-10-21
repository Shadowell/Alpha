"""预测 / Kronos / 预测选股 / 热门股票 AI 路由。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.container import ServiceContainer
from app.dependencies import get_container
from app.models import HotStockAIConfigUpdate, MovePoolRequest, PredictFunnelConfigUpdate
from app.services.data_provider import normalize_symbol
from app.services.error_response import error_detail

router = APIRouter(tags=["预测"])


# ── Kronos ──


@router.get("/predict/kronos/status")
async def kronos_model_status(container: ServiceContainer = Depends(get_container)):
    """Kronos 模型加载状态（首次预测会触发权重下载，前端据此展示徽标）。"""
    return container.kronos_service.status()


@router.get("/predict/{symbol}/kronos")
async def predict_kronos(
    symbol: str,
    lookback: int = 180,
    horizon: int = 3,
    container: ServiceContainer = Depends(get_container),
):
    clean = normalize_symbol(symbol)
    try:
        return await container.kronos_service.predict(clean, lookback, horizon)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ImportError as exc:
        return error_detail(
            "Kronos 运行环境未就绪，请检查模型依赖",
            exc,
            status_code=503,
            context="kronos dependencies missing",
        )
    except RuntimeError as exc:
        return error_detail(
            "Kronos 服务暂不可用，请稍后重试",
            exc,
            status_code=503,
            context="kronos predict runtime error",
        )
    except Exception as exc:  # noqa: BLE001 — 预测内部异常映射为 500
        return error_detail(
            "预测失败，请稍后重试",
            exc,
            context="kronos predict failed",
        )


# ── 预测选股 ──


@router.get("/predict-funnel")
async def get_predict_funnel(container: ServiceContainer = Depends(get_container)):
    return container.predict_funnel_service.get_snapshot()


@router.post("/predict-funnel/trigger")
async def trigger_predict_funnel(container: ServiceContainer = Depends(get_container)):
    service = container.predict_funnel_service
    if service.running:
        raise HTTPException(status_code=409, detail="预测任务已在执行中")
    container.background_tasks.spawn(service.run(trigger="manual"), name="predict-funnel-manual")
    return {
        "success": True,
        "message": "预测任务已启动，请查看进度",
        "snapshot": service.get_snapshot(),
    }


@router.get("/predict-funnel/config")
async def get_predict_funnel_config(container: ServiceContainer = Depends(get_container)):
    return container.predict_funnel_service.get_config()


@router.post("/predict-funnel/config")
async def update_predict_funnel_config(
    payload: PredictFunnelConfigUpdate,
    container: ServiceContainer = Depends(get_container),
):
    patch = payload.model_dump(exclude_unset=True, exclude_none=True)
    cfg = container.predict_funnel_service.update_config(patch)
    return {"success": True, "config": cfg}


# ── 热门股票智能分析 ──


@router.get("/strategy/hot-stock-ai")
async def get_hot_stock_ai_snapshot(container: ServiceContainer = Depends(get_container)):
    return container.hot_stock_ai_service.get_snapshot()


@router.post("/strategy/hot-stock-ai/run")
async def run_hot_stock_ai(container: ServiceContainer = Depends(get_container)):
    service = container.hot_stock_ai_service
    if service.running:
        raise HTTPException(status_code=409, detail="热门股票智能分析任务已在执行中")
    container.background_tasks.spawn(service.run(trigger="manual"), name="hot-stock-ai-manual")
    return {
        "success": True,
        "message": "热门股票智能分析任务已启动",
        "snapshot": service.get_snapshot(),
    }


@router.post("/strategy/hot-stock-ai/pool/move")
async def move_hot_stock_ai_pool(
    req: MovePoolRequest,
    container: ServiceContainer = Depends(get_container),
):
    try:
        return container.hot_stock_ai_service.move_pool(req.symbol, req.target_pool)
    except KeyError:
        raise HTTPException(status_code=404, detail="symbol not found in hot stock ai pools")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/strategy/hot-stock-ai/config")
async def get_hot_stock_ai_config(container: ServiceContainer = Depends(get_container)):
    return container.hot_stock_ai_service.get_config()


@router.post("/strategy/hot-stock-ai/config")
async def update_hot_stock_ai_config(
    payload: HotStockAIConfigUpdate,
    container: ServiceContainer = Depends(get_container),
):
    patch = payload.model_dump(exclude_unset=True, exclude_none=True)
    cfg = container.hot_stock_ai_service.update_config(patch)
    return {"success": True, "config": cfg}
