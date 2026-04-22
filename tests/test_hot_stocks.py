import asyncio

import pandas as pd

from app.services.data_provider import AkshareDataProvider, normalize_hot_stocks_df


def test_normalize_hot_stocks_filters_main_board_and_st():
    raw = pd.DataFrame(
        {
            "当前排名": [1, 2, 3, 4],
            "代码": ["SZ002342", "SH600111", "SZ300001", "SH600222"],
            "股票名称": ["巨力索具", "北方稀土", "创业测试", "*ST测试"],
            "最新价": [15.59, 24.1, 9.2, 6.5],
            "涨跌额": [1.56, 0.12, 0.3, -0.1],
            "涨跌幅": [10.02, 0.5, 3.2, -1.5],
        }
    )

    out = normalize_hot_stocks_df(raw)

    assert list(out["symbol"]) == ["002342", "600111"]
    assert list(out["rank"]) == [1, 2]
    assert out.iloc[0]["name"] == "巨力索具"


def test_fetch_hot_stocks_prefers_live_interface_without_db_fallback():
    provider = AkshareDataProvider()
    called = {"live": 0, "snapshot": 0}

    async def fake_live():
        called["live"] += 1
        return pd.DataFrame(
            {
                "代码": ["300540", "688811", "600111"],
                "名称": ["蜀道装备", "有研复材", "北方稀土"],
                "最新价": [26.64, 23.82, 24.1],
                "涨跌额": [4.44, 3.97, 0.12],
                "涨跌幅": [20.0, 20.0, 0.5],
            }
        )

    async def forbidden_snapshot(*args, **kwargs):
        called["snapshot"] += 1
        raise AssertionError("hot stocks should not use DB-backed realtime snapshot path")

    provider._fetch_spot_em = fake_live  # type: ignore[method-assign]
    provider.get_realtime_snapshot = forbidden_snapshot  # type: ignore[method-assign]

    out = asyncio.run(provider._fetch_hot_stocks_from_spot())

    assert called["live"] == 1
    assert called["snapshot"] == 0
    assert list(out["symbol"][:2]) == ["300540", "688811"]
