import json
import subprocess
import sys
from pathlib import Path


def test_security_scan_detects_high_findings(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text('OPENAI_API_KEY="sk-abcdefghijklmnopqrstuvwxyz1234"\n', encoding="utf-8")

    rules = tmp_path / "rules.json"
    rules.write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "id": "openai_api_key",
                        "description": "Potential OpenAI API key in source code",
                        "severity": "high",
                        "pattern": "sk-[A-Za-z0-9_-]{20,}",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    output = tmp_path / "findings.json"
    summary = tmp_path / "summary.md"

    cmd = [
        sys.executable,
        "scripts/security_scan.py",
        "--root",
        str(src),
        "--rules",
        str(rules),
        "--output",
        str(output),
        "--summary-md",
        str(summary),
        "--fail-on-high",
    ]
    result = subprocess.run(cmd, cwd="/Users/jie.feng/wlb/Alpha", capture_output=True, text=True)

    assert result.returncode == 1
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["counts"]["high"] == 1
    assert report["findings"][0]["rule_id"] == "openai_api_key"
    assert "总命中" in summary.read_text(encoding="utf-8")


def test_security_scan_ignore_marker(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "b.py").write_text(
        'OPENAI_API_KEY="sk-abcdefghijklmnopqrstuvwxyz1234"  # secretscan:ignore\n',
        encoding="utf-8",
    )

    rules = tmp_path / "rules.json"
    rules.write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "id": "openai_api_key",
                        "description": "Potential OpenAI API key in source code",
                        "severity": "high",
                        "pattern": "sk-[A-Za-z0-9_-]{20,}",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    output = tmp_path / "findings.json"
    summary = tmp_path / "summary.md"

    cmd = [
        sys.executable,
        "scripts/security_scan.py",
        "--root",
        str(src),
        "--rules",
        str(rules),
        "--output",
        str(output),
        "--summary-md",
        str(summary),
        "--fail-on-high",
    ]
    result = subprocess.run(cmd, cwd="/Users/jie.feng/wlb/Alpha", capture_output=True, text=True)

    assert result.returncode == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["counts"]["high"] == 0
