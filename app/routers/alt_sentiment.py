"""#33 另类情绪因子路由（受 ALPHA_ALT_SENTIMENT_ENABLED 开关门控，默认 false）。"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.container import ServiceContainer
from app.dependencies import get_container
from app.services.alt_sentiment.normalize import normalize_symbol
from app.services.alt_sentiment.service import AltSentimentService

router = APIRouter(prefix="/alt-sentiment", tags=["另类情绪"])


def _is_enabled() -> bool:
    return os.getenv("ALPHA_ALT_SENTIMENT_ENABLED", "false").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _service(container: ServiceContainer) -> AltSentimentService:
    svc = getattr(container, "alt_sentiment_service", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="AltSentimentService 未初始化")
    return svc


class AltSyncRequest(BaseModel):
    trade_date: str | None = Field(default=None, max_length=16)
    symbols: list[str] = Field(default_factory=list, max_length=200)
    include_market_comment: bool = True
    include_irm: bool = True


@router.get("/sources")
async def get_alt_sources(container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    svc = _service(container)
    return {
        "enabled": _is_enabled(),
        "assessment": svc.source_assessment(),
    }


@router.post("/sync")
async def sync_alt_sentiment(
    req: AltSyncRequest,
    container: ServiceContainer = Depends(get_container),
) -> dict[str, Any]:
    if not _is_enabled():
        raise HTTPException(
            status_code=403,
            detail="另类情绪采集未启用（需设置 ALPHA_ALT_SENTIMENT_ENABLED=true）",
        )
    svc = _service(container)
    res = await svc.run_daily(
        trade_date=req.trade_date,
        symbols=req.symbols or None,
        include_market_comment=req.include_market_comment,
        include_irm=req.include_irm,
    )
    return {"status": "ok", "result": res}


@router.get("/factors/{symbol}")
async def get_alt_factors(
    symbol: str,
    trade_date: str | None = Query(default=None),
    container: ServiceContainer = Depends(get_container),
) -> dict[str, Any]:
    svc = _service(container)
    norm = normalize_symbol(symbol)
    rows = svc.store.load_daily(
        symbol=norm, start_date=trade_date, end_date=trade_date
    )
    items = [r.to_dict() if hasattr(r, "to_dict") else r for r in rows]
    return {"symbol": norm, "trade_date": trade_date, "count": len(items), "items": items}


@router.get("/coverage")
async def get_alt_coverage(
    trade_date: str | None = Query(default=None),
    container: ServiceContainer = Depends(get_container),
) -> dict[str, Any]:
    svc = _service(container)
    rows = svc.store.load_daily(start_date=trade_date, end_date=trade_date)
    return {
        "enabled": _is_enabled(),
        "trade_date": trade_date,
        "daily_rows": len(rows),
        "symbols": sorted(
            {str(getattr(r, "symbol", "")) for r in rows if getattr(r, "symbol", None)}
        ),
    }


@router.get("/offline-report")
async def get_alt_offline_report(
    container: ServiceContainer = Depends(get_container),
) -> dict[str, Any]:
    svc = _service(container)
    return {
        "enabled": _is_enabled(),
        "assessment": svc.source_assessment(),
        "markdown": svc.report_markdown(),
    }
