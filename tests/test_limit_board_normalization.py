"""打板数据底座（issue #22）字段归一化与降级自算单元测试。

覆盖：单位统一为元、缺失保持 None、时间格式、买卖方向、唯一键去重聚合、
重复同步幂等、以及 ``limit_list_d`` 无权限时的自算降级。
"""

from __future__ import annotations

import pandas as pd

from app.services.kline_store import KlineSQLiteStore
from app.services.limit_board_service import (
    build_ladder,
    clean_clock,
    clean_date,
    clean_number,
    clean_side,
    dedupe_rows,
    derive_board_from_daily,
    normalize_rows,
)

TRADE_DATE = "2026-10-21"


def _limit_list_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "trade_date": "20261021",
                "ts_code": "000001.SZ",
                "industry": "银行",
                "name": "测试银",
                "close": 11.0,
                "pct_chg": 10.0,
                "amount": 900000000.0,
                "limit_amount": 80000000.0,
                "float_mv": 4000000000.0,
                "total_mv": 5000000000.0,
                "turnover_ratio": 9.5,
                "fd_amount": 50000000.0,
                "first_time": "92500",
                "last_time": "145433",
                "open_times": 2,
                "up_stat": "1/1",
                "limit_times": 1,
                "limit": "U",
            },
            {
                "trade_date": "20261021",
                "ts_code": "300002.SZ",
                "industry": "软件",
                "name": "测试软",
                "close": 20.0,
                "pct_chg": 20.0,
                "amount": 1.0e8,
                "limit_amount": None,
                "float_mv": 2.0e9,
                "total_mv": 3.0e9,
                "turnover_ratio": 30.0,
                "fd_amount": None,
                "first_time": "100912",
                "last_time": None,
                "open_times": 8,
                "up_stat": "0/0",
                "limit_times": None,
                "limit": "Z",
            },
        ]
    )


# ── 归一化基础 ────────────────────────────────────────────


def test_clean_helpers_keep_missing_as_none_and_never_zero():
    assert clean_number(None) is None
    assert clean_number(float("nan")) is None
    assert clean_number("") is None
    assert clean_number("1,234.5") == 1234.5
    assert clean_number("inf") is None
    assert clean_number(2.0, 10000.0) == 20000.0
    assert clean_date("20261021") == TRADE_DATE
    assert clean_date(TRADE_DATE) == TRADE_DATE
    assert clean_date("bad") is None


def test_clean_clock_normalizes_tushare_hhmmss_variants():
    assert clean_clock("92500") == "09:25:00"
    assert clean_clock("145433") == "14:54:33"
    assert clean_clock(92500) == "09:25:00"
    assert clean_clock("09:25:00") == "09:25:00"
    assert clean_clock("930") == "09:30:00"
    assert clean_clock("256100") is None  # 非法小时
    assert clean_clock(None) is None
    assert clean_clock("09:25") is None


def test_clean_side_maps_tushare_direction_codes():
    assert clean_side("0") == "buy"
    assert clean_side(1) == "sell"
    assert clean_side("买入") == "buy"
    assert clean_side("SELL") == "sell"
    assert clean_side(None) is None
    assert clean_side("weird") is None


# ── 字段映射与单位 ────────────────────────────────────────


def test_normalize_limit_list_d_converts_keys_and_keeps_yuan():
    rows = normalize_rows("limit_list_d", _limit_list_frame())

    assert len(rows) == 2
    first = rows[0]
    assert first["symbol"] == "000001"
    assert first["ts_code"] == "000001.SZ"
    assert first["trade_date"] == TRADE_DATE
    assert first["limit"] == "U"
    assert first["first_time"] == "09:25:00"
    assert first["last_time"] == "14:54:33"
    assert first["open_times"] == 2
    assert first["limit_times"] == 1
    # 单位口径：limit_list_d 的金额已是元，映射层不再缩放。
    assert first["fd_amount"] == 50000000.0
    assert first["amount"] == 900000000.0
    assert first["float_mv"] == 4000000000.0

    broken = rows[1]
    assert broken["limit"] == "Z"
    assert broken["fd_amount"] is None
    assert broken["limit_times"] is None
    assert broken["last_time"] is None
    assert broken["limit_amount"] is None


def test_normalize_rows_drops_rows_without_symbol_or_date():
    frame = pd.DataFrame(
        [
            {"trade_date": "20261021", "ts_code": None, "limit": "U"},
            {"trade_date": None, "ts_code": "000001.SZ", "limit": "U"},
        ]
    )

    assert normalize_rows("limit_list_d", frame) == []
    assert normalize_rows("unknown_api", _limit_list_frame()) == []
    assert normalize_rows("limit_list_d", None) == []


def test_normalize_kpl_and_top_list_fields():
    kpl = normalize_rows(
        "kpl_list",
        pd.DataFrame(
            [
                {
                    "ts_code": "000001.SZ",
                    "trade_date": "20261021",
                    "status": "首板",
                    "theme": "AI",
                    "lu_desc": "AI应用",
                }
            ]
        ),
    )
    assert kpl[0] == {
        "symbol": "000001",
        "trade_date": TRADE_DATE,
        "board_type": "首板",
        "theme": "AI",
        "limit_reason": "AI应用",
    }

    tiger = normalize_rows(
        "top_list",
        pd.DataFrame(
            [
                {
                    "trade_date": "20261021",
                    "ts_code": "000530.SZ",
                    "name": "测试股",
                    "close": 5.0,
                    "pct_change": 10.0,
                    "turnover_rate": 20.0,
                    "amount": 2141168991.0,
                    "l_buy": 1.0e8,
                    "l_sell": 9.0e7,
                    "l_amount": 1.9e8,
                    "net_amount": 7.1e7,
                    "net_rate": 5.7,
                    "amount_rate": 27.0,
                    "float_values": 5.2e9,
                    "reason": "日振幅值达到15%的前5只证券",
                }
            ]
        ),
    )
    assert tiger[0]["symbol"] == "000530"
    assert tiger[0]["amount"] == 2141168991.0
    assert tiger[0]["reason"] == "日振幅值达到15%的前5只证券"


def test_normalize_hm_list_parses_orgs_json():
    rows = normalize_rows(
        "hm_list",
        pd.DataFrame(
            [
                {
                    "name": "测试游资",
                    "desc": "简介",
                    "orgs": '["华泰证券测试路"]',
                    "org_code": None,
                }
            ]
        ),
    )
    assert rows[0]["seat_name"] == "测试游资"
    assert rows[0]["org_code"] is None
    assert rows[0]["orgs"] == ["华泰证券测试路"]
    assert rows[0]["description"] == "简介"


def test_normalize_hsgt_top10_keeps_null_amounts():
    rows = normalize_rows(
        "hsgt_top10",
        pd.DataFrame(
            [
                {
                    "trade_date": "20261021",
                    "ts_code": "600519.SH",
                    "name": "测试酒",
                    "close": 1600.0,
                    "change": 3.1,
                    "rank": 2,
                    "market_type": "1",
                    "amount": 1.6e10,
                    "net_amount": None,
                    "buy": None,
                    "sell": None,
                }
            ]
        ),
    )
    assert rows[0]["symbol"] == "600519"
    assert rows[0]["market_type"] == "1"
    assert rows[0]["rank_no"] == 2
    assert rows[0]["net_amount"] is None
    assert rows[0]["buy_amount"] is None


# ── 唯一键聚合 ────────────────────────────────────────────


def test_dedupe_rows_merges_same_seat_and_sums_amounts():
    raw = normalize_rows(
        "top_inst",
        pd.DataFrame(
            [
                {
                    "trade_date": "20261021",
                    "ts_code": "000530.SZ",
                    "exalter": "机构专用",
                    "side": "0",
                    "reason": "r1",
                    "buy": 1.0e7,
                    "buy_rate": 1.0,
                    "sell": 2.0e7,
                    "sell_rate": 2.0,
                    "net_buy": -1.0e7,
                },
                {
                    "trade_date": "20261021",
                    "ts_code": "000530.SZ",
                    "exalter": "机构专用",
                    "side": "0",
                    "reason": "r1",
                    "buy": 3.0e7,
                    "buy_rate": 3.0,
                    "sell": 4.0e7,
                    "sell_rate": 4.0,
                    "net_buy": -1.0e7,
                },
                {
                    "trade_date": "20261021",
                    "ts_code": "000530.SZ",
                    "exalter": "机构专用",
                    "side": "1",
                    "reason": "r1",
                    "buy": None,
                    "buy_rate": None,
                    "sell": 5.0e6,
                    "sell_rate": None,
                    "net_buy": None,
                },
            ]
        ),
    )

    merged = dedupe_rows(raw, ("trade_date", "symbol", "seat_name", "side", "reason"))

    assert len(merged) == 2
    buy = next(m for m in merged if m["side"] == "buy")
    assert buy["seat_count"] == 2
    assert buy["buy_amount"] == 4.0e7
    assert buy["sell_amount"] == 6.0e7
    sell = next(m for m in merged if m["side"] == "sell")
    assert sell["seat_count"] == 1
    assert sell["buy_amount"] is None
    assert sell["sell_amount"] == 5.0e6


# ── 自算降级与天梯 ────────────────────────────────────────


def test_derive_board_from_daily_detects_up_down_and_broken():
    rows = derive_board_from_daily(
        [
            # 涨停封住
            {
                "symbol": "000001",
                "trade_date": TRADE_DATE,
                "open": 10.4,
                "high": 11.0,
                "low": 10.4,
                "close": 11.0,
                "amount": 1.0e8,
                "up_limit": 11.0,
                "down_limit": 9.0,
                "circ_mv": 4.0e9,
                "turnover_rate": 9.0,
            },
            # 摸到涨停但没封住 → 炸板
            {
                "symbol": "000002",
                "trade_date": TRADE_DATE,
                "open": 10.0,
                "high": 11.0,
                "low": 9.9,
                "close": 10.5,
                "amount": 2.0e8,
                "up_limit": 11.0,
                "down_limit": 9.0,
            },
            # 跌停
            {
                "symbol": "000003",
                "trade_date": TRADE_DATE,
                "open": 9.0,
                "high": 9.6,
                "low": 9.0,
                "close": 9.0,
                "amount": 3.0e8,
                "up_limit": 11.0,
                "down_limit": 9.0,
            },
            # 缺涨跌停价 → 无法判定，必须跳过而不是猜
            {
                "symbol": "000004",
                "trade_date": TRADE_DATE,
                "open": 10.0,
                "high": 11.0,
                "low": 9.0,
                "close": 11.0,
                "amount": 4.0e8,
                "up_limit": None,
                "down_limit": None,
            },
        ]
    )

    flags = {row["symbol"]: row["limit"] for row in rows}
    assert flags == {"000001": "U", "000002": "Z", "000003": "D"}
    first = rows[0]
    assert first["source"] == "self_calc.daily"
    # 降级只能给出日线可得字段，封板质量字段必须保持缺失
    assert first["fd_amount"] is None
    assert first["first_time"] is None
    assert first["open_times"] is None
    assert first["limit_times"] is None
    assert first["float_mv"] == 4.0e9


def test_derive_board_from_daily_marks_one_word_board():
    rows = derive_board_from_daily(
        [
            {
                "symbol": "000001",
                "trade_date": TRADE_DATE,
                "open": 11.0,
                "high": 11.0,
                "low": 11.0,
                "close": 11.0,
                "amount": 1.0e7,
                "up_limit": 11.0,
                "down_limit": 9.0,
            }
        ]
    )
    assert rows[0]["board_type"] == "一字板"


def test_build_ladder_aggregates_levels_and_promotion():
    board = [
        {
            "symbol": "000001",
            "name": "A",
            "trade_date": TRADE_DATE,
            "limit": "U",
            "limit_times": 1,
            "fd_amount": 1.0e7,
            "amount": 2.0e7,
            "open_times": 1,
        },
        {
            "symbol": "000002",
            "name": "B",
            "trade_date": TRADE_DATE,
            "limit": "U",
            "limit_times": 2,
            "fd_amount": None,
            "amount": 3.0e7,
            "open_times": 3,
        },
        {
            "symbol": "000003",
            "name": "C",
            "trade_date": TRADE_DATE,
            "limit": "Z",
            "limit_times": None,
            "fd_amount": 5.0e6,
            "amount": 4.0e7,
            "open_times": 5,
        },
    ]

    ladder = build_ladder(board, prev_counts={1: 2}, source="aggregate.limit_list_d")

    assert [level["ladder_level"] for level in ladder] == [2, 1]
    # 晋级率 = 今日 n 连板数 / 昨日 n-1 连板数
    level2 = ladder[0]
    assert level2["stock_count"] == 1
    assert level2["prev_level_count"] == 2
    assert level2["promotion_ratio"] == 0.5
    assert level2["fd_amount_sum"] is None  # 组内 fd_amount 全缺失 → 保持 None
    assert level2["amount_sum"] == 3.0e7
    level1 = ladder[1]
    assert level1["stock_count"] == 1
    assert level1["prev_level_count"] is None
    assert level1["promotion_ratio"] is None
    assert level1["fd_amount_sum"] == 1.0e7
    assert level1["avg_open_times"] == 1.0
    # 炸板（Z）不进入天梯
    assert all(level["ladder_level"] != 0 for level in ladder)


# ── 落库唯一键与重复同步幂等 ──────────────────────────────


def test_store_upsert_is_idempotent_and_preserves_existing_values(tmp_path):
    store = KlineSQLiteStore(str(tmp_path / "market_kline.db"))
    rows = normalize_rows("limit_list_d", _limit_list_frame())

    assert store.upsert_limit_board_daily(rows, "2026-10-21T16:00:00") == 2
    # 第二次同数据同步：行数不变，不产生重复主键
    assert store.upsert_limit_board_daily(rows, "2026-10-21T17:00:00") == 2
    loaded = store.get_limit_board_daily(TRADE_DATE)
    assert len(loaded) == 2
    assert loaded[0]["limit_flag"] in {"U", "Z"}

    # 后续同步缺 fd_amount 时，COALESCE 保留已有值而不是擦成 NULL
    partial = [
        {
            "symbol": "000001",
            "trade_date": TRADE_DATE,
            "limit": "U",
            "fd_amount": None,
            "first_time": "09:25:00",
        }
    ]
    store.upsert_limit_board_daily(partial, "2026-10-21T18:00:00")
    kept = next(
        r for r in store.get_limit_board_daily(TRADE_DATE) if r["symbol"] == "000001"
    )
    assert kept["fd_amount"] == 50000000.0
    assert kept["synced_at"] == "2026-10-21T18:00:00"

    # 题材只回填已存在的行
    assert (
        store.update_limit_board_theme(
            [
                {
                    "symbol": "000001",
                    "trade_date": TRADE_DATE,
                    "board_type": "首板",
                    "theme": "AI",
                },
                {"symbol": "999999", "trade_date": TRADE_DATE, "theme": "幽灵"},
            ],
            "2026-10-21T19:00:00",
        )
        == 1
    )


def test_store_latest_and_previous_board_dates(tmp_path):
    store = KlineSQLiteStore(str(tmp_path / "market_kline.db"))
    assert store.latest_board_trade_date() == ""
    assert store.previous_board_trade_date(TRADE_DATE) == ""

    store.upsert_limit_board_daily(
        [
            {
                "symbol": "000001",
                "trade_date": "2026-10-20",
                "limit": "U",
                "limit_times": 1,
            },
            {
                "symbol": "000001",
                "trade_date": TRADE_DATE,
                "limit": "U",
                "limit_times": 2,
            },
        ],
        "sync",
    )
    assert store.latest_board_trade_date() == TRADE_DATE
    assert store.previous_board_trade_date(TRADE_DATE) == "2026-10-20"
    assert store.previous_board_trade_date("2026-10-20") == ""


def test_store_coverage_reports_up_row_ratios(tmp_path):
    store = KlineSQLiteStore(str(tmp_path / "market_kline.db"))
    store.upsert_limit_board_daily(
        normalize_rows("limit_list_d", _limit_list_frame()), "2026-10-21T16:00:00"
    )

    coverage = store.get_limit_board_coverage(TRADE_DATE)

    assert coverage["total_rows"] == 2
    assert coverage["up_count"] == 1
    assert coverage["broken_count"] == 1
    # 分母只算涨停行：炸板缺 fd_amount 不应拉低验收口径的覆盖率
    assert coverage["up_non_null_ratio"]["fd_amount"] == 1.0
    assert coverage["up_non_null_ratio"]["limit_times"] == 1.0
    assert coverage["non_null_ratio"]["fd_amount"] == 0.5
