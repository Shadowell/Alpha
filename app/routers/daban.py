"""打板研究与回测路由（#23 标签 / #24 微观结构 / #25 市场情绪 / #26 回测 / #36 增量验证）。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import date, timedelta
from typing import Any

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.container import ServiceContainer
from app.dependencies import get_container
from app.services.limit_up_backtest import run_limit_up_backtest
from app.services.sentiment_factors import compute_sentiment_frame
from app.services.time_utils import now_cn

router = APIRouter(tags=["打板研究"])


def _safe_float(val: Any) -> float | None:
    if val is None or pd.isna(val):
        return None
    f = float(val)
    return f if pd.notna(f) else None


class SentimentComputeRequest(BaseModel):
    start_date: str | None = None
    end_date: str | None = None
    market_scope: str = "all"
    persist: bool = True


@router.post("/market/sentiment/compute")
async def compute_market_sentiment(
    req: SentimentComputeRequest,
    container: ServiceContainer = Depends(get_container),
) -> dict[str, Any]:
    """#25：计算（并可选落库至 market_sentiment_daily）市场情绪周期与连板梯队因子。"""
    end_d = req.end_date or now_cn().date().isoformat()
    start_d = req.start_date or (date.fromisoformat(end_d) - timedelta(days=30)).isoformat()
    db_path = container.kline_db_path

    def _run() -> list[dict[str, Any]]:
        frame = compute_sentiment_frame(
            db_path, start_d, end_d, market_scope=req.market_scope
        )
        if frame.empty:
            return []
        records: list[dict[str, Any]] = []
        ts = now_cn().isoformat()
        for _, row in frame.iterrows():
            rec = {k: (None if pd.isna(v) else v) for k, v in row.to_dict().items()}
            records.append(rec)

        if req.persist:
            with sqlite3.connect(str(db_path), timeout=5.0) as conn:
                for rec in records:
                    conn.execute(
                        """
                        INSERT INTO market_sentiment_daily (
                            trade_date, sentiment_score, regime, limit_up_count,
                            limit_down_count, broken_board_rate, max_board_height,
                            first_board_count, continuation_count, promotion_rate,
                            payload_json, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(trade_date) DO UPDATE SET
                            sentiment_score=excluded.sentiment_score,
                            regime=excluded.regime,
                            limit_up_count=excluded.limit_up_count,
                            limit_down_count=excluded.limit_down_count,
                            broken_board_rate=excluded.broken_board_rate,
                            max_board_height=excluded.max_board_height,
                            first_board_count=excluded.first_board_count,
                            continuation_count=excluded.continuation_count,
                            promotion_rate=excluded.promotion_rate,
                            payload_json=excluded.payload_json,
                            updated_at=excluded.updated_at
                        """,
                        (
                            str(rec.get("trade_date", "")),
                            _safe_float(rec.get("sentiment_score")),
                            str(rec.get("regime", "")),
                            rec.get("limit_up_count"),
                            rec.get("limit_down_count"),
                            _safe_float(rec.get("broken_board_rate")),
                            rec.get("max_board_height"),
                            rec.get("first_board_count"),
                            rec.get("continuation_count"),
                            _safe_float(rec.get("promotion_rate")),
                            json.dumps(rec, ensure_ascii=False, default=str),
                            ts,
                        ),
                    )
                conn.commit()
        return records

    items = await asyncio.to_thread(_run)
    return {"status": "ok", "count": len(items), "items": items[-10:]}


@router.get("/market/sentiment/latest")
async def get_latest_market_sentiment(
    container: ServiceContainer = Depends(get_container),
) -> dict[str, Any]:
    """#25：读取最新一日市场情绪周期快照。"""
    db_path = container.kline_db_path

    def _query() -> dict[str, Any] | None:
        with sqlite3.connect(str(db_path), timeout=5.0) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM market_sentiment_daily ORDER BY trade_date DESC LIMIT 1"
            ).fetchone()
            return dict(row) if row is not None else None

    latest = await asyncio.to_thread(_query)
    return {"available": latest is not None, "latest": latest}


@router.get("/market/sentiment/history")
async def get_market_sentiment_history(
    limit: int = Query(30, ge=1, le=500),
    container: ServiceContainer = Depends(get_container),
) -> dict[str, Any]:
    """#25：读取历史市场情绪周期序列。"""
    db_path = container.kline_db_path

    def _query() -> list[dict[str, Any]]:
        with sqlite3.connect(str(db_path), timeout=5.0) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM market_sentiment_daily ORDER BY trade_date DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    items = await asyncio.to_thread(_query)
    return {"count": len(items), "items": items}


class LimitUpBacktestRequest(BaseModel):
    signals: list[dict[str, Any]] = Field(default_factory=list)
    as_of: str | None = None
    mode: str = "strict"
    initial_cash: float = Field(default=1_000_000.0, gt=0)


@router.post("/daban/backtest")
async def post_daban_backtest(
    req: LimitUpBacktestRequest,
    container: ServiceContainer = Depends(get_container),
) -> dict[str, Any]:
    """#26：打板可成交性、容量与真实交易约束回测入口。"""
    if req.mode not in ("strict", "loose"):
        raise HTTPException(status_code=400, detail="mode 仅支持 strict / loose")
    result = await asyncio.to_thread(
        run_limit_up_backtest,
        req.signals,
        db_path=container.kline_db_path,
        as_of=req.as_of,
        mode=req.mode,
        initial_capital=req.initial_cash,
    )
    return result


@router.get("/daban/microstructure/quality")
async def get_microstructure_quality(
    as_of_date: str | None = Query(default=None),
    symbols: str | None = Query(default=None, description="逗号分隔股票代码"),
    container: ServiceContainer = Depends(get_container),
) -> dict[str, Any]:
    """#24：涨停微观结构与股性因子质量报告。"""
    from strategy.first_limit_alpha.microstructure_features import (
        microstructure_quality_report,
    )

    dt = as_of_date or now_cn().date().isoformat()
    sym_list = [s.strip() for s in symbols.split(",") if s.strip()] if symbols else []
    if not sym_list:
        with sqlite3.connect(str(container.kline_db_path), timeout=5.0) as conn:
            rows = conn.execute(
                "SELECT DISTINCT symbol FROM limit_board_daily ORDER BY trade_date DESC LIMIT 50"
            ).fetchall()
            sym_list = [str(r[0]) for r in rows if r and r[0]]
    report = await asyncio.to_thread(
        microstructure_quality_report, container.kline_db_path, sym_list, dt
    )
    return report


class LabelBuildRequest(BaseModel):
    mode: str = "all_limit_up"
    start_date: str | None = None
    end_date: str | None = None
    symbols: list[str] | None = None


@router.post("/daban/labels/build")
async def post_daban_labels_build(
    req: LabelBuildRequest,
    container: ServiceContainer = Depends(get_container),
) -> dict[str, Any]:
    """#23：构建打板可执行标签数据集并返回分布报告。"""
    from strategy.first_limit_alpha.label_engine import build_executable_label_dataset

    res = await asyncio.to_thread(
        build_executable_label_dataset,
        container.kline_db_path,
        None,
        mode=req.mode,
        start_date=req.start_date,
        end_date=req.end_date,
        symbols=req.symbols,
    )
    summary = {k: v for k, v in res.items() if k != "frame"}
    frame = res.get("frame")
    summary["row_count"] = int(len(frame)) if isinstance(frame, pd.DataFrame) else 0
    return summary


class ValidationRunRequest(BaseModel):
    scenario: str = "informative_sequence"
    n_dates: int = Field(default=60, ge=30, le=300)


@router.post("/daban/validation/run")
async def post_daban_validation_run(req: ValidationRunRequest) -> dict[str, Any]:
    """#36：运行序列与多模态模型增量验证（基于 Synthetic/Panel 契约）。"""
    from strategy.model_validation.cli import (
        GateConfig,
        ModelSpec,
        ROLE_BASELINE,
        ROLE_CANDIDATE,
        SCENARIOS,
        SyntheticConfig,
        ValidationConfig,
        WalkForwardConfig,
        make_synthetic_panel,
        run_validation,
    )

    if req.scenario not in SCENARIOS:
        raise HTTPException(
            status_code=400, detail=f"scenario 仅支持: {', '.join(SCENARIOS)}"
        )
    panel = make_synthetic_panel(
        SyntheticConfig(scenario=req.scenario, n_dates=req.n_dates)  # type: ignore[arg-type]
    )
    cfg = ValidationConfig(
        seed=42,
        label_horizon=2,
        top_k=5,
        walk_forward=WalkForwardConfig(
            test_size=6, train_size=20, embargo=1, max_folds=3
        ),
        gate=GateConfig(min_windows=3),
        models=(
            ModelSpec(
                "tabular_baseline",
                ROLE_BASELINE,
                "RidgeTabularModel",
                {"name": "tabular_baseline"},
            ),
            ModelSpec(
                "sequence_candidate",
                ROLE_CANDIDATE,
                "SequenceTemporalModel",
                {"name": "sequence_candidate"},
            ),
        ),
        data_version=f"synthetic:{req.scenario}",
        feature_version="synthetic:v0",
    )
    report = await asyncio.to_thread(run_validation, panel, cfg)
    return {
        "scenario": req.scenario,
        "overall_decision": getattr(report, "overall_decision", "reject"),
        "keep": getattr(report, "overall_decision", "") == "keep",
        "blockers": list(getattr(report, "blockers", [])),
    }
