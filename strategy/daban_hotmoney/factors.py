"""个股层面的游资聚合因子（issue #34「个股聚合」小节）。

约定沿用 #24（:mod:`strategy.first_limit_alpha.microstructure_features`）：

* 每个因子配 ``<name>_is_missing`` 指示列，缺失一律 ``NULL``，**绝不填 0**；
* 代理口径配 ``<name>_is_proxy`` 指示列（如 ``hot_money_x_theme_is_proxy``）；
* PIT：``as_of_date`` 的行只用**当日盘后已披露**的龙虎榜 + 当日板行，
  席位分数来自 :mod:`strategy.daban_hotmoney.profile` 的滚动窗口画像
  （其观察日已 ``<= as_of_date``），因此整张表可以在 T 日收盘后算、
  用于 T+1 决策，不含任何未来数据。

默认只统计**可靠席位**（``is_reliable=1``），不可靠席次的信息量按 issue
要求不进入评分；``hot_money_count_all`` / ``hot_money_reliable_share``
保留了"含不可靠"的对照口径，便于研究为什么被过滤掉。
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from strategy.daban_hotmoney.profile import (
    SeatProfileConfig,
    SeatProfileReport,
    build_seat_events,
    build_seat_profiles,
)
from strategy.daban_hotmoney.source import (
    load_board_rows,
    load_dragon_tiger,
    load_seat_events,
    open_readonly,
)


@dataclass(frozen=True, slots=True)
class HotMoneyFactorConfig:
    """聚合因子口径。"""

    high_alpha_threshold: float = 0.60
    high_risk_threshold: float = 0.50
    huddle_min_seats: int = 2
    theme_heat_cap: float = 10.0
    board_position_cap: int = 5
    include_unreliable: bool = False


@dataclass(frozen=True, slots=True)
class FactorSpec:
    """因子注册表条目（与 #24 同构，便于统一进 FACTOR_CATALOG）。"""

    name: str
    group: str
    formula: str
    unit: str
    source: str
    missing_rule: str
    pit_note: str
    is_proxy_capable: bool = False


_SPECS: tuple[FactorSpec, ...] = (
    FactorSpec(
        "hot_money_count",
        "游资席位",
        "count(当日买方席位 where is_reliable=1)",
        "个",
        "dragon_tiger_seat_daily + seat_profile",
        "未上榜→NULL（is_missing=1）",
        "T 日盘后可知",
    ),
    FactorSpec(
        "hot_money_count_all",
        "游资席位",
        "count(当日全部买方席位，含不可靠席位)",
        "个",
        "dragon_tiger_seat_daily",
        "未上榜→NULL",
        "T 日盘后可知",
    ),
    FactorSpec(
        "hot_money_net_buy",
        "游资席位",
        "sum(可靠席位当日 net_amount)",
        "元",
        "dragon_tiger_seat_daily",
        "源 net_amount 缺失→NULL",
        "T 日盘后可知",
    ),
    FactorSpec(
        "hot_money_buy_amount",
        "游资席位",
        "sum(可靠席位当日 buy_amount)",
        "元",
        "dragon_tiger_seat_daily",
        "源 buy_amount 缺失→NULL",
        "T 日盘后可知",
    ),
    FactorSpec(
        "hot_money_alpha_score",
        "游资席位",
        "按 buy_amount 加权的 seat_alpha_score 均值",
        "0-1",
        "seat_profile",
        "无可靠席位→NULL（不返回 0.5 中性值）",
        "画像观察日 <= as_of",
    ),
    FactorSpec(
        "hot_money_risk_score",
        "游资席位",
        "按 sell_amount 加权的 seat_risk_score 均值",
        "0-1",
        "seat_profile",
        "无可靠卖方席位→NULL",
        "画像观察日 <= as_of",
    ),
    FactorSpec(
        "seat_concentration",
        "游资席位",
        "max(买方席位 buy_amount) / sum(买方 buy_amount)",
        "0-1",
        "dragon_tiger_seat_daily",
        "买方金额全缺→NULL",
        "T 日盘后可知",
    ),
    FactorSpec(
        "seat_conflict",
        "游资席位",
        "当日该股买卖同席的席位个数",
        "个",
        "dragon_tiger_seat_daily",
        "未上榜→NULL",
        "T 日盘后可知",
    ),
    FactorSpec(
        "seat_conflict_flag",
        "游资席位",
        "1 if seat_conflict >= 1 else 0",
        "0/1",
        "dragon_tiger_seat_daily",
        "未上榜→NULL",
        "T 日盘后可知",
    ),
    FactorSpec(
        "hot_money_reliable_share",
        "游资席位",
        "可靠买方席位数 / 全部买方席位数",
        "0-1",
        "seat_profile",
        "无买方席位→NULL",
        "T 日盘后可知",
    ),
    FactorSpec(
        "hot_money_huddle_seats",
        "游资抱团",
        "count(可靠席位 where seat_alpha_score >= high_alpha_threshold)",
        "个",
        "seat_profile",
        "无可靠席位→NULL",
        "画像观察日 <= as_of",
    ),
    FactorSpec(
        "hot_money_huddle_flag",
        "游资抱团",
        "1 if hot_money_huddle_seats >= huddle_min_seats else 0",
        "0/1",
        "seat_profile",
        "无可靠席位→NULL",
        "画像观察日 <= as_of",
    ),
    FactorSpec(
        "hot_money_x_theme",
        "游资×题材",
        "hot_money_alpha_score * min(1, 当日同题材涨停家数 / theme_heat_cap)",
        "0-1",
        "limit_board_daily.theme(kpl)",
        "alpha 或 theme 缺失→NULL",
        "题材与家数均为 T 日盘后口径",
        True,
    ),
    FactorSpec(
        "hot_money_x_board_position",
        "游资×板位",
        "hot_money_alpha_score * min(1, (limit_times-1)/board_position_cap)",
        "0-1",
        "limit_board_daily.limit_times",
        "alpha 或 limit_times 缺失→NULL",
        "T 日盘后可知",
    ),
    FactorSpec(
        "hot_money_theme_match",
        "游资×题材",
        "可靠买方席位中，历史偏好题材命中当日题材的席位占比",
        "0-1",
        "seat_profile.pref_theme_share",
        "无题材→NULL",
        "画像观察日 <= as_of",
    ),
)

PROXY_FACTORS: tuple[str, ...] = ("hot_money_x_theme",)

#: 席位分数之外、需要显式标注代理的标签口径
LABEL_PROXY_REASON = (
    "游资名录来自 #22 的营业部名代理（hm_list/hm_detail 无权限），"
    "席位统计可用，但任何'游资身份'标签均不可用"
)


@dataclass(slots=True)
class HotMoneyFactorResult:
    """因子表 + 质量报告（覆盖率 / 缺失率 / 代理率）。"""

    as_of_date: str
    rows: list[dict[str, Any]] = field(default_factory=list)
    specs: list[dict[str, Any]] = field(default_factory=list)
    quality: dict[str, Any] = field(default_factory=dict)
    profile_summary: dict[str, Any] = field(default_factory=dict)
    label_meta: dict[str, Any] = field(default_factory=dict)

    def by_symbol(self) -> dict[str, dict[str, Any]]:
        return {str(row["symbol"]): row for row in self.rows}

    def to_frame(self) -> Any:
        import pandas as pd

        frame = pd.DataFrame(self.rows)
        if frame.empty:
            return frame.reindex(columns=hot_money_output_columns())
        return frame


def _spec_rows() -> list[dict[str, Any]]:
    return [asdict(spec) for spec in _SPECS]


def hot_money_factor_specs() -> list[dict[str, Any]]:
    """因子目录：名称、分组、公式、单位、来源、缺失规则、PIT 说明、是否代理。"""
    return _spec_rows()


def hot_money_output_columns() -> list[str]:
    columns = ["symbol", "trade_date", "name"]
    for spec in _SPECS:
        columns.append(spec.name)
        columns.append(f"{spec.name}_is_missing")
    columns.extend(f"{name}_is_proxy" for name in PROXY_FACTORS)
    columns += [
        "hot_money_label_is_proxy",
        "hot_money_seats_detail",
        "hot_money_listed_reason",
    ]
    return columns


def _safe_div(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def _theme_heat(board_rows: Iterable[dict[str, Any]]) -> dict[str, int]:
    """当日各题材涨停家数（盘后可知，不含未来）。"""
    heat: dict[str, int] = {}
    for row in board_rows:
        theme = str(row.get("theme") or "").strip()
        if not theme or str(row.get("limit_flag") or "").upper() != "U":
            continue
        for part in [piece.strip() for piece in theme.split(",") if piece.strip()]:
            heat[part] = heat.get(part, 0) + 1
    return heat


def _seat_score_lookup(report: SeatProfileReport) -> dict[str, dict[str, Any]]:
    return report.by_seat()


def build_hot_money_factors(
    db_path: str | Path | None = None,
    *,
    conn: sqlite3.Connection | None = None,
    as_of_date: str,
    config: HotMoneyFactorConfig | None = None,
    profiles: SeatProfileReport | None = None,
    profile_config: SeatProfileConfig | None = None,
    symbols: Iterable[str] | None = None,
) -> HotMoneyFactorResult:
    """计算 ``as_of_date`` 收盘后（T 日盘后）可得的个股游资因子表。"""
    cfg = config or HotMoneyFactorConfig()
    own_conn = conn is None
    connection = conn if conn is not None else open_readonly(db_path)  # type: ignore[arg-type]
    try:
        profile_report = profiles or build_seat_profiles(
            conn=connection, as_of_date=as_of_date, config=profile_config
        )
        scores = _seat_score_lookup(profile_report)
        day_events, _coverage = build_seat_events(
            load_seat_events(connection, start_date=as_of_date, upto_date=as_of_date)
        )
        board_rows = load_board_rows(connection, start_date=as_of_date, upto_date=as_of_date)
        board_by_symbol = {
            str(row["symbol"]).zfill(6): row for row in board_rows if row.get("symbol")
        }
        heat = _theme_heat(board_rows)
        name_by_symbol: dict[str, str] = {}
        for row in load_dragon_tiger(connection, start_date=as_of_date, upto_date=as_of_date):
            key = str(row.get("symbol") or "").zfill(6)
            if key and row.get("name"):
                name_by_symbol[key] = str(row["name"])

        grouped: dict[str, list[Any]] = {}
        for event in day_events:
            grouped.setdefault(event.symbol, []).append(event)

        wanted = None if symbols is None else {str(s).zfill(6) for s in symbols}
        universe = set(grouped) | set(board_by_symbol)
        if wanted is not None:
            universe &= wanted

        rows = [
            _factor_row(
                symbol,
                as_of_date,
                grouped.get(symbol, []),
                scores,
                cfg,
                board=board_by_symbol.get(symbol),
                theme_heat=heat,
                name=name_by_symbol.get(symbol, ""),
                label_is_proxy=int(
                    profile_report.label_meta.get("hot_money_label_is_proxy", 1)
                ),
            )
            for symbol in sorted(universe)
        ]
        return HotMoneyFactorResult(
            as_of_date=as_of_date,
            rows=rows,
            specs=_spec_rows(),
            quality=factor_quality_report(rows),
            profile_summary=profile_report.summary,
            label_meta=profile_report.label_meta,
        )
    finally:
        if own_conn:
            connection.close()


def _factor_row(
    symbol: str,
    as_of_date: str,
    events: list[Any],
    scores: dict[str, dict[str, Any]],
    cfg: HotMoneyFactorConfig,
    *,
    board: dict[str, Any] | None,
    theme_heat: dict[str, int],
    name: str,
    label_is_proxy: int,
) -> dict[str, Any]:
    buyers = [e for e in events if e.has_buy]
    sellers = [e for e in events if e.has_sell]
    reliable_buyers = [e for e in buyers if _reliable(scores.get(e.seat_name_std))]
    reliable_sellers = [e for e in sellers if _reliable(scores.get(e.seat_name_std))]
    usable_buyers = reliable_buyers if not cfg.include_unreliable else buyers

    net_values = [e.net_amount for e in usable_buyers if e.net_amount is not None]
    buy_values = [e.buy_amount for e in usable_buyers if e.buy_amount is not None]
    net_buy = sum(net_values) if net_values else None
    buy_amount = sum(buy_values) if buy_values else None

    weighted_alpha = _weighted_score(usable_buyers, scores, "seat_alpha_score")
    weighted_risk = _weighted_score(reliable_sellers, scores, "seat_risk_score")
    top_buy = max((e.buy_amount or 0.0) for e in usable_buyers) if usable_buyers else None
    concentration = _safe_div(top_buy, buy_amount)
    conflicts = [e for e in events if e.is_conflict]

    huddle = [
        e
        for e in usable_buyers
        if (scores.get(e.seat_name_std) or {}).get("seat_alpha_score") is not None
        and scores[e.seat_name_std]["seat_alpha_score"] >= cfg.high_alpha_threshold
    ]
    theme = str((board or {}).get("theme") or "").strip()
    heat_value = theme_heat.get(theme) if theme else None
    heat_norm = (
        min(1.0, heat_value / cfg.theme_heat_cap) if heat_value is not None else None
    )
    limit_times = (board or {}).get("limit_times")
    position_norm = (
        min(1.0, max(0.0, (int(limit_times) - 1) / max(1, cfg.board_position_cap)))
        if limit_times is not None
        else None
    )
    x_theme = (
        weighted_alpha * heat_norm
        if weighted_alpha is not None and heat_norm is not None
        else None
    )
    x_position = (
        weighted_alpha * position_norm
        if weighted_alpha is not None and position_norm is not None
        else None
    )
    theme_match = _theme_match_share(usable_buyers, scores, theme)

    values: dict[str, Any] = {
        "symbol": symbol,
        "trade_date": as_of_date,
        "name": name,
        "hot_money_count": len(usable_buyers) if events else None,
        "hot_money_count_all": len(buyers) if events else None,
        "hot_money_net_buy": round(net_buy, 2) if net_buy is not None else None,
        "hot_money_buy_amount": round(buy_amount, 2) if buy_amount is not None else None,
        "hot_money_alpha_score": _round(weighted_alpha),
        "hot_money_risk_score": _round(weighted_risk),
        "seat_concentration": _round(concentration),
        "seat_conflict": len(conflicts) if events else None,
        "seat_conflict_flag": (1 if conflicts else 0) if events else None,
        "hot_money_reliable_share": _round(_safe_div(len(reliable_buyers), len(buyers) or None)),
        "hot_money_huddle_seats": len(huddle) if usable_buyers else None,
        "hot_money_huddle_flag": (
            1 if len(huddle) >= cfg.huddle_min_seats else 0 if usable_buyers else None
        ),
        "hot_money_x_theme": _round(x_theme),
        "hot_money_x_board_position": _round(x_position),
        "hot_money_theme_match": _round(theme_match),
        "hot_money_label_is_proxy": label_is_proxy,
        "hot_money_seats_detail": [
            {
                "seat_name_std": event.seat_name_std,
                "sides": list(event.sides),
                "buy_amount": event.buy_amount,
                "sell_amount": event.sell_amount,
                "net_amount": event.net_amount,
                "seat_alpha_score": (scores.get(event.seat_name_std) or {}).get(
                    "seat_alpha_score"
                ),
                "seat_risk_score": (scores.get(event.seat_name_std) or {}).get(
                    "seat_risk_score"
                ),
                "seat_style": (scores.get(event.seat_name_std) or {}).get("seat_style"),
                "is_reliable": (scores.get(event.seat_name_std) or {}).get("is_reliable"),
                "hot_money_label": (scores.get(event.seat_name_std) or {}).get(
                    "hot_money_label"
                ),
            }
            for event in events
        ],
        "hot_money_listed_reason": next(
            (event.reason for event in events if event.reason), None
        ),
    }
    for spec in _SPECS:
        value = values.get(spec.name)
        values[f"{spec.name}_is_missing"] = int(value is None)
    for proxy_name in PROXY_FACTORS:
        values[f"{proxy_name}_is_proxy"] = 1
    return values


def _reliable(profile: dict[str, Any] | None) -> bool:
    return bool(profile and profile.get("is_reliable"))


def _weighted_score(
    events: list[Any],
    scores: dict[str, dict[str, Any]],
    field_name: str,
) -> float | None:
    total_weight = 0.0
    total = 0.0
    for event in events:
        profile = scores.get(event.seat_name_std) or {}
        value = profile.get(field_name)
        if value is None:
            continue
        weight = float(event.buy_amount or 0.0) if field_name == "seat_alpha_score" else float(
            event.sell_amount or 0.0
        )
        weight = weight if weight > 0 else 1.0
        total += float(value) * weight
        total_weight += weight
    return (total / total_weight) if total_weight else None


def _theme_match_share(
    events: list[Any],
    scores: dict[str, dict[str, Any]],
    theme: str,
) -> float | None:
    if not events or not theme:
        return None
    hits = 0
    counted = 0
    for event in events:
        share = (scores.get(event.seat_name_std) or {}).get("pref_theme_share") or {}
        if not share:
            continue
        counted += 1
        if float(share.get(theme, 0.0)) >= 0.25:
            hits += 1
    return (hits / counted) if counted else None


def _round(value: float | None, digits: int = 6) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def factor_quality_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """缺失率 / 代理率（issue 验收项要求每个因子显式标注缺失，不许默默填 0）。"""
    total = len(rows)
    missing_rates: dict[str, float] = {}
    for spec in _SPECS:
        column = f"{spec.name}_is_missing"
        missing_rates[spec.name] = (
            round(sum(int(row.get(column, 1)) for row in rows) / total, 6) if total else 1.0
        )
    proxy_rates = {
        name: (round(sum(int(row.get(f"{name}_is_proxy", 0)) for row in rows) / total, 6) if total else 0.0)
        for name in PROXY_FACTORS
    }
    listed = sum(1 for row in rows if row.get("hot_money_count_all"))
    return {
        "rows_total": total,
        "rows_listed": listed,
        "listed_coverage": round(listed / total, 6) if total else None,
        "missing_rates": missing_rates,
        "proxy_rates": proxy_rates,
        "label_proxy_note": LABEL_PROXY_REASON,
    }


__all__ = [
    "PROXY_FACTORS",
    "FactorSpec",
    "HotMoneyFactorConfig",
    "HotMoneyFactorResult",
    "build_hot_money_factors",
    "factor_quality_report",
    "hot_money_factor_specs",
    "hot_money_output_columns",
]
