import pandas as pd

from app.services.data_provider import normalize_hot_stocks_df


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
