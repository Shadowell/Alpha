import asyncio

import pandas as pd

from app.services.notice_service import NoticeService
from app.services.sqlite_store import SQLiteStateStore


class FakeKlineCache:
    def get_kline(self, symbol: str, days: int = 30):
        return [
            {"date": "2026-04-10", "open": 10, "high": 11, "low": 9.8, "close": 10.5, "volume": 1000000},
            {"date": "2026-04-11", "open": 10.5, "high": 11.1, "low": 10.2, "close": 10.9, "volume": 1200000},
        ]


def test_notice_screen_and_detail(monkeypatch, tmp_path):
    df = pd.DataFrame(
        {
            "代码": ["000001", "000001", "600111", "300001"],
            "名称": ["平安银行", "平安银行", "北方稀土", "创业测试"],
            "公告标题": ["业绩预增公告", "重大合同签订", "股份回购计划", "股东大会决议"],
            "公告类型": ["业绩预告", "重大合同", "回购", "其他"],
            "公告日期": ["2026-04-13"] * 4,
            "网址": ["http://x/1", "http://x/2", "http://x/3", "http://x/4"],
        }
    )

    monkeypatch.setattr("app.services.notice_service.ak.stock_notice_report", lambda symbol, date: df)
    monkeypatch.setattr(
        "app.services.notice_service.score_with_llm",
        lambda notices: ({"000001": {"score": 88, "reason": "业绩+合同共振", "risk": "兑现风险"}}, True),
    )

    store = SQLiteStateStore(str(tmp_path / "state.db"))
    service = NoticeService(state_store=store, kline_cache_service=FakeKlineCache())

    result = asyncio.run(service.run_notice_screen(notice_date="20260413", limit=10))
    assert result["success"] is True
    assert result["candidate_count"] >= 1

    funnel = asyncio.run(service.get_notice_funnel())
    assert funnel.stats["buy"] >= 1
    symbol = funnel.pools["buy"][0].symbol

    detail = asyncio.run(service.get_notice_detail(symbol))
    assert detail.symbol == symbol
    assert len(detail.kline) == 2
