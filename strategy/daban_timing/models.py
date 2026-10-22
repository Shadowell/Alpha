"""组合层共享数据结构（issue #32 约束 / 交易规则 / 提醒 / 回测共用）。

只放"输入契约"与最小算术，业务判断分别留在
:mod:`~strategy.daban_timing.trade_rules`（交易规则）、
:mod:`~strategy.daban_timing.constraints`（组合约束）、
:mod:`~strategy.daban_timing.risk`（回撤熔断）、
:mod:`~strategy.daban_timing.alerts`（止损止盈提醒）。
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

BUY = "buy"
SELL = "sell"
VALID_SIDES: tuple[str, ...] = (BUY, SELL)

#: 缺数标记：任何字段拿不到就写进 missing，禁止用 0 冒充有效值。
MISSING = "missing"


def to_float(value: Any) -> float | None:
    """宽松转 float；None/NaN/Inf/不可解析 → None（缺失）。"""

    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def round4(value: float | None) -> float | None:
    return None if value is None else round(float(value), 6)


@dataclass(frozen=True)
class Holding:
    """一笔存量持仓（模拟盘 / 真实持仓的只读快照）。"""

    symbol: str
    name: str = ""
    theme: str | None = None
    qty: int = 0
    cost_price: float | None = None
    current_price: float | None = None
    open_date: str | None = None  # 建仓交易日（T+1 判定用）
    market_value: float | None = None  # 缺省时用 qty × current_price 推算

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> Holding:
        theme = payload.get("theme") or payload.get("concept")
        return cls(
            symbol=str(payload.get("symbol") or payload.get("code") or ""),
            name=str(payload.get("name") or ""),
            theme=None if theme in (None, "") else str(theme),
            qty=int(to_float(payload.get("qty")) or 0),
            cost_price=to_float(payload.get("cost_price")),
            current_price=to_float(
                payload.get("current_price") or payload.get("price")
            ),
            open_date=(
                None
                if payload.get("open_date") in (None, "")
                else str(payload["open_date"])
            ),
            market_value=to_float(payload.get("market_value")),
        )

    def value(self) -> float | None:
        if self.market_value is not None:
            return self.market_value
        if self.current_price is None or self.qty <= 0:
            return None
        return self.current_price * self.qty


@dataclass(frozen=True)
class PortfolioState:
    """组合快照：净值 + 现金 + 持仓 + 当日已新开仓数。"""

    trade_date: str
    equity: float
    cash: float | None = None
    holdings: tuple[Holding, ...] = ()
    new_positions_today: int = 0
    peak_equity: float | None = None
    missing: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> PortfolioState:
        raw_holdings: Iterable[Any] = (
            payload.get("holdings") or payload.get("positions") or ()
        )
        holdings = tuple(
            item if isinstance(item, Holding) else Holding.from_mapping(item)
            for item in raw_holdings
        )
        equity = to_float(payload.get("equity") or payload.get("total_assets"))
        missing = list(payload.get("missing") or ())
        if equity is None:
            missing.append("equity_missing")
        return cls(
            trade_date=str(payload.get("trade_date") or ""),
            equity=float(equity) if equity is not None else 0.0,
            cash=to_float(payload.get("cash")),
            holdings=holdings,
            new_positions_today=int(to_float(payload.get("new_positions_today")) or 0),
            peak_equity=to_float(payload.get("peak_equity")),
            missing=tuple(missing),
        )

    def holding(self, symbol: str) -> Holding | None:
        for item in self.holdings:
            if item.symbol == str(symbol):
                return item
        return None

    @property
    def positions_value(self) -> float:
        return float(sum((item.value() or 0.0) for item in self.holdings))

    @property
    def gross_exposure(self) -> float | None:
        """存量仓位占总净值比例；净值缺失时返回 None（不臆造 0）。"""

        if self.equity <= 0:
            return None
        return round(self.positions_value / self.equity, 6)

    def weight(self, symbol: str) -> float | None:
        if self.equity <= 0:
            return None
        item = self.holding(symbol)
        if item is None:
            return 0.0
        value = item.value()
        return None if value is None else round(value / self.equity, 6)

    def theme_weights(self) -> dict[str, float | None]:
        """按题材聚合权重；缺题材的持仓归入 ``UNKNOWN_THEME`` 且标 None。"""

        if self.equity <= 0:
            return {}
        buckets: dict[str, float | None] = {}
        for item in self.holdings:
            key = item.theme or UNKNOWN_THEME
            value = item.value()
            if value is None:
                buckets[key] = None
                continue
            base = buckets.get(key) or 0.0
            buckets[key] = round4(base + value / self.equity)
        return buckets


UNKNOWN_THEME = "__unknown_theme__"


@dataclass(frozen=True)
class OrderRequest:
    """一笔拟下单（买入候选 / 卖出请求）。

    ``amount`` 与 ``qty`` 二者可缺其一：只有 ``qty`` 时按 ``price`` 折算金额；
    只有 ``amount`` 时由约束层反推整手数（``qty`` 会被重算并覆盖）。
    """

    symbol: str
    side: str = BUY
    name: str = ""
    theme: str | None = None
    qty: int | None = None
    price: float | None = None
    amount: float | None = None
    board_height: int | None = None
    board_type: str | None = None
    turnover_amount: float | None = None  # 当日成交额（流动性约束）
    weak_to_strong: bool | None = None  # 分歧期"弱转强"标记（#24 微观结构）
    priority: float = 0.0  # 候选排序分（越大越优先，来自模型/评分）
    hold_days: int = 0  # 卖出时用于时间止损 / T+1 说明
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> OrderRequest:
        side = str(payload.get("side") or payload.get("direction") or BUY).lower()
        theme = (
            payload.get("theme")
            or payload.get("concept")
            or payload.get("limit_reason")
        )
        qty = to_float(payload.get("qty") or payload.get("quantity"))
        height = to_float(
            payload.get("board_height")
            or payload.get("limit_times")
            or payload.get("boards")
        )
        weak = payload.get("weak_to_strong")
        return cls(
            symbol=str(payload.get("symbol") or payload.get("code") or ""),
            side=SELL if side in (SELL, "short") else BUY,
            name=str(payload.get("name") or ""),
            theme=None if theme in (None, "") else str(theme),
            qty=None if qty is None else int(qty),
            price=to_float(payload.get("price") or payload.get("last_price")),
            amount=to_float(payload.get("amount") or payload.get("notional")),
            board_height=None if height is None else int(height),
            board_type=None
            if payload.get("board_type") in (None, "")
            else str(payload["board_type"]),
            turnover_amount=to_float(
                payload.get("turnover_amount") or payload.get("amount_today")
            ),
            weak_to_strong=None if weak is None else bool(weak),
            priority=float(
                to_float(payload.get("priority") or payload.get("score")) or 0.0
            ),
            hold_days=int(to_float(payload.get("hold_days")) or 0),
            meta={
                k: v
                for k, v in dict(payload).items()
                if k
                not in {
                    "symbol",
                    "code",
                    "side",
                    "direction",
                    "name",
                    "theme",
                    "concept",
                    "qty",
                    "quantity",
                    "price",
                    "last_price",
                    "amount",
                    "notional",
                    "board_height",
                    "limit_times",
                    "boards",
                    "board_type",
                    "turnover_amount",
                    "amount_today",
                    "weak_to_strong",
                    "priority",
                    "score",
                    "hold_days",
                }
            },
        )

    @property
    def is_buy(self) -> bool:
        return self.side == BUY

    def requested_amount(self) -> float | None:
        if self.amount is not None:
            return self.amount
        if self.qty is not None and self.price is not None:
            return self.qty * self.price
        return None

    def with_qty(self, qty: int) -> OrderRequest:
        return replace(
            self, qty=qty, amount=None if self.price is None else qty * self.price
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "name": self.name,
            "theme": self.theme,
            "qty": self.qty,
            "price": self.price,
            "amount": round4(self.requested_amount()),
            "board_height": self.board_height,
            "board_type": self.board_type,
            "turnover_amount": self.turnover_amount,
            "priority": self.priority,
        }


def normalize_orders(
    orders: Any,
) -> tuple[OrderRequest, ...]:
    """接受 ``OrderRequest`` / dict / dict 序列三种输入，统一成元组。"""

    if orders is None:
        return ()
    if isinstance(orders, Mapping):
        raise TypeError("orders must be a sequence of mappings, not a single mapping")
    items: list[Any]
    if isinstance(orders, Iterable) and not isinstance(orders, (str, bytes)):
        items = list(orders)
    else:  # pragma: no cover - defensive
        raise TypeError(f"unsupported orders type: {type(orders).__name__}")
    out: list[OrderRequest] = []
    for item in items:
        out.append(
            item if isinstance(item, OrderRequest) else OrderRequest.from_mapping(item)
        )
    return tuple(out)
