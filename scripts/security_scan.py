#!/usr/bin/env python3
"""Simple sensitive-point scanner for source code repositories."""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List

DEFAULT_EXCLUDES = [
    ".git/**",
    ".venv/**",
    "venv/**",
    "__pycache__/**",
    ".pytest_cache/**",
    "node_modules/**",
    "logs/**",
    "data/**",
]


@dataclass
class Rule:
    rid: str
    description: str
    pattern: str
    severity: str


def _is_excluded(path: Path, root: Path, excludes: List[str]) -> bool:
    rel = path.relative_to(root).as_posix()
    return any(fnmatch.fnmatch(rel, pat) for pat in excludes)


def _is_text_file(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            chunk = f.read(2048)
        if b"\x00" in chunk:
            return False
    except OSError:
        return False
    return True


def load_rules(path: Path) -> List[Rule]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rules: List[Rule] = []
    for item in payload.get("rules", []):
        rules.append(
            Rule(
                rid=item["id"],
                description=item["description"],
                pattern=item["pattern"],
                severity=item.get("severity", "medium").lower(),
            )
        )
    return rules


def scan_repo(root: Path, rules: List[Rule], excludes: List[str]) -> Dict[str, List[Dict[str, object]]]:
    findings: List[Dict[str, object]] = []
    rule_hits: Dict[str, int] = {rule.rid: 0 for rule in rules}
    compiled = {rule.rid: re.compile(rule.pattern) for rule in rules}

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if _is_excluded(path, root, excludes):
            continue
        if not _is_text_file(path):
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        rel_path = path.relative_to(root).as_posix()
        for lineno, line in enumerate(content.splitlines(), start=1):
            if "secretscan:ignore" in line:
                continue
            for rule in rules:
                if compiled[rule.rid].search(line):
                    findings.append(
                        {
                            "rule_id": rule.rid,
                            "severity": rule.severity,
                            "description": rule.description,
                            "path": rel_path,
                            "line": lineno,
                            "snippet": line.strip()[:200],
                        }
                    )
                    rule_hits[rule.rid] += 1

    return {"findings": findings, "rule_hits": rule_hits}


def _summary(findings: Iterable[Dict[str, object]]) -> Dict[str, int]:
    out = {"high": 0, "medium": 0, "low": 0}
    for item in findings:
        sev = str(item.get("severity", "low"))
        out[sev] = out.get(sev, 0) + 1
    out["total"] = out["high"] + out["medium"] + out["low"]
    return out


def write_markdown_summary(path: Path, root: Path, report: Dict[str, object]) -> None:
    findings: List[Dict[str, object]] = report["findings"]  # type: ignore[index]
    counts = _summary(findings)
    lines = [
        "# 密点扫描结果",
        "",
        f"- 扫描目录: `{root}`",
        f"- 总命中: `{counts['total']}`",
        f"- High: `{counts['high']}` / Medium: `{counts['medium']}` / Low: `{counts['low']}`",
        "",
    ]
    if not findings:
        lines.append("## 结果")
        lines.append("")
        lines.append("未发现命中项。")
    else:
        lines.append("## 命中明细")
        lines.append("")
        for item in findings:
            lines.append(
                "- [{severity}] {rule} `{path}:{line}` {desc}".format(
                    severity=item["severity"],
                    rule=item["rule_id"],
                    path=item["path"],
                    line=item["line"],
                    desc=item["description"],
                )
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan repository for sensitive code points.")
    parser.add_argument("--root", default=".", help="Repository root to scan.")
    parser.add_argument("--rules", default="security/secret_rules.json", help="Rules json path.")
    parser.add_argument("--output", default="reports/security_findings.json", help="Output json report.")
    parser.add_argument(
        "--summary-md",
        default="reports/security_summary.md",
        help="Output markdown summary path.",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Additional exclude glob pattern. Can be provided multiple times.",
    )
    parser.add_argument(
        "--fail-on-high",
        action="store_true",
        help="Exit with code 1 when high severity findings are detected.",
    )
    args = parser.parse_args()

    root = Path(args.root).resolve()
    rules = load_rules(Path(args.rules))
    excludes = DEFAULT_EXCLUDES + list(args.exclude)

    report = scan_repo(root, rules, excludes)
    findings: List[Dict[str, object]] = report["findings"]  # type: ignore[index]
    counts = _summary(findings)
    final_report = {
        "root": str(root),
        "rules_path": str(Path(args.rules).resolve()),
        "counts": counts,
        "findings": findings,
        "rule_hits": report["rule_hits"],
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(final_report, ensure_ascii=False, indent=2), encoding="utf-8")

    write_markdown_summary(Path(args.summary_md), root, final_report)
    print(json.dumps(final_report["counts"], ensure_ascii=False))

    if args.fail_on_high and counts["high"] > 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
