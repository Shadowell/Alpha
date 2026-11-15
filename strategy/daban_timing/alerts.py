"""持仓提醒（止损 / 止盈 / 时间止损 / 跌破分时均价 / 封单走弱）。

issue #32 明确「所有提醒先进入"建议"，人工确认后才下单；不自动实盘」，
因此本模块的所有输出都带 ``requires_manual_confirmation=True``，且每条提醒
必须能回答三件事：**触发了哪条规则、什么时间、什么价格**（验收项
「所有提醒可追溯」）。

* 提醒只做"该卖了"的判断，不做 T+1 之外的任何成交假设；
  当日建仓的持仓会额外标注 ``blocked_by_t_plus_1``，只提示次日执行。
* 盘中口径（分时均价、封单金额）依赖实时数据，缺数时**跳过并记录原因**，
  不会用 0 触发"封单走弱"这种假信号。
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .config import TimingConfig
from .models import Holding, to_float
from .risk import BreakerState
from .timing import TimingPlan
from .trade_rules import sellable_quantity

KIND_STOP_LOSS = "stop_loss"
KIND_TAKE_PROFIT = "take_profit"
KIND_PARTIAL_TAKE_PROFIT = "partial_take_profit"
KIND_TIME_STOP = "time_stop"
KIND_TRAILING_STOP = "trailing_stop"
KIND_AVG_PRICE_BREAK = "avg_price_break"
KIND_SEAL_WEAKENING = "seal_weakening"
KIND_BREAKER_RISK = "portfolio_drawdown_breaker"

#: 提醒优先级（数字越小越先展示）。
KIND_SEVERITY: dict[str, int] = {
    KIND_BREAKER_RISK: 0,
    KIND_STOP_LOSS: 1,
    KIND_TAKE_PROFIT: 2,
    KIND_PARTIAL_TAKE_PROFIT: 3,
    KIND_TIME_STOP: 4,
    KIND_TRAILING_STOP: 5,
    KIND_AVG_PRICE_BREAK: 6,
    KIND_SEAL_WEAKENING: 7,
}


@dataclass(frozen=True)
class PositionAlert:
    """一条可追溯的持仓建议（永不自动下单）。"""

    alert_id: str
    symbol: str
    name: str
    kind: str
    rule_id: str
    message: str
    triggered_at: str
    price: float | None
    threshold: float | None
    actual: float | None
    severity: int
    requires_manual_confirmation: bool = True
    suggested_action: str = "人工确认后再操作"
    blocked_by_t_plus_1: bool = False
    sellable_qty: int | None = None
    missing: tuple[str, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "alert_id": self.alert_id,
            "symbol": self.symbol,
            "name": self.name,
            "kind": self.kind,
            "rule_id": self.rule_id,
            "message": self.message,
            "triggered_at": self.triggered_at,
            "price": self.price,
            "threshold": self.threshold,
            "actual": self.actual,
            "severity": self.severity,
            "requires_manual_confirmation": self.requires_manual_confirmation,
            "suggested_action": self.suggested_action,
            "blocked_by_t_plus_1": self.blocked_by_t_plus_1,
            "sellable_qty": self.sellable_qty,
            "missing": list(self.missing),
            "extra": dict(self.extra),
        }
        return payload


def _alert_id(trade_date: str, symbol: str, kind: str, rule_id: str) -> str:
    digest = hashlib.sha1(f"{trade_date}|{symbol}|{kind}|{rule_id}".encode()).hexdigest()[:8]
    return f"alert:{trade_date}:{symbol}:{kind}:{digest}"


def _vwap(quote: Mapping[str, Any]) -> float | None:
    explicit = to_float(quote.get("vwap") or quote.get("avg_price"))
    if explicit is not None:
        return explicit
    amount = to_float(quote.get("turnover_amount") or quote.get("amount"))
    volume = to_float(quote.get("volume"))
    if amount is not None and volume and volume > 0:
        return amount / volume
    return None


def data_gaps(
    holding: Holding | Mapping[str, Any], market: Mapping[str, Any] | None = None
) -> tuple[str, ...]:
    """提醒所缺的输入（缺数显式化，不触发任何假信号）。"""

    item = holding if isinstance(holding, Holding) else Holding.from_mapping(holding)
    quote = dict(market or {})
    gaps: list[str] = []
    price = to_float(quote.get("price") or quote.get("last_price") or item.current_price)
    if price is None:
        gaps.append("price_missing")
    if item.cost_price is None or item.cost_price <= 0:
        gaps.append("cost_price_missing")
    if quote:
        if _vwap(quote) is None:
            gaps.append("vwap_missing")
        if to_float(quote.get("fd_amount") or quote.get("seal_amount")) is None:
            gaps.append("fd_amount_missing")
    return tuple(gaps)


def evaluate_position_alerts(
    holding: Holding | Mapping[str, Any],
    *,
    quote: Mapping[str, Any] | None = None,
    plan: TimingPlan,
    trade_date: str,
    triggered_at: str | None = None,
    config: TimingConfig | None = None,
    peak_price: float | None = None,
    fd_amount_baseline: float | None = None,
    trade_dates: Iterable[str] | None = None,
) -> list[PositionAlert]:
    """单个持仓的提醒列表（含 T+1 状态与缺数说明）。"""

    config = config or TimingConfig()
    alerts_cfg = config.alerts
    item = holding if isinstance(holding, Holding) else Holding.from_mapping(holding)
    market = dict(quote or {})
    price = to_float(market.get("price") or market.get("last_price") or item.current_price)
    moment = str(triggered_at or market.get("as_of") or trade_date)
    missing: list[str] = list(data_gaps(item, market))
    sellable = sellable_quantity(item, trade_date=trade_date)
    blocked = sellable == 0 and item.qty > 0

    def build(
        kind: str,
        rule_id: str,
        message: str,
        threshold: float | None,
        actual: float | None,
        **extra: Any,
    ) -> PositionAlert:
        return PositionAlert(
            alert_id=_alert_id(trade_date, item.symbol, kind, rule_id),
            symbol=item.symbol,
            name=item.name,
            kind=kind,
            rule_id=rule_id,
            message=message,
            triggered_at=moment,
            price=price,
            threshold=threshold,
            actual=actual,
            severity=KIND_SEVERITY.get(kind, 9),
            blocked_by_t_plus_1=blocked,
            sellable_qty=sellable,
            missing=tuple(missing),
            extra={
                "cost_price": item.cost_price,
                "qty": item.qty,
                "open_date": item.open_date,
                "hold_days": extra.pop("hold_days", None),
                **extra,
            },
        )

    out: list[PositionAlert] = []
    if price is None or item.cost_price is None or item.cost_price <= 0:
        # 关键输入缺失：不出提醒，缺数原因由 data_gaps() 统一暴露
        return out

    pnl_pct = (price - item.cost_price) / item.cost_price
    hold_days = _hold_days(
        item.open_date,
        trade_date,
        market.get("hold_days"),
        trade_dates,
    )
    if hold_days is None:
        missing.append("hold_days_unknown")

    if pnl_pct <= -alerts_cfg.stop_loss_pct:
        out.append(
            build(
                KIND_STOP_LOSS,
                "alert:stop_loss",
                f"浮亏 {pnl_pct:.2%} 触及止损线 {-alerts_cfg.stop_loss_pct:.2%}，建议止损",
                -alerts_cfg.stop_loss_pct,
                round(pnl_pct, 6),
                hold_days=hold_days,
            )
        )
    elif pnl_pct >= alerts_cfg.take_profit_pct:
        out.append(
            build(
                KIND_TAKE_PROFIT,
                "alert:take_profit",
                f"浮盈 {pnl_pct:.2%} 达到止盈线 {alerts_cfg.take_profit_pct:.2%}，建议止盈",
                alerts_cfg.take_profit_pct,
                round(pnl_pct, 6),
                hold_days=hold_days,
            )
        )

    # 分批止盈：达到中间档位且当日计划要求"分批止盈"（高潮 regime）
    for step in alerts_cfg.take_profit_steps:
        if step >= alerts_cfg.take_profit_pct:
            continue
        if pnl_pct >= step and any("分批止盈" in action for action in plan.actions):
            out.append(
                build(
                    KIND_PARTIAL_TAKE_PROFIT,
                    "alert:partial_take_profit",
                    f"浮盈 {pnl_pct:.2%} 达到分批止盈档 {step:.2%}（{plan.regime} 建议分批止盈）",
                    step,
                    round(pnl_pct, 6),
                    hold_days=hold_days,
                )
            )
            break

    if hold_days is not None and hold_days >= alerts_cfg.max_hold_days and pnl_pct < 0:
        out.append(
            build(
                KIND_TIME_STOP,
                "alert:time_stop",
                f"持有 {hold_days} 个交易日仍未盈利（{pnl_pct:.2%}），触发时间止损",
                float(alerts_cfg.max_hold_days),
                float(hold_days),
                pnl_pct=round(pnl_pct, 6),
            )
        )

    if alerts_cfg.trailing_stop_from_peak_pct > 0:
        peak = to_float(peak_price if peak_price is not None else market.get("peak_price"))
        if peak is not None and peak > 0:
            pullback = (peak - price) / peak
            if pullback >= alerts_cfg.trailing_stop_from_peak_pct and pnl_pct > 0:
                out.append(
                    build(
                        KIND_TRAILING_STOP,
                        "alert:trailing_stop",
                        f"自最高价 {peak} 回撤 {pullback:.2%}，建议保护浮盈",
                        alerts_cfg.trailing_stop_from_peak_pct,
                        round(pullback, 6),
                        peak_price=peak,
                    )
                )
        else:
            if "peak_price_missing" not in missing:
                missing.append("peak_price_missing")

    vwap = _vwap(market)
    if vwap is not None and vwap > 0 and price < vwap * (1 - alerts_cfg.avg_price_break_tolerance):
        out.append(
            build(
                KIND_AVG_PRICE_BREAK,
                "alert:avg_price_break",
                f"现价 {price} 跌破分时均价 {round(vwap, 3)}，建议关注封单与承接",
                round(vwap, 6),
                price,
            )
        )
    elif market and "vwap_missing" not in missing:
        missing.append("vwap_missing")

    fd_now = to_float(market.get("fd_amount") or market.get("seal_amount"))
    baseline = to_float(fd_amount_baseline if fd_amount_baseline is not None else market.get("fd_amount_prev"))
    if fd_now is not None and baseline and baseline > 0:
        ratio = fd_now / baseline
        if ratio <= 1 - alerts_cfg.seal_weakening_ratio:
            out.append(
                build(
                    KIND_SEAL_WEAKENING,
                    "alert:seal_weakening",
                    f"封单 {fd_now:.0f} 较基准 {baseline:.0f} 走弱（{ratio:.2%}），建议关注炸板风险",
                    round(1 - alerts_cfg.seal_weakening_ratio, 6),
                    round(ratio, 6),
                )
            )
    elif market and "fd_amount_missing" not in missing:
        missing.append("fd_amount_missing")

    return out


def _hold_days(
    open_date: str | None,
    trade_date: str,
    explicit: Any,
    trade_dates: Iterable[str] | None,
) -> int | None:
    """持有交易日数：优先用调用方显式给的天数，其次按交易日历数日子。"""

    value = to_float(explicit)
    if value is not None:
        return int(value)
    if open_date is None or not trade_date or not trade_dates:
        return None
    dates = sorted({str(d) for d in trade_dates})
    if str(open_date) in dates and trade_date in dates:
        return max(0, dates.index(trade_date) - dates.index(str(open_date)))
    return None


def evaluate_portfolio_alerts(
    holdings: Iterable[Holding | Mapping[str, Any]],
    *,
    plan: TimingPlan,
    trade_date: str,
    quotes: Mapping[str, Mapping[str, Any]] | None = None,
    config: TimingConfig | None = None,
    breaker: BreakerState | None = None,
    triggered_at: str | None = None,
    trade_dates: Iterable[str] | None = None,
) -> list[PositionAlert]:
    """组合级提醒：逐仓提醒 + 熔断状态提示，按严重度排序。"""

    config = config or TimingConfig()
    alerts_cfg = config.alerts
    out: list[PositionAlert] = []
    for raw in holdings:
        item = raw if isinstance(raw, Holding) else Holding.from_mapping(raw)
        out.extend(
            evaluate_position_alerts(
                item,
                quote=(quotes or {}).get(item.symbol),
                plan=plan,
                trade_date=trade_date,
                triggered_at=triggered_at,
                config=config,
                trade_dates=trade_dates,
            )
        )
    if breaker is not None and breaker.level != "normal":
        moment = str(triggered_at or trade_date)
        rule_id = breaker.rule_ids[0] if breaker.rule_ids else "risk:breaker"
        out.append(
            PositionAlert(
                alert_id=_alert_id(trade_date, "PORTFOLIO", KIND_BREAKER_RISK, rule_id),
                symbol="PORTFOLIO",
                name="组合",
                kind=KIND_BREAKER_RISK,
                rule_id=rule_id,
                message=(
                    f"组合回撤 {breaker.drawdown:.2%} 触发熔断（{breaker.level}）："
                    + ("停止新开仓" if breaker.level == "halt" else "新开仓降半仓")
                    + f"，止损线维持 {alerts_cfg.stop_loss_pct:.2%}"
                ),
                triggered_at=moment,
                price=None,
                threshold=None,
                actual=breaker.drawdown,
                severity=KIND_SEVERITY[KIND_BREAKER_RISK],
                extra={"peak_equity": breaker.peak_equity, "equity": breaker.equity},
            )
        )
    return sorted(out, key=lambda a: (a.severity, a.symbol, a.alert_id))
