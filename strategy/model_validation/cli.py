"""Reproducible entry point for the increment validation runs (issue #36).

Usage::

    python -m strategy.model_validation.cli demo                 # synthetic demo, both verdicts
    python -m strategy.model_validation.cli run --input-csv p.csv --seq-npy x.npy
    python -m strategy.model_validation.cli demo --out /tmp/mv --fail-on-reject

Reports default to ``data/model_validation`` - a path that is gitignored in this
repo (``data/``), so experiment output never lands in a commit.  The CLI is
research-only: nothing in ``app/`` imports it and it never starts a server.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .contracts import ColumnNames, Panel, SequenceBundle
from .fixtures import SyntheticConfig, make_synthetic_panel
from .gate import GateConfig
from .runner import (
    ROLE_BASELINE,
    ROLE_CANDIDATE,
    ModelSpec,
    ValidationConfig,
    run_validation,
)
from .splitting import WalkForwardConfig

DEFAULT_OUT = Path("data/model_validation")
SCENARIOS = ("informative_sequence", "pure_noise_sequence")


def _folds_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--windows",
        type=int,
        default=3,
        help="minimum number of OOS walk-forward windows",
    )
    parser.add_argument(
        "--test-size", type=int, default=6, help="trading dates per OOS window"
    )
    parser.add_argument(
        "--train-size",
        type=int,
        default=24,
        help="trading dates of the rolling train block",
    )
    parser.add_argument(
        "--embargo",
        type=int,
        default=1,
        help="extra guard band on top of the label horizon",
    )
    parser.add_argument("--label-horizon", type=int, default=2)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--out", type=Path, default=DEFAULT_OUT, help="gitignored output directory"
    )
    parser.add_argument(
        "--fail-on-reject",
        action="store_true",
        help="exit non-zero when every candidate is rejected",
    )
    parser.add_argument(
        "--save-artifacts",
        action="store_true",
        help="persist each fold's fitted model artifact for reproducibility",
    )


def _config(
    args: argparse.Namespace, models: tuple[ModelSpec, ...], *, data_version: str = ""
) -> ValidationConfig:
    return ValidationConfig(
        seed=args.seed,
        label_horizon=args.label_horizon,
        top_k=args.top_k,
        walk_forward=WalkForwardConfig(
            test_size=args.test_size,
            train_size=args.train_size,
            embargo=max(1, args.embargo),
            max_folds=max(3, args.windows),
        ),
        gate=GateConfig(min_windows=max(3, args.windows)),
        models=models,
        save_artifacts=bool(getattr(args, "save_artifacts", False)),
        data_version=data_version or "unknown",
        feature_version="synthetic:v0"
        if data_version.startswith("synthetic")
        else "external",
    )


def _load_csv_panel(
    path: Path, args: argparse.Namespace, sequence_path: Path | None
) -> Panel:
    frame = pd.read_csv(path)
    columns = ColumnNames(
        symbol=args.symbol_col,
        event_date=args.date_col,
        label=args.label_col,
        gross_return=args.return_col,
    )
    feature_cols = [c for c in frame.columns if c.startswith(args.feature_prefix)]
    if args.features:
        feature_cols = [c.strip() for c in args.features.split(",") if c.strip()]
    sequence = None
    if sequence_path is not None:
        values = np.load(sequence_path)
        keys = frame[[columns.symbol, columns.event_date]].copy()
        if values.shape[0] != len(frame):
            raise SystemExit(
                f"sequence rows {values.shape[0]} != panel rows {len(frame)}"
            )
        names = ()
        meta = sequence_path.with_suffix(".meta.json")
        if meta.exists():
            names = tuple(
                json.loads(meta.read_text(encoding="utf-8")).get("feature_names", [])
            )
        sequence = SequenceBundle(values=values, keys=keys, feature_names=names)
    return Panel(
        frame=frame, features=tuple(feature_cols), columns=columns, sequence=sequence
    )


def _summarise(results: dict[str, Any]) -> str:
    lines: list[str] = []
    for scenario, result in results.items():
        lines.append(f"[{scenario}] overall={result.overall_decision}")
        for name, verdict in result.verdicts.items():
            lines.append(f"  - {name}: {verdict.decision} ({verdict.conclusion_zh()})")
            for blocker in verdict.blockers[:4]:
                lines.append(f"      * {blocker}")
    return "\n".join(lines)


def cmd_demo(args: argparse.Namespace) -> int:
    models = (
        ModelSpec(
            "tabular_baseline",
            ROLE_BASELINE,
            "RidgeTabularModel",
            {"name": "tabular_baseline"},
        ),
        ModelSpec(
            "sequence_candidate",
            ROLE_CANDIDATE,
            "SequenceTemporalModel",
            {"name": "sequence_candidate"},
        ),
        ModelSpec(
            "blend_candidate",
            ROLE_CANDIDATE,
            "RankBlendModel",
            {"name": "blend_candidate", "weight": 0.5},
        ),
    )
    results: dict[str, Any] = {}
    for scenario in SCENARIOS:
        panel = make_synthetic_panel(
            SyntheticConfig(
                scenario=scenario,  # type: ignore[arg-type]
                seed=args.seed,
                n_dates=max(
                    40,
                    args.train_size
                    + 2 * (args.embargo + args.test_size)
                    + args.label_horizon
                    + 6,
                ),
            )
        )
        cfg = replace(
            _config(args, models),
            data_version=f"synthetic:{scenario}",
            feature_version="synthetic:v0",
        )
        out = args.out / scenario
        results[scenario] = run_validation(panel, cfg, output_dir=out)
    print(_summarise(results))
    for scenario, result in results.items():
        print(f"report -> {result.artifacts.get('report')}")
    if args.fail_on_reject and all(
        r.overall_decision != "keep" for r in results.values()
    ):
        return 1
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    panel = _load_csv_panel(
        Path(args.input_csv), args, Path(args.seq_npy) if args.seq_npy else None
    )
    models = (
        ModelSpec(
            "tabular_baseline",
            ROLE_BASELINE,
            "RidgeTabularModel",
            {"name": "tabular_baseline"},
        ),
        ModelSpec(
            "sequence_candidate",
            ROLE_CANDIDATE,
            "SequenceTemporalModel",
            {
                "name": "sequence_candidate",
                "seq_len": args.seq_len,
                "length_policy": args.length_policy,
            },
        ),
    )
    cfg = _config(args, models)
    result = run_validation(
        panel,
        cfg,
        output_dir=args.out / (args.tag or "run"),
    )
    print(_summarise({args.tag or "run": result}))
    print(f"report -> {result.artifacts.get('report')}")
    if args.fail_on_reject and result.overall_decision != "keep":
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="model_validation", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser(
        "demo", help="run both synthetic scenarios (keep + reject paths)"
    )
    _folds_arg(demo)
    demo.set_defaults(func=cmd_demo)

    run = sub.add_parser("run", help="run against a real panel CSV produced by #23/#26")
    _folds_arg(run)
    run.add_argument("--input-csv", required=True)
    run.add_argument("--seq-npy", default=None)
    run.add_argument("--symbol-col", default="symbol")
    run.add_argument("--date-col", default="event_date")
    run.add_argument("--label-col", default="label")
    run.add_argument("--return-col", default="outcome_return")
    run.add_argument(
        "--features",
        default="",
        help="comma separated feature columns (default: prefix match)",
    )
    run.add_argument("--feature-prefix", default="f_")
    run.add_argument("--seq-len", type=int, default=None)
    run.add_argument(
        "--length-policy", default="error", choices=("error", "truncate", "pad")
    )
    run.add_argument("--tag", default="")
    run.set_defaults(func=cmd_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
