from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.config import POOL_BUY, POOL_CANDIDATE, POOL_FOCUS
from app.services.strategy_rules import RULE_REGISTRY, RuleParam, RuleSpec

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
    cumulative_10d_pct: float = 0.0


class HotStocksResponse(BaseModel):
    trade_date: str
    updated_at: str
    frozen: bool
    items: list[HotStockItem]


class MovePoolRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=32)
    target_pool: PoolName
    source_pool: PoolName | None = None
    note: str | None = Field(default=None, max_length=500)


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


class NoticeItem(BaseModel):
    symbol: str
    name: str
    title: str
    notice_type: str
    notice_date: str
    url: str
    score: float
    pool: PoolName
    reason: str = ""
    risk: str = ""
    updated_at: str


class NoticeFunnelResponse(BaseModel):
    trade_date: str
    updated_at: str
    pools: dict[str, list[NoticeItem]]
    stats: dict[str, int]
    llm_enabled: bool
    source: str


class NoticeDetailResponse(BaseModel):
    symbol: str
    name: str
    score: float
    pool: PoolName
    reason: str = ""
    risk: str = ""
    notices: list[dict[str, Any]] = Field(default_factory=list)
    kline: list[KlinePoint] = Field(default_factory=list)


DEFAULT_EMPTY_FUNNEL = FunnelResponse(
    trade_date="",
    updated_at="",
    pools={POOL_CANDIDATE: [], POOL_FOCUS: [], POOL_BUY: []},
    stats={"candidate": 0, "focus": 0, "buy": 0},
)


# ─────────────────────────── 写接口请求模型 ───────────────────────────
# 说明：config 类接口允许未知字段透传（extra="allow"），但已知字段有严格类型/范围；
# 其余写接口对未声明字段忽略，避免前端历史 payload 直接 422。


def _coerce_rule_param(spec: RuleSpec, param: RuleParam, value: Any) -> Any:
    """按 RULE_REGISTRY 的 RuleParam schema 校验单个参数并做规范化。"""
    key = param.key
    if param.type == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"规则 {spec.code} 参数 {key} 必须为布尔值")
        return value
    if param.type == "text":
        if not isinstance(value, str):
            raise ValueError(f"规则 {spec.code} 参数 {key} 必须为字符串")
        if spec.code == "ma_bull_stack" and key == "periods":
            parts = [p.strip() for p in value.split(",") if p.strip()]
            try:
                periods = [int(p) for p in parts]
            except ValueError as exc:
                raise ValueError(
                    "规则 ma_bull_stack 参数 periods 需为逗号分隔的整数，例如 5,10,20"
                ) from exc
            if len(periods) < 2 or any(p <= 0 for p in periods):
                raise ValueError("规则 ma_bull_stack 参数 periods 至少需要 2 个正整数")
        return value

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"规则 {spec.code} 参数 {key} 必须为数字")
    if param.type == "int":
        if isinstance(value, float) and not value.is_integer():
            raise ValueError(f"规则 {spec.code} 参数 {key} 必须为整数")
        number: float | int = int(value)
    else:
        number = float(value)
    if param.min is not None and number < param.min:
        raise ValueError(f"规则 {spec.code} 参数 {key} 不能小于 {param.min}")
    if param.max is not None and number > param.max:
        raise ValueError(f"规则 {spec.code} 参数 {key} 不能大于 {param.max}")
    return number


class StrategyRuleIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    rule_code: str = Field(min_length=1, max_length=64)
    enabled: bool = True
    params: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_params(self) -> "StrategyRuleIn":
        spec = RULE_REGISTRY.get(self.rule_code)
        if spec is None:
            raise ValueError(f"未知规则: {self.rule_code}")
        allowed = {p.key: p for p in spec.params}
        unknown = sorted(set(self.params) - set(allowed))
        if unknown:
            raise ValueError(f"规则 {self.rule_code} 不支持参数: {', '.join(unknown)}")
        for key, value in list(self.params.items()):
            self.params[key] = _coerce_rule_param(spec, allowed[key], value)
        return self


class CustomStrategyUpsert(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str | None = Field(default=None, max_length=128)
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=2000)
    rules: list[StrategyRuleIn] = Field(min_length=1)
    is_default: bool = False

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("策略名称不能为空")
        return cleaned

    @field_validator("id")
    @classmethod
    def _empty_id_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class PredictFunnelConfigUpdate(BaseModel):
    """预测选股配置（仅更新显式传入字段）。"""

    model_config = ConfigDict(extra="allow")

    top_k_boards: int | None = Field(default=None, ge=1, le=50)
    top_m_stocks: int | None = Field(default=None, ge=1, le=50)
    threshold_candidate: float | None = Field(default=None, ge=0, le=100)
    threshold_focus: float | None = Field(default=None, ge=0, le=100)
    threshold_buy: float | None = Field(default=None, ge=0, le=100)
    horizon: int | None = Field(default=None, ge=1, le=10)
    lookback: int | None = Field(default=None, ge=30, le=240)
    feishu_enabled: bool | None = None
    auto_after_close: bool | None = None


class HotStockAIConfigUpdate(BaseModel):
    """热门股票智能分析配置（仅更新显式传入字段）。"""

    model_config = ConfigDict(extra="allow")

    top_n: int | None = Field(default=None, ge=5, le=30)
    lookback: int | None = Field(default=None, ge=30, le=240)
    horizon: int | None = Field(default=None, ge=1, le=5)
    threshold_candidate: float | None = Field(default=None, ge=0, le=100)
    threshold_focus: float | None = Field(default=None, ge=0, le=100)
    threshold_buy: float | None = Field(default=None, ge=0, le=100)
    max_buy_pool_size: int | None = Field(default=None, ge=1, le=10)
    auto_refresh_enabled: bool | None = None
    refresh_interval_minutes: int | None = Field(default=None, ge=1, le=60)
    use_kronos: bool | None = None
    tradingagents_enabled: bool | None = None
    tradingagents_top_n: int | None = Field(default=None, ge=0, le=20)
    tradingagents_timeout_seconds: int | None = Field(default=None, ge=30, le=900)
    tradingagents_provider: str | None = Field(default=None, max_length=64)
    tradingagents_quick_model: str | None = Field(default=None, max_length=128)
    tradingagents_deep_model: str | None = Field(default=None, max_length=128)


class AgentRunRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    task_type: Literal["daily_review", "notice_review", "full_diagnosis"] = (
        "daily_review"
    )
    params: dict[str, Any] = Field(default_factory=dict)


class MonitorConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    system_prompt: str | None = Field(default=None, max_length=20000)
    interval_minutes: int | None = Field(default=None, ge=1, le=240)
    enabled: bool | None = None


class PaperBuyRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    symbol: str = Field(min_length=1, max_length=32)
    name: str | None = Field(default=None, max_length=64)
    qty: int = Field(default=100, gt=0, le=1_000_000)
    note: str = Field(default="", max_length=500)


class PaperSellRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    position_id: str = Field(min_length=1, max_length=64)
    note: str = Field(default="", max_length=500)


class PaperSettingsUpdate(BaseModel):
    """模拟盘费用参数：费率 [0,1]，最低佣金 [0,100]，滑点 [0,0.1]。"""

    model_config = ConfigDict(extra="ignore")

    commission_rate: float | None = Field(default=None, ge=0, le=1)
    min_commission: float | None = Field(default=None, ge=0, le=100)
    stamp_tax_rate: float | None = Field(default=None, ge=0, le=1)
    slippage_rate: float | None = Field(default=None, ge=0, le=0.1)


JobStatus = Literal["queued", "running", "succeeded", "failed", "interrupted"]


class JobAccepted(BaseModel):
    job_id: str
    kind: str
    status: JobStatus
    poll_url: str


class JobOut(BaseModel):
    id: str
    kind: str
    status: JobStatus
    progress: dict[str, Any] = Field(default_factory=dict)
    result: Any | None = None
    error: str | None = None
    created_at: str
    updated_at: str
    finished_at: str | None = None


class JobListOut(BaseModel):
    items: list[JobOut] = Field(default_factory=list)
    total: int = 0


# ─────────────────── 打板核心数据底座（issue #22）───────────────────
# 只读接口的响应模型。缺失值一律是 JSON null（不会填 0），
# 因此数值/文本字段全部 Optional；额外字段允许透传，方便后续批次补列。


class _PermissiveOut(BaseModel):
    model_config = ConfigDict(extra="allow")


class TushareCapabilityItem(_PermissiveOut):
    api: str
    capability: Literal["available", "denied", "error", "not_configured"]
    reason: str = ""
    sample_rows: int = 0


class TushareCapabilitiesOut(_PermissiveOut):
    enabled: bool
    probed: bool = False
    trade_date: str | None = None
    probed_at: str | None = None
    apis: list[TushareCapabilityItem] = Field(default_factory=list)
    degradations: dict[str, str] = Field(default_factory=dict)
    note: str = ""


class LimitBoardDatasetState(_PermissiveOut):
    dataset: str
    label: str = ""
    trade_date: str = ""
    status: str = "none"
    row_count: int = 0
    message: str = ""
    updated_at: str = ""
    latest_trade_date: str = ""
    total_rows: int = 0


class LimitBoardCoverage(_PermissiveOut):
    trade_date: str
    total_rows: int = 0
    up_count: int = 0
    down_count: int = 0
    broken_count: int = 0
    non_null_ratio: dict[str, float | None] = Field(default_factory=dict)
    synced_at: str = ""


class LimitBoardStatusOut(_PermissiveOut):
    enabled: bool
    latest_trade_date: str = ""
    coverage: LimitBoardCoverage | None = None
    datasets: list[LimitBoardDatasetState] = Field(default_factory=list)
    capabilities: dict[str, Any] = Field(default_factory=dict)
    primary_apis: dict[str, str] = Field(default_factory=dict)
    degradations: dict[str, str] = Field(default_factory=dict)


class LimitBoardItem(_PermissiveOut):
    symbol: str
    trade_date: str
    limit_flag: Literal["U", "D", "Z"] | None = None
    first_time: str | None = None
    last_time: str | None = None
    open_times: int | None = None
    limit_times: int | None = None
    up_stat: str | None = None
    fd_amount: float | None = None
    amount: float | None = None
    limit_amount: float | None = None
    turnover_ratio: float | None = None
    float_mv: float | None = None
    total_mv: float | None = None
    close: float | None = None
    pct_chg: float | None = None
    board_type: str | None = None
    theme: str | None = None
    limit_reason: str | None = None
    source: str = ""
    synced_at: str = ""


class LimitLadderLevel(_PermissiveOut):
    trade_date: str
    ladder_level: int
    stock_count: int = 0
    symbols: list[dict[str, Any]] = Field(default_factory=list)
    fd_amount_sum: float | None = None
    amount_sum: float | None = None
    avg_open_times: float | None = None
    prev_level_count: int | None = None
    promotion_ratio: float | None = None
    source: str = ""


class LimitBoardDayOut(_PermissiveOut):
    trade_date: str
    board: str | None = None
    count: int = 0
    items: list[LimitBoardItem] = Field(default_factory=list)
    ladder: list[LimitLadderLevel] = Field(default_factory=list)
    coverage: LimitBoardCoverage | None = None


class DragonTigerItem(_PermissiveOut):
    symbol: str
    trade_date: str
    reason: str = ""
    name: str | None = None
    close: float | None = None
    pct_change: float | None = None
    turnover_rate: float | None = None
    amount: float | None = None
    l_amount: float | None = None
    net_amount: float | None = None
    net_rate: float | None = None
    amount_rate: float | None = None
    float_values: float | None = None


class DragonTigerSeatItem(_PermissiveOut):
    trade_date: str
    symbol: str
    seat_name: str
    side: Literal["buy", "sell"]
    reason: str = ""
    buy_amount: float | None = None
    sell_amount: float | None = None
    net_amount: float | None = None
    seat_count: int = 1
    source: str = ""


class DragonTigerDayOut(_PermissiveOut):
    trade_date: str
    symbol: str | None = None
    count: int = 0
    items: list[DragonTigerItem] = Field(default_factory=list)
    seats: list[DragonTigerSeatItem] = Field(default_factory=list)


class HotMoneySeatItem(_PermissiveOut):
    seat_name: str
    org_code: str = ""
    category: str | None = None
    description: str | None = None
    orgs: list[str] = Field(default_factory=list)
    appear_count: int = 0
    last_trade_date: str | None = None
    source: str = ""


class HotMoneySeatOut(_PermissiveOut):
    count: int = 0
    items: list[HotMoneySeatItem] = Field(default_factory=list)
