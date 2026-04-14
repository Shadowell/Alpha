"""K 线同步模块 — 缓存同步、实时行情合并、定时任务。

路由前缀: /api  (由 main.py include 时指定)
"""
from __future__ import annotations

import asyncio
import time as _time

from fastapi import APIRouter, HTTPException

from app.services.data_provider import AkshareDataProvider, normalize_symbol
from app.services.kline_cache_service import KlineCacheService
from app.services.strategy_engine import get_last_n_trade_window
from app.services.time_utils import now_cn, is_market_open, is_after_close

router = APIRouter(tags=["K线同步"])

# 由 init_kline_router() 注入
_provider: AkshareDataProvider | None = None
_kline_cache_service: KlineCacheService | None = None


def init_kline_router(
    provider: AkshareDataProvider,
    kline_cache_service: KlineCacheService,
) -> APIRouter:
    """注入依赖并返回 router，供 main.py 调用。"""
    global _provider, _kline_cache_service
    _provider = provider
    _kline_cache_service = kline_cache_service
    return router


# ── 同步任务路由 ──────────────────────────────────────────


@router.post("/jobs/kline-cache/sync")
async def run_kline_cache_sync(
    trade_date: str | None = None,
    force: bool = False,
    trigger_mode: str = "manual",
):
    payload = await _kline_cache_service.sync_trade_date(
        trade_date=trade_date, force=force, trigger_mode=trigger_mode,
    )
    if not payload.get("success"):
        raise HTTPException(status_code=503, detail=payload.get("message", "同步失败"))
    return payload


@router.post("/jobs/kline-cache/incremental-sync")
async def run_kline_incremental_sync(
    trade_date: str | None = None,
    trigger_mode: str = "manual",
):
    payload = await _kline_cache_service.incremental_sync(
        trade_date=trade_date, trigger_mode=trigger_mode,
    )
    if not payload.get("success"):
        raise HTTPException(status_code=503, detail=payload.get("message", "增量同步失败"))
    return payload


@router.get("/jobs/kline-cache/status")
async def get_kline_cache_status():
    return _kline_cache_service.get_sync_state()


@router.get("/jobs/kline-cache/progress")
async def get_kline_cache_progress():
    return _kline_cache_service.get_sync_progress()


@router.get("/jobs/kline-cache/logs")
async def get_kline_cache_logs(page: int = 1, page_size: int = 20):
    return _kline_cache_service.list_sync_logs(page=page, page_size=page_size)


@router.get("/jobs/kline-cache/logs/{task_id}")
async def get_kline_cache_log_detail(task_id: str):
    payload = _kline_cache_service.get_sync_log_detail(task_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="task not found")
    return payload


# ── K 线查询路由 ──────────────────────────────────────────


@router.get("/kline/{symbol}")
async def get_cached_kline(symbol: str, days: int = 30):
    clean_symbol = normalize_symbol(symbol)
    items = _kline_cache_service.get_kline(symbol=clean_symbol, days=days)
    if not items:
        trade_days = await _provider.get_trade_days()
        try:
            start_date, end_date = get_last_n_trade_window(
                trade_days, now_cn().date().isoformat(), max(10, min(days, 180)),
            )
            hist = await _provider.get_hist(clean_symbol, start_date, end_date)
        except Exception:
            hist = None
        if hist is not None and not hist.empty:
            for _, row in hist.tail(max(1, min(days, 365))).iterrows():
                items.append({
                    "date": str(row.get("日期", "")),
                    "open": float(row.get("开盘", 0)),
                    "high": float(row.get("最高", 0)),
                    "low": float(row.get("最低", 0)),
                    "close": float(row.get("收盘", 0)),
                    "volume": float(row.get("成交量", 0)),
                    "amount": float(row.get("成交额", 0)),
                })
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


# ── 实时 Today Bar（内部辅助）─────────────────────────────


_trade_date_cache: tuple[float, str | None] = (0.0, None)
_TRADE_DATE_TTL = 300  # 5 min


async def _get_latest_trade_date() -> str | None:
    global _trade_date_cache
    now = _time.monotonic()
    if _trade_date_cache[1] is not None and (now - _trade_date_cache[0]) < _TRADE_DATE_TTL:
        return _trade_date_cache[1]
    try:
        import pandas as pd
        df = await _provider.get_trade_days()
        if df.empty:
            result = now_cn().date().isoformat()
        else:
            today = pd.Timestamp(now_cn().date())
            df["trade_date"] = pd.to_datetime(df["trade_date"])
            valid = df[df["trade_date"] <= today]["trade_date"]
            result = valid.iloc[-1].strftime("%Y-%m-%d") if not valid.empty else now_cn().date().isoformat()
    except Exception:
        result = now_cn().date().isoformat()
    _trade_date_cache = (now, result)
    return result


_today_bar_cache: dict[str, tuple[float, dict | None]] = {}


async def _build_today_bar(symbol: str) -> dict | None:
    """从个股实时行情构造当天 K 线柱。

    - 非交易时段直接返回缓存（避免无意义的网络请求）
    - 交易时段带 15s TTL 内存缓存 + 5s 超时保护
    """
    if is_after_close() or not is_market_open():
        return _today_bar_cache.get(symbol, (0, None))[1]

    now = _time.monotonic()
    cached = _today_bar_cache.get(symbol)
    if cached and (now - cached[0]) < 15:
        return cached[1]

    trade_date = await _get_latest_trade_date()
    if not trade_date:
        return None

    bar = await _fetch_today_bar_from_bid_ask(symbol, trade_date)
    if bar is None:
        bar = await _fetch_today_bar_from_snapshot(symbol, trade_date)

    _today_bar_cache[symbol] = (_time.monotonic(), bar)
    return bar


async def _fetch_today_bar_from_bid_ask(symbol: str, trade_date: str) -> dict | None:
    import akshare as ak
    try:
        raw_symbol = symbol.replace("sh", "").replace("sz", "").replace("bj", "")
        df = await asyncio.wait_for(
            asyncio.to_thread(ak.stock_bid_ask_em, symbol=raw_symbol),
            timeout=5.0,
        )
        if df is not None and not df.empty:
            lookup = dict(zip(df["item"], df["value"]))
            price = float(lookup.get("最新", 0))
            open_ = float(lookup.get("今开", 0))
            if price > 0 and open_ > 0:
                return {
                    "date": trade_date,
                    "open": open_,
                    "high": float(lookup.get("最高", price)),
                    "low": float(lookup.get("最低", price)),
                    "close": price,
                    "volume": float(lookup.get("总手", 0)) * 100,
                    "amount": float(lookup.get("金额", 0)),
                }
    except Exception:
        pass
    return None


async def _fetch_today_bar_from_snapshot(symbol: str, trade_date: str) -> dict | None:
    try:
        snapshot = await asyncio.wait_for(
            _provider.get_realtime_snapshot(cache_ttl_seconds=300),
            timeout=5.0,
        )
        if not snapshot.empty:
            row = snapshot[snapshot["代码"] == symbol]
            if not row.empty:
                r = row.iloc[0]
                price = float(r.get("最新价", 0))
                open_ = float(r.get("今开", 0))
                if price > 0 and open_ > 0:
                    return {
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
    return None


# ── 后台定时同步任务 ──────────────────────────────────────


async def kline_cache_loop(kline_cache_service: KlineCacheService) -> None:
    """后台循环：每 10 分钟检测是否需要自动同步 K 线。"""
    from app.services.feishu_notify import notify_sync_complete

    await asyncio.sleep(10)
    while True:
        try:
            result = await kline_cache_service.run_if_due()
            if result is not None:
                print(f"[kline-cache] daily sync completed: {result.get('message')}")
                try:
                    await notify_sync_complete(
                        trade_date=result.get("trade_date", ""),
                        success_count=result.get("success_symbols", result.get("symbol_count", 0)),
                        failed_count=result.get("failed_symbols", 0),
                        total=result.get("total_symbols", 0),
                        elapsed_sec=result.get("elapsed_sec", 0),
                        mode="全量",
                    )
                    print("[kline-cache] feishu notification sent")
                except Exception as notify_exc:
                    print(f"[kline-cache] feishu notify failed: {notify_exc}")
        except Exception as exc:
            print(f"[kline-cache] error: {exc}")
        await asyncio.sleep(600)
