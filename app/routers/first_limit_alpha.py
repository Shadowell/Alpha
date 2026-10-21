"""FirstLimit Alpha 研究路由（训练/推理/回测）。"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends

from app.container import ServiceContainer
from app.dependencies import get_container
from app.services.first_limit_alpha_service import FirstLimitAlphaService
from strategy.first_limit_alpha import (
    BacktestConfig,
    FeatureConfig,
    LabelConfig,
    SampleBuildConfig,
    SequenceConfig,
    TrainingConfig,
)

router = APIRouter(tags=["FirstLimit Alpha"])

# 兼容外部显式注入
_service: FirstLimitAlphaService | None = None


def init_first_limit_alpha_router(service: FirstLimitAlphaService) -> APIRouter:
    global _service
    _service = service
    return router


def _service_of(container: ServiceContainer) -> FirstLimitAlphaService:
    if _service is not None:
        return _service
    return container.first_limit_alpha_service


async def _run(fn, /, **kwargs):
    """训练/推理为同步重 CPU 调用（torch/lightgbm），统一下放线程避免阻塞事件循环。"""
    return await asyncio.to_thread(lambda: fn(**kwargs))


@router.get("/strategy/first-limit-alpha/status")
async def get_first_limit_alpha_status(container: ServiceContainer = Depends(get_container)):
    return _service_of(container).get_status()


@router.post("/strategy/first-limit-alpha/dataset/build")
async def build_first_limit_alpha_dataset(
    start_date: str | None = None,
    end_date: str | None = None,
    container: ServiceContainer = Depends(get_container),
):
    return await _run(
        _service_of(container).build_dataset,
        start_date=start_date,
        end_date=end_date,
        build_cfg=SampleBuildConfig(),
        label_cfg=LabelConfig(),
    )


@router.post("/strategy/first-limit-alpha/features/build")
async def build_first_limit_alpha_features(container: ServiceContainer = Depends(get_container)):
    return await _run(_service_of(container).build_features, feature_cfg=FeatureConfig())


@router.post("/strategy/first-limit-alpha/train/baseline")
async def train_first_limit_alpha_baseline(container: ServiceContainer = Depends(get_container)):
    return await _run(_service_of(container).train_baseline, training_cfg=TrainingConfig())


@router.post("/strategy/first-limit-alpha/train/sequence")
async def train_first_limit_alpha_sequence(container: ServiceContainer = Depends(get_container)):
    return await _run(_service_of(container).train_sequence, sequence_cfg=SequenceConfig())


@router.post("/strategy/first-limit-alpha/inference/run")
async def run_first_limit_alpha_inference(
    trade_date: str | None = None,
    container: ServiceContainer = Depends(get_container),
):
    return await _run(_service_of(container).run_inference, trade_date=trade_date)


@router.post("/strategy/first-limit-alpha/backtest")
async def run_first_limit_alpha_backtest(container: ServiceContainer = Depends(get_container)):
    return await _run(_service_of(container).backtest_latest, config=BacktestConfig())
