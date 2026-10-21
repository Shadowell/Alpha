"""模拟盘路由 — 买卖、持仓、汇总、费用设置。"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.container import ServiceContainer
from app.dependencies import get_container
from app.models import PaperBuyRequest, PaperSellRequest, PaperSettingsUpdate
from app.services.observability import METRICS
from app.services.data_provider import normalize_symbol

router = APIRouter(tags=["模拟盘"])

PAPER_SNAPSHOT_TIMEOUT_SECONDS = 2.5
PAPER_DB_FALLBACK_TIMEOUT_SECONDS = 1.0


def _is_a_market_open() -> bool:
    from app.services.time_utils import now_cn

    now = now_cn()
    if now.weekday() >= 5:
        return False
    t = now.hour * 60 + now.minute
    return (570 <= t < 690) or (780 <= t < 900)


async def _get_realtime_price(provider: Any, symbol: str) -> tuple[float, str]:
    """从实时快照获取个股最新价（下单专用，强制走 live）。

    返回 (price, source)，source ∈ em_live | db_fallback | stale_cache | none
    """
    df, source = await _get_paper_realtime_snapshot(
        provider, cache_ttl_seconds=5, prefer_live=True
    )
    if df is None or df.empty:
        return 0.0, source
    match = df[df["代码"] == symbol]
    if match.empty:
        return 0.0, source
    val = match.iloc[0].get("最新价", 0)
    return (float(val) if val else 0.0), source


async def _get_paper_realtime_snapshot(
    provider: Any,
    cache_ttl_seconds: int,
    prefer_live: bool,
) -> tuple[Any | None, str]:
    source = "none"
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
    except Exception as exc:  # noqa: BLE001 — 超时/上游失败统一走 DB 兜底
        METRICS.inc_timeout("paper_snapshot")
        source = f"snapshot_timeout:{type(exc).__name__}"

    try:
        df = await asyncio.wait_for(
            asyncio.to_thread(provider._snapshot_from_db),  # noqa: SLF001
            timeout=PAPER_DB_FALLBACK_TIMEOUT_SECONDS,
        )
        if df is not None and not df.empty:
            return df, "db_fallback_after_timeout"
    except Exception as exc:  # noqa: BLE001
        METRICS.inc_degraded("paper_db_fallback")
        source = f"{source};db_fallback_failed:{type(exc).__name__}"
    return None, source


@router.post("/paper/buy")
async def paper_buy(
    req: PaperBuyRequest,
    container: ServiceContainer = Depends(get_container),
):
    if not _is_a_market_open():
        raise HTTPException(400, "当前非交易时段，无法模拟买入")
    symbol = normalize_symbol(req.symbol)
    if not symbol:
        raise HTTPException(400, "缺少 symbol")
    name = req.name or symbol
    price, price_source = await _get_realtime_price(container.provider, symbol)
    if price <= 0:
        raise HTTPException(400, f"无法获取 {symbol} 实时价格（source={price_source}）")
    pos = container.paper_trading.open_position(
        symbol, name, price, req.qty, note=req.note
    )
    return {
        "success": True,
        "position": pos,
        "realtime_price": price,
        "price_source": price_source,
    }


@router.post("/paper/sell")
async def paper_sell(
    req: PaperSellRequest,
    container: ServiceContainer = Depends(get_container),
):
    if not _is_a_market_open():
        raise HTTPException(400, "当前非交易时段，无法模拟卖出")
    opens = container.paper_trading.get_open_positions()
    target = next((p for p in opens if p["id"] == req.position_id), None)
    if not target:
        raise HTTPException(404, "持仓不存在或已平仓")
    price, price_source = await _get_realtime_price(container.provider, target["symbol"])
    if price <= 0:
        raise HTTPException(400, f"无法获取 {target['symbol']} 实时价格（source={price_source}）")
    pos = container.paper_trading.close_position(req.position_id, price, note=req.note)
    if not pos:
        raise HTTPException(404, "持仓不存在或已平仓")
    return {
        "success": True,
        "position": pos,
        "realtime_price": price,
        "price_source": price_source,
    }


@router.get("/paper/positions")
async def paper_positions(container: ServiceContainer = Depends(get_container)):
    opens = container.paper_trading.get_open_positions()
    price_map: dict[str, float] = {}
    source = "none"
    if opens:
        open_market = _is_a_market_open()
        ttl = 10 if open_market else 300
        df, source = await _get_paper_realtime_snapshot(
            container.provider, cache_ttl_seconds=ttl, prefer_live=open_market
        )
        if df is not None and not df.empty:
            for _, r in df.iterrows():
                price_map[r["代码"]] = float(r["最新价"]) if r["最新价"] else 0
            container.paper_trading.update_prices(price_map)
            opens = container.paper_trading.get_open_positions()
    return {"positions": opens, "price_source": source}


@router.get("/paper/history")
async def paper_history(
    limit: int = Query(default=50, ge=1, le=1000),
    container: ServiceContainer = Depends(get_container),
):
    return {"positions": container.paper_trading.get_closed_positions(limit)}


@router.get("/paper/summary")
async def paper_summary(container: ServiceContainer = Depends(get_container)):
    paper = container.paper_trading
    opens = paper.get_open_positions()
    summary = paper.get_summary()
    source = "none"
    if opens:
        open_market = _is_a_market_open()
        ttl = 10 if open_market else 300
        df, source = await _get_paper_realtime_snapshot(
            container.provider, cache_ttl_seconds=ttl, prefer_live=open_market
        )
        if df is not None and not df.empty:
            price_map: dict[str, float] = {}
            for _, r in df.iterrows():
                price_map[r["代码"]] = float(r["最新价"]) if r["最新价"] else 0
            paper.update_prices(price_map)
            summary = paper.get_summary()
    summary["price_source"] = source
    return summary


@router.get("/paper/trades")
async def paper_trades(
    limit: int = Query(default=100, ge=1, le=1000),
    container: ServiceContainer = Depends(get_container),
):
    return {"trades": container.paper_trading.get_trades(limit)}


@router.get("/paper/settings")
async def paper_settings_get(container: ServiceContainer = Depends(get_container)):
    return container.paper_trading.get_settings()


@router.post("/paper/settings")
async def paper_settings_post(
    req: PaperSettingsUpdate,
    container: ServiceContainer = Depends(get_container),
):
    container.paper_trading.update_settings(**req.model_dump(exclude_unset=True, exclude_none=True))
    return container.paper_trading.get_settings()
