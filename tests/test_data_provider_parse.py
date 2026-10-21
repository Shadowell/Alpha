import asyncio
import time

import pandas as pd

from app.services import data_provider as provider_mod
from app.services.blocking_io import reset_external_io_state
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
    out = asyncio.run(provider.get_hist("000592", "20260301", "20260413"))

    assert not out.empty
    assert {"日期", "开盘", "收盘", "最高", "最低", "成交量", "成交额"}.issubset(set(out.columns))
    assert out.iloc[0]["日期"] == "2026-04-10"


class _FakeKlineStore:
    def get_latest_snapshot(self):
        return [
            {
                "symbol": "000001",
                "close": 10.2,
                "prev_close": 10.0,
                "open": 10.0,
                "high": 10.3,
                "low": 9.9,
                "volume": 1000,
                "amount": 10200,
            }
        ]


def test_hanging_akshare_source_falls_back_to_db_and_keeps_default_executor_free(monkeypatch):
    """永久挂起的 AkShare 调用必须在超时内降级，且不能占满默认线程池。"""
    monkeypatch.setenv("ALPHA_EXTERNAL_TIMEOUT_SECONDS", "0.15")
    reset_external_io_state()

    def _hang(*args, **kwargs):
        time.sleep(0.8)
        return pd.DataFrame()

    monkeypatch.setattr(provider_mod.ak, "stock_zh_a_spot_em", _hang)
    monkeypatch.setattr(provider_mod.ak, "stock_zh_a_spot", _hang)

    provider = AkshareDataProvider(kline_store=_FakeKlineStore())
    started = time.monotonic()
    snapshot = asyncio.run(provider.get_realtime_snapshot(prefer_live=True, retries=0))
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert not snapshot.empty
    assert provider.realtime_snapshot_source == "db_fallback"

    # 默认执行器仍然可用：并发 20 个 to_thread 任务应当很快完成。
    async def _default_executor_probe():
        return await asyncio.wait_for(
            asyncio.gather(*[asyncio.to_thread(lambda: 1) for _ in range(20)]),
            timeout=1.0,
        )

    started = time.monotonic()
    assert asyncio.run(_default_executor_probe()) == [1] * 20
    assert time.monotonic() - started < 1.0


def test_realtime_snapshot_sina_is_skipped_after_em_timeout(monkeypatch):
    """东财超时后不应继续等新浪超时，直接走 DB 降级。"""
    monkeypatch.setenv("ALPHA_EXTERNAL_TIMEOUT_SECONDS", "0.15")
    reset_external_io_state()
    calls = {"sina": 0}

    def _hang_em(*args, **kwargs):
        time.sleep(0.8)
        return pd.DataFrame()

    def _sina(*args, **kwargs):
        calls["sina"] += 1
        return pd.DataFrame()

    monkeypatch.setattr(provider_mod.ak, "stock_zh_a_spot_em", _hang_em)
    monkeypatch.setattr(provider_mod.ak, "stock_zh_a_spot", _sina)

    provider = AkshareDataProvider(kline_store=_FakeKlineStore())
    started = time.monotonic()
    snapshot = asyncio.run(provider.get_realtime_snapshot(prefer_live=True, retries=2))
    elapsed = time.monotonic() - started

    assert elapsed < 0.6
    assert calls["sina"] == 0
    assert provider.realtime_snapshot_source == "db_fallback"
    assert not snapshot.empty
