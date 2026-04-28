import asyncio

import httpx

from app.services.market_data_client import EastmoneyMarketDataClient


def _run(coro):
    return asyncio.run(coro)


def test_fetch_hist_parses_eastmoney_kline():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/qt/stock/kline/get"
        assert request.url.params["secid"] == "0.000001"
        return httpx.Response(
            200,
            json={
                "data": {
                    "klines": [
                        "2026-04-09,10.0,10.5,10.8,9.9,123456,4567890,0,0,0,0,0",
                        "2026-04-10,10.5,10.7,11.0,10.2,223456,5567890,0,0,0,0,0",
                    ]
                }
            },
        )

    client = EastmoneyMarketDataClient(transport=httpx.MockTransport(handler), retries=0)
    out = _run(client.fetch_hist("000001", "20260401", "20260410"))

    assert list(out.columns) == ["日期", "开盘", "收盘", "最高", "最低", "成交量", "成交额"]
    assert out.iloc[0]["日期"] == "2026-04-09"
    assert out.iloc[1]["收盘"] == 10.7


def test_fetch_spot_parses_eastmoney_snapshot():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/qt/clist/get"
        return httpx.Response(
            200,
            json={
                "data": {
                    "total": 2,
                    "diff": [
                        {
                            "f12": "000001",
                            "f14": "平安银行",
                            "f2": 10.5,
                            "f3": 1.2,
                            "f4": 0.12,
                            "f5": 1000,
                            "f6": 10000,
                            "f15": 10.8,
                            "f16": 10.1,
                            "f17": 10.2,
                            "f18": 10.38,
                            "f20": 100000000,
                        },
                        {
                            "f12": "600111",
                            "f14": "北方稀土",
                            "f2": 20.5,
                            "f3": -0.5,
                            "f4": -0.1,
                            "f5": 2000,
                            "f6": 40000,
                            "f15": 21.0,
                            "f16": 20.0,
                            "f17": 20.8,
                            "f18": 20.6,
                            "f20": 200000000,
                        },
                    ],
                }
            },
        )

    client = EastmoneyMarketDataClient(transport=httpx.MockTransport(handler), retries=0)
    out = _run(client.fetch_spot())

    assert list(out["代码"]) == ["000001", "600111"]
    assert out.iloc[0]["名称"] == "平安银行"
    assert out.iloc[1]["最新价"] == 20.5


def test_fetch_trade_days_parses_plain_text_fallback():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/klc_td_sh.txt")
        return httpx.Response(200, text='var dummy="2026-04-09,20260410";')

    client = EastmoneyMarketDataClient(transport=httpx.MockTransport(handler), retries=0)
    out = _run(client.fetch_trade_days(min_days=3))

    assert "2026-04-09" in set(out["trade_date"])
    assert "2026-04-10" in set(out["trade_date"])


def test_timeout_returns_empty_frame_without_blocking():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("boom", request=request)

    client = EastmoneyMarketDataClient(transport=httpx.MockTransport(handler), retries=0)

    assert _run(client.fetch_hist("000001", "20260401", "20260410")).empty
    assert _run(client.fetch_spot()).empty
