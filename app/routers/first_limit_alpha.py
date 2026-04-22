from __future__ import annotations

from fastapi import APIRouter

from app.services.first_limit_alpha_service import FirstLimitAlphaService
from strategy.first_limit_alpha import BacktestConfig, FeatureConfig, LabelConfig, SampleBuildConfig, SequenceConfig, TrainingConfig

router = APIRouter(tags=["FirstLimit Alpha"])

_service: FirstLimitAlphaService | None = None


def init_first_limit_alpha_router(service: FirstLimitAlphaService) -> APIRouter:
    global _service
    _service = service
    return router


@router.get("/strategy/first-limit-alpha/status")
async def get_first_limit_alpha_status():
    return _service.get_status()


@router.post("/strategy/first-limit-alpha/dataset/build")
async def build_first_limit_alpha_dataset(
    start_date: str | None = None,
    end_date: str | None = None,
):
    return await _service.build_dataset(
        start_date=start_date,
        end_date=end_date,
        build_cfg=SampleBuildConfig(),
        label_cfg=LabelConfig(),
    )


@router.post("/strategy/first-limit-alpha/features/build")
async def build_first_limit_alpha_features():
    return _service.build_features(feature_cfg=FeatureConfig())


@router.post("/strategy/first-limit-alpha/train/baseline")
async def train_first_limit_alpha_baseline():
    return _service.train_baseline(training_cfg=TrainingConfig())


@router.post("/strategy/first-limit-alpha/train/sequence")
async def train_first_limit_alpha_sequence():
    return _service.train_sequence(sequence_cfg=SequenceConfig())


@router.post("/strategy/first-limit-alpha/inference/run")
async def run_first_limit_alpha_inference(trade_date: str | None = None):
    return _service.run_inference(trade_date=trade_date)


@router.post("/strategy/first-limit-alpha/backtest")
async def run_first_limit_alpha_backtest():
    return _service.backtest_latest(config=BacktestConfig())
