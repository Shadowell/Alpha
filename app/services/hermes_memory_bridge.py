"""Hermes Memory Bridge — 将 Alpha 的反馈写入 Hermes Agent 的永久记忆。

负责在 approve/reject 提案时，将反馈经验同步到 Hermes 的 MEMORY.md，
使其在后续诊断中能参考历史决策。
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

HERMES_MEMORY_PATH = Path.home() / ".hermes" / "memories" / "MEMORY.md"
MEMORY_CHAR_LIMIT = 2200

_EXPERIENCE_HEADER = "**历史调参经验**："
_EXPERIENCE_SECTION_RE = re.compile(
    r"(\*\*历史调参经验\*\*：\n)((?:- .+\n)*)",
    re.MULTILINE,
)
_MAX_EXPERIENCE_LINES = 8


def record_feedback_to_hermes_memory(
    proposal_title: str,
    proposal_type: str,
    action: str,
    note: str = "",
    diff_payload: dict | None = None,
) -> bool:
    """将提案反馈写入 Hermes MEMORY.md 的历史经验区。

    Returns True if memory was updated successfully.
    """
    if not HERMES_MEMORY_PATH.exists():
        return False

    try:
        content = HERMES_MEMORY_PATH.read_text(encoding="utf-8")
    except Exception:
        return False

    date_str = datetime.now().strftime("%Y-%m-%d")
    action_zh = "✅批准" if action == "approve" else "❌驳回"

    parts = [f"- {date_str}: {action_zh}「{proposal_title}」"]
    if diff_payload and isinstance(diff_payload, dict):
        for k, v in list(diff_payload.items())[:3]:
            if isinstance(v, dict) and "from" in v and "to" in v:
                parts.append(f"({k}: {v['from']}→{v['to']})")
    if note:
        parts.append(f"原因: {note}")

    new_line = " ".join(parts) + "\n"

    match = _EXPERIENCE_SECTION_RE.search(content)
    if match:
        existing_lines = match.group(2).strip().split("\n") if match.group(2).strip() else []
        existing_lines.insert(0, new_line.strip())
        existing_lines = existing_lines[:_MAX_EXPERIENCE_LINES]
        updated_block = f"{_EXPERIENCE_HEADER}\n" + "\n".join(existing_lines) + "\n"
        content = content[:match.start()] + updated_block + content[match.end():]
    else:
        content = content.rstrip() + f"\n\n{_EXPERIENCE_HEADER}\n{new_line}"

    if len(content.encode("utf-8")) > MEMORY_CHAR_LIMIT:
        content = _trim_to_limit(content)

    try:
        HERMES_MEMORY_PATH.write_text(content, encoding="utf-8")
        return True
    except Exception:
        return False


def record_outcome_to_hermes_memory(
    proposal_title: str,
    outcome_summary: str,
) -> bool:
    """将提案效果追踪结果写入 Hermes MEMORY.md。"""
    if not HERMES_MEMORY_PATH.exists():
        return False

    try:
        content = HERMES_MEMORY_PATH.read_text(encoding="utf-8")
    except Exception:
        return False

    date_str = datetime.now().strftime("%Y-%m-%d")
    new_line = f"- {date_str}: 📊效果追踪「{proposal_title}」→ {outcome_summary}\n"

    match = _EXPERIENCE_SECTION_RE.search(content)
    if match:
        existing_lines = match.group(2).strip().split("\n") if match.group(2).strip() else []
        existing_lines.insert(0, new_line.strip())
        existing_lines = existing_lines[:_MAX_EXPERIENCE_LINES]
        updated_block = f"{_EXPERIENCE_HEADER}\n" + "\n".join(existing_lines) + "\n"
        content = content[:match.start()] + updated_block + content[match.end():]
    else:
        content = content.rstrip() + f"\n\n{_EXPERIENCE_HEADER}\n{new_line}"

    if len(content.encode("utf-8")) > MEMORY_CHAR_LIMIT:
        content = _trim_to_limit(content)

    try:
        HERMES_MEMORY_PATH.write_text(content, encoding="utf-8")
        return True
    except Exception:
        return False


def _trim_to_limit(content: str) -> str:
    """如果超过字符限制，从经验区尾部裁剪。"""
    match = _EXPERIENCE_SECTION_RE.search(content)
    if not match:
        return content[:MEMORY_CHAR_LIMIT]

    while len(content.encode("utf-8")) > MEMORY_CHAR_LIMIT:
        match = _EXPERIENCE_SECTION_RE.search(content)
        if not match or not match.group(2).strip():
            break
        lines = match.group(2).strip().split("\n")
        if len(lines) <= 1:
            break
        lines = lines[:-1]
        updated_block = f"{_EXPERIENCE_HEADER}\n" + "\n".join(lines) + "\n"
        content = content[:match.start()] + updated_block + content[match.end():]

    return content
