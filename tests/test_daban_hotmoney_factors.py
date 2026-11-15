"""#34 个股游资聚合因子单测：缺失标注、买卖同席、集中度、题材/板位交互。

全部使用 ``tmp_path`` 合成库；不读仓库 ``data/*.db``、不访问网络。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.kline_store import KlineSQLiteStore
from strategy.daban_hotmoney.factors import (
    HotMoneyFactorConfig,
    build_hot_money_factors,
    factor_quality_report,
    hot_money_factor_specs,
    hot_money_output_columns,
)
from strategy.daban_hotmoney.profile import SeatProfileConfig
from strategy.daban_hotmoney.sample_data import make_hotmoney_fixture, write_fixture_db

SYNC_AT = "2026-01-01T17:00:00+08:00"
DATES = [
    "2026-01-05",
    "2026-01-06",
    "2026-01-07",
    "2026-01-08",
    "2026-01-09",
    "2026-01-12",
]
SEAT_A = "测试第一大证券股份有限公司上海张杨路证券营业部"
SEAT_B = "测试第二大证券股份有限公司深圳科技园证券营业部"
SEAT_C = "测试第三大证券股份有限公司杭州文二西路证券营业部"
STD_A = "测试第一大证券上海张杨路营业部"
STD_B = "测试第二大证券深圳科技园营业部"


def _bars(pct: float) -> list[dict]:
    bars = []
    close = 10.0
    for date in DATES:
        prev = close
        close = round(prev * (1 + pct), 4)
        bars.append(
            {
                "trade_date": date,
                "open": round(prev * (1 + pct / 2), 4),
                "high": max(prev, close) * 1.001,
                "low": min(prev, close) * 0.999,
                "close": close,
                "volume": 1e6,
                "amount": 1e6 * close,
            }
        )
    return bars


def _seat(symbol: str, seat: str, *, buy: float, sell: float = 0.0, date: str) -> dict:
    side = "buy" if buy >= sell else "sell"
    return {
        "trade_date": date,
        "symbol": symbol,
        "seat_name": seat,
        "side": side,
        "reason": "测试上榜",
        "buy_amount": buy,
        "sell_amount": sell,
        "net_amount": buy - sell,
        "seat_count": 1,
    }


def _board(symbol: str, date: str, **overrides: object) -> dict:
    row = {
        "symbol": symbol,
        "trade_date": date,
        "limit_flag": "U",
        "first_time": "09:35:00",
        "last_time": "14:00:00",
        "open_times": 0,
        "limit_times": 2,
        "up_stat": "2/2",
        "fd_amount": 1e8,
        "amount": 5e8,
        "limit_amount": 1e8,
        "turnover_ratio": 5.0,
        "float_mv": 3e9,
        "total_mv": 6e9,
        "close": 11.0,
        "pct_chg": 10.0,
    }
    row.update(overrides)
    return row


def build_factor_db(tmp_path: Path) -> str:
    """历史窗口（d0..d3）让 A/B 成为可靠席位，最后两天作为因子计算日。"""
    store = KlineSQLiteStore(str(tmp_path / "factors.db"))
    for symbol in ("600001", "600002", "600003"):
        store.upsert_symbol_klines(symbol, _bars(0.05), SYNC_AT)
    history = []
    for i in range(4):
        history.append(_seat("600001", SEAT_A, buy=1.0e8, date=DATES[i]))
        history.append(_seat("600002", SEAT_B, buy=1.2e8, date=DATES[i]))
        history.append(_seat("600001", SEAT_C, buy=0.5e8, sell=0.4e8, date=DATES[i]))
    latest = [
        _seat("600001", SEAT_A, buy=3.0e8, date=DATES[5]),
        _seat("600001", SEAT_B, buy=1.0e8, date=DATES[5]),
        _seat("600001", SEAT_C, buy=0.0, sell=2.0e8, date=DATES[5]),
        # 买卖同席：A 在 600002 同日既买又卖
        _seat("600002", SEAT_A, buy=2.0e8, sell=1.5e8, date=DATES[5]),
    ]
    store.upsert_dragon_tiger_seats(history + latest, SYNC_AT)
    boards = [
        _board("600001", DATES[5]),
        _board("600002", DATES[5], limit_times=1),
        # 有涨停板行但当日无龙虎榜席位：因子必须显式标缺失，而不是 0
        _board("600003", DATES[5], limit_times=3),
    ]
    boards += [_board("600001", DATES[i]) for i in range(4)]
    boards += [_board("600002", DATES[i]) for i in range(4)]
    store.upsert_limit_board_daily(boards, SYNC_AT)
    store.update_limit_board_theme(
        [{"symbol": "600001", "trade_date": d, "theme": "测试算力", "board_type": "换手板"} for d in DATES]
        + [
            {
                "symbol": "600002",
                "trade_date": d,
                "theme": "测试医药",
                "board_type": "一字板",
            }
            for d in DATES
        ]
        + [
            {
                "symbol": "600003",
                "trade_date": DATES[5],
                "theme": "测试无人",
                "board_type": "换手板",
            }
        ],
        SYNC_AT,
    )
    store.upsert_hot_money_seats(
        [
            {
                "seat_name": SEAT_A,
                "org_code": "",
                "orgs": [SEAT_A],
                "appear_count": 0,
                "last_trade_date": DATES[5],
                "source": "proxy.top_inst",
            }
        ],
        SYNC_AT,
    )
    return str(store.db_path)


@pytest.fixture()
def factors(tmp_path):
    db = build_factor_db(tmp_path)
    return build_hot_money_factors(
        db,
        as_of_date=DATES[5],
        profile_config=SeatProfileConfig(min_events=3, prior_strength=5.0, window_days=10),
    )


def test_specs_and_columns_are_complete(factors) -> None:
    specs = hot_money_factor_specs()
    assert {spec["name"] for spec in specs} >= {
        "hot_money_count",
        "hot_money_net_buy",
        "hot_money_alpha_score",
        "seat_concentration",
        "seat_conflict",
        "hot_money_x_theme",
        "hot_money_x_board_position",
    }
    for spec in specs:
        assert spec["formula"] and spec["unit"] and spec["missing_rule"] and spec["pit_note"]
    columns = hot_money_output_columns()
    for row in factors.rows:
        assert set(columns) <= set(row)
        for spec in specs:
            assert f"{spec['name']}_is_missing" in row


def test_aggregates_and_concentration(factors) -> None:
    row = factors.by_symbol()["600001"]
    assert row["hot_money_count"] == 2  # A + B 两个可靠买方席位（C 只做卖出）
    assert row["hot_money_count_all"] == 2
    assert row["hot_money_net_buy"] == pytest.approx(4.0e8 - 0.0, abs=1e5)
    assert row["hot_money_buy_amount"] == pytest.approx(4.0e8, abs=1e5)
    assert row["seat_concentration"] == pytest.approx(0.75, abs=1e-6)
    assert row["seat_conflict"] == 0
    assert row["hot_money_reliable_share"] == pytest.approx(1.0)
    assert 0.5 < row["hot_money_alpha_score"] < 1.0
    assert row["hot_money_alpha_score_is_missing"] == 0


def test_seat_conflict_detected(factors) -> None:
    row = factors.by_symbol()["600002"]
    assert row["hot_money_count"] == 1
    assert row["seat_conflict"] == 1
    assert row["seat_conflict_flag"] == 1
    detail = row["hot_money_seats_detail"]
    assert {entry["seat_name_std"] for entry in detail} == {STD_A}
    assert detail[0]["sides"] == ["buy", "sell"]


def test_theme_and_position_interactions(factors) -> None:
    a = factors.by_symbol()["600001"]
    # 题材热度：600001 是当日唯一"测试算力"涨停 → heat=1, cap=10 → 0.1
    assert a["hot_money_x_theme_is_proxy"] == 1
    assert a["hot_money_x_theme"] == pytest.approx(a["hot_money_alpha_score"] * 0.1, abs=1e-6)
    # 板位：limit_times=2 → (2-1)/5 = 0.2
    assert a["hot_money_x_board_position"] == pytest.approx(
        a["hot_money_alpha_score"] * 0.2, abs=1e-6
    )
    b = factors.by_symbol()["600002"]
    # 首板（limit_times=1）→ 板位归一 0，但仍应有值而不是缺失
    assert b["hot_money_x_board_position"] == pytest.approx(0.0)
    assert b["hot_money_x_board_position_is_missing"] == 0


def test_unlisted_symbol_marks_missing(factors) -> None:
    row = factors.by_symbol()["600003"]
    assert row["hot_money_count"] is None
    assert row["hot_money_count_is_missing"] == 1
    assert row["hot_money_alpha_score_is_missing"] == 1
    assert row["seat_conflict_flag_is_missing"] == 1
    assert row["hot_money_x_board_position_is_missing"] == 1
    assert row["hot_money_seats_detail"] == []


def test_unreliable_seats_are_excluded_by_default(tmp_path) -> None:
    db = build_factor_db(tmp_path)
    strict = build_hot_money_factors(
        db,
        as_of_date=DATES[5],
        profile_config=SeatProfileConfig(min_events=50, window_days=10),
    )
    row = strict.by_symbol()["600001"]
    assert row["hot_money_count"] == 0
    assert row["hot_money_count_all"] == 2
    assert row["hot_money_alpha_score"] is None
    assert row["hot_money_alpha_score_is_missing"] == 1
    loose = build_hot_money_factors(
        db,
        as_of_date=DATES[5],
        config=HotMoneyFactorConfig(include_unreliable=True),
        profile_config=SeatProfileConfig(min_events=50, window_days=10),
    )
    assert loose.by_symbol()["600001"]["hot_money_count"] == 2


def test_label_proxy_flag_and_quality_report(factors) -> None:
    assert all(row["hot_money_label_is_proxy"] == 1 for row in factors.rows)
    quality = factors.quality
    assert quality["rows_total"] == len(factors.rows)
    assert quality["proxy_rates"]["hot_money_x_theme"] == pytest.approx(1.0)
    assert "游资身份" in quality["label_proxy_note"]
    assert 0.0 <= quality["missing_rates"]["hot_money_alpha_score"] <= 1.0
    assert factors.label_meta["label_source"] == "seat_name_proxy"


def test_quality_report_on_empty_rows() -> None:
    report = factor_quality_report([])
    assert report["rows_total"] == 0
    assert report["listed_coverage"] is None
    assert all(value == 1.0 for value in report["missing_rates"].values())


def test_factors_on_generated_fixture(tmp_path) -> None:
    rows = make_hotmoney_fixture(days=45, symbols=8, seed=11)
    db = write_fixture_db(tmp_path / "gen.db", rows)
    result = build_hot_money_factors(db, as_of_date=rows.dates[-3])
    assert result.rows
    scored = [row for row in result.rows if row["hot_money_alpha_score"] is not None]
    assert scored
    assert all(0.0 <= row["hot_money_alpha_score"] <= 1.0 for row in scored)
    assert result.profile_summary["seats_reliable"] > 0
