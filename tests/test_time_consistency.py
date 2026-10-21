"""#21：时间戳一致性与 hot_stocks_updated_at 持久化回归测试。"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import APP_TZ
from app.services.time_utils import (
    now_cn,
    now_cn_iso,
    now_cn_naive,
    parse_cn_datetime,
)

ROOT = Path(__file__).resolve().parents[1]


class FakeStateStore:
    def __init__(self, payload: dict | None = None):
        self.payload = payload
        self.saved: list[dict] = []

    def save_state(self, payload: dict) -> None:
        self.saved.append(payload)

    def load_state(self) -> dict | None:
        return self.payload


def _bare_service():
    from app.services.funnel_service import FunnelService

    service = FunnelService.__new__(FunnelService)
    service.entries = {}
    service.hot_concepts = []
    service.hot_stocks = []
    service.frozen = False
    service.trade_date = now_cn().date().isoformat()
    service.updated_at = now_cn().isoformat()
    service.hot_stocks_updated_at = service.updated_at
    service.legacy_json_file = Path("/nonexistent/funnel_state.json")
    service.state_store = FakeStateStore()
    return service


def test_now_cn_is_aware_asia_shanghai():
    current = now_cn()
    assert current.tzinfo is not None
    assert current.utcoffset() == timedelta(hours=8)
    assert current.tzinfo == APP_TZ


def test_now_cn_iso_is_parseable_and_aware():
    parsed = datetime.fromisoformat(now_cn_iso())
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(hours=8)
    assert parse_cn_datetime(now_cn_iso()).tzinfo == APP_TZ


def test_parse_cn_datetime_round_trip_and_naive_compat():
    aware = datetime(2026, 10, 22, 9, 30, tzinfo=APP_TZ)
    assert parse_cn_datetime(aware.isoformat()) == aware

    naive = parse_cn_datetime("2026-10-22T09:30:00")
    assert naive.tzinfo == APP_TZ
    assert naive == aware

    utc_value = datetime(2026, 10, 22, 1, 30, tzinfo=timezone.utc)
    assert parse_cn_datetime(utc_value.isoformat()) == aware


def test_now_cn_naive_is_cn_wall_time_without_tzinfo():
    naive = now_cn_naive()
    assert naive.tzinfo is None
    assert abs((naive - now_cn().replace(tzinfo=None)).total_seconds()) < 5


def test_hot_stocks_staleness_accepts_legacy_naive_timestamp():
    service = _bare_service()
    service.hot_stocks_updated_at = "2020-01-01T09:30:00"
    assert service._is_hot_stocks_stale(ttl_seconds=300) is True

    service.hot_stocks_updated_at = now_cn().isoformat()
    assert service._is_hot_stocks_stale(ttl_seconds=300) is False

    service.hot_stocks_updated_at = "not-a-timestamp"
    assert service._is_hot_stocks_stale(ttl_seconds=300) is True


def test_save_state_payload_includes_hot_stocks_updated_at():
    service = _bare_service()
    service.hot_stocks_updated_at = "2026-10-22T10:00:00+08:00"

    service._save_state()

    assert len(service.state_store.saved) == 1
    payload = service.state_store.saved[0]
    assert payload["hot_stocks_updated_at"] == "2026-10-22T10:00:00+08:00"
    assert payload["updated_at"] == service.updated_at
    assert payload["trade_date"] == service.trade_date


def test_load_state_restores_hot_stocks_updated_at():
    service = _bare_service()
    service.state_store = FakeStateStore(
        {
            "trade_date": service.trade_date,
            "entries": {"600000": {"symbol": "600000"}},
            "hot_concepts": [],
            "hot_stocks": [{"symbol": "600000"}],
            "updated_at": "2026-10-22T09:00:00+08:00",
            "hot_stocks_updated_at": "2026-10-22T09:30:00+08:00",
            "frozen": True,
        }
    )

    service._load_state()

    assert service.hot_stocks_updated_at == "2026-10-22T09:30:00+08:00"
    assert service.updated_at == "2026-10-22T09:00:00+08:00"
    assert service.frozen is True


def test_no_naive_datetime_now_in_funnel_and_kline_cache():
    """naive datetime.now() 不得在时间敏感服务中重新出现。"""
    pattern = re.compile(r"datetime\.now\(\s*\)")
    for rel in (
        "app/services/funnel_service.py",
        "app/services/kline_cache_service.py",
    ):
        source = (ROOT / rel).read_text(encoding="utf-8")
        assert pattern.search(source) is None, f"{rel} 仍存在 naive datetime.now()"
