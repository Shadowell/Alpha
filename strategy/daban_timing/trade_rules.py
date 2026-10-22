"""A 股交易规则校验（issue #32「交易规则」条目，纯函数版）。

覆盖验收项：T+1、100 股整数倍、停牌 / 涨跌停不可成交、买入金额上限。

设计要点：

* **只判定、不下单**：所有结论是 ``Violation(rule_id, detail, threshold, price)``，
  上层（模拟盘 / API）把它当作"拒绝原因 + 提醒"落库，天然满足可追溯要求。
* **缺数不猜**：拿不到行情（停牌标志、涨跌停价、昨收）时记入
  ``checks_skipped``，而不是默认"可成交"或"成交额 0"。
* **可配置**：手数、是否允许一字/封板排队买入、零股清仓卖出等都走
  :class:`~strategy.daban_timing.config.TimingConfig`，回测与实盘同一口径。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from .config import TimingConfig
from .models import Holding, OrderRequest, to_float

#: 板块涨跌停比例（按代码前缀判定；ST 由调用方显式标记）。
LIMIT_RATIO_MAIN = 0.10
LIMIT_RATIO_GEM_STAR = 0.20  # 创业板 300/301、科创板 688/689
LIMIT_RATIO_BSE = 0.30  # 北交所 4/8 开头
LIMIT_RATIO_ST = 0.05


def limit_ratio_for(symbol: str, *, is_st: bool = False) -> float:
    code = str(symbol or "").strip().upper().split(".")[0]
    if is_st:
        return LIMIT_RATIO_ST
    if code.startswith(("300", "301", "688", "689")):
        return LIMIT_RATIO_GEM_STAR
    if code.startswith(("43", "83", "87", "88", "92")):
        return LIMIT_RATIO_BSE
    return LIMIT_RATIO_MAIN


def round_price(value: float) -> float:
    """涨跌停价按交易所口径四舍五入到分。"""

    return math.floor(value * 100 + 0.5) / 100


def compute_price_limits(
    prev_close: float | None,
    *,
    symbol: str = "",
    is_st: bool = False,
    ratio: float | None = None,
) -> tuple[float | None, float | None]:
    if prev_close is None:
        return None, None
    effective = ratio if ratio is not None else limit_ratio_for(symbol, is_st=is_st)
    return round_price(prev_close * (1 + effective)), round_price(
        prev_close * (1 - effective)
    )


def round_to_lot(qty: float, lot_size: int = 100, *, mode: str = "floor") -> int:
    """按手数取整（默认向下）；负数或非法输入返回 0，绝不产生负股数。"""

    if lot_size <= 0:
        raise ValueError("lot_size must be > 0")
    number = to_float(qty)
    if number is None or number <= 0:
        return 0
    if mode == "floor":
        units = math.floor(number / lot_size)
    elif mode == "ceil":
        units = math.ceil(number / lot_size)
    elif mode == "nearest":
        units = math.floor(number / lot_size + 0.5)
    else:
        raise ValueError(f"unknown rounding mode: {mode!r}")
    return int(max(0, units) * lot_size)


@dataclass(frozen=True)
class QuoteSnapshot:
    """下单时点的行情快照（可成交性判定的唯一市场信息源）。"""

    symbol: str = ""
    trade_date: str = ""
    price: float | None = None
    prev_close: float | None = None
    limit_up: float | None = None
    limit_down: float | None = None
    suspended: bool | None = None
    sealed_at_limit_up: bool | None = None
    sealed_at_limit_down: bool | None = None
    is_yizi_board: bool | None = None
    turnover_amount: float | None = None
    is_st: bool = False
    as_of: str | None = None
    missing: tuple[str, ...] = ()

    @classmethod
    def from_mapping(
        cls, payload: Mapping[str, Any], *, symbol: str = "", trade_date: str = ""
    ) -> QuoteSnapshot:
        data = dict(payload)
        tri_state_keys = (
            "suspended",
            "sealed_at_limit_up",
            "sealed_at_limit_down",
            "is_yizi_board",
        )

        def tri(key: str) -> bool | None:
            if key not in data or data[key] is None:
                return None
            return bool(data[key])

        missing: list[str] = []
        for key in tri_state_keys:
            if tri(key) is None and key != "is_yizi_board":
                missing.append(f"{key}_unknown")
        snap = cls(
            symbol=str(data.get("symbol") or symbol),
            trade_date=str(data.get("trade_date") or trade_date),
            price=to_float(
                data.get("price") or data.get("last_price") or data.get("close")
            ),
            prev_close=to_float(data.get("prev_close") or data.get("pre_close")),
            limit_up=to_float(data.get("limit_up") or data.get("upper_limit")),
            limit_down=to_float(data.get("limit_down") or data.get("lower_limit")),
            suspended=tri("suspended"),
            sealed_at_limit_up=tri("sealed_at_limit_up"),
            sealed_at_limit_down=tri("sealed_at_limit_down"),
            is_yizi_board=tri("is_yizi_board"),
            turnover_amount=to_float(data.get("turnover_amount") or data.get("amount")),
            is_st=bool(data.get("is_st") or False),
            as_of=(None if data.get("as_of") is None else str(data.get("as_of"))),
            missing=tuple(missing),
        )
        if snap.limit_up is None or snap.limit_down is None:
            up, down = compute_price_limits(
                snap.prev_close, symbol=snap.symbol, is_st=snap.is_st
            )
            snap = _fill_limits(snap, up, down)
            if snap.limit_up is None:
                snap = _add_missing(snap, "price_limits_unknown")
        return snap


def _fill_limits(
    snap: QuoteSnapshot, up: float | None, down: float | None
) -> QuoteSnapshot:
    return replace(
        snap,
        limit_up=snap.limit_up if snap.limit_up is not None else up,
        limit_down=snap.limit_down if snap.limit_down is not None else down,
    )


def _add_missing(snap: QuoteSnapshot, reason: str) -> QuoteSnapshot:
    return replace(snap, missing=snap.missing + (reason,))


@dataclass(frozen=True)
class Violation:
    """一条硬拦截（订单不可提交 / 必须缩减）。"""

    rule_id: str
    detail: str
    threshold: float | None = None
    actual: float | None = None
    price: float | None = None
    at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "detail": self.detail,
            "threshold": self.threshold,
            "actual": self.actual,
            "price": self.price,
            "at": self.at,
        }


@dataclass(frozen=True)
class TradeRuleResult:
    allowed: bool
    violations: tuple[Violation, ...] = ()
    warnings: tuple[Violation, ...] = ()
    checks_skipped: tuple[str, ...] = ()
    max_buy_qty: int | None = None
    sellable_qty: int | None = None

    @property
    def rule_ids(self) -> tuple[str, ...]:
        return tuple(item.rule_id for item in self.violations)

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "violations": [item.to_dict() for item in self.violations],
            "warnings": [item.to_dict() for item in self.warnings],
            "checks_skipped": list(self.checks_skipped),
            "max_buy_qty": self.max_buy_qty,
            "sellable_qty": self.sellable_qty,
        }


@dataclass
class _Accumulator:
    violations: list[Violation] = field(default_factory=list)
    warnings: list[Violation] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def block(self, **kwargs: Any) -> None:
        self.violations.append(Violation(**kwargs))

    def warn(self, **kwargs: Any) -> None:
        self.warnings.append(Violation(**kwargs))

    def skip(self, reason: str) -> None:
        self.skipped.append(reason)


def sellable_quantity(
    holding: Holding | None,
    *,
    trade_date: str,
) -> int:
    """T+1 可卖股数：建仓日 == 交易日则冻结（零股清仓另由校验层判定）。"""

    if holding is None or holding.qty <= 0:
        return 0
    open_day = str(holding.open_date or "").replace("-", "")[:8]
    decision_day = str(trade_date or "").replace("-", "")[:8]
    if open_day and decision_day and open_day == decision_day:
        return 0  # T+1：当日买入不可卖
    return int(holding.qty)


def validate_order(
    order: OrderRequest,
    *,
    holding: Holding | None = None,
    quote: QuoteSnapshot | Mapping[str, Any] | None = None,
    trade_date: str = "",
    max_buy_amount: float | None = None,
    config: TimingConfig | None = None,
    allow_limit_up_queue_buy: bool = False,
) -> TradeRuleResult:
    """校验一笔拟下单是否可提交（并给出可执行上限）。"""

    config = config or TimingConfig()
    limits = config.portfolio
    lot = limits.lot_size
    acc = _Accumulator()
    snap: QuoteSnapshot | None
    if quote is None:
        snap = None
        acc.skip("quote_missing:停牌/涨跌停可成交性未校验")
    elif isinstance(quote, QuoteSnapshot):
        snap = quote
    else:
        snap = QuoteSnapshot.from_mapping(
            quote, symbol=order.symbol, trade_date=trade_date
        )
    if snap is not None:
        for reason in snap.missing:
            acc.skip(f"quote_field_missing:{reason}")

    if not order.symbol:
        acc.block(rule_id="trade:symbol_required", detail="缺少证券代码")
    price = order.price if order.price is not None else (snap.price if snap else None)
    if price is None or price <= 0:
        acc.block(
            rule_id="trade:price_required", detail="缺少有效委托价格，无法计算整手数"
        )

    # ── 100 股整数倍 / 零股 ──
    qty = order.qty
    if qty is None and order.amount is not None and price:
        qty = round_to_lot(order.amount / price, lot, mode="floor")
    if qty is not None and qty < 0:
        acc.block(rule_id="trade:qty_negative", detail=f"委托数量非法: {qty}")
        qty = None
    if order.is_buy and qty is not None and qty > 0 and qty % lot != 0:
        acc.block(
            rule_id="trade:lot_size",
            detail=f"买入必须为 {lot} 股整数倍（收到 {qty} 股）",
            threshold=float(lot),
            actual=float(qty),
            price=price,
        )

    # ── 停牌 ──
    if snap is not None and snap.suspended is True:
        acc.block(
            rule_id="trade:suspended",
            detail="停牌，不可成交",
            price=price,
            at=snap.as_of,
        )

    # ── 涨跌停：价格越界 / 封板不可成交 ──
    if snap is not None and price is not None:
        if snap.limit_up is not None and price > snap.limit_up + 1e-9:
            acc.block(
                rule_id="trade:price_above_limit_up",
                detail=f"委托价 {price} 超过涨停价 {snap.limit_up}",
                threshold=snap.limit_up,
                actual=price,
                price=price,
            )
        if snap.limit_down is not None and price < snap.limit_down - 1e-9:
            acc.block(
                rule_id="trade:price_below_limit_down",
                detail=f"委托价 {price} 低于跌停价 {snap.limit_down}",
                threshold=snap.limit_down,
                actual=price,
                price=price,
            )
        if order.is_buy:
            if snap.sealed_at_limit_up is True and not allow_limit_up_queue_buy:
                acc.block(
                    rule_id="trade:limit_up_sealed",
                    detail=(
                        "涨停封板排队成交不确定"
                        + ("（一字板）" if snap.is_yizi_board else "")
                        + "，默认不假定成交"
                    ),
                    threshold=snap.limit_up,
                    price=price,
                    at=snap.as_of,
                )
            elif snap.is_yizi_board is True and not allow_limit_up_queue_buy:
                acc.block(
                    rule_id="trade:yizi_board",
                    detail="一字板买不到，拒绝买入",
                    price=price,
                    at=snap.as_of,
                )
            if snap.sealed_at_limit_down is True:
                acc.warn(
                    rule_id="trade:buy_on_limit_down",
                    detail="标的跌停，买入风险提醒（不硬拦）",
                    price=price,
                )
        else:
            if snap.sealed_at_limit_down is True:
                acc.block(
                    rule_id="trade:limit_down_sealed",
                    detail="跌停封单卖不出，禁止卖出",
                    threshold=snap.limit_down,
                    price=price,
                    at=snap.as_of,
                )

    # ── T+1 与可卖数量 ──
    sellable: int | None = None
    if not order.is_buy:
        sellable = sellable_quantity(
            holding,
            trade_date=trade_date or (snap.trade_date if snap else ""),
        )
        if holding is None:
            acc.block(rule_id="trade:position_missing", detail="无对应持仓，不能卖出")
        elif sellable == 0:
            acc.block(
                rule_id="trade:t_plus_1",
                detail=f"T+1 限制：{holding.open_date} 建仓当日不可卖",
                price=price,
            )
        elif qty is not None and qty > sellable:
            acc.block(
                rule_id="trade:sell_exceeds_sellable",
                detail=f"卖出 {qty} 股超过可卖 {sellable} 股",
                threshold=float(sellable),
                actual=float(qty),
                price=price,
            )
        odd_lot = (
            qty is not None
            and qty > 0
            and sellable
            and qty <= sellable
            and qty % lot != 0
        )
        if odd_lot:
            closing_all = holding is not None and qty == holding.qty
            if not (limits.allow_odd_lot_sell and closing_all):
                acc.block(
                    rule_id="trade:odd_lot_sell",
                    detail=f"卖出需为 {lot} 股整数倍，零股仅可在清仓时卖出",
                    threshold=float(lot),
                    actual=float(qty or 0),
                    price=price,
                )

    # ── 买入金额上限 ──
    amount = (
        order.amount
        if order.amount is not None
        else (qty * price if qty and price else None)
    )
    max_qty: int | None = None
    if order.is_buy:
        if amount is not None and amount <= 0:
            acc.block(
                rule_id="trade:amount_non_positive",
                detail=f"买入金额非法: {amount}",
                price=price,
            )
        if (
            max_buy_amount is not None
            and amount is not None
            and amount > max_buy_amount + 1e-6
        ):
            acc.block(
                rule_id="trade:max_buy_amount",
                detail=f"买入金额 {round(amount, 2)} 超上限 {round(max_buy_amount, 2)}",
                threshold=round(max_buy_amount, 2),
                actual=round(amount, 2),
                price=price,
            )
        if price:
            budget = amount if amount is not None else max_buy_amount
            if budget is not None:
                if max_buy_amount is not None:
                    budget = min(budget, max_buy_amount)
                max_qty = round_to_lot(budget / price, lot, mode="floor")

    return TradeRuleResult(
        allowed=not acc.violations,
        violations=tuple(acc.violations),
        warnings=tuple(acc.warnings),
        checks_skipped=tuple(dict.fromkeys(acc.skipped)),
        max_buy_qty=max_qty,
        sellable_qty=sellable,
    )
