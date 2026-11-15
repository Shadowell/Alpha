"""#35 事件结构化抽取、防幻觉校验、去重聚类与安全护栏测试。"""

from __future__ import annotations

import time

from strategy.daban_events.dedupe import cluster_events, normalize_title
from strategy.daban_events.extraction import RawNotice, extract_event, rule_extract
from strategy.daban_events.guard import (
    STATUS_ERROR,
    STATUS_OK,
    STATUS_TIMEOUT,
    redact,
    run_with_timeout,
)


def _notice(
    title: str,
    body: str,
    announce_date: str = "2026-10-20",
    announce_time: str = "18:30",
    symbol: str = "000001",
) -> RawNotice:
    return RawNotice(
        symbol=symbol,
        title=title,
        body=body,
        announce_date=announce_date,
        announce_time=announce_time,
    )


def test_guard_redact_and_timeout():
    masked = redact("token=sk-1234567890abcdef secret=mysecret123")
    assert "sk-1234567890abcdef" not in masked

    ok = run_with_timeout(lambda: [{"a": 1}], source="test_ok", timeout_seconds=1.0)
    assert ok.status == STATUS_OK
    assert ok.rows == [{"a": 1}]

    def _boom():
        raise RuntimeError("boom")

    err = run_with_timeout(_boom, source="test_err", timeout_seconds=1.0)
    assert err.status == STATUS_ERROR

    def _slow():
        time.sleep(0.3)
        return 1

    to = run_with_timeout(_slow, source="test_timeout", timeout_seconds=0.05)
    assert to.status == STATUS_TIMEOUT


def test_rule_extract_and_llm_fallback():
    notice = _notice(
        "关于签订重大合同的公告",
        "公司与某国有企业签订日常经营重大合同，金额约12.5亿元，占营收比例35%。",
    )
    ext = rule_extract(notice)
    assert ext.symbol == "000001"
    assert ext.event_type != ""

    # LLM 返回无效 JSON 时应安全降级到规则抽取
    ext_fb = extract_event(notice, llm_client=lambda _msgs: "not json", timeout_seconds=1.0)
    assert ext_fb.symbol == "000001"
    assert ext_fb.event_type == ext.event_type


def test_dedupe_cluster_events():
    n1 = _notice("关于筹划重大资产重组的公告", "公司拟筹划重大资产重组事项。", "2026-10-10", "19:00")
    n2 = _notice("关于筹划重大资产重组的进展公告", "公司重大资产重组事项正常推进中。", "2026-10-15", "19:00")
    e1 = rule_extract(n1)
    e2 = rule_extract(n2)
    assert normalize_title(n1.title)
    clusters = cluster_events([e1, e2])
    assert len(clusters) >= 1
    assert sum(len(c.members) for c in clusters) == 2
