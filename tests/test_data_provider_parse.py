import pandas as pd

from app.services import data_provider as provider_mod
from app.services.data_provider import AkshareDataProvider, _parse_percent, _parse_up_down


def test_parse_percent_handles_sign_and_suffix():
    assert _parse_percent("+1.23%") == 1.23
    assert _parse_percent("-0.56%") == -0.56
    assert _parse_percent("--") == 0.0


def test_parse_up_down_handles_valid_and_invalid_values():
    assert _parse_up_down("21/28") == (21, 28)
    assert _parse_up_down("0/0") == (0, 0)
    assert _parse_up_down("N/A") == (0, 0)


def test_get_hist_fallback_to_tx_when_main_source_fails(monkeypatch):
    def _raise_main(**kwargs):
        raise RuntimeError("main source down")

    tx_df = pd.DataFrame(
        {
            "date": ["2026-04-10", "2026-04-11"],
            "open": [10.1, 10.2],
            "close": [10.3, 10.4],
            "high": [10.5, 10.6],
            "low": [10.0, 10.1],
            "amount": [1234567, 2345678],
        }
    )

    def _ok_tx(**kwargs):
        assert kwargs["symbol"] == "sz000592"
        return tx_df

    monkeypatch.setattr(provider_mod.ak, "stock_zh_a_hist", _raise_main)
    monkeypatch.setattr(provider_mod.ak, "stock_zh_a_hist_tx", _ok_tx)

    provider = AkshareDataProvider()
    out = provider.get_hist("000592", "20260301", "20260413")

    assert not out.empty
    assert {"日期", "开盘", "收盘", "最高", "最低", "成交量", "成交额"}.issubset(set(out.columns))
    assert out.iloc[0]["日期"] == "2026-04-10"
