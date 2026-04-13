from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd

from app.config import POOL_BUY, POOL_CANDIDATE, POOL_FOCUS, StrategyConfig
from app.services.data_provider import to_float


def clamp(value: float, min_v: float, max_v: float) -> float:
    return max(min_v, min(value, max_v))


def get_last_n_trade_window(trade_days_df: pd.DataFrame, base_date: str, n: int) -> tuple[str, str]:
    date_format = "%Y-%m-%d"
    target = datetime.strptime(base_date, date_format)

    days = trade_days_df.copy()
    days["trade_date"] = pd.to_datetime(days["trade_date"])
    selected = days[days["trade_date"] < target].tail(n)["trade_date"].tolist()
    if len(selected) < n:
        raise ValueError("Not enough trade days to calculate lookback window")

    return selected[0].strftime("%Y%m%d"), selected[-1].strftime("%Y%m%d")


def is_main_board_stock(code: str, name: str, config: StrategyConfig) -> bool:
    code = str(code)
    name = str(name)
    if config.exclude_st and "ST" in name.upper():
        return False
    if config.exclude_gem and code.startswith("30"):
        return False
    if config.exclude_star and code.startswith("688"):
        return False
    if code.startswith(("43", "8", "9")):
        return False
    return code.startswith(("00", "60", "30", "688"))


def prefilter_universe(snapshot: pd.DataFrame, config: StrategyConfig) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if snapshot.empty:
        return result

    for _, row in snapshot.iterrows():
        code = str(row.get("代码", "")).strip()
        name = str(row.get("名称", "")).strip()
        if not code or not is_main_board_stock(code, name, config):
            continue

        price = to_float(row.get("最新价"))
        market_cap_raw = row.get("总市值")
        market_cap = to_float(market_cap_raw, default=-1)
        if price <= config.close_price_threshold:
            continue
        has_market_cap = market_cap_raw is not None and str(market_cap_raw).strip() not in {"", "nan", "NaN", "<NA>"}
        if has_market_cap and (market_cap <= config.market_capital_low or market_cap > config.market_capital_high):
            continue

        result.append(
            {
                "symbol": code,
                "name": name,
                "price": price,
                "open": to_float(row.get("今开")),
                "pre_close": to_float(row.get("昨收")),
                "volume": to_float(row.get("成交量")),
                "amount": to_float(row.get("成交额")),
                "market_cap": market_cap,
            }
        )
    return result


def analyze_adjustment_candidate(
    stock: dict[str, Any],
    history: pd.DataFrame,
    config: StrategyConfig,
) -> tuple[bool, list[str], dict[str, Any]]:
    if history.empty or len(history) < config.period_days:
        return False, [], {}

    recent = history.tail(config.period_days).copy()

    highs = recent["最高"].astype(float)
    lows = recent["最低"].astype(float)
    closes = recent["收盘"].astype(float)
    opens = recent["开盘"].astype(float)
    volumes = recent["成交量"].astype(float)
    amounts = recent["成交额"].astype(float) if "成交额" in recent.columns else volumes * closes

    hhv20 = float(highs.max())
    llv20 = float(lows.min())
    close = float(closes.iloc[-1])
    open_ = float(opens.iloc[-1])

    box_range = (hhv20 - llv20) / max(llv20, 0.01)
    amp5 = ((highs.tail(5) - lows.tail(5)) / lows.tail(5).replace(0, 0.01)).fillna(0.0)
    amp20 = ((highs - lows) / lows.replace(0, 0.01)).fillna(0.0)
    amp_ratio = float(amp5.mean() / max(float(amp20.mean()), 0.001))

    vol_ma5 = float(volumes.tail(5).mean())
    vol_ma10 = float(volumes.tail(10).mean())
    vol_ma20 = float(volumes.mean())
    avg_amount20 = float(amounts.mean())
    vol_shrink_ratio = vol_ma5 / max(vol_ma20, 1)
    vol_recover_ratio = vol_ma5 / max(vol_ma10, 1)

    not_breakout = close <= hhv20 * config.pre_breakout_buffer
    close_ge_open = close >= open_
    shadow_support = float(min(open_, close) - float(lows.iloc[-1])) / max(close, 0.01)

    last_ret = (close / max(float(closes.iloc[-2]), 0.01)) - 1 if len(closes) >= 2 else 0.0
    last_vol_ratio = float(volumes.iloc[-1]) / max(vol_ma20, 1)
    chase_risk = bool(last_ret >= config.chase_risk_return and last_vol_ratio >= config.chase_risk_vol_ratio)

    pass_rules = (
        box_range <= config.box_range_threshold
        and amp_ratio <= config.amp_ratio_threshold
        and vol_shrink_ratio <= config.volume_shrink_threshold
        and vol_recover_ratio >= config.volume_recover_threshold
        and not_breakout
        and close_ge_open
        and shadow_support >= config.shadow_support_threshold
        and (not chase_risk)
    )

    reasons = []
    if box_range <= config.box_range_threshold:
        reasons.append("缩量横盘收敛")
    if amp_ratio <= config.amp_ratio_threshold:
        reasons.append("近5日振幅收敛")
    if vol_shrink_ratio <= config.volume_shrink_threshold:
        reasons.append("成交量低于中期均量")
    if vol_recover_ratio >= config.volume_recover_threshold:
        reasons.append("量能出现温和恢复")
    if close_ge_open and shadow_support >= config.shadow_support_threshold:
        reasons.append("止跌企稳，下影承接")
    if not_breakout:
        reasons.append("临近前高但未突破")
    if chase_risk:
        reasons.append("末端爆量长阳，追高风险")

    metrics = {
        "box_range": box_range,
        "amp_ratio": amp_ratio,
        "vol_shrink_ratio": vol_shrink_ratio,
        "vol_recover_ratio": vol_recover_ratio,
        "breakout_level": hhv20,
        "avg_amount20": avg_amount20,
        "close": close,
        "open": open_,
        "chase_risk": chase_risk,
    }
    return pass_rules, reasons, metrics


def compute_intraday_score(
    entry: dict[str, Any],
    market_row: dict[str, Any],
    elapsed_ratio: float,
    config: StrategyConfig,
) -> tuple[float, dict[str, float], dict[str, Any], list[str]]:
    price = to_float(market_row.get("最新价"))
    open_ = to_float(market_row.get("今开"))
    pre_close = to_float(market_row.get("昨收"))
    day_high = to_float(market_row.get("最高"), price)
    amount = to_float(market_row.get("成交额"))
    volume = to_float(market_row.get("成交量"))
    pct_change = to_float(market_row.get("涨跌幅"))

    breakout_level = to_float(entry.get("breakout_level"))
    avg_amount20 = to_float(entry.get("avg_amount20"), 1)

    breakout_ratio = (price / max(breakout_level, 0.01)) - 1
    breakout_score = clamp(breakout_ratio / 0.03, 0, 1) * config.score_weight_breakout

    expected_amount = avg_amount20 * max(elapsed_ratio, 0.01)
    volume_ratio = amount / max(expected_amount, 1)
    volume_score = clamp(volume_ratio / 2, 0, 1) * config.score_weight_volume

    vwap = amount / volume if volume > 0 else price
    above_vwap = price >= vwap
    drawdown_from_high = (day_high - price) / max(day_high, 0.01)
    structure_score = 0.0
    if above_vwap:
        structure_score += config.score_weight_above_vwap
    if price >= open_:
        structure_score += config.score_weight_close_ge_open
    structure_score += clamp((0.03 - drawdown_from_high) / 0.03, 0, 1) * config.score_weight_drawdown

    gap_up = (open_ / max(pre_close, 0.01)) - 1 if pre_close > 0 else 0.0
    penalty = 0.0
    penalty += clamp((gap_up - 0.03) / 0.05, 0, 1) * config.penalty_gap_up
    penalty += clamp((drawdown_from_high - 0.03) / 0.04, 0, 1) * config.penalty_drawdown
    if pct_change >= config.near_limit_pct:
        penalty += config.penalty_near_limit

    final_score = clamp(breakout_score + volume_score + structure_score - penalty, 0, 100)

    warnings = []
    if gap_up > 0.06:
        warnings.append("高开幅度过大")
    if drawdown_from_high > 0.03:
        warnings.append("冲高回落明显")
    if pct_change >= config.near_limit_pct:
        warnings.append("接近涨停，流动性风险增加")

    breakdown = {
        "breakout_strength": round(breakout_score, 2),
        "volume_quality": round(volume_score, 2),
        "intraday_structure": round(structure_score, 2),
        "risk_penalty": round(penalty, 2),
        "total": round(final_score, 2),
    }

    metrics = {
        "price": price,
        "open": open_,
        "pre_close": pre_close,
        "day_high": day_high,
        "breakout_level": breakout_level,
        "breakout_ratio": breakout_ratio,
        "volume_ratio": volume_ratio,
        "vwap": vwap,
        "above_vwap": above_vwap,
        "amount": amount,
        "volume": volume,
        "pct_change": pct_change,
        "drawdown_from_high": drawdown_from_high,
        "expected_amount": expected_amount,
    }
    return final_score, breakdown, metrics, warnings


def apply_transition_rules(entry: dict[str, Any], config: StrategyConfig) -> dict[str, Any]:
    transitions = entry.setdefault(
        "transitions",
        {
            "above60_count": 0,
            "breakout_confirm_count": 0,
            "below65_count": 0,
        },
    )

    score = float(entry.get("score", 0))
    metrics = entry.get("metrics", {})
    pool = entry.get("pool", POOL_CANDIDATE)

    if score >= config.focus_score_threshold:
        transitions["above60_count"] += 1
    else:
        transitions["above60_count"] = 0

    breakout_ready = bool(
        metrics.get("price", 0) >= metrics.get("breakout_level", 1) * config.buy_breakout_price_buffer
        and metrics.get("volume_ratio", 0) >= config.buy_volume_ratio_threshold
        and metrics.get("above_vwap", False)
    )

    if breakout_ready:
        transitions["breakout_confirm_count"] += 1
    else:
        transitions["breakout_confirm_count"] = 0

    if pool == POOL_BUY and score < config.downgrade_score_threshold:
        transitions["below65_count"] += 1
    else:
        transitions["below65_count"] = 0

    recommended_pool = None
    trigger_notes: list[str] = []
    auto_move_to = None

    if pool == POOL_CANDIDATE:
        if score >= config.focus_score_immediate or transitions["above60_count"] >= config.focus_consecutive_minutes:
            recommended_pool = POOL_FOCUS
            trigger_notes.append("满足重点池条件")

    if pool in {POOL_CANDIDATE, POOL_FOCUS}:
        if (
            score >= config.buy_score_threshold
            and transitions["breakout_confirm_count"] >= config.buy_breakout_consecutive_minutes
        ):
            recommended_pool = POOL_BUY
            trigger_notes.append("满足买入池触发条件")

    if pool == POOL_BUY and transitions["below65_count"] >= config.downgrade_consecutive_minutes:
        auto_move_to = POOL_FOCUS
        trigger_notes.append("触发买入池自动降级")

    return {
        "recommended_pool": recommended_pool,
        "auto_move_to": auto_move_to,
        "trigger_notes": trigger_notes,
        "transitions": transitions,
        "breakout_ready": breakout_ready,
    }
