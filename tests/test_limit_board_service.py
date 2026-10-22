"""打板数据底座同步行为测试（issue #22）。

覆盖：能力矩阵状态、数据集失败隔离、无权限降级、指数退避重试、
收盘前拒绝落库、重复同步幂等，以及与主 K 线同步的接线隔离。
"""

from __future__ import annotations

import asyncio
import datetime as dt

import pandas as pd
import pytest

from app.services.kline_cache_service import KlineCacheService
from app.services.kline_store import KlineSQLiteStore
from app.services.limit_board_service import (
    DATASET_DRAGON_TIGER,
    DATASET_DRAGON_TIGER_SEAT,
    DATASET_HOT_MONEY_SEAT,
    DATASET_HSGT_TOP10,
    DATASET_LIMIT_BOARD,
    DATASET_LIMIT_LADDER,
    DATASET_LIMIT_THEME,
    LimitBoardService,
    TERMINAL_STATUSES,
)
from app.services.tushare_market_data_client import (
    CAPABILITY_DENIED,
    CAPABILITY_ERROR,
    CAPABILITY_NOT_CONFIGURED,
    LIMIT_BOARD_APIS,
    LimitApiResult,
)

TRADE_DATE = "2026-10-21"
YESTERDAY = "2026-10-20"


def _run(coro):
    return asyncio.run(coro)


def _limit_frame(trade_date: str = TRADE_DATE) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "trade_date": trade_date.replace("-", ""),
                "ts_code": "000001.SZ",
                "industry": "银行",
                "name": "测试银",
                "close": 11.0,
                "pct_chg": 10.0,
                "amount": 9.0e8,
                "limit_amount": 8.0e7,
                "float_mv": 4.0e9,
                "total_mv": 5.0e9,
                "turnover_ratio": 9.5,
                "fd_amount": 5.0e7,
                "first_time": "92500",
                "last_time": "145433",
                "open_times": 1,
                "up_stat": "1/1",
                "limit_times": 1,
                "limit": "U",
            },
            {
                "trade_date": trade_date.replace("-", ""),
                "ts_code": "000002.SZ",
                "industry": "软件",
                "name": "测试软",
                "close": 22.0,
                "pct_chg": 20.0,
                "amount": 3.0e8,
                "limit_amount": 2.0e7,
                "float_mv": 2.0e9,
                "total_mv": 3.0e9,
                "turnover_ratio": 12.0,
                "fd_amount": 2.0e7,
                "first_time": "100912",
                "last_time": "140742",
                "open_times": 0,
                "up_stat": "2/2",
                "limit_times": 2,
                "limit": "U",
            },
        ]
    )


class FakeLimitClient:
    """可编程的假 Tushare 客户端：按接口名返回结果或抛异常。"""

    def __init__(self, **overrides: object) -> None:
        self.is_ready = True
        self.responses: dict[str, list[LimitApiResult]] = {}
        self.denied: set[str] = set()
        self.errors: set[str] = set()
        self.calls: list[tuple[str, str]] = []
        self.raise_on: set[str] = set()
        for key, value in overrides.items():
            setattr(self, key, value)

    def _result(self, api: str, trade_date: str = TRADE_DATE) -> LimitApiResult:
        compact = trade_date.replace("-", "")
        queue = self.responses.get(api)
        if queue:
            return queue.pop(0) if len(queue) > 1 else queue[0]
        if api in self.errors:
            return LimitApiResult(
                pd.DataFrame(), CAPABILITY_ERROR, "api_error:RuntimeError"
            )
        if api in self.denied:
            return LimitApiResult(
                pd.DataFrame(), CAPABILITY_DENIED, "permission_denied"
            )
        if api == "limit_list_d":
            return LimitApiResult(_limit_frame(trade_date), "ok", "")
        if api == "kpl_list":
            return LimitApiResult(
                pd.DataFrame(
                    [
                        {
                            "ts_code": "000001.SZ",
                            "trade_date": compact,
                            "status": "首板",
                            "theme": "AI应用",
                            "lu_desc": "数据要素",
                        }
                    ]
                ),
                "ok",
                "",
            )
        if api == "top_list":
            return LimitApiResult(
                pd.DataFrame(
                    [
                        {
                            "trade_date": compact,
                            "ts_code": "000001.SZ",
                            "name": "测试银",
                            "close": 11.0,
                            "pct_change": 10.0,
                            "turnover_rate": 9.5,
                            "amount": 9.0e8,
                            "l_buy": 1.0e8,
                            "l_sell": 9.0e7,
                            "l_amount": 1.9e8,
                            "net_amount": 1.0e7,
                            "net_rate": 1.1,
                            "amount_rate": 21.0,
                            "float_values": 4.0e9,
                            "reason": "日涨幅偏离值达到7%的前5只证券",
                        }
                    ]
                ),
                "ok",
                "",
            )
        if api == "top_inst":
            return LimitApiResult(
                pd.DataFrame(
                    [
                        {
                            "trade_date": compact,
                            "ts_code": "000001.SZ",
                            "exalter": "测试营业部",
                            "buy": 1.0e7,
                            "buy_rate": 1.0,
                            "sell": None,
                            "sell_rate": None,
                            "net_buy": 1.0e7,
                            "side": "0",
                            "reason": "日涨幅偏离值达到7%的前5只证券",
                        }
                    ]
                ),
                "ok",
                "",
            )
        if api == "hm_list":
            return LimitApiResult(
                pd.DataFrame(
                    [{"name": "测试游资", "desc": "测试简介", "orgs": '["测试营业部"]'}]
                ),
                "ok",
                "",
            )
        if api == "hsgt_top10":
            return LimitApiResult(
                pd.DataFrame(
                    [
                        {
                            "trade_date": compact,
                            "ts_code": "600519.SH",
                            "name": "测试酒",
                            "close": 1600.0,
                            "change": 3.0,
                            "rank": 1,
                            "market_type": "1",
                            "amount": 1.6e10,
                            "net_amount": None,
                            "buy": None,
                            "sell": None,
                        }
                    ]
                ),
                "ok",
                "",
            )
        # limit_step / hm_detail 默认无权限
        return LimitApiResult(pd.DataFrame(), CAPABILITY_DENIED, "permission_denied")

    async def call_limit_api(
        self, api: str, *, trade_date: str = "", ts_code: str = "", timeout=None
    ) -> LimitApiResult:
        self.calls.append((api, trade_date))
        if api in self.raise_on:
            raise RuntimeError("boom")
        return self._result(api, trade_date or TRADE_DATE)

    async def probe_limit_capabilities(self, trade_date: str, apis=None) -> list[dict]:
        out = []
        for api in apis or LIMIT_BOARD_APIS:
            result = await self.call_limit_api(
                api, trade_date=trade_date, ts_code="000001.SZ"
            )
            capability = (
                "available" if result.status in {"ok", "empty"} else result.status
            )
            out.append(
                {
                    "api": api,
                    "capability": capability,
                    "reason": "ok" if capability == "available" else result.reason,
                    "sample_rows": len(result.frame),
                }
            )
        return out


def _service(tmp_path, client) -> tuple[LimitBoardService, KlineSQLiteStore]:
    store = KlineSQLiteStore(str(tmp_path / "market_kline.db"))
    service = LimitBoardService(store=store, market_data_client=client)
    return service, store


def _by_dataset(result: dict) -> dict[str, dict]:
    return {item["dataset"]: item for item in result["datasets"]}


# ── 能力矩阵 ─────────────────────────────────────────────


def test_probe_capabilities_reports_matrix_without_secrets(tmp_path):
    client = FakeLimitClient(denied={"limit_step", "hm_detail"}, errors={"hsgt_top10"})
    service, _ = _service(tmp_path, client)

    matrix = _run(service.probe_capabilities(trade_date=TRADE_DATE, force=True))

    statuses = {item["api"]: item["capability"] for item in matrix["apis"]}
    assert statuses["limit_list_d"] == "available"
    assert statuses["limit_step"] == "denied"
    assert statuses["hm_detail"] == "denied"
    assert statuses["hsgt_top10"] == "error"
    assert set(statuses) == set(LIMIT_BOARD_APIS)
    reasons = {item["api"]: item["reason"] for item in matrix["apis"]}
    assert reasons["limit_step"] == "permission_denied"
    assert "token" not in str(matrix).lower()
    assert (
        matrix["degradations"][DATASET_LIMIT_BOARD] == "self_calc:kline_daily+stk_limit"
    )


def test_probe_capabilities_without_tushare_marks_not_configured(tmp_path):
    client = FakeLimitClient(is_ready=False)
    service, _ = _service(tmp_path, client)

    matrix = _run(service.probe_capabilities(trade_date=TRADE_DATE))

    assert {item["capability"] for item in matrix["apis"]} == {
        CAPABILITY_NOT_CONFIGURED
    }
    assert {item["reason"] for item in matrix["apis"]} == {"tushare_not_configured"}
    assert client.calls == []


def test_probe_capabilities_is_cached_until_force_refresh(tmp_path):
    client = FakeLimitClient()
    service, _ = _service(tmp_path, client)

    _run(service.probe_capabilities(trade_date=TRADE_DATE, force=True))
    first_calls = len(client.calls)
    _run(service.probe_capabilities(trade_date=TRADE_DATE))
    assert len(client.calls) == first_calls
    _run(service.probe_capabilities(trade_date=TRADE_DATE, force=True))
    assert len(client.calls) == first_calls * 2


# ── 同步主流程 ───────────────────────────────────────────


def test_sync_writes_all_datasets_and_terminal_states(tmp_path):
    service, store = _service(tmp_path, FakeLimitClient())

    result = _run(service.sync_trade_date(TRADE_DATE))
    states = _by_dataset(result)

    assert result["success"] is True
    assert states[DATASET_LIMIT_BOARD]["status"] == "success"
    assert states[DATASET_LIMIT_BOARD]["rows"] == 2
    assert states[DATASET_LIMIT_THEME]["status"] == "success"
    # limit_step 无权限 → 由涨停板聚合，标记 degraded 而不是失败
    assert states[DATASET_LIMIT_LADDER]["status"] == "degraded"
    assert states[DATASET_DRAGON_TIGER]["status"] == "success"
    assert states[DATASET_DRAGON_TIGER_SEAT]["status"] == "success"
    assert states[DATASET_HOT_MONEY_SEAT]["status"] == "success"
    assert states[DATASET_HSGT_TOP10]["status"] == "success"

    board = store.get_limit_board_daily(TRADE_DATE)
    assert [row["limit_flag"] for row in board] == ["U", "U"]
    assert board[0]["fd_amount"] == 5.0e7
    assert board[0]["theme"] == "AI应用"
    assert board[1]["theme"] is None
    ladder = store.get_limit_ladder(TRADE_DATE)
    assert [(level["ladder_level"], level["stock_count"]) for level in ladder] == [
        (2, 1),
        (1, 1),
    ]
    seats = store.get_dragon_tiger_seats(TRADE_DATE)
    assert seats[0]["seat_name"] == "测试营业部"
    assert seats[0]["side"] == "buy"
    assert store.get_hot_money_seats()[0]["seat_name"] == "测试游资"
    assert store.get_hsgt_top10(TRADE_DATE)[0]["amount"] == 1.6e10

    overview = {item["dataset"]: item for item in store.get_enrichment_overview()}
    assert overview[DATASET_LIMIT_BOARD]["status"] == "success"
    assert overview[DATASET_LIMIT_BOARD]["total_rows"] == 2
    assert all(
        overview[name]["status"] in TERMINAL_STATUSES
        for name in (
            DATASET_LIMIT_BOARD,
            DATASET_LIMIT_THEME,
            DATASET_LIMIT_LADDER,
            DATASET_DRAGON_TIGER,
            DATASET_DRAGON_TIGER_SEAT,
            DATASET_HOT_MONEY_SEAT,
            DATASET_HSGT_TOP10,
        )
    )


def test_sync_is_idempotent_on_repeat(tmp_path):
    service, store = _service(tmp_path, FakeLimitClient())

    _run(service.sync_trade_date(TRADE_DATE))
    again = _run(service.sync_trade_date(TRADE_DATE))

    assert again["success"] is True
    assert len(store.get_limit_board_daily(TRADE_DATE)) == 2
    assert len(store.get_dragon_tiger(TRADE_DATE)) == 1
    assert len(store.get_limit_ladder(TRADE_DATE)) == 2


def test_backfill_history_and_ladder_promotion(tmp_path):
    service, store = _service(tmp_path, FakeLimitClient())

    _run(service.sync_trade_date(YESTERDAY))
    _run(service.sync_trade_date(TRADE_DATE))

    ladder = {
        level["ladder_level"]: level for level in store.get_limit_ladder(TRADE_DATE)
    }
    prev_ladder = {
        level["ladder_level"]: level for level in store.get_limit_ladder(YESTERDAY)
    }
    assert prev_ladder[1]["stock_count"] == 1
    # 晋级率 = 今日 2 连板家数 / 昨日 1 连板家数
    assert ladder[2]["prev_level_count"] == 1
    assert ladder[2]["promotion_ratio"] == 1.0
    # 昨日没有 0 连板层级 → 首板晋级率保持缺失而不是 0
    assert ladder[1]["prev_level_count"] is None
    assert ladder[1]["promotion_ratio"] is None


def test_single_dataset_exception_is_isolated(tmp_path):
    client = FakeLimitClient(raise_on={"top_list"})
    service, store = _service(tmp_path, client)

    result = _run(service.sync_trade_date(TRADE_DATE))
    states = _by_dataset(result)

    assert result["success"] is False
    assert states[DATASET_DRAGON_TIGER]["status"] == "failed"
    assert states[DATASET_DRAGON_TIGER]["reason"].startswith("api_error:")
    # 其它数据集照常完成
    assert states[DATASET_LIMIT_BOARD]["status"] == "success"
    assert states[DATASET_DRAGON_TIGER_SEAT]["status"] == "success"
    assert len(store.get_limit_board_daily(TRADE_DATE)) == 2


def test_denied_limit_board_degrades_to_self_calc(tmp_path):
    client = FakeLimitClient(denied={"limit_list_d"})
    service, store = _service(tmp_path, client)
    store.upsert_symbol_klines(
        "000001",
        [
            {
                "trade_date": TRADE_DATE,
                "open": 10.4,
                "high": 11.0,
                "low": 10.4,
                "close": 11.0,
                "volume": 100.0,
                "amount": 1.0e8,
            }
        ],
        "2026-10-21T16:00:00",
    )
    store.upsert_daily_extras(
        [
            {
                "symbol": "000001",
                "trade_date": TRADE_DATE,
                "up_limit": 11.0,
                "down_limit": 9.0,
                "circ_mv": 4.0e9,
                "turnover_rate": 9.0,
            }
        ],
        "2026-10-21T16:00:00",
    )

    result = _run(service.sync_trade_date(TRADE_DATE))
    states = _by_dataset(result)

    assert states[DATASET_LIMIT_BOARD]["status"] == "degraded"
    assert "self_calc" in states[DATASET_LIMIT_BOARD]["reason"]
    assert "permission_denied" in states[DATASET_LIMIT_BOARD]["reason"]
    board = store.get_limit_board_daily(TRADE_DATE)
    assert board[0]["source"] == "self_calc.daily"
    assert board[0]["limit_flag"] == "U"
    assert board[0]["fd_amount"] is None  # 降级无法给出封单额，保持缺失
    # 天梯仍可由聚合得到，但来源标注为自算
    ladder = store.get_limit_ladder(TRADE_DATE)
    assert ladder[0]["source"] == "aggregate.self_calc"


def test_empty_response_is_success_without_rows(tmp_path):
    client = FakeLimitClient(
        responses={
            "limit_list_d": [LimitApiResult(pd.DataFrame(), "empty", "empty_response")]
        }
    )
    service, store = _service(tmp_path, client)

    result = _run(service.sync_trade_date(TRADE_DATE))

    assert _by_dataset(result)[DATASET_LIMIT_BOARD]["status"] == "success"
    assert store.get_limit_board_daily(TRADE_DATE) == []


def test_error_is_retried_with_backoff_then_degrades(tmp_path, monkeypatch):
    client = FakeLimitClient(
        responses={
            "limit_list_d": [
                LimitApiResult(pd.DataFrame(), CAPABILITY_ERROR, "timeout"),
                LimitApiResult(_limit_frame(), "ok", ""),
            ]
        }
    )
    service, store = _service(tmp_path, client)
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("asyncio.sleep", fake_sleep)

    result = _run(service.sync_trade_date(TRADE_DATE, datasets=[DATASET_LIMIT_BOARD]))

    assert [api for api, _ in client.calls].count("limit_list_d") == 2
    assert sleeps and sleeps[0] >= 0.05  # 指数退避，不是立刻重试
    assert _by_dataset(result)[DATASET_LIMIT_BOARD]["status"] == "success"
    assert len(store.get_limit_board_daily(TRADE_DATE)) == 2


def test_denied_api_is_not_recalled_when_matrix_is_fresh(tmp_path):
    client = FakeLimitClient(denied={"top_list"})
    service, _ = _service(tmp_path, client)
    _run(service.probe_capabilities(trade_date=TRADE_DATE, force=True))
    client.calls.clear()

    result = _run(service.sync_trade_date(TRADE_DATE, datasets=[DATASET_DRAGON_TIGER]))

    assert _by_dataset(result)[DATASET_DRAGON_TIGER]["status"] == "skipped"
    assert [api for api, _ in client.calls] == []  # 已知无权限：短路，不打必然失败请求


def test_intraday_sync_is_refused(tmp_path, monkeypatch):
    service, store = _service(tmp_path, FakeLimitClient())
    today = dt.date.today().isoformat()
    monkeypatch.setattr(
        LimitBoardService,
        "date_settled",
        staticmethod(lambda trade_date, now=None: trade_date < today),
    )

    result = _run(service.sync_trade_date(today))

    assert result["skipped_intraday"] is True
    assert result["datasets"] == []
    assert store.get_limit_board_daily(today) == []


def test_force_allows_explicit_backfill_of_unsettled_date(tmp_path, monkeypatch):
    service, store = _service(tmp_path, FakeLimitClient())
    today = dt.date.today().isoformat()
    monkeypatch.setattr(
        LimitBoardService,
        "date_settled",
        staticmethod(lambda trade_date, now=None: False),
    )

    result = _run(service.sync_trade_date(today, force=True))

    assert result["trade_date"] == today
    assert len(store.get_limit_board_daily(today)) == 2


def test_seat_dataset_falls_back_to_hm_detail(tmp_path):
    client = FakeLimitClient(
        denied={"top_inst"},
        responses={
            "hm_detail": [
                LimitApiResult(
                    pd.DataFrame(
                        [
                            {
                                "trade_date": TRADE_DATE.replace("-", ""),
                                "ts_code": "000001.SZ",
                                "exalter": "游资测试营业部",
                                "side": "1",
                                "buy": None,
                                "sell": 2.0e7,
                                "net_buy": -2.0e7,
                            }
                        ]
                    ),
                    "ok",
                    "",
                )
            ]
        },
    )
    service, store = _service(tmp_path, client)

    result = _run(
        service.sync_trade_date(TRADE_DATE, datasets=[DATASET_DRAGON_TIGER_SEAT])
    )
    states = _by_dataset(result)

    assert states[DATASET_DRAGON_TIGER_SEAT]["status"] == "success"
    assert states[DATASET_DRAGON_TIGER_SEAT]["degraded"] is True
    seats = store.get_dragon_tiger_seats(TRADE_DATE)
    assert seats[0]["seat_name"] == "游资测试营业部"
    assert seats[0]["side"] == "sell"
    assert seats[0]["source"] == "tushare.hm_detail"
    assert seats[0]["reason"] == ""  # 缺失上榜原因归一为空串，保证主键可写


def test_hot_money_proxy_from_seats_when_hm_list_denied(tmp_path):
    client = FakeLimitClient(denied={"hm_list"})
    service, store = _service(tmp_path, client)

    _run(
        service.sync_trade_date(
            TRADE_DATE, datasets=[DATASET_DRAGON_TIGER_SEAT, DATASET_HOT_MONEY_SEAT]
        )
    )

    seats = store.get_hot_money_seats()
    assert [seat["seat_name"] for seat in seats] == ["测试营业部"]
    assert seats[0]["source"] == "proxy.top_inst"
    assert seats[0]["appear_count"] >= 1


def test_ladder_uses_limit_step_when_permitted(tmp_path):
    client = FakeLimitClient(
        responses={
            "limit_step": [
                LimitApiResult(
                    pd.DataFrame(
                        [
                            {
                                "trade_date": TRADE_DATE.replace("-", ""),
                                "ts_code": "000001.SZ",
                                "name": "测试银",
                                "num_times": 1,
                            },
                            {
                                "trade_date": TRADE_DATE.replace("-", ""),
                                "ts_code": "000002.SZ",
                                "name": "测试软",
                                "num_times": 2,
                            },
                        ]
                    ),
                    "ok",
                    "",
                )
            ]
        }
    )
    client.denied.discard("limit_step")
    service, store = _service(tmp_path, client)

    result = _run(service.sync_trade_date(TRADE_DATE, datasets=[DATASET_LIMIT_LADDER]))

    assert _by_dataset(result)[DATASET_LIMIT_LADDER]["status"] == "success"
    ladder = store.get_limit_ladder(TRADE_DATE)
    assert [(level["ladder_level"], level["stock_count"]) for level in ladder] == [
        (2, 1),
        (1, 1),
    ]
    assert ladder[0]["source"] == "limit_step"


def test_sync_disabled_returns_explanation(tmp_path):
    service, _ = _service(tmp_path, FakeLimitClient())
    service._enabled = False

    result = _run(service.sync_trade_date(TRADE_DATE))

    assert result["success"] is False
    assert result["datasets"] == []


def test_status_payload_exposes_matrix_states_and_coverage(tmp_path):
    client = FakeLimitClient(denied={"limit_step", "hm_detail"})
    service, _ = _service(tmp_path, client)
    _run(service.probe_capabilities(trade_date=TRADE_DATE, force=True))
    _run(service.sync_trade_date(TRADE_DATE))

    payload = service.status_payload(service.capability_snapshot)

    assert payload["enabled"] is True
    assert payload["latest_trade_date"] == TRADE_DATE
    assert payload["coverage"]["total_rows"] == 2
    assert payload["coverage"]["up_non_null_ratio"]["fd_amount"] == 1.0
    assert {item["dataset"] for item in payload["datasets"]} == {
        DATASET_LIMIT_BOARD,
        DATASET_LIMIT_THEME,
        DATASET_LIMIT_LADDER,
        DATASET_DRAGON_TIGER,
        DATASET_DRAGON_TIGER_SEAT,
        DATASET_HOT_MONEY_SEAT,
        DATASET_HSGT_TOP10,
    }
    assert payload["capabilities"]["apis"]
    assert payload["primary_apis"][DATASET_LIMIT_BOARD] == "limit_list_d"


# ── 与主 K 线同步的接线 ──────────────────────────────────


class DummyProvider:
    pass


def _cache_service(tmp_path, client, limit_service):
    store = KlineSQLiteStore(str(tmp_path / "market_kline.db"))
    return KlineCacheService(
        provider=DummyProvider(),
        store=store,
        market_data_client=client,
        limit_board_service=limit_service,
    ), store


def test_kline_cache_runs_limit_board_after_enrichment(tmp_path):
    class EnrichClient(FakeLimitClient):
        async def fetch_daily_basic_by_trade_date(self, trade_date):
            return pd.DataFrame(
                [{"symbol": "000001", "trade_date": trade_date, "pe_ttm": 6.0}]
            )

        async def fetch_price_limits_by_trade_date(self, trade_date):
            return pd.DataFrame(
                [{"symbol": "000001", "trade_date": trade_date, "up_limit": 11.0}]
            )

        async def fetch_adj_factors_by_trade_date(self, trade_date):
            return pd.DataFrame(
                [{"symbol": "000001", "trade_date": trade_date, "adj_factor": 1.0}]
            )

        async def fetch_moneyflow_by_trade_date(self, trade_date):
            return pd.DataFrame(
                [{"symbol": "000001", "trade_date": trade_date, "net_mf_amount": 1.0}]
            )

        async def fetch_moneyflow_hsgt(self, start_date, end_date):
            return pd.DataFrame([{"trade_date": end_date, "north_money": 1.0}])

        async def fetch_index_daily(self, trade_date, ts_codes=None):
            return pd.DataFrame(
                [{"ts_code": "000001.SH", "trade_date": trade_date, "close": 3900.0}]
            )

    client = EnrichClient()
    limit_service, _store = _service(tmp_path, client)
    cache, store = _cache_service(tmp_path, client, limit_service)

    enrichment = _run(cache.sync_enrichment(TRADE_DATE))
    _run(cache._run_enrichment_safely(TRADE_DATE, "auto"))

    assert enrichment["success"] is True
    assert store.get_limit_board_daily(TRADE_DATE)
    assert store.get_enrichment_overview()


def test_limit_board_failure_does_not_break_kline_enrichment(tmp_path):
    class BoomService:
        async def sync_trade_date(self, trade_date, **kwargs):
            raise RuntimeError("should not break kline sync")

    class EnrichClient(FakeLimitClient):
        async def fetch_daily_basic_by_trade_date(self, trade_date):
            return pd.DataFrame(
                [{"symbol": "000001", "trade_date": trade_date, "pe_ttm": 6.0}]
            )

        async def fetch_price_limits_by_trade_date(self, trade_date):
            return pd.DataFrame(
                [{"symbol": "000001", "trade_date": trade_date, "up_limit": 11.0}]
            )

        async def fetch_adj_factors_by_trade_date(self, trade_date):
            return pd.DataFrame(
                [{"symbol": "000001", "trade_date": trade_date, "adj_factor": 1.0}]
            )

        async def fetch_moneyflow_by_trade_date(self, trade_date):
            return pd.DataFrame(
                [{"symbol": "000001", "trade_date": trade_date, "net_mf_amount": 1.0}]
            )

        async def fetch_moneyflow_hsgt(self, start_date, end_date):
            return pd.DataFrame([{"trade_date": end_date, "north_money": 1.0}])

        async def fetch_index_daily(self, trade_date, ts_codes=None):
            return pd.DataFrame(
                [{"ts_code": "000001.SH", "trade_date": trade_date, "close": 3900.0}]
            )

    cache, _store = _cache_service(tmp_path, EnrichClient(), BoomService())

    # 主 K 线增强同步必须照常完成，打板异常被吞掉
    result = _run(cache.sync_enrichment(TRADE_DATE))
    _run(cache._run_enrichment_safely(TRADE_DATE, "auto"))
    assert result["success"] is True


def test_pending_retry_skips_terminal_limit_board_states(tmp_path):
    service, store = _service(
        tmp_path, FakeLimitClient(denied={"limit_step", "hm_detail"})
    )
    _run(service.probe_capabilities(trade_date=TRADE_DATE, force=True))
    _run(service.sync_trade_date(TRADE_DATE))

    overview = {item["dataset"]: item for item in store.get_enrichment_overview()}
    assert (
        KlineCacheService._dataset_satisfied(overview[DATASET_LIMIT_LADDER], TRADE_DATE)
        is True
    )
    assert (
        KlineCacheService._dataset_satisfied(overview[DATASET_LIMIT_BOARD], TRADE_DATE)
        is True
    )
    stale = dict(overview[DATASET_LIMIT_BOARD])
    stale["trade_date"] = YESTERDAY
    assert KlineCacheService._dataset_satisfied(stale, TRADE_DATE) is False
    failed = dict(overview[DATASET_LIMIT_BOARD])
    failed["status"] = "failed"
    assert KlineCacheService._dataset_satisfied(failed, TRADE_DATE) is False


def test_service_can_be_disabled_by_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ENABLE_LIMIT_BOARD_DATA", "false")
    store = KlineSQLiteStore(str(tmp_path / "market_kline.db"))
    service = LimitBoardService(store=store, market_data_client=FakeLimitClient())

    assert service.is_enabled is False
    matrix = _run(service.probe_capabilities(trade_date=TRADE_DATE))
    assert matrix["enabled"] is False
    assert _run(service.sync_trade_date(TRADE_DATE))["datasets"] == []


@pytest.mark.parametrize(
    "dataset",
    [
        DATASET_LIMIT_BOARD,
        DATASET_LIMIT_THEME,
        DATASET_LIMIT_LADDER,
        DATASET_DRAGON_TIGER,
        DATASET_DRAGON_TIGER_SEAT,
        DATASET_HOT_MONEY_SEAT,
        DATASET_HSGT_TOP10,
    ],
)
def test_each_dataset_can_be_backfilled_alone(tmp_path, dataset):
    service, store = _service(tmp_path, FakeLimitClient())

    result = _run(service.sync_trade_date(TRADE_DATE, datasets=[dataset]))

    assert [item["dataset"] for item in result["datasets"]] == [dataset]
    assert result["trade_date"] == TRADE_DATE
