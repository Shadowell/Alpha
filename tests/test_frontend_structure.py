"""前端结构与安全渲染约束（#8 / #12），无需浏览器即可在 CI 运行。"""

from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
MODULES = STATIC / "js"

EXPECTED_ORDER = [
    "security.js",
    "core.js",
    "summary.js",
    "api.js",
    "polling.js",
    "navigation.js",
    "funnel.js",
    "data-center.js",
    "charts.js",
    "stock.js",
    "notice-tab.js",
    "loaders.js",
    "paper.js",
    "agent.js",
    "monitor.js",
    "hover.js",
    "predict-modal.js",
    "ws.js",
    "boot-helpers.js",
    "boot.js",
    "predict.js",
    "strategy.js",
]


def test_app_js_is_a_thin_entrypoint():
    entry = (STATIC / "app.js").read_text()
    assert len(entry.splitlines()) < 1000
    assert "bootstrapAlpha" in entry
    assert "function esc(" not in entry


def test_every_module_exists_and_index_loads_them_in_order():
    assert all((MODULES / name).exists() for name in EXPECTED_ORDER)
    html = (STATIC / "index.html").read_text()
    loaded = re.findall(r"/static/js/([a-z0-9-]+\.js)", html)
    assert loaded == EXPECTED_ORDER
    # 入口脚本必须在所有模块之后
    assert html.index("/static/app.js") > html.index("/static/js/strategy.js")


def test_no_duplicate_top_level_declarations_between_modules():
    """classic scripts 共享全局作用域，重复 const/let 会直接 SyntaxError。"""
    seen: dict[str, str] = {}
    pattern = re.compile(r"^(?:const|let)\s+([A-Za-z_$][\w$]*)", re.MULTILINE)
    for name in EXPECTED_ORDER:
        for symbol in pattern.findall((MODULES / name).read_text()):
            assert symbol not in seen, f"{symbol} 同时在 {seen[symbol]} 与 {name} 声明"
            seen[symbol] = name


def test_pages_declare_csp_and_have_no_inline_handlers():
    for page in ("index.html", "notice.html"):
        html = (STATIC / page).read_text()
        assert 'http-equiv="Content-Security-Policy"' in html
        assert "script-src 'self' https://cdn.jsdelivr.net" in html
        assert "frame-ancestors 'none'" in html
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>[\s\S]*?</script>", html, re.I)
        assert not re.search(r"\son(?:click|error|load)\s*=", html, re.I)


def test_external_notice_fields_never_hit_innerhtml_raw():
    notice_tab = (MODULES / "notice-tab.js").read_text()
    assert "esc(stock.title)" in notice_tab
    assert "esc(stock.notice_type)" in notice_tab
    assert "esc(stock.reason" in notice_tab
    assert "esc(stock.risk" in notice_tab
    assert "safeLinkHtml(first.url" in notice_tab

    notice_legacy = (STATIC / "notice.js").read_text()
    assert "safeLinkHtml(first.url" in notice_legacy
    assert "href=\"${safeUrl(" not in notice_legacy


def test_polling_is_centralized():
    source = "\n".join((MODULES / name).read_text() for name in EXPECTED_ORDER)
    page_files = [
        name for name in EXPECTED_ORDER
        if name not in {"polling.js", "security.js", "api.js"}
    ]
    scattered = [name for name in page_files if "setInterval(" in (MODULES / name).read_text()]
    assert scattered == [], f"周期轮询应集中在 polling.js，实际出现在 {scattered}"
    polling = (MODULES / "polling.js").read_text()
    assert "const Polling" in polling
    assert "visibilitychange" in polling
    assert "window.Polling = Polling" in polling
    # 页面脚本允许一次性 setTimeout（toast/滚动/浮窗），但不应再自建轮询循环。
    assert "setInterval(" not in source


def test_jobs_contract_is_supported():
    api = (MODULES / "api.js").read_text()
    assert "job_id" in api
    assert "/api/jobs/" in api
    assert "err.status === 409" in api
    assert "requestJob" in api
