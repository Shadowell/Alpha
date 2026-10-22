"""应用服务容器 — 集中装配服务、后台循环与生命周期。

main.py 只保留 FastAPI 实例、中间件、router include 与静态挂载；
所有服务实例在 lifespan 中通过 `build_container()` 构造，避免 import 时副作用。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.services.backtest_lab import BacktestLab
from app.services.custom_strategy import (
    BUILTIN_STRATEGIES,
    CustomStrategyScanner,
)
from app.services.data_provider import AkshareDataProvider
from app.services.first_limit_alpha_service import FirstLimitAlphaService
from app.services.funnel_service import FunnelService
from app.services.hermes_memory import HermesMemory
from app.services.hermes_runtime import HermesRuntime
from app.services.hot_stock_ai_service import HotStockAIService
from app.services.job_store import JobStore
from app.services.paths import DATA_DIR
from app.services.jobs import JobManager
from app.services.kline_cache_service import KlineCacheService
from app.services.kline_store import KlineSQLiteStore
from app.services.kronos_predict_service import KronosPredictService
from app.services.limit_board_service import LimitBoardService
from app.services.market_data_client import EastmoneyMarketDataClient
from app.services.notice_service import NoticeService
from app.services.observability import BackgroundTaskRegistry
from app.services.paper_trading import PaperTradingService
from app.services.predict_funnel_service import PredictFunnelService
from app.services.quiet_breakout_scanner import QuietBreakoutScanner
from app.services.realtime import RealtimeHub
from app.services.time_utils import now_cn
from app.services.tradingagents_adapter import TradingAgentsAdapter
from app.services.tushare_market_data_client import TushareFirstMarketDataClient

log = logging.getLogger("container")

DATA_DIR_ENV = "ALPHA_DATA_DIR"

# 兼容旧脚本 `from app.main import paper_trading` 等别名
COMPAT_ALIASES = {
    "provider",
    "eastmoney_market_data_client",
    "market_data_client",
    "kline_cache_service",
    "service",
    "notice_service",
    "hub",
    "kronos_service",
    "paper_trading",
    "predict_funnel_service",
    "tradingagents_adapter",
    "hot_stock_ai_service",
    "first_limit_alpha_service",
    "quiet_breakout_scanner",
    "custom_strategy_scanner",
    "hermes_memory",
    "hermes_runtime",
    "backtest_lab",
    "job_manager",
    "job_store",
}
_COMPAT_ATTR_MAP = {"service": "funnel_service"}


def resolve_compat_attr(app: Any, name: str) -> Any:
    """解析旧别名；未启动时惰性构建容器（不启动后台循环），兼容脚本 import。"""
    if name not in COMPAT_ALIASES:
        raise AttributeError(f"module 'app.main' has no attribute {name!r}")
    container = getattr(app.state, "container", None)
    if container is None:
        container = build_container()
        app.state.container = container
    return getattr(container, _COMPAT_ATTR_MAP.get(name, name))


# 后台循环任务在 app.state 上的键；health 检查与 shutdown 共用。
LOOP_TASK_KEYS = (
    "backfill_task",
    "ticker_task",
    "kline_cache_task",
    "minute_sync_task",
    "hermes_task",
    "monitor_task",
    "predict_funnel_task",
    "hot_stock_ai_task",
)


def resolve_data_dir(data_dir: str | Path | None = None) -> Path:
    raw = data_dir or os.getenv(DATA_DIR_ENV, "").strip()
    resolved = Path(raw).expanduser().resolve() if raw else DATA_DIR
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _env_flag(name: str, default: bool) -> bool:
    """读取布尔开关环境变量；未设置时用默认值，其余按 0/false/no/off 判关闭。"""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


@dataclass
class ServiceContainer:
    data_dir: Path
    provider: AkshareDataProvider
    eastmoney_market_data_client: EastmoneyMarketDataClient
    market_data_client: TushareFirstMarketDataClient
    kline_store: KlineSQLiteStore
    kline_cache_service: KlineCacheService
    funnel_service: FunnelService
    notice_service: NoticeService
    hub: RealtimeHub
    kronos_service: KronosPredictService
    paper_trading: PaperTradingService
    predict_funnel_service: PredictFunnelService
    tradingagents_adapter: TradingAgentsAdapter
    hot_stock_ai_service: HotStockAIService
    first_limit_alpha_service: FirstLimitAlphaService
    quiet_breakout_scanner: QuietBreakoutScanner
    custom_strategy_scanner: CustomStrategyScanner
    hermes_memory: HermesMemory
    hermes_runtime: HermesRuntime
    backtest_lab: BacktestLab
    job_store: JobStore
    job_manager: JobManager
    background_tasks: BackgroundTaskRegistry
    # 打板数据底座（issue #22）；Tushare 未配置时依然可用（自算降级）。
    limit_board_service: LimitBoardService | None = None

    # ── 路径/健康辅助 ──

    @property
    def funnel_db_path(self) -> Path:
        return self.data_dir / "funnel_state.db"

    @property
    def kline_db_path(self) -> Path:
        return self.data_dir / "market_kline.db"

    @property
    def jobs_db_path(self) -> Path:
        return self.data_dir / "jobs.db"

    @property
    def db_paths(self) -> list[Path]:
        return [self.funnel_db_path, self.kline_db_path, self.jobs_db_path]

    # ── 数据广播 ──

    async def broadcast_snapshot(self) -> None:
        funnel = await self.funnel_service.get_funnel()
        hot = await self.funnel_service.get_hot_concepts()
        hot_stocks = await self.funnel_service.get_hot_stocks()
        await self.hub.broadcast(
            "snapshot",
            {
                "funnel": funnel.model_dump(),
                "hot_concepts": hot.model_dump(),
                "hot_stocks": hot_stocks.model_dump(),
            },
        )

    # ── 启动/关闭 ──

    async def startup(self) -> None:
        inserted = 0
        try:
            inserted = self.funnel_service.state_store.ensure_builtin_custom_strategies(
                [s.to_dict() for s in BUILTIN_STRATEGIES]
            )
        except Exception as exc:  # noqa: BLE001 — 启动期数据迁移失败不应阻断服务
            log.exception("ensure builtin strategies failed: %s", exc)
        if inserted:
            log.info("inserted %d builtin custom strategies", inserted)
        self.job_manager.recover()
        self._spawn_capability_probe()

    def _spawn_capability_probe(self) -> None:
        """启动后延迟探测 Tushare 打板接口权限（只读探测，不做全量回填）。

        未配置 Tushare 时探测不会发起任何网络请求，只把矩阵标记为
        ``not_configured``，保证状态接口在本地/CI 环境也可读。
        """
        service = self.limit_board_service
        if service is None or not service.is_enabled:
            return
        if not _env_flag("ALPHA_LIMIT_BOARD_PROBE_ON_STARTUP", True):
            return

        async def _probe() -> None:
            try:
                await asyncio.sleep(5)
                matrix = await service.probe_capabilities(force=True)
                summary = {
                    str(item.get("api")): str(item.get("capability"))
                    for item in (matrix.get("apis") or [])
                    if isinstance(item, dict)
                }
                log.info("tushare limit-board capability matrix: %s", summary)
            except Exception as exc:  # noqa: BLE001 — 探测失败不影响服务启动
                log.warning(
                    "limit board capability probe failed: %s", type(exc).__name__
                )

        self.background_tasks.spawn(_probe(), name="limit-board-capability-probe")

    async def shutdown(self, app_state: Any) -> None:
        # 顺序：先取消并等待循环/散任务 → 停 JobManager → WAL checkpoint，
        # 防止 checkpoint 与活跃写入竞态导致 macOS UEs。
        for key in LOOP_TASK_KEYS:
            task = getattr(app_state, key, None)
            if task is None:
                continue
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await self.background_tasks.shutdown()
        await self.job_manager.shutdown()
        await asyncio.to_thread(checkpoint_all_sqlite, [str(p) for p in self.db_paths])

    # ── 后台循环 ──

    async def backfill_names(self) -> None:
        try:
            fixed = await self.funnel_service.backfill_names()
            if fixed:
                log.info("backfilled %d stock names", fixed)
        except Exception as exc:  # noqa: BLE001 — 启动回填失败不影响主流程
            log.warning("backfill_names failed: %s", exc)

    async def ticker_loop(self) -> None:
        await asyncio.sleep(5)
        while True:
            try:
                changed = await self.funnel_service.tick()
                if changed:
                    await self.broadcast_snapshot()
                # 实时更新模拟盘持仓价格（复用已缓存的 snapshot）
                try:
                    snapshot = self.provider.realtime_snapshot_cache
                    if snapshot is not None:
                        _, df = snapshot
                        if (
                            not df.empty
                            and "代码" in df.columns
                            and "最新价" in df.columns
                        ):
                            price_map = dict(
                                zip(df["代码"].astype(str), df["最新价"].astype(float))
                            )
                            self.paper_trading.update_prices(price_map)
                except Exception as exc:  # noqa: BLE001 — 持仓价格更新失败不阻断 ticker
                    log.warning("paper price update failed: %s", exc)
            except Exception as exc:  # noqa: BLE001 — 循环必须存活
                log.exception("ticker loop error: %s", exc)
            await asyncio.sleep(60)

    async def predict_funnel_scheduler_loop(self) -> None:
        """收盘后 16:15 自动触发预测选股扫描（每日一次）。"""
        await asyncio.sleep(60)
        last_run_date: str | None = None
        while True:
            try:
                service = self.predict_funnel_service
                cfg = service.get_config()
                if cfg.get("auto_after_close"):
                    n = now_cn()
                    today = n.date().isoformat()
                    if n.hour == 16 and 15 <= n.minute < 30 and last_run_date != today:
                        snap = service.get_snapshot()
                        if (snap.get("trade_date") != today) or not snap.get("entries"):
                            log.info(
                                "predict funnel scheduled auto run at %s", n.isoformat()
                            )
                            try:
                                await service.run(trigger="auto")
                                last_run_date = today
                            except Exception as exc:  # noqa: BLE001 — 单次调度失败下次重试
                                log.exception(
                                    "predict funnel scheduled run failed: %s", exc
                                )
                        else:
                            last_run_date = today
            except Exception as exc:  # noqa: BLE001 — 调度循环必须存活
                log.exception("predict funnel scheduler error: %s", exc)
            await asyncio.sleep(300)

    async def hot_stock_ai_scheduler_loop(self) -> None:
        """热门股票智能分析自动刷新，每 60s 检查一次是否到达配置刷新窗口。"""
        await asyncio.sleep(90)
        while True:
            try:
                service = self.hot_stock_ai_service
                cfg = service.get_config()
                if (
                    cfg.get("auto_refresh_enabled")
                    and not service.running
                    and service.is_stale()
                ):
                    log.info("hot stock ai scheduled run at %s", now_cn().isoformat())

                    async def _run_auto() -> None:
                        try:
                            await service.run(trigger="auto")
                        except Exception as exc:  # noqa: BLE001 — 自动任务失败仅记录
                            log.exception("hot stock ai scheduled run failed: %s", exc)

                    self.background_tasks.spawn(_run_auto(), name="hot-stock-ai-auto")
            except Exception as exc:  # noqa: BLE001 — 调度循环必须存活
                log.exception("hot stock ai scheduler error: %s", exc)
            await asyncio.sleep(60)


def _build_name_lookup(provider: AkshareDataProvider, kline_store: KlineSQLiteStore):
    def _lookup(symbol: str) -> str:
        try:
            if provider.symbol_name_cache is not None:
                _, name_map = provider.symbol_name_cache
                if name_map and symbol in name_map:
                    return name_map[symbol]
        except Exception as exc:  # noqa: BLE001 — 名称缺失降级为未命名
            log.debug("symbol name cache lookup failed for %s: %s", symbol, exc)
        try:
            return kline_store.load_symbol_names().get(symbol, "")
        except Exception as exc:  # noqa: BLE001
            log.debug("symbol name db lookup failed for %s: %s", symbol, exc)
            return ""

    return _lookup


def checkpoint_all_sqlite(paths: list[str]) -> None:
    """对所有 SQLite 库执行 WAL checkpoint（shutdown 时在后台线程调用）。"""
    for path in paths:
        if not Path(path).exists():
            continue
        try:
            conn = sqlite3.connect(path, timeout=5)
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.close()
        except Exception as exc:  # noqa: BLE001 — 单个库 checkpoint 失败不阻断关闭
            log.warning("checkpoint %s failed (non-fatal): %s", path, exc)


def build_container(data_dir: str | Path | None = None) -> ServiceContainer:
    directory = resolve_data_dir(data_dir)

    kline_store = KlineSQLiteStore(str(directory / "market_kline.db"))
    provider = AkshareDataProvider(kline_store=kline_store)
    eastmoney = EastmoneyMarketDataClient(store=kline_store)
    market_data_client = TushareFirstMarketDataClient(fallback=eastmoney)
    limit_board_service = LimitBoardService(
        store=kline_store, market_data_client=market_data_client
    )
    kline_cache_service = KlineCacheService(
        provider=provider,
        store=kline_store,
        market_data_client=market_data_client,
        limit_board_service=limit_board_service,
    )
    funnel_service = FunnelService(
        provider=provider,
        kline_cache_service=kline_cache_service,
        persist_db_path=str(directory / "funnel_state.db"),
        legacy_json_path=str(directory / "funnel_state.json"),
    )
    notice_service = NoticeService(
        state_store=funnel_service.state_store,
        kline_cache_service=kline_cache_service,
        provider=provider,
    )
    hub = RealtimeHub()
    kronos_service = KronosPredictService(kline_store=kline_store, provider=provider)
    paper_trading = PaperTradingService(directory / "funnel_state.db")
    predict_funnel_service = PredictFunnelService(
        provider=provider,
        kronos_service=kronos_service,
        state_store=funnel_service.state_store,
    )
    tradingagents_adapter = TradingAgentsAdapter()
    hot_stock_ai_service = HotStockAIService(
        provider=provider,
        kline_store=kline_store,
        kronos_service=kronos_service,
        state_store=funnel_service.state_store,
        tradingagents_adapter=tradingagents_adapter,
    )
    first_limit_alpha_service = FirstLimitAlphaService(
        kline_store=kline_store,
        provider=provider,
        state_store=funnel_service.state_store,
        artifact_root=directory / "first_limit_alpha",
    )

    name_lookup = _build_name_lookup(provider, kline_store)
    quiet_breakout_scanner = QuietBreakoutScanner(
        kline_store=kline_store, name_lookup=name_lookup
    )
    custom_strategy_scanner = CustomStrategyScanner(
        kline_store=kline_store,
        name_lookup=name_lookup,
        state_store=funnel_service.state_store,
    )
    backtest_lab = BacktestLab(kline_store=kline_store, name_lookup=name_lookup)

    hermes_memory = HermesMemory(str(directory / "funnel_state.db"))
    hermes_runtime = HermesRuntime(
        memory=hermes_memory,
        funnel_service=funnel_service,
        notice_service=notice_service,
        kline_cache_service=kline_cache_service,
        kronos_service=kronos_service,
    )

    job_store = JobStore(directory / "jobs.db")
    job_manager = JobManager(job_store)

    # 显式初始化行情库 schema，保证空库场景下 health 可读。
    try:
        kline_store.get_sync_state()
    except Exception as exc:  # noqa: BLE001 — 空库初始化失败留给具体请求报错
        log.warning("kline schema init failed: %s", exc)

    return ServiceContainer(
        data_dir=directory,
        provider=provider,
        eastmoney_market_data_client=eastmoney,
        market_data_client=market_data_client,
        kline_store=kline_store,
        kline_cache_service=kline_cache_service,
        funnel_service=funnel_service,
        notice_service=notice_service,
        hub=hub,
        kronos_service=kronos_service,
        paper_trading=paper_trading,
        predict_funnel_service=predict_funnel_service,
        tradingagents_adapter=tradingagents_adapter,
        hot_stock_ai_service=hot_stock_ai_service,
        first_limit_alpha_service=first_limit_alpha_service,
        quiet_breakout_scanner=quiet_breakout_scanner,
        custom_strategy_scanner=custom_strategy_scanner,
        hermes_memory=hermes_memory,
        hermes_runtime=hermes_runtime,
        backtest_lab=backtest_lab,
        job_store=job_store,
        job_manager=job_manager,
        background_tasks=BackgroundTaskRegistry(),
        limit_board_service=limit_board_service,
    )
