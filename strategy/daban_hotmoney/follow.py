"""跟随 / 反跟策略与个股解释（issue #34「策略研究」小节）。

硬性 PIT 约束
--------------
龙虎榜是**盘后披露**数据，因此：

* 信号日 ``signal_date = as_of_date``（T 日榜单），入场只允许 T+1 竞价 / 开盘；
* 任何"T 日涨停价排队"（``queue_t``）入场都会被 :func:`assert_entry_scene`
  直接 ``ValueError`` 拒绝 —— 这在代码层面写死，不靠调用方自觉。

策略族（全部走同一份可靠席位口径）
----------------------------------
* **跟随**：可靠 + 高 ``seat_alpha_score`` 席位净买 → T+1 接力；
* **反跟 / 回避**：高 ``seat_risk_score`` 或 ``seat_style=dump`` 席位在卖 → 回避该股；
* **游资×题材**：席位历史偏好题材命中当日题材 → 加分（``theme_match_bonus``）；
* **游资抱团**：≥ ``min_huddle_seats`` 个高 alpha 席位同时买入 → 加分；
* **买卖同席**（对倒嫌疑）：默认直接拒绝，不参与跟随。

代理与合规
----------
游资名录若来自 #22 的营业部名代理（``hm_list`` 无权限），评分只用可复现的
席位历史统计，**不使用任何"游资身份"推断**；若把 ``require_curated_label``
打开，则要求买方席位命中人工名录，否则拒绝。输出恒带免责声明，
不含任何自然人 / 账户信息。
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from strategy.daban_hotmoney.factors import (
    HotMoneyFactorConfig,
    HotMoneyFactorResult,
    build_hot_money_factors,
)
from strategy.daban_hotmoney.profile import (
    STYLE_DUMP,
    SeatProfileConfig,
    SeatProfileReport,
    build_seat_events,
    build_seat_profiles,
)
from strategy.daban_hotmoney.seat_names import LABEL_DISCLAIMER
from strategy.daban_hotmoney.source import load_seat_events, open_readonly

#: #26 回测里允许跟在龙虎榜之后的入场场景（T+1 竞价 / 开盘）。
ALLOWED_ENTRY_SCENES = ("next_auction", "next_open", "next_close", "hold_days")
REJECTED_ENTRY_SCENES = ("queue_t", "t_queue", "same_day")

REJECT_NOT_LISTED = "not_listed"
REJECT_NO_BUYER = "no_buy_side_seat"
REJECT_NO_RELIABLE_SEAT = "no_reliable_seat"
REJECT_ALPHA_BELOW = "seat_alpha_below_threshold"
REJECT_RISK_ABOVE = "seat_risk_above_threshold"
REJECT_NET_BUY = "net_buy_not_positive"
REJECT_CONFLICT = "seat_conflict"
REJECT_SCORE_MISSING = "alpha_score_missing"
REJECT_TOP_N_CUT = "top_n_cut"
REJECT_LABEL_REQUIRED = "curated_label_required_but_missing"
AVOID_RISKY_SELLER = "risky_seat_selling"
AVOID_DUMP_STYLE_SELLER = "dump_style_seat_selling"


@dataclass(frozen=True, slots=True)
class FollowConfig:
    """跟随 / 反跟阈值（研究口径，全部随结果导出）。"""

    min_alpha_score: float = 0.62
    max_risk_score: float = 0.55
    require_reliable: bool = True
    min_net_buy_cny: float = 0.0
    reject_conflict: bool = True
    min_huddle_seats: int = 2
    theme_match_min: float = 0.50
    theme_match_bonus: float = 0.05
    huddle_bonus: float = 0.05
    require_curated_label: bool = False
    top_n: int = 20
    entry_scene: str = "next_open"
    exit_scene: str = "hold_days"
    hold_days: int = 1


@dataclass(slots=True)
class FollowCandidate:
    """单只股票的跟随评估结果（接受与拒绝共用同一结构）。"""

    symbol: str
    signal_date: str
    name: str
    follow_score: float | None
    accepted: bool
    reasons: list[str] = field(default_factory=list)
    seats: list[dict[str, Any]] = field(default_factory=list)
    factors: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class FollowResult:
    """跟随 / 回避 / 拒绝三段输出 + 可直接喂给 #26 的信号表。"""

    as_of_date: str
    config: dict[str, Any]
    accepted: list[dict[str, Any]] = field(default_factory=list)
    avoided: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    disclaimer: str = LABEL_DISCLAIMER

    def signals(self) -> list[dict[str, Any]]:
        """#26 ``run_limit_up_backtest`` 可直接消费的信号行。"""
        return [
            {
                "signal_date": row["signal_date"],
                "symbol": row["symbol"],
                "name": row.get("name") or "",
                "follow_score": row.get("follow_score"),
            }
            for row in self.accepted
        ]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def assert_entry_scene(entry_scene: str) -> str:
    """龙虎榜盘后披露 → 禁止 T 日排队入场；非法场景直接抛错。"""
    scene = str(entry_scene or "").strip()
    if scene in REJECTED_ENTRY_SCENES:
        raise ValueError(
            f"入场场景 {scene!r} 违反龙虎榜 PIT：榜单 T 日盘后才披露，"
            f"只允许 T+1 竞价/开盘入场（允许值 {ALLOWED_ENTRY_SCENES}）"
        )
    if scene not in ALLOWED_ENTRY_SCENES:
        raise ValueError(f"未知入场场景 {scene!r}，允许 {ALLOWED_ENTRY_SCENES}")
    return scene


def _acquire(
    db_path: str | Path | None, conn: sqlite3.Connection | None
) -> tuple[sqlite3.Connection, bool]:
    if conn is not None:
        return conn, False
    if db_path is None:
        raise ValueError("必须提供 db_path 或 conn 之一")
    return open_readonly(db_path), True


def build_follow_signals(
    db_path: str | Path | None = None,
    *,
    conn: sqlite3.Connection | None = None,
    as_of_date: str,
    config: FollowConfig | None = None,
    factor_config: HotMoneyFactorConfig | None = None,
    factors: HotMoneyFactorResult | None = None,
    profiles: SeatProfileReport | None = None,
    profile_config: SeatProfileConfig | None = None,
    symbols: Iterable[str] | None = None,
) -> FollowResult:
    """在 ``as_of_date`` 收盘后生成 T+1 跟随 / 回避清单（只读，不写库）。"""
    cfg = config or FollowConfig()
    assert_entry_scene(cfg.entry_scene)
    connection, own_conn = _acquire(db_path, conn)
    try:
        profile_report = (
            profiles
            if profiles is not None
            else build_seat_profiles(
                conn=connection, as_of_date=as_of_date, config=profile_config
            )
        )
        scores = profile_report.by_seat()
        factor_result = factors or build_hot_money_factors(
            conn=connection,
            as_of_date=as_of_date,
            config=factor_config,
            profiles=profile_report,
            profile_config=profile_config,
            symbols=symbols,
        )
        wanted = None if symbols is None else {str(s).zfill(6) for s in symbols}
        candidates: list[FollowCandidate] = []
        avoided: list[dict[str, Any]] = []
        for row in factor_result.rows:
            symbol = str(row["symbol"])
            if wanted is not None and symbol not in wanted:
                continue
            detail = row.get("hot_money_seats_detail") or []
            _collect_avoidance(symbol, as_of_date, detail, scores, cfg, avoided)
            candidates.append(_evaluate(symbol, as_of_date, row, detail, scores, cfg))
        accepted = sorted(
            (c for c in candidates if c.accepted),
            key=lambda c: (-(c.follow_score or 0.0), c.symbol),
        )
        cut, spilled = accepted[: cfg.top_n], accepted[cfg.top_n :]
        for item in spilled:
            item.accepted = False
            item.reasons.append(REJECT_TOP_N_CUT)
        rejected = [c for c in candidates if not c.accepted]
        return FollowResult(
            as_of_date=as_of_date,
            config=asdict(cfg),
            accepted=[item.to_dict() for item in cut],
            avoided=avoided,
            rejected=[item.to_dict() for item in rejected],
            summary={
                "candidates_total": len(candidates),
                "accepted": len(cut),
                "rejected": len(rejected),
                "avoided": len(avoided),
                "entry_scene": cfg.entry_scene,
                "exit_scene": cfg.exit_scene,
                "hold_days": cfg.hold_days,
                "pit_note": "signal_date = T 日榜单日，入场在 T+1；不存在 T 日盘中使用",
                "factor_quality": factor_result.quality,
                "profile_summary": profile_report.summary,
                "label_is_proxy": int(
                    bool(profile_report.label_meta.get("hot_money_label_is_proxy"))
                ),
                "require_curated_label": cfg.require_curated_label,
                "disclaimer": LABEL_DISCLAIMER,
            },
        )
    finally:
        if own_conn:
            connection.close()


def _alpha(entry: dict[str, Any], scores: dict[str, dict[str, Any]]) -> float | None:
    return (scores.get(entry["seat_name_std"]) or {}).get("seat_alpha_score")


def _evaluate(
    symbol: str,
    signal_date: str,
    row: dict[str, Any],
    detail: list[dict[str, Any]],
    scores: dict[str, dict[str, Any]],
    cfg: FollowConfig,
) -> FollowCandidate:
    """跟随判定：可靠高 alpha 席位净买 + 无买卖同席 + 分数过门槛。"""
    buyers = [entry for entry in detail if "buy" in (entry.get("sides") or [])]
    candidate = FollowCandidate(
        symbol=symbol,
        signal_date=signal_date,
        name=str(row.get("name") or ""),
        follow_score=None,
        accepted=False,
        seats=buyers,
        factors={
            key: value
            for key, value in row.items()
            if key.startswith("hot_money_") or key.startswith("seat_")
        },
    )
    if not detail:
        candidate.reasons.append(REJECT_NOT_LISTED)
        return candidate
    if not buyers:
        candidate.reasons.append(REJECT_NO_BUYER)
        return candidate
    reliable = [
        entry
        for entry in buyers
        if int((scores.get(entry["seat_name_std"]) or {}).get("is_reliable") or 0)
    ]
    if cfg.require_reliable and not reliable:
        candidate.reasons.append(REJECT_NO_RELIABLE_SEAT)
        return candidate
    pool = reliable if cfg.require_reliable else buyers
    scored = [entry for entry in pool if _alpha(entry, scores) is not None]
    if not scored:
        candidate.reasons.append(REJECT_SCORE_MISSING)
        return candidate
    if cfg.require_curated_label:
        unlabeled = [
            entry
            for entry in scored
            if not (scores.get(entry["seat_name_std"]) or {}).get("hot_money_label")
        ]
        if unlabeled:
            candidate.reasons.append(
                f"{REJECT_LABEL_REQUIRED}(unlabeled={len(unlabeled)}/{len(scored)})"
            )
            return candidate
    reasons: list[str] = []
    alpha = max(float(_alpha(entry, scores) or 0.0) for entry in scored)
    if alpha < cfg.min_alpha_score:
        reasons.append(f"{REJECT_ALPHA_BELOW}({alpha:.3f}<{cfg.min_alpha_score})")
    net_buy = row.get("hot_money_net_buy")
    if net_buy is None or float(net_buy) <= cfg.min_net_buy_cny:
        reasons.append(f"{REJECT_NET_BUY}({net_buy})")
    risk_values = [
        (scores.get(entry["seat_name_std"]) or {}).get("seat_risk_score")
        for entry in reliable
    ]
    risk_values = [float(value) for value in risk_values if value is not None]
    if risk_values and min(risk_values) > cfg.max_risk_score:
        reasons.append(f"{REJECT_RISK_ABOVE}({min(risk_values):.3f})")
    if cfg.reject_conflict and int(row.get("seat_conflict") or 0) >= 1:
        reasons.append(REJECT_CONFLICT)

    score = alpha
    huddle = [entry for entry in scored if float(_alpha(entry, scores) or 0.0) >= cfg.min_alpha_score]
    if len(huddle) >= max(1, cfg.min_huddle_seats):
        score += cfg.huddle_bonus
    match = row.get("hot_money_theme_match")
    if match is not None and float(match) >= cfg.theme_match_min:
        score += cfg.theme_match_bonus
    candidate.follow_score = round(min(1.0, score), 6)
    candidate.reasons = reasons
    candidate.accepted = not reasons
    return candidate


def _collect_avoidance(
    symbol: str,
    signal_date: str,
    detail: list[dict[str, Any]],
    scores: dict[str, dict[str, Any]],
    cfg: FollowConfig,
    out: list[dict[str, Any]],
) -> None:
    """反跟：砸盘风格 / 高风险席位当日在卖 → 进回避清单。"""
    for entry in detail:
        if "sell" not in (entry.get("sides") or []):
            continue
        profile = scores.get(entry["seat_name_std"]) or {}
        if not profile.get("is_reliable"):
            continue
        risk = profile.get("seat_risk_score")
        style = profile.get("seat_style")
        reason = None
        if style == STYLE_DUMP:
            reason = AVOID_DUMP_STYLE_SELLER
        elif risk is not None and float(risk) >= cfg.max_risk_score:
            reason = AVOID_RISKY_SELLER
        if reason is None:
            continue
        out.append(
            {
                "symbol": symbol,
                "as_of_date": signal_date,
                "seat_name_std": entry["seat_name_std"],
                "seat_risk_score": risk,
                "seat_style": style,
                "sell_amount": entry.get("sell_amount"),
                "reason": reason,
                "action": "avoid_next_day_relay",
                "disclaimer": LABEL_DISCLAIMER,
            }
        )


def explain_stock_hot_money(
    db_path: str | Path | None = None,
    *,
    conn: sqlite3.Connection | None = None,
    symbol: str,
    as_of_date: str,
    recent_events: int = 20,
    profiles: SeatProfileReport | None = None,
    profile_config: SeatProfileConfig | None = None,
    factor_config: HotMoneyFactorConfig | None = None,
) -> dict[str, Any]:
    """研究口径的"谁在买、历史风格如何"解释。

    主 agent 接 ``/api/hot-money/{symbol}`` 时直接返回本结构：只读汇总 +
    文本化说明，不给投资建议；游资身份标签只在人工名录命中时出现，
    否则显式标注 ``unlabeled_source_insufficient``。
    """
    connection, own_conn = _acquire(db_path, conn)
    code = str(symbol).strip().zfill(6)
    try:
        profile_report = (
            profiles
            if profiles is not None
            else build_seat_profiles(
                conn=connection, as_of_date=as_of_date, config=profile_config
            )
        )
        scores = profile_report.by_seat()
        factor_result = build_hot_money_factors(
            conn=connection,
            as_of_date=as_of_date,
            config=factor_config,
            profiles=profile_report,
            symbols=[code],
        )
        row = factor_result.by_symbol().get(code)
        seats_today = list((row or {}).get("hot_money_seats_detail") or [])
        blocks = [_seat_block(entry, scores) for entry in seats_today]
        history, _coverage = build_seat_events(
            load_seat_events(connection, upto_date=as_of_date, symbols=[code])
        )
        recent = sorted(history, key=lambda e: e.trade_date, reverse=True)[: max(
            1, int(recent_events)
        )]
        return {
            "symbol": code,
            "as_of_date": as_of_date,
            "disclosure_note": "龙虎榜为 T 日盘后披露，本解释最早可用于 T+1 决策",
            "factors": {
                key: value
                for key, value in (row or {}).items()
                if key.startswith("hot_money_") or key.startswith("seat_")
            },
            "buyers": [b for b in blocks if "buy" in b["sides"]],
            "sellers": [b for b in blocks if "sell" in b["sides"]],
            "recent_seat_events": [
                {
                    "trade_date": event.trade_date,
                    "seat_name_std": event.seat_name_std,
                    "sides": list(event.sides),
                    "net_amount": event.net_amount,
                }
                for event in recent
            ],
            "narrative": _narrative(code, as_of_date, row, blocks, profile_report),
            "profile_window": {
                "window_start_date": profile_report.window_start_date,
                "window_days": profile_report.config["window_days"],
                "min_events": profile_report.config["min_events"],
                "seats_total": profile_report.summary["seats_total"],
                "seats_reliable": profile_report.summary["seats_reliable"],
            },
            "label_meta": profile_report.label_meta,
            "data_availability": profile_report.availability,
            "disclaimer": LABEL_DISCLAIMER,
        }
    finally:
        if own_conn:
            connection.close()


def _seat_block(entry: dict[str, Any], scores: dict[str, dict[str, Any]]) -> dict[str, Any]:
    profile = scores.get(entry["seat_name_std"]) or {}
    return {
        "seat_name_std": entry["seat_name_std"],
        "sides": entry.get("sides") or [],
        "buy_amount": entry.get("buy_amount"),
        "sell_amount": entry.get("sell_amount"),
        "net_amount": entry.get("net_amount"),
        "hot_money_label": profile.get("hot_money_label"),
        "label_status": (
            "curated" if profile.get("hot_money_label") else "unlabeled_source_insufficient"
        ),
        "seat_style": profile.get("seat_style"),
        "seat_style_note": profile.get("seat_style_note"),
        "seat_alpha_score": profile.get("seat_alpha_score"),
        "seat_risk_score": profile.get("seat_risk_score"),
        "seat_sample_events": profile.get("seat_sample_events"),
        "win_rate": profile.get("win_rate"),
        "win_rate_shrunk": profile.get("win_rate_shrunk"),
        "win_rate_ci": [profile.get("win_rate_ci_low"), profile.get("win_rate_ci_high")],
        "next_close_premium": profile.get("next_close_premium"),
        "next_close_premium_shrunk": profile.get("next_close_premium_shrunk"),
        "sell_dump_rate_shrunk": profile.get("sell_dump_rate_shrunk"),
        "is_reliable": profile.get("is_reliable"),
        "reject_reasons": profile.get("reject_reasons"),
        "pref_theme_share": profile.get("pref_theme_share"),
        "pref_position_share": profile.get("pref_position_share"),
        "partners": profile.get("partners"),
    }


def _narrative(
    symbol: str,
    as_of_date: str,
    row: dict[str, Any] | None,
    blocks: list[dict[str, Any]],
    profiles: SeatProfileReport,
) -> list[str]:
    """把"谁在买 + 历史风格"写成可读结论（纯文本，不含身份断言）。"""
    if not row or not blocks:
        return [
            f"{symbol} 在 {as_of_date} 未出现在龙虎榜席位明细中，"
            f"无法给出'谁在买'的解释（数据缺失，不猜测）。"
        ]
    lines: list[str] = []
    buyers = [b for b in blocks if "buy" in b["sides"]]
    sellers = [b for b in blocks if "sell" in b["sides"]]
    lines.append(
        f"{symbol}（{as_of_date}）龙虎榜买方 {len(buyers)} 家、卖方 {len(sellers)} 家，"
        f"可靠买方席位 {row.get('hot_money_count')} 家，"
        f"净买额 {_yi(row.get('hot_money_net_buy'))} 亿元。"
    )
    for block in sorted(buyers, key=lambda b: -(b.get("seat_alpha_score") or 0.0))[:5]:
        if block.get("is_reliable"):
            lines.append(
                f"- {block['seat_name_std']}：风格 {block.get('seat_style')}"
                f"（{block.get('seat_style_note')}），样本 {block.get('seat_sample_events')} 次，"
                f"次日胜率 {block.get('win_rate')}（收缩后 {block.get('win_rate_shrunk')}，"
                f"95% CI {block.get('win_rate_ci')}），"
                f"alpha {block.get('seat_alpha_score')}。"
            )
        else:
            lines.append(
                f"- {block['seat_name_std']}：样本不足"
                f"（{block.get('reject_reasons')}），不给评分，仅作披露记录。"
            )
    if row.get("seat_conflict"):
        lines.append(
            f"- 注意：当日存在 {row['seat_conflict']} 个买卖同席席位，"
            "对倒 / 出货嫌疑，跟随默认拒绝。"
        )
    if row.get("hot_money_huddle_flag"):
        lines.append(
            f"- {row.get('hot_money_huddle_seats')} 家高 alpha 可靠席位同时买入（抱团信号），"
            f"题材交互分 {row.get('hot_money_x_theme')}。"
        )
    if profiles.label_meta.get("hot_money_label_is_proxy"):
        lines.append(
            "- 游资身份标签不可用：名录来自营业部名称代理"
            "（hm_list / hm_detail 无权限），以上只陈述席位历史统计。"
        )
    lines.append(LABEL_DISCLAIMER)
    return lines


def _yi(value: Any) -> str:
    if value is None:
        return "缺失"
    return f"{float(value) / 1e8:.2f}"


__all__ = [
    "ALLOWED_ENTRY_SCENES",
    "AVOID_DUMP_STYLE_SELLER",
    "AVOID_RISKY_SELLER",
    "FollowCandidate",
    "FollowConfig",
    "FollowResult",
    "REJECTED_ENTRY_SCENES",
    "assert_entry_scene",
    "build_follow_signals",
    "explain_stock_hot_money",
]
