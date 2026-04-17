"""缩量启动 + 首板放量涨停 形态扫描。

核心形态（对应图中例子）：
  - 前 N 天（默认 25）缩量横盘：
      * 振幅：(max_high - min_low) / mean_close <= amp_threshold（默认 20%）
      * 缩量：前 N 日成交量 std / mean <= vol_cv_threshold（默认 0.4）
  - 第 N+1 天（最新一天）突破：
      * 涨停：close / prev_close - 1 >= 涨停阈值（主板 9.7%、创业/科创 19.7%、ST 4.7%）
      * 放量：当日 volume / 前 N 日 mean_volume >= vol_spike_ratio（默认 3.0）

输出每只候选股包含：横盘振幅、缩量 CV、当日涨幅、放量倍数、close、signals 等，
便于前端排序/筛选。
"""
from __future__ import annotations

import asyncio
import logging
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.services.kline_store import KlineSQLiteStore

log = logging.getLogger("quiet_breakout")


@dataclass
class QuietBreakoutConfig:
    lookback_days: int = 25
    amp_threshold: float = 0.20
    vol_cv_threshold: float = 0.40
    vol_spike_ratio: float = 3.0
    require_limit_up: bool = True
    exclude_st: bool = True
    limit_tolerance: float = 0.003  # 允许略低于涨停 0.3%（收盘打板前秒回落）


def _limit_pct(symbol: str, name: str) -> float:
    """返回涨停阈值（小数形式）。"""
    sym = (symbol or "").strip()
    nm = (name or "").upper()
    if "ST" in nm:
        return 0.05
    if sym.startswith("30") or sym.startswith("688"):
        return 0.20
    return 0.10


def _is_a_main(symbol: str) -> bool:
    s = (symbol or "").strip()
    if len(s) != 6:
        return False
    return s[:2] in ("00", "30", "60", "68")


@dataclass
class QuietBreakoutHit:
    symbol: str
    name: str
    trade_date: str
    close: float
    prev_close: float
    change_pct: float            # 最新一天真实涨幅 (%)
    limit_pct: float             # 该股涨停阈值 (%)
    is_limit_up: bool
    amp_pct: float               # 横盘振幅 (%)
    vol_cv: float                # 前 N 日成交量变异系数（std/mean）
    vol_spike: float             # 当日量 / 前 N 日均量
    base_high: float             # 前 N 日最高价
    base_low: float              # 前 N 日最低价
    score: float = 0.0           # 综合分（放量倍数主导）
    signals: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "trade_date": self.trade_date,
            "close": round(self.close, 2),
            "prev_close": round(self.prev_close, 2),
            "change_pct": round(self.change_pct, 2),
            "limit_pct": round(self.limit_pct, 2),
            "is_limit_up": self.is_limit_up,
            "amp_pct": round(self.amp_pct, 2),
            "vol_cv": round(self.vol_cv, 3),
            "vol_spike": round(self.vol_spike, 2),
            "base_high": round(self.base_high, 2),
            "base_low": round(self.base_low, 2),
            "score": round(self.score, 2),
            "signals": self.signals,
        }


def _evaluate(kline: list[dict], symbol: str, name: str, cfg: QuietBreakoutConfig) -> QuietBreakoutHit | None:
    n = cfg.lookback_days
    if len(kline) < n + 1:
        return None

    base = kline[-(n + 1):-1]  # 前 N 日横盘窗口
    today = kline[-1]

    closes = [float(r["close"]) for r in base if r.get("close") is not None]
    highs = [float(r["high"]) for r in base if r.get("high") is not None]
    lows = [float(r["low"]) for r in base if r.get("low") is not None]
    vols = [float(r["volume"]) for r in base if r.get("volume") is not None]
    if len(closes) < n or not vols or min(closes) <= 0:
        return None

    mean_close = sum(closes) / len(closes)
    base_high = max(highs)
    base_low = min(lows)
    amp_pct = (base_high - base_low) / mean_close * 100.0

    mean_vol = sum(vols) / len(vols)
    if mean_vol <= 0:
        return None
    std_vol = statistics.pstdev(vols)
    vol_cv = std_vol / mean_vol

    prev_close = float(base[-1]["close"])
    today_close = float(today.get("close") or 0)
    today_vol = float(today.get("volume") or 0)
    if prev_close <= 0 or today_close <= 0 or today_vol <= 0:
        return None

    change_pct = (today_close / prev_close - 1.0) * 100.0
    vol_spike = today_vol / mean_vol
    lim = _limit_pct(symbol, name)
    is_limit_up = change_pct >= (lim * 100.0 - cfg.limit_tolerance * 100.0)

    if amp_pct > cfg.amp_threshold * 100.0:
        return None
    if vol_cv > cfg.vol_cv_threshold:
        return None
    if vol_spike < cfg.vol_spike_ratio:
        return None
    if cfg.require_limit_up and not is_limit_up:
        return None

    signals: list[str] = []
    signals.append(f"横盘{n}天 振幅{amp_pct:.1f}%")
    signals.append(f"量能CV={vol_cv:.2f}")
    signals.append(f"放量{vol_spike:.1f}倍")
    if is_limit_up:
        signals.append(f"涨停({lim*100:.0f}cm)")

    score = vol_spike * 1.0 + (cfg.vol_cv_threshold - vol_cv) * 10.0 + (cfg.amp_threshold * 100 - amp_pct) * 0.5
    if is_limit_up:
        score += 5.0

    return QuietBreakoutHit(
        symbol=symbol,
        name=name,
        trade_date=str(today.get("date") or ""),
        close=today_close,
        prev_close=prev_close,
        change_pct=change_pct,
        limit_pct=lim * 100.0,
        is_limit_up=is_limit_up,
        amp_pct=amp_pct,
        vol_cv=vol_cv,
        vol_spike=vol_spike,
        base_high=base_high,
        base_low=base_low,
        score=score,
        signals=signals,
    )


class QuietBreakoutScanner:
    def __init__(self, kline_store: KlineSQLiteStore, name_lookup=None) -> None:
        self.store = kline_store
        self._name_lookup = name_lookup  # callable(symbol) -> name（可选）
        self.lock = asyncio.Lock()
        self.last_snapshot: dict[str, Any] = {
            "generated_at": None,
            "config": None,
            "total_scanned": 0,
            "total_hits": 0,
            "hits": [],
            "running": False,
        }

    async def scan(self, cfg: QuietBreakoutConfig | None = None, limit: int | None = None) -> dict[str, Any]:
        cfg = cfg or QuietBreakoutConfig()
        async with self.lock:
            self.last_snapshot["running"] = True
            try:
                return await self._scan_impl(cfg, limit)
            finally:
                self.last_snapshot["running"] = False

    async def _scan_impl(self, cfg: QuietBreakoutConfig, limit: int | None) -> dict[str, Any]:
        t0 = datetime.now()
        symbols = await asyncio.to_thread(self.store.get_all_symbols)
        symbols = [s for s in symbols if _is_a_main(s)]
        if cfg.exclude_st:
            pass  # ST 靠 _limit_pct + name 处理；一般 exchange code 无法直接过滤
        if limit and limit > 0:
            symbols = symbols[:limit]

        hits: list[QuietBreakoutHit] = []
        need_days = cfg.lookback_days + 3

        def _work(sym: str) -> QuietBreakoutHit | None:
            try:
                rows = self.store.get_kline(sym, days=need_days)
                if len(rows) < cfg.lookback_days + 1:
                    return None
                name = ""
                if self._name_lookup is not None:
                    try:
                        name = self._name_lookup(sym) or ""
                    except Exception:
                        name = ""
                if cfg.exclude_st and "ST" in name.upper():
                    return None
                return _evaluate(rows, sym, name, cfg)
            except Exception as exc:
                log.debug("scan %s failed: %s", sym, exc)
                return None

        sem = asyncio.Semaphore(16)

        async def _bounded(sym: str):
            async with sem:
                return await asyncio.to_thread(_work, sym)

        tasks = [asyncio.create_task(_bounded(s)) for s in symbols]
        for fut in asyncio.as_completed(tasks):
            result = await fut
            if result is not None:
                hits.append(result)

        hits.sort(key=lambda h: (-h.score, -h.vol_spike))
        payload = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "config": {
                "lookback_days": cfg.lookback_days,
                "amp_threshold": cfg.amp_threshold,
                "vol_cv_threshold": cfg.vol_cv_threshold,
                "vol_spike_ratio": cfg.vol_spike_ratio,
                "require_limit_up": cfg.require_limit_up,
            },
            "total_scanned": len(symbols),
            "total_hits": len(hits),
            "elapsed_seconds": round((datetime.now() - t0).total_seconds(), 2),
            "hits": [h.to_dict() for h in hits],
            "running": False,
        }
        self.last_snapshot = payload
        log.info(
            "quiet_breakout: scanned=%d hits=%d elapsed=%.2fs",
            len(symbols), len(hits), payload["elapsed_seconds"],
        )
        return payload

    def get_snapshot(self) -> dict[str, Any]:
        return dict(self.last_snapshot)
