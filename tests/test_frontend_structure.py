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


# ─────────────────── 前端 QA 批次（#50/#53/#54/#55/#56/#57）结构约束 ───────────────────


def test_market_time_uses_single_china_timezone_helper():
    """#57：交易时段判定必须收敛到东八区工具函数，禁止各处内联本地时钟。"""
    core = (MODULES / "core.js").read_text()
    assert "Intl.DateTimeFormat" in core
    assert "timeZone: CHINA_TZ_ID" in core
    assert 'const CHINA_TZ_ID = "Asia/Shanghai"'.replace('"', "'") in core
    for helper in ("getChinaNowParts", "getMarketSession", "isChinaMarketOpen"):
        assert f"function {helper}(" in core, f"core.js 缺少 {helper}"
    # 服务端契约优先，缺失时才走前端东八区兜底
    assert "market_session" in core and "is_trading_day" in core
    # 节假日仍靠后端，必须留下明确说明而不是假装支持
    assert "节假日" in core

    funnel = (MODULES / "funnel.js").read_text()
    assert "function _isMarketOpen" in funnel
    assert "isChinaMarketOpen(" in funnel
    assert "now.getHours()" not in funnel
    assert "now.getDay()" not in funnel
    # 状态灯同样不再读本地时钟
    assert "getMarketSession(now)" in core


def test_request_has_timeout_and_abort():
    """#55：request() 必须有 AbortController + 可配置超时，并区分三类错误。"""
    api = (MODULES / "api.js").read_text()
    assert "AbortController" in api
    assert "signal: controller.signal" in api
    assert "API_TIMEOUT_DEFAULTS = { read: 10000, write: 20000 }" in api
    assert "window.API_TIMEOUT_MS" in api
    for code in ("'timeout'", "'network'", "'http'"):
        assert f"error.code = {code}" in api or f"err.code = {code}" in api, f"缺少错误分类 {code}"
    # 轮询任务的每一轮也必须受超时保护：偶发超时不能直接终止整个任务等待
    assert "transientRetries" in api


def test_block_level_errors_are_independent():
    """#55：单个区块失败只影响该区块，并给出独立重试入口。"""
    core = (MODULES / "core.js").read_text()
    assert "function showBlockError(" in core
    assert "function reportBlockFailure(" in core
    assert "block-retry" in core
    # 挂起/失败的并发分支都要走同一套区块级出口
    for name in ("data-center.js", "loaders.js", "boot.js", "paper.js", "notice-tab.js"):
        src = (MODULES / name).read_text()
        assert "Promise.allSettled" in src
        assert ("reportBlockFailure" in src or "showBlockError" in src), f"{name} 缺少区块级失败处理"


def test_data_center_polling_is_split_by_change_frequency():
    """#54：状态 5s/60s、概览 30~60s、报告与日志按关键状态条件刷新。"""
    dc = (MODULES / "data-center.js").read_text()
    assert "const DC_POLL = {" in dc
    assert "status: { running: 5000, idle: 60000 }" in dc
    for kind in ("stats", "enrichment"):
        block = dc.split(f"{kind}: {{", 1)[1].split("}", 1)[0]
        running, idle = (int(x) for x in block.replace("running:", "").replace("idle:", "").split(",")[:2])
        assert 30000 <= running <= 60000, f"{kind} running={running}"
        assert 30000 <= idle <= 60000, f"{kind} idle={idle}"
    # report/logs 只在阶段跃迁或用户操作后刷新
    for kind in ("report", "logs"):
        block = dc.split(f"{kind}: {{", 1)[1].split("}", 1)[0]
        running, idle = (int(x) for x in block.replace("running:", "").replace("idle:", "").split(",")[:2])
        assert idle > running, f"{kind} 空闲兜底应慢于运行中：{running}/{idle}"
    assert "_dcStatusSignatureOf" in dc and "_dcMarkStale" in dc
    # 同一区块不得叠加请求
    assert "_dcRunOnce" in dc

    boot = (MODULES / "boot.js").read_text()
    assert "Polling.register('dc-status'" in boot
    assert "Polling.register('dc-detail'" in boot
    assert "interval: 3000, tab: 'data'" not in boot
    assert "Polling.reschedule" in dc


def test_strategy_hit_cards_have_action_area_and_enroll_contract():
    """#50：策略命中卡片必须提供一致操作区，并按入池接口契约调用。"""
    funnel = (MODULES / "funnel.js").read_text()
    assert "const POOL_ENROLL_PATH = '/api/pool/enroll'" in funnel
    assert 'source: "strategy_scan"' in funnel or "source: 'strategy_scan'" in funnel
    # 后端未上线时降级到迁移接口，且必须给出可见原因
    assert "err && err.status === 404" in funnel
    assert "_fallbackEnrollViaMove" in funnel
    # 操作按钮不得冒泡到卡片本体的预测 modal
    assert 'querySelectorAll(".sc-hit-card button[data-enroll-symbol]")'.replace("'", '"') in funnel.replace("'", '"')
    assert "e.stopPropagation()" in funnel
    # 候选计数只统计实际可操作卡片
    assert "_strategyHitsAreActionable" in funnel
    assert "poolEnrollUnsupported" in funnel


def test_concept_filter_applies_to_strategy_hits():
    """#53：概念筛选要同时作用于策略命中与候选计数，缺标签时显式声明。"""
    funnel = (MODULES / "funnel.js").read_text()
    assert "function _filterHitsByConcept" in funnel
    assert "hitResult.items" in funnel
    assert "_getCompositeCandidateParts" in funnel
    assert "策略命中未带概念标签，暂不受筛选影响" in funnel


def test_notice_pool_matches_funnel_promotion_rules():
    """#56：公告候选不再跨级直达买入池，长列表折叠，分数不再恒为上涨色。"""
    notice = (MODULES / "notice-tab.js").read_text()
    body = notice.split("function noticeCardActions", 1)[1]
    # 只取 candidate 分支那一行，避免把 focus 分支的合法操作误判为跨级
    candidate_branch = next(
        line for line in body.splitlines() if "'candidate'" in line.split("return")[0]
    )
    assert "'candidate'" in candidate_branch
    assert "buy" not in candidate_branch, "公告候选仍可直接跳买入"
    assert "_noticeScoreCls" in notice
    assert 'class="score up"' not in notice, "公告卡片分数仍写死为上涨色"
    assert "_applyLongListCollapse" in notice

    core = (MODULES / "core.js").read_text()
    assert "function _applyLongListCollapse" in core
    assert "POOL_LIST_COLLAPSE_THRESHOLD" in core

    funnel = (MODULES / "funnel.js").read_text()
    assert "_applyLongListCollapse" in funnel
