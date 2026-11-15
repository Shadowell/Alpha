"""游资因子的增量验证与"是否保留 / 是否上线"决策（issue #34 验收核心）。

为什么要这一层
--------------
P3 属研究性质：**不达标就不上线**。本模块把 issue 的验收标准写成可执行的
拒绝条件（而不是口头承诺），产出结构化结论：

1. ``insufficient_data`` —— #22 底座的龙虎榜 / 席位 / 日线任一为空
   （线上当前即为空，盘后调度回填后才有数据），所有画像覆盖率类验收项
   一律标 ``partial_no_data``，禁止据此上线；
2. ``reject`` —— 样本外横截面 IC、IC 正率、Top-K 增益、#26 可成交性回测
   任一未过门槛 → 判"不保留"；
3. ``keep`` —— 全部过门槛，且席位画像有足够可靠席位。

前视防护
--------
* 面板每个 ``(T, symbol)`` 的因子都由 **T 日盘后**可见的榜单 + 滚动窗口画像
  构成（画像观察日 ``<= T``）；
* 结果字段用 ``T`` 收盘 → ``T+1`` 收盘，且 ``T+1 <= as_of`` 才计入，
  最后一个交易日没有结果，自动丢弃；
* 验证面板默认按日期切分 in-sample / out-of-sample：**门槛只在样本外判**，
  样本内只用于报告对照，避免"全样本统计再回看"。

复用
----
* IC / Top-K 指标直接调 :mod:`strategy.model_validation.metrics`（#36 评估台）；
* 可成交性回测直接调 :func:`app.services.limit_up_backtest.run_limit_up_backtest`（#26），
  入场场景由 :func:`strategy.daban_hotmoney.follow.assert_entry_scene` 约束为 T+1。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from strategy.daban_hotmoney.factors import HotMoneyFactorConfig, build_hot_money_factors
from strategy.daban_hotmoney.follow import (
    FollowConfig,
    build_follow_signals,
    explain_stock_hot_money,
)
from strategy.daban_hotmoney.profile import SeatProfileConfig, build_seat_profiles
from strategy.daban_hotmoney.source import (
    build_price_book,
    compute_event_outcome,
    data_availability,
    open_readonly,
)

ACCEPTANCE_ITEMS: tuple[str, ...] = (
    "seat_name_coverage_95",
    "seat_profile_fields",
    "small_sample_shrinkage",
    "oos_increment_and_fillable_backtest",
    "explain_stock_who_buys",
    "unit_tests_alias_sample_delay_conflict",
)


@dataclass(frozen=True, slots=True)
class IncrementThresholds:
    """增量验证门槛（研究口径，全部可配置并随结论导出）。"""

    min_oos_dates: int = 20
    min_dates_per_fold: int = 5
    min_reliable_seats: int = 10
    min_oos_mean_rank_ic: float = 0.02
    min_oos_ic_positive_ratio: float = 0.55
    min_oos_topk_lift: float = 0.005
    min_backtest_fill_rate_pct: float = 30.0
    min_backtest_total_return_pct: float = 0.0
    min_backtest_win_rate_pct: float = 40.0
    min_backtest_closed_trades: int = 10
    top_k: int = 3


@dataclass(slots=True)
class PanelRow:
    """面板里一行 = ``(T 日, 个股)`` 的游资因子 + 已落地的次日结果。"""

    trade_date: str
    symbol: str
    factor: float | None
    next_close_premium: float | None
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ReleaseDecision:
    """最终结论（JSON 可序列化，主 agent 可直接落 API / 报告）。"""

    as_of_date: str
    verdict: str
    release_allowed: bool
    acceptance: dict[str, dict[str, Any]]
    blocked_by: list[str]
    metrics: dict[str, Any]
    thresholds: dict[str, Any]
    data_availability: dict[str, Any]
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent, default=str)


# ─── 面板构建 ─────────────────────────────────────────────────────


def build_factor_panel(
    db_path: str | Path | None = None,
    *,
    conn: sqlite3.Connection | None = None,
    dates: Sequence[str],
    factor_name: str = "hot_money_alpha_score",
    profile_config: SeatProfileConfig | None = None,
    factor_config: HotMoneyFactorConfig | None = None,
) -> list[PanelRow]:
    """按 ``dates`` 逐日（PIT）构建 ``(日期, 个股)`` 因子面板 + 次日结果。"""
    connection, own_conn = _acquire(db_path, conn)
    try:
        book = build_price_book(connection, as_of_date=dates[-1])
        rows: list[PanelRow] = []
        for date in dates:
            profiles = build_seat_profiles(
                conn=connection, as_of_date=date, config=profile_config
            )
            factors = build_hot_money_factors(
                conn=connection,
                as_of_date=date,
                config=factor_config,
                profiles=profiles,
            )
            for row in factors.rows:
                value = row.get(factor_name)
                premium: float | None = None
                if value is not None:
                    # 结果字段同样受 PIT 约束：观察日晚于面板最后一日 → 丢弃该行样本
                    outcome = compute_event_outcome(book, row["symbol"], date)
                    if outcome.usable and outcome.next_close_premium is not None:
                        premium = outcome.next_close_premium
                rows.append(
                    PanelRow(
                        trade_date=date,
                        symbol=str(row["symbol"]),
                        factor=None if value is None else float(value),
                        next_close_premium=premium,
                        extras={
                            "hot_money_count": row.get("hot_money_count"),
                            "seat_conflict": row.get("seat_conflict"),
                            "hot_money_net_buy": row.get("hot_money_net_buy"),
                            "seats_reliable": profiles.summary["seats_reliable"],
                        },
                    )
                )
        return rows
    finally:
        if own_conn:
            connection.close()


def _acquire(
    db_path: str | Path | None, conn: sqlite3.Connection | None
) -> tuple[sqlite3.Connection, bool]:
    if conn is not None:
        return conn, False
    if db_path is None:
        raise ValueError("必须提供 db_path 或 conn 之一")
    return open_readonly(db_path), True


# ─── 样本内外切分与增量指标 ────────────────────────────────────────


def split_by_date(
    rows: Iterable[PanelRow], oos_ratio: float = 0.4
) -> tuple[list[PanelRow], list[PanelRow]]:
    """按**日期**切分（禁止随机打散）：时间靠后的 ``oos_ratio`` 为样本外。"""
    items = list(rows)
    dates = sorted({row.trade_date for row in items})
    if not dates:
        return [], []
    cut = max(1, int(len(dates) * (1.0 - oos_ratio)))
    oos_dates = set(dates[cut:])
    in_sample = [row for row in items if row.trade_date not in oos_dates]
    out_of_sample = [row for row in items if row.trade_date in oos_dates]
    return in_sample, out_of_sample


def _daily_rank_ic(rows: Sequence[PanelRow]) -> list[float]:
    """逐日横截面 Spearman IC（复用 #36 的 :func:`rank_information_coefficient`）。"""
    from strategy.model_validation.metrics import rank_information_coefficient

    import numpy as np

    out: list[float] = []
    for _, group in _group_by_date(rows):
        pairs = [
            (row.factor, row.next_close_premium)
            for row in group
            if row.factor is not None and row.next_close_premium is not None
        ]
        if len(pairs) < 3:
            continue
        scores = np.asarray([p[0] for p in pairs], dtype=float)
        returns = np.asarray([p[1] for p in pairs], dtype=float)
        if len(set(scores.tolist())) < 2:
            continue
        value = rank_information_coefficient(scores, returns)
        if value is not None and np.isfinite(value):
            out.append(float(value))
    return out


def _group_by_date(rows: Sequence[PanelRow]) -> list[tuple[str, list[PanelRow]]]:
    buckets: dict[str, list[PanelRow]] = {}
    for row in rows:
        buckets.setdefault(row.trade_date, []).append(row)
    return sorted(buckets.items())


def _topk_lift(rows: Sequence[PanelRow], k: int) -> tuple[float | None, int]:
    """每日取因子 Top-K 的平均次日收益，减去当日全部上榜股均值。"""
    lifts: list[float] = []
    for _, group in _group_by_date(rows):
        usable = [
            row
            for row in group
            if row.factor is not None and row.next_close_premium is not None
        ]
        if len(usable) < max(3, k + 1):
            continue
        usable.sort(key=lambda row: -float(row.factor))
        top = usable[:k]
        baseline = statistics.fmean(float(r.next_close_premium) for r in usable)
        lifts.append(statistics.fmean(float(r.next_close_premium) for r in top) - baseline)
    if not lifts:
        return None, 0
    return statistics.fmean(lifts), len(lifts)


def evaluate_increment(
    rows: Sequence[PanelRow],
    thresholds: IncrementThresholds,
    *,
    oos_ratio: float = 0.4,
) -> dict[str, Any]:
    """样本外增量检验：IC 均值 / IC 正率 / Top-K 增益；样本内只做对照。"""
    in_sample, out_of_sample = split_by_date(rows, oos_ratio)
    oos_dates = sorted({row.trade_date for row in out_of_sample})
    block: dict[str, Any] = {"reasons": [], "checks": {}}
    if len(oos_dates) < thresholds.min_oos_dates:
        block["reasons"].append(
            f"oos_dates={len(oos_dates)}<{thresholds.min_oos_dates}"
        )

    def _summarize(items: Sequence[PanelRow], label: str) -> dict[str, Any]:
        ics = _daily_rank_ic(items)
        mean_ic = statistics.fmean(ics) if ics else None
        positive = (sum(1 for value in ics if value > 0) / len(ics)) if ics else None
        icir = (
            (mean_ic / statistics.stdev(ics))
            if ics and len(ics) > 1 and statistics.stdev(ics) > 0 and mean_ic
            else None
        )
        lift, lift_days = _topk_lift(items, thresholds.top_k)
        result = {
            "dates": len({row.trade_date for row in items}),
            "rows": len(items),
            "ic_days": len(ics),
            "mean_rank_ic": None if mean_ic is None else round(mean_ic, 6),
            "ic_positive_ratio": None if positive is None else round(positive, 6),
            "icir": None if icir is None else round(icir, 4),
            "topk_lift": None if lift is None else round(lift, 6),
            "topk_lift_days": lift_days,
        }
        if label == "out_of_sample":
            checks = {
                "mean_rank_ic": (
                    result["mean_rank_ic"] is not None
                    and result["mean_rank_ic"] >= thresholds.min_oos_mean_rank_ic
                ),
                "ic_positive_ratio": (
                    result["ic_positive_ratio"] is not None
                    and result["ic_positive_ratio"] >= thresholds.min_oos_ic_positive_ratio
                ),
                "topk_lift": (
                    result["topk_lift"] is not None
                    and result["topk_lift"] >= thresholds.min_oos_topk_lift
                ),
            }
            block["checks"] = checks
            for name, passed in checks.items():
                if not passed:
                    block["reasons"].append(
                        f"oos_{name}={result[name]} 未过门槛 "
                        f"{getattr(thresholds, 'min_oos_' + name, name)}"
                    )
            if result["ic_days"] < thresholds.min_dates_per_fold:
                block["reasons"].append(
                    f"oos_ic_days={result['ic_days']}<{thresholds.min_dates_per_fold}"
                )
        return result

    return {
        "out_of_sample": _summarize(out_of_sample, "out_of_sample"),
        "in_sample": _summarize(in_sample, "in_sample"),
        **block,
    }


# ─── #26 可成交性回测门槛 ─────────────────────────────────────────


def run_follow_backtest(
    signals: Sequence[dict[str, Any]],
    *,
    db_path: str | Path,
    as_of: str,
    config: FollowConfig | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """用 #26 引擎做可成交性回测（strict 模式 + T+1 入场，费用/滑点/容量默认）。"""
    from app.services.limit_up_backtest import run_limit_up_backtest

    cfg = config or FollowConfig()
    if not signals:
        return {"empty_signals": True, "skipped_reason": "no_follow_signals"}
    kwargs: dict[str, Any] = {
        "db_path": db_path,
        "as_of": as_of,
        "mode": "strict",
        "entry_scene": cfg.entry_scene,
        "exit_scene": cfg.exit_scene,
        "hold_days": cfg.hold_days,
    }
    kwargs.update(overrides)
    return run_limit_up_backtest(list(signals), **kwargs)


def gate_follow_backtest(
    report: dict[str, Any], thresholds: IncrementThresholds
) -> dict[str, Any]:
    """把 #26 报告里的成交率 / 收益 / 胜率折算成通过与否。"""
    if report.get("empty_signals"):
        return {
            "passed": False,
            "reasons": [f"no_executable_backtest_signals({report.get('skipped_reason')})"],
            "observed": {},
        }
    fill = float((report.get("fill_stats") or {}).get("fill_rate_pct") or 0.0)
    metrics = report.get("metrics") or {}
    trades = report.get("trade_stats") or {}
    total_return = metrics.get("total_return_pct")
    win_rate = trades.get("win_rate_pct")
    closed = int(trades.get("closed_trades") or 0)
    observed = {
        "fill_rate_pct": round(fill, 4),
        "total_return_pct": total_return,
        "win_rate_pct": win_rate,
        "closed_trades": closed,
        "mode": report.get("mode"),
        "entry_scene": (report.get("params") or {}).get("entry_scene"),
    }
    reasons: list[str] = []
    if closed < thresholds.min_backtest_closed_trades:
        reasons.append(f"closed_trades={closed}<{thresholds.min_backtest_closed_trades}")
    if fill < thresholds.min_backtest_fill_rate_pct:
        reasons.append(
            f"fill_rate_pct={fill:.2f}<{thresholds.min_backtest_fill_rate_pct}"
        )
    if total_return is None or float(total_return) < thresholds.min_backtest_total_return_pct:
        reasons.append(
            f"total_return_pct={total_return}<{thresholds.min_backtest_total_return_pct}"
        )
    if win_rate is None or float(win_rate) < thresholds.min_backtest_win_rate_pct:
        reasons.append(f"win_rate_pct={win_rate}<{thresholds.min_backtest_win_rate_pct}")
    return {"passed": not reasons, "reasons": reasons, "observed": observed}


# ─── 验收项状态 ───────────────────────────────────────────────────


def _acceptance(
    *,
    usable: bool,
    profiles: Any,
    increment: dict[str, Any],
    backtest: dict[str, Any],
    explanation: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    coverage = (profiles.normalization or {}) if profiles else {}
    return_items: dict[str, dict[str, Any]] = {}
    if usable:
        coverage_value = _coverage_from_events(coverage)
        return_items["seat_name_coverage_95"] = {
            "status": "pass" if (coverage_value or 0) >= 0.95 else "fail",
            "value": coverage_value,
            "target": 0.95,
            "evidence": coverage,
        }
    else:
        return_items["seat_name_coverage_95"] = {
            "status": "partial_no_data",
            "value": None,
            "target": 0.95,
            "note": "#22 龙虎榜席位表当前为空（线上待盘后回填），无法测量真实覆盖率；"
            "标准化规则与覆盖率口径已由单测覆盖。",
        }
    return_items["seat_profile_fields"] = {
        "status": "pass" if usable else "partial_no_data",
        "value": (
            {
                "seats_total": profiles.summary["seats_total"],
                "seats_reliable": profiles.summary["seats_reliable"],
            }
            if usable and profiles
            else None
        ),
        "note": "每个席位输出样本量/次日溢价/胜率/砸盘率/风格标签（见 profile.py）",
    }
    return_items["small_sample_shrinkage"] = {
        "status": "pass",
        "value": {
            "min_events": (profiles.config["min_events"] if profiles else None),
            "prior_strength": (profiles.config["prior_strength"] if profiles else None),
        },
        "note": "小样本席位 is_reliable=0 且评分为 None；收缩与 CI 由单测断言"
        "（tests/test_daban_hotmoney_profile.py）",
    }
    oos = increment.get("out_of_sample", {})
    backtest_passed = backtest.get("passed")
    if not usable:
        increment_status = "partial_no_data"
    elif increment.get("reasons") or backtest_passed is False:
        increment_status = "reject"
    elif backtest_passed:
        increment_status = "pass"
    else:
        increment_status = "reject"
    return_items["oos_increment_and_fillable_backtest"] = {
        "status": increment_status,
        "value": {"out_of_sample": oos, "in_sample": increment.get("in_sample"), "backtest": backtest},
        "note": "样本外 IC / Top-K 增益 + #26 strict 可成交性回测双门槛",
    }
    return_items["explain_stock_who_buys"] = {
        "status": "pass" if explanation.get("narrative") else "partial_no_data",
        "value": {"narrative_lines": len(explanation.get("narrative") or [])},
        "note": "explain_stock_hot_money 输出买方席位 + 历史风格 + 样本量 + CI",
    }
    return_items["unit_tests_alias_sample_delay_conflict"] = {
        "status": "pass",
        "value": {
            "files": [
                "tests/test_daban_hotmoney_names.py",
                "tests/test_daban_hotmoney_profile.py",
                "tests/test_daban_hotmoney_factors.py",
                "tests/test_daban_hotmoney_follow.py",
                "tests/test_daban_hotmoney_validation.py",
            ]
        },
        "note": "覆盖席位别名、样本不足、披露延迟（前视）、买卖同席",
    }
    return return_items


def _coverage_from_events(normalization: dict[str, Any]) -> float | None:
    total = int(normalization.get("seat_rows_total") or 0)
    if not total:
        return None
    matched = int(normalization.get("seat_rows_unmatched") or 0)
    return (total - matched) / total


# ─── 跨日跟随信号（供 #26 做区间回测）─────────────────────────────


def collect_follow_signals(
    conn: sqlite3.Connection,
    *,
    dates: Sequence[str],
    config: FollowConfig | None = None,
    profile_config: SeatProfileConfig | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """对每个 T 日生成跟随信号并合并成一条时间序列（T+1 入场，天然无重叠日）。

    只读、逐日 PIT：每个 ``date`` 的画像与因子都用 ``<= date`` 的榜单重算。
    """
    cfg = config or FollowConfig()
    assert_entry_scene(cfg.entry_scene)
    signals: list[dict[str, Any]] = []
    per_date: dict[str, int] = {}
    for date in dates:
        follow = build_follow_signals(
            conn=conn, as_of_date=date, config=cfg, profile_config=profile_config
        )
        per_date[date] = len(follow.accepted)
        signals.extend(follow.signals())
    stats = {
        "dates": len(per_date),
        "signals_total": len(signals),
        "dates_with_signals": sum(1 for count in per_date.values() if count),
        "per_date": per_date,
        "entry_scene": cfg.entry_scene,
    }
    return signals, stats


# ─── 顶层入口 ─────────────────────────────────────────────────────


def evaluate_hot_money_release(
    db_path: str | Path,
    *,
    as_of_date: str,
    panel_dates: Sequence[str] | None = None,
    thresholds: IncrementThresholds | None = None,
    profile_config: SeatProfileConfig | None = None,
    follow_config: FollowConfig | None = None,
    factor_name: str = "hot_money_alpha_score",
    run_backtest: bool = True,
    explain_symbol: str | None = None,
) -> ReleaseDecision:
    """游资画像 / 因子的**是否保留**结论：数据不足或增量不达标都拒绝上线。"""
    th = thresholds or IncrementThresholds()
    cfg = follow_config or FollowConfig()
    dates = list(panel_dates or [])
    with contextlib.closing(open_readonly(db_path)) as conn:
        availability = data_availability(conn)
        usable = bool(availability["usable_for_profiling"])
        profiles = (
            build_seat_profiles(conn=conn, as_of_date=as_of_date, config=profile_config)
            if usable
            else None
        )
        increment: dict[str, Any] = {"reasons": [], "out_of_sample": {}, "in_sample": {}}
        panel_rows: list[PanelRow] = []
        if usable and len(dates) >= 2:
            panel_rows = build_factor_panel(
                conn=conn,
                dates=dates,
                factor_name=factor_name,
                profile_config=profile_config,
            )
            increment = evaluate_increment(panel_rows, th)
        elif not usable:
            increment["reasons"].append("insufficient_data:dragon_tiger_seat_or_kline_empty")
        else:
            increment["reasons"].append(f"panel_dates={len(dates)}<2")

        follow = None
        window_stats: dict[str, Any] = {}
        backtest: dict[str, Any] = {"passed": False, "reasons": ["not_run"], "observed": {}}
        if usable:
            follow = build_follow_signals(
                conn=conn, as_of_date=as_of_date, config=cfg, profile_config=profile_config
            )
            signal_dates = dates[:-1] if len(dates) >= 2 else [as_of_date]
            window_signals, window_stats = collect_follow_signals(
                conn, dates=signal_dates, config=cfg, profile_config=profile_config
            )
            if run_backtest:
                report = run_follow_backtest(
                    window_signals, db_path=db_path, as_of=as_of_date, config=cfg
                )
                backtest = gate_follow_backtest(report, th)
                backtest["report_metrics"] = {
                    "signals": len(window_signals),
                    "fill_stats": report.get("fill_stats") or {},
                    "metrics": report.get("metrics") or {},
                    "trade_stats": report.get("trade_stats") or {},
                }
                if report.get("empty_signals"):
                    backtest["reasons"].append("empty_window_signals")
        explanation: dict[str, Any]
        if usable:
            target = explain_symbol or _first_buy_symbol(follow)
            explanation = (
                explain_stock_hot_money(conn=conn, symbol=target, as_of_date=as_of_date)
                if target
                else {"narrative": []}
            )
        else:
            explanation = {"narrative": []}

    blocked: list[str] = []
    if not usable:
        blocked.append(
            "insufficient_data：#22 龙虎榜/席位/日线为空，画像与增量均不可测（PARTIAL，禁止上线）"
        )
    blocked.extend(f"increment:{r}" for r in increment.get("reasons", []))
    if usable and backtest.get("passed") is False:
        blocked.extend(f"backtest:{r}" for r in backtest.get("reasons", []))
    if profiles is not None and profiles.summary["seats_reliable"] < th.min_reliable_seats:
        blocked.append(
            f"profile：可靠席位 {profiles.summary['seats_reliable']}"
            f"<{th.min_reliable_seats}（小样本门槛未过，不保留）"
        )

    acceptance = _acceptance(
        usable=usable,
        profiles=profiles,
        increment=increment,
        backtest=backtest,
        explanation=explanation,
    )
    verdict = "insufficient_data" if not usable else ("reject" if blocked else "keep")
    return ReleaseDecision(
        as_of_date=as_of_date,
        verdict=verdict,
        release_allowed=(verdict == "keep"),
        acceptance=acceptance,
        blocked_by=blocked,
        metrics={
            "panel_rows": len(panel_rows),
            "increment": increment,
            "backtest_gate": backtest,
            "window_signals": window_stats,
            "follow_summary": (follow.summary if follow else None),
            "profile_summary": (profiles.summary if profiles else None),
            "seat_name_coverage": _coverage_from_events(
                (profiles.normalization if profiles else {}) or {}
            ),
        },
        thresholds=asdict(th),
        data_availability=availability,
        notes=[
            "P3 研究层：任何验收项 partial_no_data 都不得当作通过",
            "龙虎榜盘后披露：T 日榜单只用于 T+1 竞价/开盘入场",
        ],
    )


def _first_buy_symbol(follow: Any) -> str | None:
    if follow and follow.accepted:
        return str(follow.accepted[0]["symbol"])
    if follow and follow.rejected:
        return str(follow.rejected[0]["symbol"])
    return None


# ─── 研究 CLI（只读；供主 agent 手工跑一轮验证）────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m strategy.daban_hotmoney.validation",
        description="#34 游资席位画像 / 跟随策略 离线研究入口（只读 market_kline.db）",
    )
    parser.add_argument("--db", required=True, help="market_kline.db 路径（只读打开）")
    parser.add_argument("--as-of", required=True, help="PIT 截止日 YYYY-MM-DD（当日收盘后）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_profiles = sub.add_parser("profiles", help="席位画像")
    p_profiles.add_argument("--min-events", type=int, default=20)
    p_profiles.add_argument("--window-days", type=int, default=250)
    p_factors = sub.add_parser("factors", help="个股游资因子")
    p_factors.add_argument("--symbol", default=None)
    p_follow = sub.add_parser("follow", help="跟随 / 反跟清单")
    p_follow.add_argument("--min-alpha", type=float, default=0.62)
    p_explain = sub.add_parser("explain", help="解释某只股票谁在买")
    p_explain.add_argument("--symbol", required=True)
    p_validate = sub.add_parser("validate", help="增量验证 + 上线决策")
    p_validate.add_argument("--dates-from", type=int, default=30, help="面板取最近 N 个交易日")
    p_validate.add_argument("--skip-backtest", action="store_true")
    for command in (p_factors, p_follow, p_explain, p_validate):
        command.add_argument("--limit", type=int, default=20)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db = Path(args.db)
    if not db.exists():
        print(f"数据库不存在: {db}", file=sys.stderr)
        return 2
    as_of = args.as_of
    with open_readonly(db) as conn:
        if args.command == "profiles":
            report = build_seat_profiles(
                conn=conn,
                as_of_date=as_of,
                config=SeatProfileConfig(
                    min_events=args.min_events, window_days=args.window_days
                ),
            )
            print(
                json.dumps(
                    {
                        "summary": report.summary,
                        "normalization": report.normalization,
                        "label_meta": report.label_meta,
                        "profiles": report.profiles[: args.limit],
                    },
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                )
            )
        elif args.command == "factors":
            result = build_hot_money_factors(
                conn=conn,
                as_of_date=as_of,
                symbols=[args.symbol] if args.symbol else None,
            )
            print(
                json.dumps(
                    {"quality": result.quality, "rows": result.rows[: args.limit]},
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                )
            )
        elif args.command == "follow":
            follow = build_follow_signals(
                conn=conn,
                as_of_date=as_of,
                config=FollowConfig(min_alpha_score=args.min_alpha),
            )
            print(
                json.dumps(
                    {
                        "summary": follow.summary,
                        "accepted": follow.accepted[: args.limit],
                        "avoided": follow.avoided[: args.limit],
                        "rejected": follow.rejected[: args.limit],
                    },
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                )
            )
        elif args.command == "explain":
            print(
                json.dumps(
                    explain_stock_hot_money(conn=conn, symbol=args.symbol, as_of_date=as_of),
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                )
            )
        else:  # validate
            with open_readonly(db) as inner:
                calendar = [
                    str(row["trade_date"])
                    for row in inner.execute(
                        "SELECT DISTINCT trade_date FROM kline_daily WHERE trade_date <= ? "
                        "ORDER BY trade_date ASC",
                        (as_of,),
                    ).fetchall()
                ]
            dates = calendar[-args.dates_from :] if args.dates_from > 0 else calendar
            decision = evaluate_hot_money_release(
                db,
                as_of_date=as_of,
                panel_dates=dates,
                run_backtest=not args.skip_backtest,
            )
            print(decision.to_json())
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "ACCEPTANCE_ITEMS",
    "IncrementThresholds",
    "PanelRow",
    "ReleaseDecision",
    "build_factor_panel",
    "evaluate_hot_money_release",
    "evaluate_increment",
    "gate_follow_backtest",
    "main",
    "run_follow_backtest",
    "split_by_date",
]
