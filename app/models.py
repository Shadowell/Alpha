from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.config import POOL_BUY, POOL_CANDIDATE, POOL_FOCUS

PoolName = Literal["candidate", "focus", "buy"]


class ConceptTag(BaseModel):
    name: str
    rank: int
    color: str
    heat: float
    change_pct: float
    limit_up_count: int
    up_count: int
    down_count: int
    leader: str = ""


class StockCard(BaseModel):
    symbol: str
    name: str
    pool: PoolName
    score: float
    score_delta: float
    recommended_pool: PoolName | None = None
    breakout_level: float = 0.0
    volume_ratio: float = 0.0
    pct_change: float = 0.0
    concept_tags: list[ConceptTag] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    updated_at: str


class FunnelResponse(BaseModel):
    trade_date: str
    updated_at: str
    pools: dict[str, list[StockCard]]
    stats: dict[str, int]


class HotConceptItem(BaseModel):
    name: str
    heat: float
    change_pct: float
    limit_up_count: int
    up_count: int
    down_count: int
    leader: str = ""
    selected_count: int = 0


class HotConceptResponse(BaseModel):
    trade_date: str
    updated_at: str
    frozen: bool
    items: list[HotConceptItem]


class HotStockItem(BaseModel):
    rank: int
    symbol: str
    name: str
    latest_price: float
    change_pct: float
    change_amount: float


class HotStocksResponse(BaseModel):
    trade_date: str
    updated_at: str
    frozen: bool
    items: list[HotStockItem]


class MovePoolRequest(BaseModel):
    symbol: str
    target_pool: PoolName
    source_pool: PoolName | None = None
    note: str | None = None


class MovePoolResponse(BaseModel):
    success: bool
    message: str
    symbol: str
    pool: PoolName


class RecomputeRequest(BaseModel):
    symbol: str | None = None


class KlinePoint(BaseModel):
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float


class StockDetailResponse(BaseModel):
    symbol: str
    name: str
    pool: PoolName
    score: float
    recommended_pool: PoolName | None = None
    score_breakdown: dict[str, float]
    metrics: dict[str, Any]
    concept_tags: list[ConceptTag]
    concept_candidates: list[dict[str, Any]]
    trigger_log: list[dict[str, Any]]
    kline: list[KlinePoint]


DEFAULT_EMPTY_FUNNEL = FunnelResponse(
    trade_date="",
    updated_at="",
    pools={POOL_CANDIDATE: [], POOL_FOCUS: [], POOL_BUY: []},
    stats={"candidate": 0, "focus": 0, "buy": 0},
)
