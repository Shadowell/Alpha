"""#34 席位画像单测：PIT、贝叶斯收缩、样本门槛、买卖同席、滚动窗口。

覆盖 issue 验收项：
* 每个席位有样本量、次日溢价、胜率、砸盘率、风格标签；
* 小样本席位使用收缩，不出现"1 次上榜 = 100% 胜率"的误导；
* 披露延迟（T 日榜单的结果字段要到 T+1 收盘才可用，禁止前视）；
* 买卖同席（``conflict_events``）与协同网络。

数据库全部建在 ``tmp_path``，只读访问，绝不碰仓库 ``data/*.db``。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.services.kline_store import KlineSQLiteStore
from strategy.daban_hotmoney.profile import (
    REASON_INSUFFICIENT_SAMPLE,
    STYLE_FIRST_BOARD,
    STYLE_INSTITUTION,
    STYLE_NORTHBOUND,
    STYLE_RELAY,
    STYLE_UNCLEAR,
    SeatProfileConfig,
    aggregate_stats,
    beta_shrunk_rate,
    board_position_label,
    bucket_edges,
    build_seat_events,
    build_seat_profiles,
    enrich_events,
    mean_shrunk,
    normal_mean_interval,
    premium_component,
    profiles_frame,
    wilson_interval,
)
from strategy.daban_hotmoney.source import (
    PitPriceBook,
    compute_event_outcome,
    data_availability,
    label_source_flags,
    open_readonly,
)
from strategy.daban_hotmoney.seat_names import HotMoneyLabel

SYNC_AT = "2026-01-01T17:00:00+08:00"
DATES = [
    "2026-01-05",
    "2026-01-06",
    "2026-01-07",
    "2026-01-08",
    "2026-01-09",
    "2026-01-12",
    "2026-01-13",
    "2026-01-14",
    "2026-01-15",
    "2026-01-16",
]

SEAT_A = "测试第一大证券股份有限公司上海张杨路证券营业部"
SEAT_B = "测试第二大证券股份有限公司深圳科技园证券营业部"
SEAT_C = "测试第三大证券股份有限公司杭州文二西路证券营业部"
SEAT_INST = "机构专用"
SEAT_NORTH = "沪股通专用"
SEAT_JUNK = "测试未知文本咨询公司"
STD_A = "测试第一大证券上海张杨路营业部"
STD_B = "测试第二大证券深圳科技园营业部"
STD_C = "测试第三大证券杭州文二西路营业部"


def _bars(daily_pct: list[float]) -> list[dict]:
    """由每日涨跌幅序列生成 K 线；``open`` 取半程涨幅，便于校验开盘溢价。"""
    bars: list[dict] = []
    close = 10.0
    for date, pct in zip(DATES, daily_pct):
        prev = close
        close = round(prev * (1 + pct), 4)
        open_ = round(prev * (1 + pct / 2), 4)
        bars.append(
            {
                "trade_date": date,
                "open": open_,
                "high": max(open_, close) * 1.001,
                "low": min(open_, close) * 0.999,
                "close": close,
                "volume": 1_000_000.0,
                "amount": 1_000_000.0 * close,
            }
        )
    return bars


def _seat_row(
    seat: str, symbol: str, date: str, side: str, *, net: float | None = None
) -> dict:
    buy = 1.0e8 if side == "buy" else 0.0
    sell = 0.0 if side == "buy" else 5.0e7
    return {
        "trade_date": date,
        "symbol": symbol,
        "seat_name": seat,
        "side": side,
        "reason": "测试上榜",
        "buy_amount": buy,
        "sell_amount": sell,
        "net_amount": net if net is not None else buy - sell,
        "seat_count": 1,
    }


def _board_row(symbol: str, date: str, **overrides: object) -> dict:
    row = {
        "symbol": symbol,
        "trade_date": date,
        "limit_flag": "U",
        "first_time": "09:35:00",
        "last_time": "14:00:00",
        "open_times": 0,
        "limit_times": 1,
        "up_stat": "1/1",
        "fd_amount": 1.0e8,
        "amount": 5.0e8,
        "limit_amount": 1.0e8,
        "turnover_ratio": 5.0,
        "float_mv": 3.0e9,
        "total_mv": 6.0e9,
        "close": 11.0,
        "pct_chg": 10.0,
    }
    row.update(overrides)
    return row


def build_db(tmp_path: Path, name: str = "market_kline.db") -> KlineSQLiteStore:
    return KlineSQLiteStore(str(tmp_path / name))


def write_small_db(store: KlineSQLiteStore) -> None:
    """固定小样本：A 高胜率、B 单次 100% 胜率、C 两次亏损、机构 / 北向 / 垃圾席位各一。"""
    klines = {
        # A 的四次买入次日都 +5%（d1..d4），第五次在 d5（结果日 d6 晚于 as_of）
        "600001": _bars([0.0, 0.05, 0.05, 0.05, 0.05, 0.05, 0.05, 0.05, 0.05, 0.05]),
        # B 买入 d2 → d3 +5%；A 同日同股一起买（协同网络）
        "600002": _bars([0.0, 0.0, 0.0, 0.05, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]),
        # C 买入 d1、d3 → 次日 -8%（大面）
        "600003": _bars([0.0, 0.0, -0.08, 0.0, -0.08, 0.0, 0.0, 0.0, 0.0, 0.0]),
        "600004": _bars([0.0] * 10),
    }
    for symbol, bars in klines.items():
        store.upsert_symbol_klines(symbol, bars, SYNC_AT)

    boards = [_board_row("600001", DATES[i]) for i in range(6)] + [
        _board_row("600002", DATES[2], float_mv=8.0e9, close=20.0)
    ]
    store.upsert_limit_board_daily(boards, SYNC_AT)
    store.update_limit_board_theme(
        [
            {
                "symbol": "600001",
                "trade_date": DATES[i],
                "theme": "测试算力",
                "board_type": [
                    "一字板",
                    "T字板",
                    "换手板",
                    "换手板",
                    "换手板",
                    "换手板",
                ][i],
            }
            for i in range(6)
        ]
        + [
            {
                "symbol": "600002",
                "trade_date": DATES[2],
                "theme": "测试机器人",
                "board_type": "一字板",
            }
        ],
        SYNC_AT,
    )

    seats = [
        _seat_row(SEAT_A, "600001", DATES[0], "buy"),
        _seat_row(SEAT_A, "600001", DATES[1], "buy"),
        _seat_row(SEAT_A, "600001", DATES[2], "buy"),
        _seat_row(SEAT_A, "600001", DATES[3], "buy"),
        _seat_row(SEAT_A, "600001", DATES[5], "buy"),
        _seat_row(SEAT_A, "600002", DATES[2], "buy"),
        # 买卖同席：A 在 600001 d1 既买又卖
        _seat_row(SEAT_A, "600001", DATES[1], "sell"),
        _seat_row(SEAT_B, "600002", DATES[2], "buy"),
        _seat_row(SEAT_C, "600003", DATES[1], "buy"),
        _seat_row(SEAT_C, "600003", DATES[3], "buy"),
        _seat_row(SEAT_C, "600003", DATES[4], "sell"),
        _seat_row(SEAT_INST, "600001", DATES[2], "buy"),
        _seat_row(SEAT_NORTH, "600001", DATES[3], "buy"),
        _seat_row(SEAT_JUNK, "600001", DATES[3], "buy"),
    ]
    store.upsert_dragon_tiger_seats(seats, SYNC_AT)
    store.upsert_hot_money_seats(
        [
            {
                "seat_name": SEAT_A,
                "org_code": "",
                "category": None,
                "description": None,
                "orgs": [SEAT_A],
                "appear_count": 0,
                "last_trade_date": DATES[5],
                "source": "proxy.top_inst",
            }
        ],
        SYNC_AT,
    )
    store.upsert_dragon_tiger(
        [
            {
                "symbol": "600001",
                "trade_date": DATES[0],
                "reason": "测试上榜",
                "close": 10.5,
                "pct_change": 10.0,
                "amount": 5.0e8,
                "l_buy": 3.0e8,
                "l_sell": 1.0e8,
                "net_amount": 2.0e8,
            }
        ],
        SYNC_AT,
    )


@pytest.fixture()
def small_db(tmp_path) -> str:
    store = build_db(tmp_path)
    write_small_db(store)
    return str(store.db_path)


# ─── 纯数学：收缩与区间 ───────────────────────────────────────────


def test_beta_shrinkage_pulls_small_sample_to_prior():
    assert beta_shrunk_rate(1, 1, 0.5, 10.0) == pytest.approx(6 / 11)
    assert beta_shrunk_rate(0, 0, 0.5, 10.0) is None
    assert beta_shrunk_rate(10, 10, 0.5, 0.0) == pytest.approx(1.0)
    with pytest.raises(ValueError):
        beta_shrunk_rate(3, 2, 0.5, 1.0)
    with pytest.raises(ValueError):
        beta_shrunk_rate(1, 2, 0.5, -1.0)


def test_mean_shrinkage_and_intervals():
    assert mean_shrunk(0.2, 5, 0.0, 15.0) == pytest.approx(0.05)
    assert mean_shrunk(None, 0, 0.0, 15.0) is None
    low, high = wilson_interval(1, 1, 1.96)
    assert low == pytest.approx(0.2065, abs=1e-3)
    assert high == 1.0
    assert wilson_interval(0, 0) is None
    interval = normal_mean_interval(0.1, [0.05, 0.15], 1.96)
    assert interval is not None and interval[0] < 0.1 < interval[1]
    assert normal_mean_interval(0.1, [0.1], 1.96) is None
    assert premium_component(0.0, 0.05) == pytest.approx(0.5)
    assert premium_component(0.05, 0.05) > 0.7
    with pytest.raises(ValueError):
        premium_component(None, 0.05)


def test_bucket_and_position_labels():
    assert bucket_edges(1.0e9, (20e8, 50e8)) == "b0"
    assert bucket_edges(30e8, (20e8, 50e8)) == "b1"
    assert bucket_edges(80e8, (20e8, 50e8)) == "b2"
    assert bucket_edges(None, (20e8,)) is None
    assert board_position_label(1, relay_min=2) == "first"
    assert board_position_label(3, relay_min=2) == "low_relay"
    assert board_position_label(5, relay_min=2) == "high_relay"
    assert board_position_label(None, relay_min=2) is None


# ─── PIT 价格账本（纯单元，不建库）────────────────────────────────


def _book(as_of: str, kline: dict, adj: dict | None = None) -> PitPriceBook:
    calendar = sorted({d for bars in kline.values() for d in bars})
    return PitPriceBook(
        as_of_date=as_of,
        calendar=calendar,
        kline=kline,
        board={},
        adj_factors=adj or {},
    )


def test_compute_event_outcome_respects_as_of_cutoff():
    kline = {
        "600001": {
            DATES[0]: {"open": 10.0, "close": 10.0},
            DATES[1]: {"open": 10.5, "close": 11.0},
        }
    }
    book = _book(DATES[0], kline)
    outcome = compute_event_outcome(book, "600001", DATES[0])
    assert outcome.usable is False
    assert outcome.reason == "outcome_not_settled_yet"
    settled = compute_event_outcome(_book(DATES[1], kline), "600001", DATES[0])
    assert settled.usable is True
    assert settled.next_open_premium == pytest.approx(0.05)
    assert settled.next_close_premium == pytest.approx(0.10)
    assert settled.next_day_drop == 0.0
    assert settled.next_is_limit_up is None  # 板行缺失 → 不猜，返回 None


def test_compute_event_outcome_drops_ex_rights_sample():
    kline = {
        "600001": {
            DATES[0]: {"open": 10.0, "close": 10.0},
            DATES[1]: {"open": 5.0, "close": 5.0},
        }
    }
    book = _book(
        DATES[1], kline, adj={("600001", DATES[0]): 1.0, ("600001", DATES[1]): 2.0}
    )
    outcome = compute_event_outcome(book, "600001", DATES[0])
    assert outcome.usable is False and outcome.reason == "ex_rights"


def test_compute_event_outcome_flags_dump_and_missing_close():
    kline = {
        "600001": {
            DATES[0]: {"open": 10.0, "close": 10.0},
            DATES[1]: {"open": 9.6, "close": 9.4},
        },
        "600002": {DATES[0]: {"open": 10.0, "close": None}},
    }
    book = _book(DATES[1], kline)
    outcome = compute_event_outcome(book, "600001", DATES[0], dump_threshold=-0.05)
    assert outcome.usable and outcome.next_day_drop == 1.0
    assert compute_event_outcome(book, "600002", DATES[0]).reason == "no_t_close"
    with pytest.raises(ValueError):
        book.nth_trade_day_after(DATES[0], 0)


# ─── 画像：事件合并 / 覆盖率 ─────────────────────────────────────


def test_build_seat_events_merges_sides_and_skips_unmatched():
    rows = [
        _seat_row(SEAT_A, "600001", DATES[0], "buy"),
        _seat_row(SEAT_A, "600001", DATES[0], "sell"),
        _seat_row(SEAT_JUNK, "600001", DATES[0], "buy"),
    ]
    events, coverage = build_seat_events(rows)
    assert coverage["seat_rows_total"] == 3
    assert coverage["seat_rows_unmatched"] == 1
    assert len(events) == 1
    event = events[0]
    assert event.sides == ("buy", "sell") and event.is_conflict is True
    assert event.seat_name_std == STD_A


def test_open_readonly_blocks_writes(small_db: str) -> None:
    conn = open_readonly(small_db)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("UPDATE kline_daily SET close = 1 WHERE symbol = '600001'")
    conn.close()
    with pytest.raises(FileNotFoundError):
        open_readonly(Path(small_db).parent / "nope.db")


def test_data_availability_and_label_proxy(small_db: str) -> None:
    with open_readonly(small_db) as conn:
        availability = data_availability(conn)
        meta = label_source_flags(conn)
    assert availability["dragon_tiger_seat"]["rows"] > 0
    assert availability["usable_for_profiling"] is True
    assert availability["blocking_datasets"] == []
    assert meta["label_source"] == "seat_name_proxy"
    assert meta["hot_money_label_is_proxy"] == 1


def test_data_availability_empty_db(tmp_path) -> None:
    store = build_db(tmp_path, "empty.db")
    with open_readonly(str(store.db_path)) as conn:
        availability = data_availability(conn)
        meta = label_source_flags(conn)
    assert availability["usable_for_profiling"] is False
    assert "dragon_tiger_seat" in availability["blocking_datasets"]
    assert meta["label_source"] == "missing"
    with pytest.raises(ValueError):
        build_seat_profiles(as_of_date=DATES[0])


# ─── 画像主流程 ──────────────────────────────────────────────────


def _profiles(small_db: str, as_of: str = DATES[5], **cfg_kwargs):
    config = SeatProfileConfig(min_events=3, prior_strength=20.0, **cfg_kwargs)
    return build_seat_profiles(small_db, as_of_date=as_of, config=config)


def test_disclosure_lag_future_outcome_excluded(small_db: str) -> None:
    """T 日榜单（as_of 当日）的结果不能进统计，即使库里已有 T+1 K 线。"""
    report = _profiles(small_db, as_of=DATES[5])
    a = report.by_seat()[STD_A]
    # A 在 600001 上 d0..d3 + d2 的 600002 共 5 个已落地样本；d5 事件结果在 d6，未落地
    assert a["listed_events"] == 6
    assert a["seat_sample_events"] == 5
    assert report.window_start_date == DATES[0]
    later = _profiles(small_db, as_of=DATES[6])
    assert later.by_seat()[STD_A]["seat_sample_events"] == 6


def test_small_sample_never_shows_misleading_100_percent(small_db: str) -> None:
    report = _profiles(small_db)
    b = report.by_seat()[STD_B]
    assert b["win_rate"] == pytest.approx(1.0)  # 原始值确实是 100%
    assert b["is_reliable"] == 0
    assert b["insufficient_sample"] == 1
    assert b["seat_alpha_score"] is None
    assert b["seat_risk_score"] is None
    assert b["reject_reasons"][0].startswith(REASON_INSUFFICIENT_SAMPLE)
    # 收缩后回到市场中枢，而不是 100%
    prior = report.summary["pool_prior_win_rate"]
    assert prior == pytest.approx(0.8)  # 10 个已落地买方事件里 8 个赚钱
    assert b["win_rate_shrunk"] < 1.0
    assert b["win_rate_shrunk"] == pytest.approx((1 + 20 * prior) / 21, abs=1e-6)


def test_reliable_seat_scoring_and_confidence_interval(small_db: str) -> None:
    report = _profiles(small_db)
    a = report.by_seat()[STD_A]
    assert a["is_reliable"] == 1
    prior = report.summary["pool_prior_win_rate"]
    assert a["win_rate_shrunk"] == pytest.approx((5 + 20 * prior) / 25, abs=1e-6)
    assert a["win_rate_shrunk"] == pytest.approx(0.84, abs=1e-6)
    assert 0.5 < a["seat_alpha_score"] < 1.0
    assert a["seat_style"] == STYLE_FIRST_BOARD  # 全部首板买入
    assert a["next_close_premium"] == pytest.approx(0.05, abs=1e-4)
    assert a["next_close_premium_shrunk"] < a["next_close_premium"]
    assert a["win_rate_ci_low"] <= a["win_rate"] <= a["win_rate_ci_high"]
    assert a["next_close_premium_ci_low"] < a["next_close_premium"]
    assert a["active_days"] == 5
    assert a["hot_money_label"] is None
    assert a["seat_label_is_proxy"] == 1
    assert a["disclaimer"]
    # 冲突席位（同日既买又卖）
    assert a["conflict_events"] == 1
    assert a["conflict_ratio"] == pytest.approx(1 / 6, abs=1e-6)
    # 协同网络：A 与 B 同日同股买入 600002
    partners = {p["seat_name_std"]: p["co_events"] for p in a["partners"]}
    assert partners[STD_B] == 1


def test_dump_seat_risk_and_style(small_db: str) -> None:
    report = _profiles(small_db)
    c = report.by_seat()[STD_C]
    # C 两次买入次日 -8%（大面），一次纯卖出次日无跌幅
    assert c["buy_face_rate"] == pytest.approx(1.0)
    assert c["seat_sample_events"] == 2 < SeatProfileConfig().min_events
    assert c["seat_alpha_score"] is None
    assert c["seat_style"] == STYLE_UNCLEAR


def test_institution_and_northbound_styles_and_filters(small_db: str) -> None:
    report = _profiles(small_db)
    assert report.by_seat()["机构专用"]["seat_style"] == STYLE_INSTITUTION
    assert report.by_seat()["沪股通"]["seat_style"] == STYLE_NORTHBOUND
    assert "机构专用" in report.by_seat()
    assert "沪股通" in report.by_seat()
    # 无法标准化的席位不进画像（计入覆盖率分母，但不伪造席位名）
    assert not any("未知文本咨询" in seat for seat in report.by_seat())
    assert report.normalization["seat_rows_unmatched"] == 1
    filtered = _profiles(small_db, include_institution=False, include_northbound=False)
    assert "机构专用" not in filtered.by_seat()
    assert "沪股通" not in filtered.by_seat()
    assert filtered.summary["seats_total"] < report.summary["seats_total"]


def test_preferences_and_themes(small_db: str) -> None:
    report = _profiles(small_db)
    a = report.by_seat()[STD_A]
    assert a["pref_theme_share"]["测试算力"] == pytest.approx(5 / 6, abs=1e-6)
    assert a["pref_position_share"]["first"] == 1.0
    assert a["pref_board_type_share"]["换手板"] > 0
    assert a["pref_missing_ratio"] == 0.0
    assert a["top_themes"][0]["theme"] == "测试算力"


def test_rolling_window_trims_history(small_db: str) -> None:
    report = _profiles(small_db, window_days=2)
    assert report.window_start_date == DATES[4]
    assert report.summary["events_total"] < _profiles(small_db).summary["events_total"]


def test_curated_label_and_audit_attach(small_db: str) -> None:
    label = HotMoneyLabel(
        standard_name=STD_A,
        label="测试游资甲",
        style_hint="relay",
        source="公开龙虎榜文本 + 人工复盘（示例）",
        curated_by="research",
        curated_at="2026-01-12",
        confidence=0.5,
    )
    report = build_seat_profiles(
        small_db,
        as_of_date=DATES[5],
        config=SeatProfileConfig(min_events=3),
        labels=[label],
    )
    a = report.by_seat()[STD_A]
    assert a["hot_money_label"] == "测试游资甲"
    assert a["label_audit"]["source"].startswith("公开龙虎榜")
    assert a["label_audit"]["confidence"] == 0.5


def test_profiles_frame_and_report_shape(small_db: str) -> None:
    report = _profiles(small_db)
    frame = profiles_frame(report)
    assert {"seat_name_std", "seat_alpha_score", "pref_mv_share"} <= set(frame.columns)
    assert len(frame) == len(report.profiles)
    payload = report.to_dict()
    assert payload["as_of_date"] == DATES[5]
    assert payload["config"]["min_events"] == 3
    assert set(payload["summary"]) >= {"seats_total", "seats_reliable", "data_usable"}
    for row in report.profiles:
        assert set(row) >= {
            "seat_sample_events",
            "next_close_premium",
            "win_rate",
            "buy_face_rate",
            "sell_dump_rate",
            "seat_style",
            "is_reliable",
        }


def test_relay_success_rate_uses_board_limit_times(tmp_path) -> None:
    """连板接力成功率：A 的 2 板买入次日全部再板，B 的全部炸板 → 收缩后差距仍在。"""
    store = build_db(tmp_path, "relay.db")
    for symbol in ("600001", "600002"):
        store.upsert_symbol_klines(symbol, _bars([0.0] + [0.10] * 9), SYNC_AT)
    boards = [_board_row("600001", DATES[i], limit_times=2) for i in range(5)]
    boards += [
        _board_row("600002", DATES[2], limit_times=2),
        _board_row("600002", DATES[3], limit_times=2, limit_flag="Z"),
        _board_row("600002", DATES[4], limit_times=None, limit_flag="Z"),
    ]
    store.upsert_limit_board_daily(boards, SYNC_AT)
    store.upsert_dragon_tiger_seats(
        [_seat_row(SEAT_A, "600001", DATES[i], "buy") for i in range(4)]
        + [_seat_row(SEAT_B, "600002", DATES[i], "buy") for i in (2, 3)],
        SYNC_AT,
    )
    report = build_seat_profiles(
        str(store.db_path), as_of_date=DATES[4], config=SeatProfileConfig(min_events=2)
    )
    a = report.by_seat()[STD_A]
    b = report.by_seat()[STD_B]
    assert a["relay_sample_events"] == 4
    assert a["relay_success_rate"] == pytest.approx(1.0)
    assert a["relay_success_rate_shrunk"] == pytest.approx(
        (4 + 20 * (4 / 6)) / 24, abs=1e-6
    )
    assert a["prior_relay_success_rate"] == pytest.approx(4 / 6, abs=1e-6)
    assert b["relay_sample_events"] == 2
    assert b["relay_success_rate"] == pytest.approx(0.0)
    assert b["relay_success_rate_shrunk"] > 0.0  # 收缩让小样本席位不判"0% 接力"
    assert a["first_sample_events"] == 0
    assert a["seat_style"] == STYLE_RELAY
    # 板表覆盖不到 T+1 时不猜：limit_flag=None → 不进接力样本（见 600002 d4 无 T 日事件）
    assert report.normalization["seat_rows_unmatched"] == 0


def test_enrich_and_aggregate_are_pure_units(tmp_path) -> None:
    """不经数据库也能算：enrich_events + aggregate_stats 的事件级口径。"""
    kline = {
        "600001": {
            DATES[0]: {"open": 10.0, "close": 10.0},
            DATES[1]: {"open": 10.2, "close": 9.0},
        }
    }
    book = _book(DATES[1], kline)
    events, _ = build_seat_events(
        [
            _seat_row(SEAT_A, "600001", DATES[0], "buy"),
            _seat_row(SEAT_B, "600001", DATES[0], "buy"),
            _seat_row(SEAT_B, "600001", DATES[0], "sell"),
        ]
    )
    cfg = SeatProfileConfig(min_events=1)
    items = enrich_events(book, events, cfg)
    stats = aggregate_stats(items, cfg)
    assert stats[STD_A].buy_outcome_n == 1
    assert stats[STD_A].buy_faces == 1  # 次日 -10% ≤ -5%
    assert stats[STD_B].conflict_events == 1
    assert stats[STD_A].partners[STD_B] == 1
    assert stats[STD_B].partners[STD_A] == 1
