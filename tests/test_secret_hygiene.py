from __future__ import annotations

import asyncio
import os
from pathlib import Path
import re
import subprocess

from app.services import feishu_notify


ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {".env", ".json", ".md", ".py", ".sh", ".toml", ".txt", ".yaml", ".yml"}
FEISHU_WEBHOOK_TOKEN = re.compile(
    r"https://open\.feishu\.cn/open-apis/bot/v2/hook/"
    r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}",
    re.IGNORECASE,
)
FEISHU_CREDENTIAL = re.compile(
    r"(?i)(?:FEISHU_APP_(?:ID|SECRET)|['\"]app_(?:id|secret)['\"])"
    r"\s*[:=]\s*['\"]([^'\"]+)['\"]"
)


def _tracked_text_files() -> list[Path]:
    output = subprocess.check_output(
        ["git", "ls-files", "-z"], cwd=ROOT
    ).decode().split("\0")
    return [ROOT / item for item in output if item and Path(item).suffix.lower() in TEXT_SUFFIXES]


def _is_placeholder(value: str) -> bool:
    lowered = value.lower()
    return (
        not value
        or "your_" in lowered
        or "your-" in lowered
        or "placeholder" in lowered
        or "<redacted>" in lowered
        or "xxxx" in lowered
    )


def test_tracked_files_do_not_contain_feishu_credentials() -> None:
    findings: list[str] = []
    for path in _tracked_text_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        if FEISHU_WEBHOOK_TOKEN.search(text):
            findings.append(str(path.relative_to(ROOT)))
        for match in FEISHU_CREDENTIAL.finditer(text):
            if not _is_placeholder(match.group(1).strip()):
                findings.append(str(path.relative_to(ROOT)))
    assert not sorted(set(findings)), f"tracked Feishu credentials found in: {sorted(set(findings))}"


def test_feishu_notify_skips_when_webhook_is_unconfigured(monkeypatch) -> None:
    monkeypatch.delenv("FEISHU_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("FEISHU_WEBHOOK", raising=False)

    result = asyncio.run(feishu_notify.send_feishu_text("security regression"))

    assert result["skipped"] is True
    assert "not configured" in result["error"]


def test_feishu_notify_uses_configured_webhook(monkeypatch) -> None:
    calls: list[tuple[str, dict]] = []

    class Response:
        @staticmethod
        def json() -> dict:
            return {"ok": True}

    class Client:
        async def post(self, url: str, *, json: dict) -> Response:
            calls.append((url, json))
            return Response()

    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "https://example.invalid/feishu-hook")
    monkeypatch.setattr(feishu_notify, "_get_client", lambda: Client())

    result = asyncio.run(feishu_notify.send_feishu_text("legitimate notification"))

    assert result == {"ok": True}
    assert calls == [
        (
            "https://example.invalid/feishu-hook",
            {"msg_type": "text", "content": {"text": "legitimate notification"}},
        )
    ]
