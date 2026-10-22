"""Report / model-card rendering for the increment validation run (issue #36).

Outputs are plain text + JSON so they can be diffed, archived, and pasted into
the issue thread.  Nothing here contains upstream raw responses or credentials.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd

from .models import ModelArtifactError

if TYPE_CHECKING:  # pragma: no cover
    from .runner import RunResult

METRIC_ROWS: tuple[tuple[str, str], ...] = (
    ("metrics.pooled_ic", "Pooled IC"),
    ("metrics.pooled_rank_ic", "Pooled Rank IC"),
    ("metrics.ic_mean", "横截面 rank IC 均值"),
    ("metrics.icir", "ICIR"),
    ("metrics.ic_t_stat", "IC t-stat"),
    ("metrics.roc_auc", "ROC AUC"),
    ("metrics.pr_auc", "PR-AUC"),
    ("metrics.precision_at_topk", "Precision@TopK"),
    ("metrics.hit_rate_topk", "TopK 命中率(收益>0)"),
    ("metrics.mean_return_topk", "TopK 平均毛收益"),
    ("metrics.brier", "Brier"),
    ("metrics.ece", "ECE 校准误差"),
    ("portfolio.gross_mean", "毛收益均值"),
    ("portfolio.net_mean", "费用后收益均值"),
    ("portfolio.cum_net_return", "费用后累计收益"),
    ("portfolio.max_drawdown", "最大回撤"),
    ("portfolio.turnover", "单边换手"),
    ("portfolio.next_day_premium_mean", "次日溢价均值"),
    ("portfolio.coverage_ratio", "可成交覆盖率"),
)

COST_ROWS: tuple[tuple[str, str], ...] = (
    ("param_count_max", "参数量"),
    ("train_seconds_mean", "训练秒数(均值/窗口)"),
    ("predict_ms_per_sample_mean", "推理 ms/样本(均值)"),
    ("predict_ms_per_sample_max", "推理 ms/样本(最大)"),
    ("peak_rss_mb_max", "峰值 RSS (MB)"),
)


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return (
            str(int(value))
            if float(value).is_integer() and abs(value) >= 1
            else f"{value:.6f}"
        )
    return str(value)


def _get(node: dict[str, Any], path: str) -> Any:
    """Read a dotted path, tolerating both nested and already-flattened dicts."""

    if isinstance(node, dict) and path in node:
        return node[path]
    cur: Any = node
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def render_markdown_report(result: "RunResult") -> str:
    """Human-readable comparison report: baseline vs every candidate, per window."""

    meta = result.metadata
    lines: list[str] = []
    lines.append("# 序列 / 多模态模型增量验证报告 (issue #36)")
    lines.append("")
    lines.append(f"- 运行目的: {meta.get('run_purpose', '')}")
    lines.append(
        f"- 随机种子: `{meta.get('seed')}` | 配置哈希: `{meta.get('config_hash')}`"
    )
    lines.append(
        f"- 数据指纹: rows={meta['data_fingerprint']['n_rows']}, "
        f"dates={meta['data_fingerprint']['n_dates']} "
        f"({meta['data_fingerprint']['first_date']} ~ {meta['data_fingerprint']['last_date']}), "
        f"key_sha256={meta['data_fingerprint']['row_key_sha256']}"
    )
    lines.append(
        f"- 数据版本: {meta.get('data_version')} | 特征版本: {meta.get('feature_version')}"
    )
    lines.append(
        f"- 滚动窗口数: {meta.get('n_folds')} "
        f"(增量门槛要求样本外窗口 >= {meta.get('config', {}).get('gate', {}).get('min_windows', 3)})"
    )
    lines.append(f"- 可成交性来源: `{meta.get('executability_source')}`")
    lines.append(
        f"- **总体结论: {'保留' if result.overall_decision == 'keep' else '不保留'}**"
    )
    lines.append("")

    lines.append("## 1. 判定结论")
    lines.append("")
    lines.append("| 候选模型 | 决策 | 中文结论 | 阻断原因 |")
    lines.append("| --- | --- | --- | --- |")
    if not result.verdicts:
        lines.append("| (无候选模型) | - | - | - |")
    for name, verdict in result.verdicts.items():
        blockers = " / ".join(verdict.blockers[:3]) if verdict.blockers else "无"
        if len(verdict.blockers) > 3:
            blockers += f" 等 {len(verdict.blockers)} 项"
        lines.append(
            f"| {name} | {verdict.decision} | {verdict.conclusion_zh()} | {blockers} |"
        )
    lines.append("")

    lines.append("## 2. 样本外指标（跨窗口均值，口径完全一致）")
    lines.append("")
    models = list(result.aggregates)
    lines.append("| 指标 | " + " | ".join(models) + " | 最优候选 Δ |")
    lines.append("| --- | " + " | ".join(["---"] * (len(models) + 1)) + " |")
    baseline_name = next(
        (
            name
            for name, agg in result.aggregates.items()
            if agg.get("role") == "baseline"
        ),
        models[0] if models else None,
    )
    for path, label in METRIC_ROWS:
        cells = [
            _fmt(_get(agg["mean"], path)) if agg else "-"
            for agg in (result.aggregates[m] for m in models)
        ]
        delta = "-"
        if baseline_name:
            base_value = _get(result.aggregates[baseline_name]["mean"], path)
            candidates = [
                _get(result.aggregates[m]["mean"], path)
                for m in models
                if m != baseline_name
            ]
            candidates = [
                c
                for c in candidates
                if isinstance(c, (int, float)) and isinstance(base_value, (int, float))
            ]
            if candidates:
                delta = _fmt(max(candidates) - float(base_value))
        lines.append(f"| {label} | " + " | ".join(cells) + f" | {delta} |")
    lines.append("")

    lines.append("## 3. 分窗口明细")
    lines.append("")
    lines.append(
        "| fold | 模型 | 状态 | train/purged/test | IC | RankIC | PR-AUC | P@K | 费用后均值 | 换手 |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for record in result.windows:
        rows = record.rows
        lines.append(
            f"| {record.fold_index} | {record.model} | {record.status} | "
            f"{rows.get('train', 0)}/{rows.get('purged', 0)}/{rows.get('test', 0)} | "
            f"{_fmt(record.metrics.get('pooled_ic'))} | {_fmt(record.metrics.get('pooled_rank_ic'))} | "
            f"{_fmt(record.metrics.get('pr_auc'))} | {_fmt(record.metrics.get('precision_at_topk'))} | "
            f"{_fmt(record.portfolio.get('net_mean'))} | {_fmt(record.portfolio.get('turnover'))} |"
        )
    lines.append("")
    failures = [w for w in result.windows if w.status != "ok"]
    if failures:
        lines.append("### 未成功窗口（不允许被忽略）")
        lines.append("")
        for record in failures:
            lines.append(
                f"- fold={record.fold_index} model={record.model} status={record.status} reason={record.reason}"
            )
        lines.append("")

    lines.append("## 4. 泄漏与切分自检")
    lines.append("")
    lines.append(
        "| fold | train 标签落入 test 的行数 | train→test 位置间隔 | 被 embargo 拦住的样本 | 被 purge 的训练行 |"
    )
    lines.append("| --- | --- | --- | --- | --- |")
    seen: set[int] = set()
    for record in result.windows:
        if record.fold_index in seen:
            continue
        seen.add(record.fold_index)
        probe = record.leakage or {}
        lines.append(
            f"| {record.fold_index} | {probe.get('train_rows_with_label_window_in_test', '-')} | "
            f"{probe.get('position_gap', '-')} | {record.rows.get('withheld_embargo', '-')} | "
            f"{record.rows.get('purged', '-')} |"
        )
    lines.append("")

    lines.append("## 5. 换手 / 成本敏感性（费用后累计收益均值）")
    lines.append("")
    lines.append("| 总成本(bps) | " + " | ".join(models) + " |")
    lines.append("| --- | " + " | ".join(["---"] * len(models)) + " |")
    cost_keys = sorted(
        {
            key
            for m in models
            for key in (result.aggregates[m]["cost_sensitivity"] or {})
        },
        key=lambda s: float(s),
    )
    for key in cost_keys:
        cells = [
            _fmt(result.aggregates[m]["cost_sensitivity"].get(key)) for m in models
        ]
        lines.append(f"| {key} | " + " | ".join(cells) + " |")
    lines.append("")

    lines.append("## 6. 成本 / 延迟 / 容量")
    lines.append("")
    lines.append("| 项目 | " + " | ".join(models) + " |")
    lines.append("| --- | " + " | ".join(["---"] * len(models)) + " |")
    for path, label in COST_ROWS:
        cells = [_fmt(result.aggregates[m]["cost"].get(path)) for m in models]
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    lines.append("")

    lines.append("## 7. 模型卡")
    lines.append("")
    from .runner import model_cards

    for card in model_cards(result):
        lines.append(f"### {card['model']} ({card['kind']}, role={card['role']})")
        lines.append("")
        lines.append(f"- 适用场景: {card['applicable_zh']}")
        lines.append(f"- 失效场景: {card['failure_zh']}")
        lines.append(f"- 数据依赖: {card['data_dependency_zh']}")
        cap = card["capacity"]
        lines.append(
            f"- 容量: 参数量 {cap['param_count']}, 训练 {cap['train_seconds_mean']}s/窗口, "
            f"推理 {cap['predict_ms_per_sample_mean']}ms/样本, 峰值 RSS {cap['peak_rss_mb_max']}MB, 设备 {cap['device']}"
        )
        lines.append(
            f"- 窗口: {card['windows']['scored']}/{card['windows']['total']} 成功打分"
        )
        if card["decision"] is None:
            lines.append("- 决策: 基准模型（不作为候选评估）")
        else:
            lines.append(f"- 决策: {card['decision']} / {card['decision_zh']}")
            for blocker in card["blockers"]:
                lines.append(f"  - 阻断: {blocker}")
        lines.append("")

    lines.append("## 8. 复现方式")
    lines.append("")
    lines.append("```bash")
    lines.append(
        "python -m strategy.model_validation.cli demo --out data/model_validation"
    )
    lines.append("```")
    lines.append("")
    lines.append(
        json.dumps(
            meta.get("config", {}), ensure_ascii=False, indent=2, sort_keys=True
        )[:4000]
    )
    lines.append("")
    return "\n".join(lines)


def windows_to_frame(result: "RunResult") -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for record in result.windows:
        flat: dict[str, Any] = {
            "fold_index": record.fold_index,
            "model": record.model,
            "status": record.status,
            "reason": record.reason,
            **{f"rows.{k}": v for k, v in record.rows.items()},
            **{
                f"leakage.{k}": v
                for k, v in record.leakage.items()
                if not isinstance(v, list)
            },
            **{f"cost.{k}": v for k, v in record.cost.items()},
        }
        for key, value in record.metrics.items():
            if isinstance(value, (int, float, str)) or value is None:
                flat[f"metrics.{key}"] = value
        for key, value in record.portfolio.items():
            if isinstance(value, (int, float, str)) or value is None:
                flat[f"portfolio.{key}"] = value
        rows.append(flat)
    return pd.DataFrame(rows)


def write_outputs(result: "RunResult", output_dir: str | Path) -> dict[str, str]:
    """Persist the report bundle; returns the written paths."""

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    from .runner import model_cards

    paths: dict[str, str] = {}
    report_path = out / "report.md"
    report_path.write_text(render_markdown_report(result), encoding="utf-8")
    paths["report"] = str(report_path)

    metrics_path = out / "metrics.json"
    metrics_path.write_text(
        json.dumps(result.to_json(), ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    paths["metrics"] = str(metrics_path)

    decisions = {name: verdict.as_dict() for name, verdict in result.verdicts.items()}
    decision_path = out / "decision.json"
    decision_path.write_text(
        json.dumps(
            {
                "overall_decision": result.overall_decision,
                "overall_decision_zh": "保留"
                if result.overall_decision == "keep"
                else "不保留",
                "kept_candidates": result.kept_candidates(),
                "rejected_candidates": result.rejected_candidates(),
                "verdicts": decisions,
                "config_hash": result.metadata.get("config_hash"),
                "data_fingerprint": result.metadata.get("data_fingerprint"),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    paths["decision"] = str(decision_path)

    cards_path = out / "model_cards.json"
    cards_path.write_text(
        json.dumps(model_cards(result), ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    paths["model_cards"] = str(cards_path)

    frame = windows_to_frame(result)
    if not frame.empty:
        csv_path = out / "windows.csv"
        frame.to_csv(csv_path, index=False)
        paths["windows_csv"] = str(csv_path)
    if not paths:  # pragma: no cover - defensive
        raise ModelArtifactError(f"failed to write validation outputs to {out}")
    return paths
