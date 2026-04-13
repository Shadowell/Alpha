from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import akshare as ak

from app.config import POOL_BUY, POOL_CANDIDATE, POOL_FOCUS, VALID_POOLS
from app.models import NoticeDetailResponse, NoticeFunnelResponse, NoticeItem
from app.services.notice_llm import score_with_llm
from app.services.sqlite_store import SQLiteStateStore
from app.services.time_utils import now_cn

BULLISH_RULES: list[tuple[str, int, tuple[str, ...]]] = [
    ("业绩预增", 14, ("预增", "扭亏", "同比增长", "大幅增长", "预盈")),
    ("高额分红", 12, ("分红", "派息", "现金红利", "利润分配", "送转", "转增")),
    ("股份回购", 13, ("回购", "增持计划", "增持股份", "回购股份")),
    ("重大合同", 11, ("重大合同", "中标", "签订", "定点", "订单", "采购协议")),
    ("资产重组", 10, ("重组", "收购", "并购", "资产注入", "购买资产")),
    ("融资获批", 9, ("获批", "审核通过", "注册生效", "获得批复")),
    ("产品突破", 8, ("量产", "商业化", "获准上市", "新品发布", "投产")),
]

BEARISH_KEYWORDS: tuple[str, ...] = (
    "减持",
    "诉讼",
    "处罚",
    "立案",
    "风险提示",
    "停牌",
    "终止",
    "违约",
    "亏损",
    "预减",
    "退市",
    "ST",
    "*ST",
)


def _normalize_notice_date(raw_date: str | None) -> tuple[str, str]:
    if raw_date:
        dt = datetime.strptime(raw_date.replace("-", ""), "%Y%m%d")
    else:
        dt = now_cn().replace(hour=0, minute=0, second=0, microsecond=0)
    return dt.strftime("%Y%m%d"), dt.strftime("%Y-%m-%d")


def _rule_score(title: str, notice_type: str) -> tuple[float, str]:
    text = f"{title} {notice_type}"
    for bad in BEARISH_KEYWORDS:
        if bad in text:
            return 20.0, f"含风险词:{bad}"

    score = 45.0
    hit_tags: list[str] = []
    for tag, weight, keys in BULLISH_RULES:
        if any(k in text for k in keys):
            score += weight
            hit_tags.append(tag)
    score = max(0.0, min(100.0, score))
    reason = "、".join(hit_tags) if hit_tags else "事件中性"
    return score, reason


def _score_to_pool(score: float) -> str:
    if score >= 80:
        return POOL_BUY
    if score >= 65:
        return POOL_FOCUS
    return POOL_CANDIDATE


class NoticeService:
    def __init__(self, state_store: SQLiteStateStore, kline_cache_service: Any | None = None) -> None:
        self.state_store = state_store
        self.kline_cache_service = kline_cache_service
        self.trade_date = now_cn().date().isoformat()
        self.entries: dict[str, dict[str, Any]] = {}
        self.updated_at = now_cn().isoformat()
        self.llm_enabled = False
        self.source = "rule"
        self._load_state()

    def _load_state(self) -> None:
        payload = self.state_store.load_notice_state()
        if payload is None:
            return
        if payload.get("trade_date") != self.trade_date:
            return
        self.entries = payload.get("entries", {})
        self.updated_at = payload.get("updated_at", self.updated_at)
        self.llm_enabled = bool(payload.get("llm_enabled", False))
        self.source = str(payload.get("source", "rule"))

    def _save_state(self) -> None:
        self.state_store.save_notice_state(
            {
                "trade_date": self.trade_date,
                "entries": self.entries,
                "updated_at": self.updated_at,
                "llm_enabled": self.llm_enabled,
                "source": self.source,
            }
        )

    async def run_notice_screen(self, notice_date: str | None = None, limit: int = 10) -> dict[str, Any]:
        yyyymmdd, display_date = _normalize_notice_date(notice_date)
        df = await asyncio.to_thread(ak.stock_notice_report, symbol="全部", date=yyyymmdd)
        if df is None or df.empty:
            self.entries = {}
            self.updated_at = now_cn().isoformat()
            self.source = "rule"
            self.llm_enabled = False
            self._save_state()
            return {"success": True, "candidate_count": 0, "notice_date": display_date, "source": "empty"}

        candidates: list[dict[str, Any]] = []
        for _, row in df.iterrows():
            code = str(row.get("代码", "")).zfill(6)
            name = str(row.get("名称", ""))
            title = str(row.get("公告标题", ""))
            notice_type = str(row.get("公告类型", ""))
            url = str(row.get("网址", ""))
            ndate = str(row.get("公告日期", display_date))
            if not code or "ST" in name.upper():
                continue
            if not (code.startswith("6") or code.startswith("00")):
                continue
            score, reason = _rule_score(title, notice_type)
            if score < 55:
                continue
            candidates.append(
                {
                    "code": code,
                    "name": name,
                    "title": title,
                    "notice_type": notice_type,
                    "notice_date": ndate,
                    "url": url,
                    "rule_score": score,
                    "rule_reason": reason,
                }
            )

        if not candidates:
            self.entries = {}
            self.updated_at = now_cn().isoformat()
            self.source = "rule"
            self.llm_enabled = False
            self._save_state()
            return {"success": True, "candidate_count": 0, "notice_date": display_date, "source": "rule"}

        dedup: dict[str, dict[str, Any]] = {}
        for item in sorted(candidates, key=lambda x: (-float(x["rule_score"]), x["code"])):
            if item["code"] not in dedup:
                dedup[item["code"]] = item
        shortlisted = list(dedup.values())[: max(10, min(limit * 4, 120))]

        llm_scores, llm_enabled = score_with_llm(shortlisted)
        self.llm_enabled = llm_enabled
        self.source = "llm" if llm_scores else "rule"

        entries: dict[str, dict[str, Any]] = {}
        for item in shortlisted:
            code = item["code"]
            llm = llm_scores.get(code, {})
            score = float(llm.get("score", item["rule_score"]))
            score = max(0.0, min(100.0, score))
            entries[code] = {
                "symbol": code,
                "name": item["name"],
                "pool": _score_to_pool(score),
                "score": round(score, 2),
                "reason": str(llm.get("reason", item["rule_reason"])),
                "risk": str(llm.get("risk", "")),
                "title": item["title"],
                "notice_type": item["notice_type"],
                "notice_date": item["notice_date"],
                "url": item["url"],
                "notices": [
                    {
                        "title": item["title"],
                        "notice_type": item["notice_type"],
                        "notice_date": item["notice_date"],
                        "url": item["url"],
                    }
                ],
                "updated_at": now_cn().isoformat(),
            }

        ranked = sorted(entries.values(), key=lambda x: x["score"], reverse=True)[: max(1, min(limit, 200))]
        self.entries = {e["symbol"]: e for e in ranked}
        self.updated_at = now_cn().isoformat()
        self._save_state()

        return {
            "success": True,
            "candidate_count": len(self.entries),
            "notice_date": display_date,
            "source": self.source,
            "llm_enabled": self.llm_enabled,
        }

    async def get_notice_funnel(self, trade_date: str | None = None) -> NoticeFunnelResponse:
        if trade_date and trade_date != self.trade_date:
            self.trade_date = trade_date
            self.entries = {}
            self.updated_at = now_cn().isoformat()
            self._save_state()

        pools_raw = {POOL_CANDIDATE: [], POOL_FOCUS: [], POOL_BUY: []}
        for entry in self.entries.values():
            pools_raw[entry["pool"]].append(entry)
        for pool_name in pools_raw:
            pools_raw[pool_name].sort(key=lambda x: x["score"], reverse=True)

        pools: dict[str, list[NoticeItem]] = {POOL_CANDIDATE: [], POOL_FOCUS: [], POOL_BUY: []}
        for pool_name, rows in pools_raw.items():
            pools[pool_name] = [
                NoticeItem(
                    symbol=r["symbol"],
                    name=r["name"],
                    title=r["title"],
                    notice_type=r["notice_type"],
                    notice_date=r["notice_date"],
                    url=r["url"],
                    score=float(r["score"]),
                    pool=r["pool"],
                    reason=r.get("reason", ""),
                    risk=r.get("risk", ""),
                    updated_at=r.get("updated_at", self.updated_at),
                )
                for r in rows
            ]

        return NoticeFunnelResponse(
            trade_date=self.trade_date,
            updated_at=self.updated_at,
            pools=pools,
            stats={
                "candidate": len(pools[POOL_CANDIDATE]),
                "focus": len(pools[POOL_FOCUS]),
                "buy": len(pools[POOL_BUY]),
            },
            llm_enabled=self.llm_enabled,
            source=self.source,
        )

    async def move_pool(self, symbol: str, target_pool: str) -> dict[str, Any]:
        if target_pool not in VALID_POOLS:
            return {"success": False, "message": "非法目标池"}
        entry = self.entries.get(symbol)
        if not entry:
            return {"success": False, "message": "股票不存在"}
        entry["pool"] = target_pool
        entry["updated_at"] = now_cn().isoformat()
        self.updated_at = now_cn().isoformat()
        self._save_state()
        return {"success": True, "message": "迁移成功", "symbol": symbol, "pool": target_pool}

    async def get_notice_detail(self, symbol: str, days: int = 30) -> NoticeDetailResponse:
        entry = self.entries.get(symbol)
        if not entry:
            raise KeyError(symbol)
        kline = []
        if self.kline_cache_service is not None:
            try:
                rows = self.kline_cache_service.get_kline(symbol, days)
            except Exception:
                rows = []
            kline = [
                {
                    "date": str(r.get("date", "")),
                    "open": float(r.get("open", 0)),
                    "high": float(r.get("high", 0)),
                    "low": float(r.get("low", 0)),
                    "close": float(r.get("close", 0)),
                    "volume": float(r.get("volume", 0)),
                }
                for r in rows
            ]
        return NoticeDetailResponse(
            symbol=entry["symbol"],
            name=entry["name"],
            score=float(entry["score"]),
            pool=entry["pool"],
            reason=entry.get("reason", ""),
            risk=entry.get("risk", ""),
            notices=entry.get("notices", []),
            kline=kline,
        )

