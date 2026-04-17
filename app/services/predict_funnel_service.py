"""预测选股服务 — 基于概念板块热门 Top K × 成分股 Top M × Kronos 三日预测。

工作流：
  1. 获取概念板块列表 → 按涨跌幅取热门 Top K（默认 10）
  2. 每个板块取涨幅领先 Top M（默认 10）成分股
  3. 全量去重 → 调用 KronosPredictService.predict() 获取未来 3 日 K 线
  4. 计算 pred_max_high_pct = (max(pred.high) − 今收) / 今收
  5. 按阈值分级到三池：候选池 / 重点关注池 / 买入池
  6. 持久化到 funnel_state.db 的 predict_funnel 键
  7. 如启用推送，向飞书发送 Top 10 摘要

阈值（激进）：候选 ≥2% / 重点 ≥4% / 买入 ≥8%
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import pandas as pd

from app.services.data_provider import AkshareDataProvider
from app.services.feishu_notify import send_feishu_text
from app.services.kronos_predict_service import KronosPredictService
from app.services.sqlite_store import SQLiteStateStore
from app.services.time_utils import now_cn

log = logging.getLogger(__name__)

POOL_CANDIDATE = "candidate"
POOL_FOCUS = "focus"
POOL_BUY = "buy"

DEFAULT_CONFIG: dict[str, Any] = {
    "top_k_boards": 10,
    "top_m_stocks": 10,
    "threshold_candidate": 2.0,
    "threshold_focus": 4.0,
    "threshold_buy": 8.0,
    "horizon": 3,
    "lookback": 30,
    "feishu_enabled": True,
    "auto_after_close": True,
}

STATE_KEY = "predict_funnel"


class PredictFunnelService:
    def __init__(
        self,
        provider: AkshareDataProvider,
        kronos_service: KronosPredictService,
        state_store: SQLiteStateStore,
    ) -> None:
        self.provider = provider
        self.kronos = kronos_service
        self.state_store = state_store
        self.lock = asyncio.Lock()
        self.running: bool = False
        self.progress: dict[str, Any] = {
            "phase": "idle",
            "current": 0,
            "total": 0,
            "detail": "",
            "started_at": None,
            "finished_at": None,
        }
        self._snapshot: dict[str, Any] = self._load_state() or {
            "trade_date": now_cn().date().isoformat(),
            "entries": [],
            "pools": {POOL_CANDIDATE: [], POOL_FOCUS: [], POOL_BUY: []},
            "updated_at": now_cn().isoformat(),
            "config": dict(DEFAULT_CONFIG),
            "meta": {},
        }

    def _load_state(self) -> dict[str, Any] | None:
        try:
            raw = self.state_store.get_kv(STATE_KEY)
            if raw:
                return raw
        except Exception as exc:
            log.warning("[predict_funnel] load state failed: %s", exc)
        return None

    def _save_state(self) -> None:
        try:
            self.state_store.set_kv(STATE_KEY, self._snapshot)
        except Exception as exc:
            log.warning("[predict_funnel] save state failed: %s", exc)

    def get_config(self) -> dict[str, Any]:
        cfg = dict(DEFAULT_CONFIG)
        cfg.update(self._snapshot.get("config", {}))
        return cfg

    def update_config(self, patch: dict[str, Any]) -> dict[str, Any]:
        cfg = self.get_config()
        for k, v in patch.items():
            if k in DEFAULT_CONFIG:
                cfg[k] = v
        self._snapshot["config"] = cfg
        self._save_state()
        return cfg

    def get_snapshot(self) -> dict[str, Any]:
        payload = dict(self._snapshot)
        payload["progress"] = dict(self.progress)
        payload["running"] = self.running
        return payload

    async def run(self, trigger: str = "manual") -> dict[str, Any]:
        if self.running:
            return {"ok": False, "error": "已有预测任务在执行", "snapshot": self.get_snapshot()}
        async with self.lock:
            self.running = True
            self.progress = {
                "phase": "init",
                "current": 0,
                "total": 0,
                "detail": "准备热门板块",
                "started_at": now_cn().isoformat(),
                "finished_at": None,
            }
            try:
                await self._execute(trigger)
            finally:
                self.progress["finished_at"] = now_cn().isoformat()
                self.progress["phase"] = "done" if not self.progress.get("error") else "error"
                self.running = False
        return {"ok": True, "snapshot": self.get_snapshot()}

    async def _execute(self, trigger: str) -> None:
        cfg = self.get_config()
        top_k = int(cfg["top_k_boards"])
        top_m = int(cfg["top_m_stocks"])
        horizon = int(cfg["horizon"])
        lookback = int(cfg["lookback"])
        t0 = time.time()
        concept_board_source = "em"

        self.progress.update(phase="boards", detail=f"获取东方财富概念板块 Top{top_k}")
        try:
            concepts, concept_board_source = await self.provider.fetch_concept_board_names_em()
        except Exception as exc:
            self.progress["error"] = f"概念板块获取失败: {exc}"
            return
        if concepts is None or concepts.empty:
            self.progress["error"] = "东方财富概念板块数据为空"
            return
        name_col = "板块名称" if "板块名称" in concepts.columns else ("name" if "name" in concepts.columns else None)
        pct_col = "涨跌幅" if "涨跌幅" in concepts.columns else None
        if not name_col:
            self.progress["error"] = f"概念板块缺少名称列: {list(concepts.columns)[:8]}"
            return
        concepts = concepts.copy()
        if pct_col:
            concepts[pct_col] = pd.to_numeric(concepts[pct_col], errors="coerce").fillna(0.0)
            concepts = concepts.sort_values(pct_col, ascending=False)
        hot_boards = concepts.head(top_k).copy()
        hot_boards = hot_boards.rename(columns={name_col: "板块名称"})
        if pct_col and pct_col != "涨跌幅":
            hot_boards = hot_boards.rename(columns={pct_col: "涨跌幅"})

        self.progress.update(phase="constituents", detail=f"拉取 {len(hot_boards)} 个板块成分股")
        stock_pool: dict[str, dict[str, Any]] = {}
        for _, brow in hot_boards.iterrows():
            board_name = str(brow.get("板块名称", "")).strip()
            if not board_name:
                continue
            board_change = float(pd.to_numeric(brow.get("涨跌幅", 0), errors="coerce") or 0.0)
            try:
                cons = await self.provider.get_concept_constituents(board_name, fetch_if_missing=True)
            except Exception as exc:
                log.warning("[predict_funnel] cons fail %s: %s", board_name, exc)
                cons = pd.DataFrame()
            if cons is None or cons.empty:
                continue
            if "涨跌幅" in cons.columns:
                cons = cons.copy()
                cons["涨跌幅"] = pd.to_numeric(cons["涨跌幅"], errors="coerce").fillna(0.0)
                cons = cons.sort_values("涨跌幅", ascending=False)
            cons_top = cons.head(top_m)
            for _, srow in cons_top.iterrows():
                code = str(srow.get("代码") or srow.get("symbol") or "").strip()
                sname = str(srow.get("名称") or srow.get("name") or "").strip()
                if not code or len(code) != 6:
                    continue
                if code[:2] not in ("00", "30", "60", "68"):
                    continue
                if "ST" in sname or "*" in sname or "退" in sname:
                    continue
                cur_price = float(pd.to_numeric(srow.get("最新价", 0), errors="coerce") or 0.0)
                cur_pct = float(pd.to_numeric(srow.get("涨跌幅", 0), errors="coerce") or 0.0)
                if code not in stock_pool:
                    stock_pool[code] = {
                        "symbol": code,
                        "name": sname,
                        "boards": [],
                        "current_price": cur_price,
                        "current_pct": cur_pct,
                    }
                stock_pool[code]["boards"].append({"name": board_name, "change_pct": board_change})

        total = len(stock_pool)
        self.progress.update(phase="predict", total=total, current=0, detail=f"开始对 {total} 只股票预测")
        entries: list[dict[str, Any]] = []
        failed = 0
        for idx, (code, info) in enumerate(stock_pool.items(), start=1):
            self.progress.update(current=idx, detail=f"{idx}/{total} {info['name'] or code}")
            try:
                pred = await self.kronos.predict(code, lookback=lookback, horizon=horizon)
            except Exception as exc:
                failed += 1
                log.info("[predict_funnel] predict fail %s: %s", code, exc)
                continue
            try:
                history = pred.get("history_kline") or []
                predicted = pred.get("predicted_kline") or []
                if not history or not predicted:
                    failed += 1
                    continue
                today_close = float(history[-1]["close"])
                if today_close <= 0:
                    failed += 1
                    continue
                highs = [float(p["high"]) for p in predicted]
                closes = [float(p["close"]) for p in predicted]
                pred_max_high = max(highs)
                pred_last_close = closes[-1]
                pred_avg_close = sum(closes) / len(closes)
                max_high_pct = (pred_max_high - today_close) / today_close * 100.0
                last_close_pct = (pred_last_close - today_close) / today_close * 100.0
                avg_close_pct = (pred_avg_close - today_close) / today_close * 100.0
                entries.append({
                    "symbol": code,
                    "name": info.get("name") or code,
                    "boards": info.get("boards", []),
                    "current_price": info.get("current_price", today_close),
                    "current_pct": info.get("current_pct", 0.0),
                    "today_close": today_close,
                    "pred_max_high": round(pred_max_high, 3),
                    "pred_last_close": round(pred_last_close, 3),
                    "pred_avg_close": round(pred_avg_close, 3),
                    "pred_max_high_pct": round(max_high_pct, 2),
                    "pred_last_close_pct": round(last_close_pct, 2),
                    "pred_avg_close_pct": round(avg_close_pct, 2),
                    "predicted_kline": predicted,
                    "horizon": len(predicted),
                })
            except Exception as exc:
                failed += 1
                log.info("[predict_funnel] parse fail %s: %s", code, exc)

        entries.sort(key=lambda e: e["pred_max_high_pct"], reverse=True)

        th_c = float(cfg["threshold_candidate"])
        th_f = float(cfg["threshold_focus"])
        th_b = float(cfg["threshold_buy"])
        pools = {POOL_CANDIDATE: [], POOL_FOCUS: [], POOL_BUY: []}
        for e in entries:
            p = e["pred_max_high_pct"]
            if p >= th_b:
                e["pool"] = POOL_BUY
                pools[POOL_BUY].append(e)
            elif p >= th_f:
                e["pool"] = POOL_FOCUS
                pools[POOL_FOCUS].append(e)
            elif p >= th_c:
                e["pool"] = POOL_CANDIDATE
                pools[POOL_CANDIDATE].append(e)

        elapsed = time.time() - t0
        self._snapshot = {
            "trade_date": now_cn().date().isoformat(),
            "entries": entries,
            "pools": pools,
            "updated_at": now_cn().isoformat(),
            "config": cfg,
            "meta": {
                "trigger": trigger,
                "concept_board_source": concept_board_source,
                "boards_used": int(len(hot_boards)),
                "stocks_scanned": total,
                "entries_count": len(entries),
                "failed_count": failed,
                "elapsed_sec": round(elapsed, 1),
                "kronos_device": self.kronos.get_device() if self.kronos.is_loaded() else "lazy",
            },
        }
        self._save_state()
        self.progress.update(phase="persisted", detail=f"保存 {len(entries)} 条")

        if cfg.get("feishu_enabled") and entries:
            try:
                await self._notify_feishu(entries[:10], self._snapshot["meta"])
            except Exception as exc:
                log.warning("[predict_funnel] feishu notify failed: %s", exc)

    async def _notify_feishu(self, top: list[dict[str, Any]], meta: dict[str, Any]) -> None:
        lines = [
            "🔮 Alpha 预测选股 Top 10",
            "━━━━━━━━━━━━━━━━━━",
            f"📅 交易日：{self._snapshot['trade_date']}  ⏱ {meta.get('elapsed_sec', 0)}s",
            f"📊 扫描板块 {meta.get('boards_used', 0)} × 股票 {meta.get('stocks_scanned', 0)}",
            "",
        ]
        for i, e in enumerate(top, start=1):
            boards = ",".join(b["name"] for b in e.get("boards", [])[:2]) or "-"
            lines.append(
                f"{i:>2}. {e['name']}({e['symbol']}) 预测高+{e['pred_max_high_pct']:.1f}% | 收盘+{e['pred_last_close_pct']:.1f}% | 板块:{boards}"
            )
        await send_feishu_text("\n".join(lines))
