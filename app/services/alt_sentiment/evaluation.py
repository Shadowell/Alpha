"""另类因子离线研究（issue #33 验收项 3 / 4）。

提供最小但够用的研究闭环：

* 严格 Point-in-Time 组装：``as_of_time`` 晚于标签窗口起点的样本一律剔除；
* 单因子 Rank IC（Spearman）、ICIR、t 统计；
* 分层（quantile）前瞻收益与单调性、多空价差；
* 与量价 / 资金流因子的截面相关性（相关性可控性检查）；
* walk-forward 折切分（含 embargo 间隔）。

不依赖任何数据库：输入都是 ``pandas.DataFrame``，便于用合成数据做单元测试。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Sequence

import numpy as np
import pandas as pd

from app.services.alt_sentiment.factors import FACTOR_NAMES
from app.services.alt_sentiment.records import AltSentimentDaily, as_cn_datetime
from app.services.alt_sentiment.sources import build_assessment_report

REQUIRED_COLUMNS = ("symbol", "trade_date", "as_of_time")

# 默认增量门槛：达不到就记录结论并停止，不接生产。
IC_THRESHOLD = 0.02
ICIR_THRESHOLD = 0.25
COVERAGE_THRESHOLD = 0.5
LAYER_SPREAD_THRESHOLD = 0.002
MAX_CORR_WITH_PRICE = 0.6


class FutureDataError(RuntimeError):
    """样本违反了 Point-in-Time 约束（未来函数）。"""


@dataclass(slots=True)
class ICReport:
    factor: str
    horizon: str
    observations: int
    coverage: float
    ic_mean: float | None
    ic_std: float | None
    icir: float | None
    t_stat: float | None
    positive_ratio: float | None
    periods: int
    verdict: str = "insufficient"
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "factor": self.factor,
            "horizon": self.horizon,
            "observations": self.observations,
            "coverage": self.coverage,
            "ic_mean": self.ic_mean,
            "ic_std": self.ic_std,
            "icir": self.icir,
            "t_stat": self.t_stat,
            "positive_ratio": self.positive_ratio,
            "periods": self.periods,
            "verdict": self.verdict,
            "details": self.details,
        }


@dataclass(slots=True)
class LayerReport:
    factor: str
    horizon: str
    layers: int
    layer_returns: list[float | None]
    long_short_spread: float | None
    monotonicity: float | None
    usable: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "factor": self.factor,
            "horizon": self.horizon,
            "layers": self.layers,
            "layer_returns": self.layer_returns,
            "long_short_spread": self.long_short_spread,
            "monotonicity": self.monotonicity,
            "usable": self.usable,
        }


def rows_to_frame(rows: Iterable[AltSentimentDaily]) -> pd.DataFrame:
    """因子行 → 研究帧；缺失保持 NaN（绝不 0 填充）。"""
    frame = pd.DataFrame.from_records(
        [
            {
                "symbol": row.symbol,
                "trade_date": row.trade_date,
                "as_of_time": row.as_of_time,
                "sources": ",".join(row.sources),
                **{name: row.values.get(name) for name in FACTOR_NAMES},
            }
            for row in rows
        ]
    )
    if frame.empty:
        return frame
    for name in FACTOR_NAMES:
        if name not in frame.columns:
            frame[name] = np.nan
        frame[name] = pd.to_numeric(frame[name], errors="coerce")
    return frame


def attach_forward_returns(
    frame: pd.DataFrame,
    forward_returns: dict[tuple[str, str], float] | pd.DataFrame,
    *,
    column: str = "fwd_ret_1d",
) -> pd.DataFrame:
    """把前瞻收益按 ``(symbol, trade_date)`` 贴到研究帧；无收益记录保持 NaN。"""
    if frame is None or frame.empty:
        return pd.DataFrame(columns=[*REQUIRED_COLUMNS, column])
    out = frame.copy()
    if isinstance(forward_returns, dict):
        pairs = pd.DataFrame.from_records(
            [
                {"symbol": symbol, "trade_date": day, column: value}
                for (symbol, day), value in forward_returns.items()
            ]
        )
    else:
        pairs = forward_returns.copy()
    if pairs.empty:
        out[column] = np.nan
        return out
    key = ["symbol", "trade_date"]
    out = out.drop(columns=[column], errors="ignore").merge(
        pairs[key + [column]], on=key, how="left"
    )
    out[column] = pd.to_numeric(out[column], errors="coerce")
    return out


def enforce_point_in_time(
    frame: pd.DataFrame,
    *,
    label_column: str,
    min_gap: timedelta = timedelta(hours=1),
) -> pd.DataFrame:
    """剔除「信号尚未可见就要用于预测」的样本。

    近似口径（不依赖交易日历）：标签窗口起点 = 该交易日次日 09:30（下一场开盘）。
    样本可用条件为 ``as_of_time + min_gap < 下一场开盘``；全部不可用则抛
    :class:`FutureDataError`，避免研究结论建立在泄漏数据上。
    """
    if frame is None or frame.empty:
        raise FutureDataError("empty research frame")
    work = frame.copy()
    missing = [c for c in (*REQUIRED_COLUMNS, label_column) if c not in work.columns]
    if missing:
        raise FutureDataError(f"research frame missing columns: {missing}")
    as_of = work["as_of_time"].map(as_cn_datetime)
    if as_of.isna().all():
        raise FutureDataError("no parsable as_of_time")
    label_anchor = _label_anchor(work["trade_date"])
    usable = pd.Series(False, index=work.index)
    for index in work.index:
        anchor = as_of.loc[index]
        if anchor is None:
            continue
        usable.loc[index] = label_anchor.loc[index] > anchor + min_gap
    kept = work[usable]
    if kept.empty:
        raise FutureDataError(
            "every sample violates point-in-time visibility; check as_of_time"
        )
    return kept.reset_index(drop=True)


NEXT_SESSION_OPEN_HOUR = 9
NEXT_SESSION_OPEN_MINUTE = 30


def _label_anchor(dates: pd.Series) -> pd.Series:
    def _anchor(value: Any) -> datetime | None:
        parsed = as_cn_datetime(value)
        if parsed is None:
            return None
        open_moment = parsed.replace(
            hour=NEXT_SESSION_OPEN_HOUR,
            minute=NEXT_SESSION_OPEN_MINUTE,
            second=0,
            microsecond=0,
        ) + timedelta(days=1)
        return open_moment

    return dates.map(_anchor)


def _spearman(left: pd.Series, right: pd.Series) -> float | None:
    mask = left.notna() & right.notna()
    if int(mask.sum()) < 3:
        return None
    ranked_left = left[mask].rank(method="average")
    ranked_right = right[mask].rank(method="average")
    if ranked_left.std(ddof=0) == 0 or ranked_right.std(ddof=0) == 0:
        return None
    value = float(np.corrcoef(ranked_left.to_numpy(), ranked_right.to_numpy())[0, 1])
    if not np.isfinite(value):
        return None
    return round(value, 6)


def factor_ic_report(
    frame: pd.DataFrame,
    *,
    factor: str,
    label_column: str,
    min_obs_per_day: int = 8,
    enforce_pit: bool = True,
) -> ICReport:
    """逐交易日截面 Rank IC 汇总。"""
    if factor not in frame.columns:
        raise KeyError(f"unknown factor column: {factor}")
    work = (
        enforce_point_in_time(frame, label_column=label_column)
        if enforce_pit
        else frame
    )
    total = int(len(work))
    valid_rows = work[factor].notna() & work[label_column].notna()
    coverage = float(valid_rows.mean()) if total else 0.0
    scores: list[float] = []
    for _, group in work.groupby("trade_date", sort=True):
        sample = group[[factor, label_column]].dropna()
        if len(sample) < max(3, int(min_obs_per_day)):
            continue
        value = _spearman(sample[factor], sample[label_column])
        if value is not None:
            scores.append(value)
    if not scores or coverage < COVERAGE_THRESHOLD:
        return ICReport(
            factor=factor,
            horizon=label_column,
            observations=total,
            coverage=round(coverage, 6),
            ic_mean=None,
            ic_std=None,
            icir=None,
            t_stat=None,
            positive_ratio=None,
            periods=len(scores),
            verdict="insufficient" if scores else "low_coverage",
        )
    array = np.asarray(scores, dtype=float)
    ic_mean = float(array.mean())
    ic_std = float(array.std(ddof=1)) if len(array) > 1 else 0.0
    icir = ic_mean / ic_std if ic_std > 1e-12 else None
    t_stat = ic_mean / (ic_std / np.sqrt(len(array))) if ic_std > 1e-12 else None
    verdict = (
        "pass"
        if abs(ic_mean) >= IC_THRESHOLD and abs(icir or 0.0) >= ICIR_THRESHOLD
        else "no_increment"
    )
    return ICReport(
        factor=factor,
        horizon=label_column,
        observations=total,
        coverage=round(coverage, 6),
        ic_mean=round(ic_mean, 6),
        ic_std=round(ic_std, 6),
        icir=round(icir, 6) if icir is not None else None,
        t_stat=round(float(t_stat), 4) if t_stat is not None else None,
        positive_ratio=round(float((array > 0).mean()), 6),
        periods=len(array),
        verdict=verdict,
    )


def quantile_layer_report(
    frame: pd.DataFrame,
    *,
    factor: str,
    label_column: str,
    layers: int = 5,
    enforce_pit: bool = True,
) -> LayerReport:
    """分层前瞻收益：逐日按因子分位分组，再对交易日求均值。"""
    work = (
        enforce_point_in_time(frame, label_column=label_column)
        if enforce_pit
        else frame
    )
    usable = work[["trade_date", factor, label_column]].dropna()
    count = max(2, int(layers))
    per_layer: dict[int, list[float]] = {index: [] for index in range(count)}
    for _, group in usable.groupby("trade_date", sort=True):
        if len(group) < count * 2:
            continue
        try:
            buckets = (
                group[factor]
                .rank(pct=True, method="first")
                .mul(count)
                .astype(int)
                .clip(0, count - 1)
            )
        except (TypeError, ValueError):
            continue
        for layer, subset in group.groupby(buckets):
            value = float(subset[label_column].mean())
            if np.isfinite(value):
                per_layer[layer].append(value)
    layer_returns = [
        round(float(np.mean(values)), 8) if values else None
        for values in (per_layer[index] for index in range(count))
    ]
    top = layer_returns[-1]
    bottom = layer_returns[0]
    spread = round(top - bottom, 8) if top is not None and bottom is not None else None
    monotonicity = None
    if all(value is not None for value in layer_returns):
        ordered = np.asarray([float(v) for v in layer_returns])
        monotonicity = _spearman(
            pd.Series(np.arange(count, dtype=float)), pd.Series(ordered)
        )
    usable_flags = [value is not None for value in layer_returns]
    ok = (
        sum(usable_flags) >= 2
        and spread is not None
        and abs(spread) >= LAYER_SPREAD_THRESHOLD
    )
    return LayerReport(
        factor=factor,
        horizon=label_column,
        layers=count,
        layer_returns=layer_returns,
        long_short_spread=spread,
        monotonicity=monotonicity,
        usable=bool(ok),
    )


def cross_factor_correlation(
    frame: pd.DataFrame,
    *,
    factor: str,
    price_columns: Sequence[str],
    min_obs_per_day: int = 10,
) -> dict[str, Any]:
    """另类因子与量价 / 资金流因子的截面相关性（越小说明增量越独立）。"""
    per_column: dict[str, float | None] = {}
    for column in price_columns:
        if column not in frame.columns:
            per_column[column] = None
            continue
        daily: list[float] = []
        for _, group in frame.groupby("trade_date", sort=True):
            sample = group[[factor, column]].dropna()
            if len(sample) < max(3, int(min_obs_per_day)):
                continue
            value = float(sample[factor].corr(sample[column], method="pearson"))
            if np.isfinite(value):
                daily.append(value)
        per_column[column] = round(float(np.mean(daily)), 6) if daily else None
    observed = {k: v for k, v in per_column.items() if v is not None}
    worst = (
        max(observed, key=lambda key: abs(observed[key] or 0.0)) if observed else None
    )
    max_abs = round(abs(observed[worst]), 6) if worst else None
    return {
        "factor": factor,
        "by_column": per_column,
        "max_abs_corr": max_abs,
        "collinear_with": worst
        if max_abs is not None and max_abs > MAX_CORR_WITH_PRICE
        else None,
        "controlled": max_abs is None or max_abs <= MAX_CORR_WITH_PRICE,
    }


def walk_forward_folds(
    trade_dates: Sequence[str],
    *,
    train_days: int,
    test_days: int,
    embargo_days: int = 1,
) -> list[dict[str, list[str]]]:
    """按交易日顺序切分 walk-forward 折，测试段前留 embargo 防泄漏。"""
    ordered = sorted({str(day) for day in trade_dates if str(day)})
    if train_days <= 0 or test_days <= 0:
        raise ValueError("train_days and test_days must be positive")
    folds: list[dict[str, list[str]]] = []
    cursor = 0
    while cursor + train_days + embargo_days + test_days <= len(ordered):
        train = ordered[cursor : cursor + train_days]
        test = ordered[
            cursor + train_days + embargo_days : cursor
            + train_days
            + embargo_days
            + test_days
        ]
        folds.append({"train": train, "test": test})
        cursor += test_days
    return folds


def evaluate_alt_factors(
    frame: pd.DataFrame,
    *,
    label_column: str,
    factors: Sequence[str] = (
        "alt_attention_score",
        "attention_index",
        "news_sentiment",
    ),
    price_columns: Sequence[str] = (),
    layers: int = 5,
) -> dict[str, Any]:
    """一次性输出单因子 IC / 分层 / 相关性报告与上线判定。"""
    ic_reports = []
    layer_reports = []
    corr_reports = []
    for name in factors:
        if name not in frame.columns:
            continue
        ic = factor_ic_report(frame, factor=name, label_column=label_column)
        ic_reports.append(ic.to_dict())
        layer_reports.append(
            quantile_layer_report(
                frame, factor=name, label_column=label_column, layers=layers
            ).to_dict()
        )
        if price_columns:
            corr_reports.append(
                cross_factor_correlation(
                    frame, factor=name, price_columns=price_columns
                )
            )
    promoted = [
        report["factor"]
        for report, layer in zip(ic_reports, layer_reports, strict=False)
        if report.get("verdict") == "pass" and layer.get("usable")
    ]
    return {
        "label_column": label_column,
        "thresholds": {
            "abs_ic": IC_THRESHOLD,
            "abs_icir": ICIR_THRESHOLD,
            "coverage": COVERAGE_THRESHOLD,
            "layer_spread": LAYER_SPREAD_THRESHOLD,
            "max_corr_with_price": MAX_CORR_WITH_PRICE,
        },
        "ic": ic_reports,
        "layers": layer_reports,
        "correlation": corr_reports,
        "promoted_factors": promoted,
        "decision": "adopt" if promoted else "stop_and_record",
        "source_assessment": build_assessment_report(),
    }


def research_note_markdown(report: dict[str, Any]) -> str:
    """把离线评估结果渲染成可归档的 Markdown 结论。"""
    lines = ["# 另类因子离线验证结论", ""]
    lines.append(f"- 标签列：`{report.get('label_column')}`")
    lines.append(f"- 结论：**{report.get('decision')}**")
    lines.append(
        f"- 门槛：|IC|≥{IC_THRESHOLD}, |ICIR|≥{ICIR_THRESHOLD}, 覆盖率≥{COVERAGE_THRESHOLD}"
    )
    lines.append("")
    lines.append("## 单因子 IC")
    lines.append("")
    lines.append("| 因子 | 样本 | 覆盖率 | IC 均值 | ICIR | t 值 | 判定 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for item in report.get("ic", []):
        lines.append(
            "| {factor} | {observations} | {coverage} | {ic_mean} | {icir} | {t_stat} | {verdict} |".format(
                **item
            )
        )
    lines.append("")
    lines.append("## 分层前瞻收益")
    lines.append("")
    lines.append("| 因子 | 层数 | 各层均值 | 多空价差 | 单调性 | 可用 |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for item in report.get("layers", []):
        lines.append(
            "| {factor} | {layers} | {layer_returns} | {long_short_spread} | {monotonicity} | {usable} |".format(
                **item
            )
        )
    if report.get("correlation"):
        lines.append("")
        lines.append("## 与量价 / 资金因子相关性")
        lines.append("")
        lines.append("| 因子 | 最大绝对相关 | 是否与量价共线 | 相关性可控 |")
        lines.append("| --- | --- | --- | --- |")
        for item in report["correlation"]:
            lines.append(
                f"| {item['factor']} | {item['max_abs_corr']} | {item['collinear_with']} | {item['controlled']} |"
            )
    lines.append("")
    if not report.get("promoted_factors"):
        lines.append(
            "无因子通过门槛：按 issue 要求记录结论并停止，不接入生产打分链路。"
        )
    else:
        lines.append("通过门槛的因子：" + ", ".join(report["promoted_factors"]) + "。")
    lines.append("")
    return "\n".join(lines)
