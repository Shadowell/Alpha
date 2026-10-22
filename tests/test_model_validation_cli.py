"""The #36 CLI must be runnable and self-contained (synthetic demo, real CSV)."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from strategy.model_validation import cli


def test_demo_writes_both_keep_and_reject_reports(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main(["demo", "--out", str(tmp_path), "--windows", "3", "--seed", "5"])
    printed = capsys.readouterr().out

    assert code == 0
    assert "[pure_noise_sequence] overall=reject" in printed
    assert "[informative_sequence] overall=keep" in printed
    for scenario in ("pure_noise_sequence", "informative_sequence"):
        report = tmp_path / scenario / "report.md"
        decision = tmp_path / scenario / "decision.json"
        assert report.exists() and report.stat().st_size > 500
        payload = json.loads(decision.read_text(encoding="utf-8"))
        assert payload["overall_decision"] == (
            "keep" if scenario == "informative_sequence" else "reject"
        )


def test_demo_fail_on_reject_only_triggers_when_nothing_beats_the_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "SCENARIOS", ("pure_noise_sequence",))
    assert (
        cli.main(
            ["demo", "--out", str(tmp_path / "a"), "--windows", "3", "--fail-on-reject"]
        )
        == 1
    )

    monkeypatch.setattr(cli, "SCENARIOS", ("informative_sequence",))
    assert (
        cli.main(
            ["demo", "--out", str(tmp_path / "b"), "--windows", "3", "--fail-on-reject"]
        )
        == 0
    )


def _write_external_panel(
    path: Path, *, n_dates: int = 40, n_symbols: int = 6
) -> tuple[Path, Path]:
    rng = np.random.default_rng(4)
    dates = [
        d.date().isoformat()
        for d in pd.date_range("2026-02-02", periods=n_dates, freq="B")
    ]
    rows = []
    tensor = []
    signal = rng.normal(size=(n_dates, n_symbols))
    extra = rng.normal(size=(n_dates, n_symbols))
    for t, date in enumerate(dates):
        for i, symbol in enumerate(
            [f"{300000 + j:06d}" for j in range(n_symbols)][:n_symbols]
        ):
            outcome = (
                0.4 * signal[t, i]
                + 0.9 * extra[max(0, t - 1) : t + 1, i].mean()
                + 0.2 * rng.normal()
            )
            rows.append(
                {
                    "symbol": symbol,
                    "event_date": date,
                    "label": float(outcome > 0),
                    "outcome_return": 0.01 * outcome,
                    "f_alpha": signal[t, i],
                    "f_noise": rng.normal(),
                    "can_buy": True,
                    "can_sell": True,
                }
            )
            window = np.zeros((6, 2))
            window[:, 0] = (
                np.cumsum(extra[max(0, t - 5) : t + 1, i])[-6:] if t >= 5 else 0.0
            )
            tensor.append(window)
    pd.DataFrame(rows).to_csv(path / "panel.csv", index=False)
    values = np.stack(tensor)
    seq_path = path / "panel_seq.npy"
    np.save(seq_path, values)
    (seq_path.with_suffix(".meta.json")).write_text(
        json.dumps({"feature_names": ["walk", "junk"]}), encoding="utf-8"
    )
    return path / "panel.csv", seq_path


def test_run_command_consumes_an_external_csv_and_sequence_tensor(
    tmp_path: Path,
) -> None:
    csv_path, seq_path = _write_external_panel(tmp_path)
    code = cli.main(
        [
            "run",
            "--input-csv",
            str(csv_path),
            "--seq-npy",
            str(seq_path),
            "--out",
            str(tmp_path / "out"),
            "--tag",
            "external",
            "--windows",
            "3",
            "--feature-prefix",
            "f_",
            "--seq-len",
            "6",
        ]
    )

    assert code == 0
    payload = json.loads(
        (tmp_path / "out" / "external" / "decision.json").read_text(encoding="utf-8")
    )
    assert payload["overall_decision"] in {"keep", "reject"}
    assert {"sequence_candidate"} <= set(payload["verdicts"])
    report = (tmp_path / "out" / "external" / "report.md").read_text(encoding="utf-8")
    assert "tabular_baseline" in report and "sequence_candidate" in report


def test_run_command_rejects_a_mismatched_tensor(tmp_path: Path) -> None:
    csv_path, seq_path = _write_external_panel(tmp_path, n_dates=20, n_symbols=3)
    np.save(seq_path, np.zeros((7, 6, 2)))  # wrong row count on purpose
    with pytest.raises(SystemExit, match="sequence rows"):
        cli.main(
            [
                "run",
                "--input-csv",
                str(csv_path),
                "--seq-npy",
                str(seq_path),
                "--out",
                str(tmp_path / "x"),
            ]
        )


def test_default_output_directory_is_gitignored() -> None:
    # the CLI must never drop experiment artefacts into a tracked path by default
    assert str(cli.DEFAULT_OUT).startswith("data/")
