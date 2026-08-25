"""K 线同步模块 — 缓存同步、定时任务。

路由前缀: /api  (由 main.py include 时指定)
"""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from fastapi import APIRouter, HTTPException, status

from app.services.data_provider import AkshareDataProvider, normalize_symbol
from app.services.kline_cache_service import KlineCacheService

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


def _checkpoint_sqlite_for_shutdown() -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    for rel in ("data/funnel_state.db", "data/market_kline.db"):
        path = Path(rel)
        if not path.exists():
            continue
        try:
            conn = sqlite3.connect(str(path), timeout=1.0)
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.close()
            results.append({"path": rel, "status": "ok"})
        except Exception as exc:
            results.append({"path": rel, "status": "failed", "error": str(exc)})
    return results


@router.post("/admin/shutdown-prepare")
async def admin_shutdown_prepare(reason: str = "restart"):
    return {
        "success": True,
        "reason": reason,
        "sqlite": await asyncio.to_thread(_checkpoint_sqlite_for_shutdown),
    }


@router.post("/jobs/kline-cache/sync", status_code=status.HTTP_202_ACCEPTED)
async def run_kline_cache_sync(
    trade_date: str | None = None,
    force: bool = False,
    trigger_mode: str = "manual",
    window_days: int | None = None,
):
    payload = _kline_cache_service.enqueue_sync_trade_date(
        trade_date=trade_date, force=force, trigger_mode=trigger_mode, window_days=window_days,
    )
    return payload


@router.post("/jobs/kline-cache/incremental-sync", status_code=status.HTTP_202_ACCEPTED)
async def run_kline_incremental_sync(
    trade_date: str | None = None,
    trigger_mode: str = "manual",
):
    payload = _kline_cache_service.enqueue_incremental_sync(
        trade_date=trade_date, trigger_mode=trigger_mode,
    )
    return payload


@router.post("/jobs/kline-cache/batch-incremental-sync", status_code=status.HTTP_202_ACCEPTED)
async def run_kline_batch_incremental_sync(
    start_date: str,
    end_date: str,
    trigger_mode: str = "manual",
):
    payload = await _kline_cache_service.enqueue_incremental_range(
        start_date=start_date,
        end_date=end_date,
        trigger_mode=trigger_mode,
    )
    if not payload.get("success"):
        raise HTTPException(status_code=400, detail=payload.get("message", "批量同步提交失败"))
    return payload


@router.post("/jobs/kline-cache/initialize", status_code=status.HTTP_202_ACCEPTED)
async def initialize_kline_cache(window_days: int = 120):
    """冷启动一键初始化：回补全市场近 N 个自然日的K线历史。

    空库时前端引导入口调用；复用 full 同步队列，进度/日志与数据中心现有展示兼容。
    """
    try:
        stats = await asyncio.to_thread(_kline_cache_service.get_stats)
    except Exception:
        stats = {}
    window = max(30, min(int(window_days), 1095))
    payload = _kline_cache_service.enqueue_sync_trade_date(
        trade_date=None,
        force=True,
        trigger_mode="initialize",
        window_days=window,
    )
    return {
        "already_initialized": int(stats.get("row_count") or 0) > 0,
        "window_days": window,
        **payload,
    }


@router.get("/jobs/kline-cache/status")
async def get_kline_cache_status():
    return _kline_cache_service.get_sync_state()


@router.get("/jobs/kline-cache/data-source")
async def get_kline_data_source_status():
    return _kline_cache_service.get_data_source_status()


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


@router.get("/jobs/kline-cache/stats")
async def get_kline_cache_stats():
    return _kline_cache_service.get_stats()


@router.post("/jobs/kline-cache/check")
async def run_kline_data_check():
    report = await _kline_cache_service.check_data_integrity(days=30)
    return report


@router.get("/jobs/kline-cache/report")
async def get_kline_check_report():
    report = _kline_cache_service.get_latest_check_report()
    if report is None:
        return {"status": "none", "message": "暂无检查报告"}
    return report


# ── K 线查询路由 ──────────────────────────────────────────


@router.get("/kline/{symbol}")
async def get_cached_kline(symbol: str, days: int = 30):
    clean_symbol = normalize_symbol(symbol)
    items = _kline_cache_service.get_kline(symbol=clean_symbol, days=days)
    return {
        "symbol": clean_symbol,
        "days": max(1, min(days, 365)),
        "count": len(items),
        "items": items,
    }


# ── 后台定时同步任务 ──────────────────────────────────────


async def kline_cache_loop(kline_cache_service: KlineCacheService) -> None:
    """后台循环：每 10 分钟检测是否需要自动同步 K 线；每天 20 点后自动备份两个 SQLite 库。"""
    await asyncio.sleep(10)
    last_backup_date: str = ""
    while True:
        try:
            result = await kline_cache_service.run_if_due()
            if result is not None:
                print(f"[kline-cache] daily sync completed: {result.get('message')}")

                report = None
                try:
                    report = await kline_cache_service.check_data_integrity(days=30)
                    print(f"[kline-cache] integrity check: {report.get('status')} coverage={report.get('coverage_pct')}%")
                except Exception as chk_exc:
                    print(f"[kline-cache] integrity check failed: {chk_exc}")

                print("[kline-cache] sync completion notification skipped")
        except Exception as exc:
            print(f"[kline-cache] error: {exc}")

        # 每日备份：funnel_state 曾因容器异常退出损坏且无备份可回滚，
        # 故每天收盘后对两个库做一次 sqlite backup API 热备，保留最近 7 份。
        try:
            from app.services.time_utils import now_cn as _now_cn

            today = _now_cn().date().isoformat()
            if _now_cn().hour >= 20 and last_backup_date != today:
                done = await asyncio.to_thread(_backup_all_sqlite)
                last_backup_date = today
                if done:
                    print(f"[kline-cache] daily sqlite backup done: {done}")
        except Exception as exc:
            print(f"[kline-cache] daily backup failed: {exc}")

        await asyncio.sleep(600)


def _backup_all_sqlite() -> list[str]:
    import glob as _glob
    import os as _os
    import sqlite3 as _sqlite3

    from app.services.time_utils import now_cn

    backed_up: list[str] = []
    backup_dir = "data/backups"
    _os.makedirs(backup_dir, exist_ok=True)
    for src in ("data/funnel_state.db", "data/market_kline.db"):
        if not _os.path.exists(src):
            continue
        stem = _os.path.splitext(_os.path.basename(src))[0]
        dst = _os.path.join(backup_dir, f"{stem}-{now_cn().strftime('%Y%m%d-%H%M%S')}.db")
        try:
            conn = _sqlite3.connect(src, timeout=30)
            dest = _sqlite3.connect(dst)
            with dest:
                conn.backup(dest)
            dest.close()
            conn.close()
            backed_up.append(dst)
            # 每个库只保留最近 7 份
            olds = sorted(_glob.glob(_os.path.join(backup_dir, f"{stem}-*.db")))
            for old in olds[:-7]:
                try:
                    _os.remove(old)
                except OSError:
                    pass
        except Exception as exc:
            print(f"[kline-cache] backup {src} failed: {exc}")
    return backed_up
