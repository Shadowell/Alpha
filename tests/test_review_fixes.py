"""本轮修复的针对性单测：

1. 写操作访问控制（access_guard）
2. notice_llm 失败语义：任何异常都必须返回 ( {}, False)，不得伪装成功
3. KronosPredictService.status() 状态面
4. 批量增量同步的节假日过滤（含日历降级路径）
"""
from __future__ import annotations

import asyncio

from app.services.access_guard import write_access_decision


# ── access_guard ──────────────────────────────────────────────

class TestWriteAccessGuard:
    def test_get_always_allowed(self):
        assert write_access_decision(method="GET", client_host="8.8.8.8", header_token=None, configured_token=None) is None

    def test_post_loopback_allowed_without_token(self):
        for host in ("127.0.0.1", "::1", "localhost", "testclient"):
            assert write_access_decision(method="POST", client_host=host, header_token=None, configured_token="") is None

    def test_post_lan_rejected_without_token(self):
        decision = write_access_decision(method="POST", client_host="192.168.1.50", header_token=None, configured_token="")
        assert decision == "non_loopback_without_token"

    def test_token_match_allows_any_host(self):
        assert write_access_decision(method="DELETE", client_host="10.0.0.9", header_token="secret", configured_token="secret") is None

    def test_token_mismatch_rejected(self):
        assert write_access_decision(method="POST", client_host="127.0.0.1", header_token="wrong", configured_token="secret") == "token_mismatch"
        assert write_access_decision(method="POST", client_host="127.0.0.1", header_token=None, configured_token="secret") == "token_mismatch"

    def test_reads_env_when_configured_token_not_passed(self, monkeypatch):
        monkeypatch.setenv("ALPHA_WRITE_TOKEN", "tok")
        try:
            assert write_access_decision(method="POST", client_host="1.2.3.4", header_token="tok") is None
            assert write_access_decision(method="PUT", client_host="1.2.3.4", header_token=None) == "token_mismatch"
        finally:
            monkeypatch.delenv("ALPHA_WRITE_TOKEN", raising=False)


# ── notice_llm ────────────────────────────────────────────────

class TestNoticeLlmFailureSemantics:
    def test_no_api_key_disabled(self, monkeypatch):
        import app.services.notice_llm as nl

        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        scores, enabled = asyncio.run(nl.score_with_llm([{"code": "600000", "title": "t"}]))
        assert scores == {} and enabled is False

    def test_network_failure_returns_disabled(self, monkeypatch):
        import app.services.notice_llm as nl

        monkeypatch.setenv("OPENAI_API_KEY", "test-key")

        class _FailingAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc_info):
                return False

            async def post(self, *args, **kwargs):
                raise ConnectionError("boom")

        monkeypatch.setattr(nl.httpx, "AsyncClient", _FailingAsyncClient)
        scores, enabled = asyncio.run(nl.score_with_llm([{"code": "600000", "title": "业绩预增"}]))
        assert scores == {} and enabled is False

    def test_empty_items_enabled(self, monkeypatch):
        import app.services.notice_llm as nl

        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        scores, enabled = asyncio.run(nl.score_with_llm([]))
        assert scores == {} and enabled is True


# ── Kronos status ─────────────────────────────────────────────

class TestKronosStatus:
    def test_status_reflects_state(self):
        from app.services.kronos_predict_service import KronosPredictService

        svc = KronosPredictService(kline_store=object(), provider=object())
        s = svc.status()
        assert s == {"loaded": False, "loading": False}


# ── enqueue_incremental_range 日历过滤 ────────────────────────

class _FakeSeries(list):
    def tolist(self):
        return list(self)


class _FakeDF(dict):
    """最小 DataFrame 替身：df.empty / df["trade_date"].tolist()。"""

    @property
    def empty(self):
        return not self.get("trade_date")

    def __getitem__(self, key):
        return _FakeSeries(dict.__getitem__(self, key))


class TestEnqueueRangeHolidayFilter:
    @staticmethod
    def _svc_with_calendar(days):
        from app.services.kline_cache_service import KlineCacheService

        async def get_trade_days(min_days: int = 0):
            if isinstance(days, Exception):
                raise days
            return _FakeDF(trade_date=list(days))

        provider = type("P", (), {})()
        provider.get_trade_days = get_trade_days
        return KlineCacheService(provider=provider, store=type("S", (), {})())

    def test_all_holiday_range_rejected(self):
        svc = self._svc_with_calendar(["2026-09-30", "2026-10-09", "2026-10-12"])
        payload = asyncio.run(svc.enqueue_incremental_range("2026-10-01", "2026-10-08"))
        # 国庆假期（10-01~10-08）全部被日历过滤，不应入队
        assert payload["success"] is False
        assert "节假日" in payload["message"]

    def test_mixed_range_filters_holidays(self):
        svc = self._svc_with_calendar(["2026-09-30", "2026-10-09", "2026-10-12"])
        payload = asyncio.run(svc.enqueue_incremental_range("2026-09-29", "2026-10-13"))
        assert payload["success"] is True
        assert payload["submitted"] >= 1
        assert payload["skipped_holidays"] >= 5
        queued_dates = {kwargs["trade_date"] for _, kwargs in svc._queue}
        assert queued_dates <= {"2026-09-30", "2026-10-09", "2026-10-12"}

    def test_calendar_failure_falls_back_to_weekdays(self):
        svc = self._svc_with_calendar(RuntimeError("calendar down"))
        payload = asyncio.run(svc.enqueue_incremental_range("2026-07-06", "2026-07-10"))
        # 日历不可用 → 退化为仅排周末，工作日全部入队
        assert payload["success"] is True
        assert payload["submitted"] == 5
