"""打板标签数据集引擎测试（issue #23）。

用临时 SQLite 文件（v3 ``limit_board_daily`` + ``kline_daily`` 读写契约）驱动
``label_engine``，覆盖：样本范围扩展到首板/连板/反包/炸板、T+1 停牌、
数据缺失与旧日线降级、窗口重叠 embargo、分布报告、CLI、金额单位与日期时区。

**测试不访问真实网络**：``test_dataset_build_never_touches_network`` 会把
``socket.socket`` 替换为直接抛错，任何联网尝试都会让测试失败。
"""

from __future__ import annotations

import json
import socket
import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from strategy.first_limit_alpha import label_engine as le
from strategy.first_limit_alpha.labeling import (
    FEATURE_SAFE_COLUMNS,
    LABEL_SOURCE_PROXY,
    LABEL_ONLY_COLUMNS,
    LS_INSUFFICIENT_HORIZON,
    STATUS_UNFILLED,
    UNFILLED_NEXT_ONE_WORD_LIMIT_UP,
    UNFILLED_NO_NEXT_BAR,
    ExecutableLabelConfig,
)

LIMIT_DDL = """
CREATE TABLE limit_board_daily (
    symbol TEXT NOT NULL, trade_date TEXT NOT NULL, ts_code TEXT, name TEXT, industry TEXT,
    limit_flag TEXT, first_time TEXT, last_time TEXT, open_times INTEGER, limit_times INTEGER,
    up_stat TEXT, fd_amount REAL, amount REAL, limit_amount REAL, turnover_ratio REAL,
    float_mv REAL, total_mv REAL, close REAL, pct_chg REAL, board_type TEXT, theme TEXT,
    limit_reason TEXT, source TEXT NOT NULL DEFAULT 'tushare.limit_list_d',
    synced_at TEXT NOT NULL, PRIMARY KEY (symbol, trade_date)
)
"""

DAILY_DDL = """
CREATE TABLE kline_daily (
    symbol TEXT NOT NULL, trade_date TEXT NOT NULL, open REAL, high REAL, low REAL,
    close REAL, volume REAL, amount REAL, source TEXT NOT NULL DEFAULT 'test',
    fallback_reason TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
    PRIMARY KEY (symbol, trade_date)
)
"""

MINUTE_DDL = """
CREATE TABLE kline_minute (
    symbol TEXT NOT NULL, bar_time TEXT NOT NULL, trade_date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (symbol, bar_time)
)
"""

LIMIT_COLUMNS = (
    "symbol,trade_date,ts_code,name,industry,limit_flag,first_time,last_time,open_times,limit_times,"
    "up_stat,fd_amount,amount,limit_amount,turnover_ratio,float_mv,total_mv,close,pct_chg,board_type,"
    "theme,limit_reason,source,synced_at"
)

TRADE_DATES = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2024-01-02", periods=140)]


def build_series(
    symbol: str,
    events: dict[int, tuple[str, int]],
    *,
    board_pct: float = 0.10,
    base_price: float = 10.0,
    n_days: int = 140,
    truncate_after: int | None = None,
    date_format: str = "%Y-%m-%d",
) -> tuple[list[tuple], list[tuple]]:
    """按事件表生成 (日线行, 打板行)。

    ``events``：``{第 i 个交易日: (形态, 连板数)}``；形态 ∈
    ``one_word | t_word | turnover | broken | one_word_open``。
    """
    dates = [d.strftime(date_format) for d in pd.bdate_range("2024-01-02", periods=n_days)]
    daily_rows: list[tuple] = []
    limit_rows: list[tuple] = []
    prev = base_price
    for i, date in enumerate(dates):
        spec = events.get(i)
        open_times = None
        if spec is None:
            o = hi = lo = c = prev
            limit_flag = None
        else:
            shape, times = spec
            limit_price = round(prev * (1.0 + board_pct) + 1e-9, 2)
            if shape == "one_word":
                o = hi = lo = c = limit_price
                open_times = 0
            elif shape == "t_word":
                o = hi = c = limit_price
                lo = round(prev * 1.02, 2)
                open_times = 1
            elif shape == "turnover":
                o = round(prev * 1.02, 2)
                hi = c = limit_price
                lo = round(prev * 0.99, 2)
                open_times = 2
            elif shape == "one_word_open":
                # 涨停开不了板：低开后拉板封死，封板后从未开板（open_times=0）
                o = lo = round(prev * 1.05, 2)
                hi = c = limit_price
                open_times = 0
            else:  # broken：摸板后炸板
                o = round(prev * 1.02, 2)
                hi = limit_price
                c = round(prev * 1.03, 2)
                lo = round(prev * 0.98, 2)
                open_times = 3
            limit_flag = "Z" if shape == "broken" else "U"
            pct = round((c / prev - 1.0) * 100.0, 4)
            limit_rows.append(
                (
                    symbol,
                    date,
                    f"{symbol}.SH",
                    f"测试{symbol[-3:]}",
                    "合成行业",
                    limit_flag,
                    "10:15:00" if shape not in {"one_word", "one_word_open"} else "09:25:00",
                    "14:05:00" if shape != "one_word" else "15:00:00",
                    open_times,
                    times,
                    f"{times}/{times}" if times else "0/1",
                    8_000_000.0 if shape != "broken" else 500_000.0,
                    round(c * 1_000_000.0, 4),
                    round(c * 300_000.0, 4),
                    6.0,
                    3_000_000_000.0,
                    6_000_000_000.0,
                    c,
                    pct,
                    "合成板块",
                    "合成题材",
                    "合成原因",
                    "tushare.limit_list_d",
                    f"{date} 15:30:00",
                )
            )
        daily_rows.append((symbol, date, o, hi, lo, c, 1_000_000.0, round(c * 1_000_000.0, 4)))
        prev = c
        if truncate_after is not None and i >= truncate_after:
            break
    return daily_rows, limit_rows


def write_db(db_path: Path, specs: list[dict], *, with_limit_table: bool = True, with_minute: bool = False) -> Path:
    conn = sqlite3.connect(str(db_path))
    conn.executescript(DAILY_DDL)
    if with_limit_table:
        conn.executescript(LIMIT_DDL)
    if with_minute:
        conn.executescript(MINUTE_DDL)
    all_daily: list[tuple] = []
    all_limit: list[tuple] = []
    for spec in specs:
        daily, limits = build_series(
            spec["symbol"],
            spec["events"],
            board_pct=spec.get("board_pct", 0.10),
            base_price=spec.get("base_price", 10.0),
            n_days=spec.get("n_days", 140),
            truncate_after=spec.get("truncate_after"),
            date_format=spec.get("date_format", "%Y-%m-%d"),
        )
        all_daily.extend(daily)
        all_limit.extend(limits)
        if with_minute and spec.get("auction_day_indices"):
            for idx in spec["auction_day_indices"]:
                date = TRADE_DATES[idx]
                px = daily[idx][3]  # 该日开盘价
                conn.execute(
                    "INSERT INTO kline_minute(symbol,bar_time,trade_date,open,high,low,close,volume) VALUES (?,?,?,?,?,?,?,?)",
                    (spec["symbol"], f"{date} 09:25:00", date, px, px, px, px, 1000.0),
                )
    daily_rows = [tuple(row) + ("test", "", "2024-07-01 15:30:00") for row in all_daily]
    conn.executemany(
        "INSERT INTO kline_daily(symbol,trade_date,open,high,low,close,volume,amount,source,fallback_reason,updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        daily_rows,
    )
    if with_limit_table and all_limit:
        conn.executemany(f"INSERT INTO limit_board_daily({LIMIT_COLUMNS}) VALUES ({','.join('?' * 24)})", all_limit)
    conn.commit()
    conn.close()
    return db_path


# 固定标的池：首板 / 一字 / 连板 / 反包 / 炸板 / 创业板 / 北交所
SPECS = [
    {"symbol": "600101", "events": {20: ("turnover", 1), 60: ("turnover", 1), 100: ("one_word", 1)}},
    {"symbol": "600102", "events": {30: ("turnover", 1), 31: ("turnover", 2), 32: ("turnover", 3)}},
    {"symbol": "600103", "events": {40: ("broken", 1), 41: ("turnover", 1)}},
    {"symbol": "600108", "events": {45: ("broken", 1)}},
    {"symbol": "600104", "events": {50: ("one_word", 1), 51: ("one_word", 2), 52: ("one_word", 3)}},
    {"symbol": "300105", "events": {25: ("turnover", 1), 70: ("t_word", 1)}, "board_pct": 0.20},
    {"symbol": "830106", "events": {35: ("turnover", 1)}, "board_pct": 0.30},
    {"symbol": "600107", "events": {55: ("one_word_open", 1), 80: ("turnover", 1)}},
]


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    return write_db(tmp_path / "market_kline.db", SPECS)


@pytest.fixture()
def labels(db: Path, tmp_path: Path) -> tuple[pd.DataFrame, dict]:
    out = tmp_path / "labels"
    meta = le.build_executable_label_dataset(db, out, mode="all")
    frame = pd.read_csv(out / le.LABELS_FILE, dtype={"symbol": str, "trade_date": str})
    return frame, meta


# ---------------------------------------------------------------------------
# 产物与 schema
# ---------------------------------------------------------------------------


def test_dataset_build_writes_all_artifacts(db: Path, tmp_path: Path):
    out = tmp_path / "labels"
    meta = le.build_executable_label_dataset(db, out, mode="all_limit_up")
    for name in (
        le.LABELS_FILE,
        le.LABELS_META_FILE,
        le.LABEL_SCHEMA_FILE,
        le.LABEL_REPORT_FILE,
        le.LABELS_EMBARGO_FILE,
    ):
        assert (out / name).exists(), name
    assert meta["schema_version"] == "daban-labels-v1"
    assert meta["sample_count"] > 0
    assert json.loads((out / le.LABEL_SCHEMA_FILE).read_text(encoding="utf-8"))
    assert meta["units"] == {"money": "元", "ratio": "小数（0.10 == 10%）", "missing": "NULL / 空"}
    assert meta["label_config"]["long_horizon"] == 5
    # 每个样本都必须能复现三个时点
    frame = pd.read_csv(out / le.LABELS_FILE, dtype={"symbol": str, "trade_date": str})
    assert {"decision_time", "entry_time_A", "label_source", "embargo_days"}.issubset(frame.columns)
    assert frame["decision_time"].astype(str).str.match(r"^\d{4}-\d{2}-\d{2} 15:00:00$").all()


def test_meta_exposes_exact_columns_for_downstream_consumers(labels):
    frame, meta = labels
    required_for_24 = set(meta["feature_safe_columns"])
    required_for_26 = {
        "entry_status_A",
        "entry_unfilled_A",
        "entry_unfilled_reason_A",
        "entry_price_A",
        "fill_probability",
        "ret_1d_A",
        "ret_3d_A",
        "ret_5d_A",
        "entry_status_B",
        "ret_1d_B",
        "label_end_date",
        "embargo_days",
    }
    assert required_for_26.issubset(set(frame.columns))
    assert required_for_24 == set(FEATURE_SAFE_COLUMNS) & set(frame.columns)
    assert not set(meta["label_only_columns"]) & required_for_24
    # 场景 A 不可成交的原因码必须可枚举，供 #5 直接建模
    assert set(frame["entry_unfilled_reason_A"].dropna()) <= {
        "one_word", "t_word", "never_opened", "not_limit_up",
        "limit_down_at_close", "flag_price_conflict", "missing_data",
    }
    assert set(frame.columns) >= required_for_26


def test_dataset_build_never_touches_network(db: Path, tmp_path: Path, monkeypatch):
    def _blocked(*args, **kwargs):
        raise AssertionError("标签构建禁止访问网络")

    monkeypatch.setattr(socket, "socket", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    meta = le.build_executable_label_dataset(db, tmp_path / "offline", mode="all")
    assert meta["sample_count"] > 0


# ---------------------------------------------------------------------------
# 样本范围：首板 / 连板 / 反包 / 炸板
# ---------------------------------------------------------------------------


def test_sample_modes_expand_beyond_first_board(db: Path):
    first = pd.DataFrame(le.build_label_frame(
        limit_frame=le.load_limit_board_rows(db), bars_frame=le.load_daily_bars(db), mode="first_board"
    ))
    cont = pd.DataFrame(le.build_label_frame(
        limit_frame=le.load_limit_board_rows(db), bars_frame=le.load_daily_bars(db), mode="continuation"
    ))
    rev = pd.DataFrame(le.build_label_frame(
        limit_frame=le.load_limit_board_rows(db), bars_frame=le.load_daily_bars(db), mode="reversal"
    ))
    allup = pd.DataFrame(le.build_label_frame(
        limit_frame=le.load_limit_board_rows(db), bars_frame=le.load_daily_bars(db), mode="all_limit_up"
    ))
    assert len(first) > 0 and len(cont) > 0
    # 连板样本必须存在（旧 data_builder 完全没有这类样本）
    assert (cont["limit_times"].astype("float") >= 2).all()
    assert set(zip(cont["symbol"], cont["trade_date"])).isdisjoint(set(zip(first["symbol"], first["trade_date"])))
    # 反包/炸板样本来自 limit_flag=Z 或前一日炸板
    assert len(rev) > 0
    assert set(rev["board_type"]) <= {"反包", "炸板", "炸板修复", "地天板"}
    # all_limit_up 是首板 + 连板 + 反包的超集
    assert len(allup) >= len(first) + len(cont)
    keys = lambda f: set(zip(f["symbol"], f["trade_date"]))  # noqa: E731
    assert keys(first) | keys(cont) | keys(rev) <= keys(allup)


def test_unknown_mode_rejected(db: Path):
    with pytest.raises(ValueError, match="未知样本模式"):
        le.build_label_frame(
            limit_frame=le.load_limit_board_rows(db),
            bars_frame=le.load_daily_bars(db),
            mode="not_a_mode",
        )


def test_high_board_and_reversal_rows_are_present(labels):
    frame, _ = labels
    assert set(frame["promotion_stage"]) >= {"1进2", "2进3", "3进4"}
    assert "炸板" in set(frame["board_type"])
    assert {"一字", "T字", "换手", "炸板未回封"} <= set(frame["limit_shape"])


# ---------------------------------------------------------------------------
# 可成交性 / 停牌 / 缺失
# ---------------------------------------------------------------------------


def test_one_word_rows_unfilled_with_null_returns(labels):
    frame, _ = labels
    one_word = frame[frame["limit_shape"] == "一字"]
    assert not one_word.empty
    assert (one_word["entry_unfilled_A"] == 1).all()
    # 关键：不可成交样本的收益标签必须是空值，而不是 0
    for col in ("ret_1d_A", "ret_3d_A", "ret_5d_A"):
        assert one_word[col].isna().all(), col
    assert (one_word["entry_price_A"].isna()).all()


def test_never_opened_rows_unfilled_with_reason(labels):
    frame, _ = labels
    never = frame[frame["entry_unfilled_reason_A"] == "never_opened"]
    assert not never.empty
    assert (never["entry_unfilled_A"] == 1).all()
    assert never["ret_1d_A"].isna().all()


def test_broken_board_rows_are_filled_and_lossy(labels):
    frame, _ = labels
    broken = frame[frame["board_type"] == "炸板"]
    assert not broken.empty
    # 炸板时排队的买单会被动成交 —— 这是真实亏损来源，不能标成"不可成交"
    assert (broken["entry_status_A"] == "filled").all()
    assert (broken["is_broken_board"] == 1).all()
    fade = frame[(frame["symbol"] == "600108") & (frame["trade_date"] == TRADE_DATES[45])].iloc[0]
    assert fade["ret_1d_A"] < 0 and fade["is_big_loss"] == 1
    assert fade["entry_price_A"] == pytest.approx(round(fade["prev_close"] * 1.1, 2))


def test_t_plus_1_suspension_marks_scene_b_unfilled(tmp_path: Path):
    """T+1 停牌（日线在 T 日之后中断）→ 场景 B 不可成交，溢价/收益全空。"""
    specs = [{"symbol": "600201", "events": {5: ("turnover", 1)}, "n_days": 140, "truncate_after": 5}]
    db_path = write_db(tmp_path / "susp.db", specs)
    frame = le.build_label_frame(
        limit_frame=le.load_limit_board_rows(db_path),
        bars_frame=le.load_daily_bars(db_path),
        mode="all_limit_up",
    )
    row = frame[frame["symbol"] == "600201"].iloc[0]
    assert row["entry_status_B"] == STATUS_UNFILLED
    assert row["entry_unfilled_reason_B"] == UNFILLED_NO_NEXT_BAR
    assert pd.isna(row["ret_1d_B"])
    assert pd.isna(row["next_close_return"])
    assert LS_INSUFFICIENT_HORIZON in row["label_status"]


def test_auction_price_from_minute_bar_is_used_then_proxy_flagged(tmp_path: Path):
    specs = [{"symbol": "600301", "events": {5: ("turnover", 1)}, "auction_day_indices": [6]}]
    db_path = write_db(tmp_path / "auction.db", specs, with_minute=True)
    frame = le.build_label_frame(
        limit_frame=le.load_limit_board_rows(db_path),
        bars_frame=le.load_daily_bars(db_path),
        mode="all_limit_up",
        auction_map=le.load_auction_prices(db_path),
    )
    row = frame.iloc[0]
    assert int(row["auction_proxy"]) == 0
    assert not pd.isna(row["auction_price"])
    assert not pd.isna(row["next_auction_premium"])

    # 关掉竞价代理 → 退回开盘价并标 auction_proxy=1
    frame2 = le.build_label_frame(
        limit_frame=le.load_limit_board_rows(db_path),
        bars_frame=le.load_daily_bars(db_path),
        mode="all_limit_up",
    )
    assert int(frame2.iloc[0]["auction_proxy"]) == 1
    assert pd.isna(frame2.iloc[0]["next_auction_premium"])


def test_next_day_one_word_limit_up_blocks_relay_entry(tmp_path: Path):
    specs = [{"symbol": "600401", "events": {5: ("turnover", 1), 6: ("one_word", 2)}}]
    db_path = write_db(tmp_path / "next_one_word.db", specs)
    frame = le.build_label_frame(
        limit_frame=le.load_limit_board_rows(db_path),
        bars_frame=le.load_daily_bars(db_path),
        mode="all_limit_up",
    )
    t0 = frame[frame["trade_date"] == TRADE_DATES[5]].iloc[0]
    assert t0["entry_unfilled_reason_B"] == UNFILLED_NEXT_ONE_WORD_LIMIT_UP
    assert pd.isna(t0["ret_1d_B"])
    assert t0["is_1_to_2"] == 1  # 隔日确实晋级，只是买不到


# ---------------------------------------------------------------------------
# 降级与空库
# ---------------------------------------------------------------------------


def test_daily_proxy_fallback_when_limit_table_empty(tmp_path: Path):
    db_path = write_db(tmp_path / "no_limit.db", SPECS, with_limit_table=False)
    meta = le.build_executable_label_dataset(db_path, tmp_path / "out", mode="all_limit_up")
    frame = pd.read_csv(tmp_path / "out" / le.LABELS_FILE, dtype={"symbol": str, "trade_date": str})
    assert meta["sample_count"] == len(frame) > 0
    assert set(frame["label_source"]) == {LABEL_SOURCE_PROXY}
    assert (frame["limit_flag_inferred"] == 1).all()
    report = json.loads((tmp_path / "out" / le.LABEL_REPORT_FILE).read_text(encoding="utf-8"))
    assert any("daily_proxy_dominant" in w for w in report["warnings"])


def test_proxy_disabled_yields_empty_dataset(tmp_path: Path):
    db_path = write_db(tmp_path / "no_limit2.db", SPECS, with_limit_table=False)
    meta = le.build_executable_label_dataset(
        db_path, tmp_path / "out2", mode="all_limit_up", allow_daily_fallback=False
    )
    assert meta["sample_count"] == 0


def test_missing_tables_are_graceful(tmp_path: Path):
    db_path = tmp_path / "empty.db"
    sqlite3.connect(str(db_path)).close()
    meta = le.build_executable_label_dataset(db_path, tmp_path / "out3", mode="all_limit_up")
    assert meta["sample_count"] == 0
    report = json.loads((tmp_path / "out3" / le.LABEL_REPORT_FILE).read_text(encoding="utf-8"))
    assert any("empty_dataset" in w for w in report["warnings"])


def test_nonexistent_db_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        le.build_executable_label_dataset(tmp_path / "nope.db", tmp_path / "out")


# ---------------------------------------------------------------------------
# embargo / walk-forward
# ---------------------------------------------------------------------------


def market_calendar(db: Path) -> list[str]:
    conn = sqlite3.connect(str(db))
    rows = conn.execute("SELECT DISTINCT trade_date FROM kline_daily ORDER BY trade_date").fetchall()
    conn.close()
    return [str(r[0]) for r in rows]


def test_walk_forward_folds_insert_embargo(labels, db):
    frame, meta = labels
    required = meta["embargo"]["required_embargo_days"]
    cal = market_calendar(db)
    folds = le.walk_forward_folds(
        frame, n_folds=3, test_days=5, min_train_days=10, embargo_days=required, calendar=cal
    )
    assert folds, "样本日期足够时应产出切分"
    for fold in folds:
        assert fold["embargo_required"] == required
        assert fold["embargo_gap_days"] >= required
        assert not set(fold["train_dates"]) & set(fold["test_dates"])
        assert not set(fold["embargo_dates"]) & set(fold["test_dates"])
        # 训练区最后一个标签窗口必须早于测试首日
        assert max(fold["train_dates"]) <= fold["train_label_end_max"] < min(fold["test_dates"])


def test_assert_embargo_respected_passes_and_catches_leak(labels, db):
    frame, meta = labels
    required = meta["embargo"]["required_embargo_days"]
    cal = market_calendar(db)
    dates = sorted(set(frame["trade_date"]))
    boundary = dates[len(dates) // 2]
    parts = le.drop_overlapping_labels(frame, boundary_date=boundary, embargo_days=required, calendar=cal)
    train, test = parts["train"], parts["test"]
    assert not train.empty and not test.empty
    assert parts["embargo_required"] == required
    # 严格按 label_end_date + 隔离带切分后应通过校验
    le.assert_embargo_respected(train, test, embargo_days=required, calendar=cal)
    # 天真按 trade_date 直接切分（不做隔离）必须报错
    naive_train = frame[frame["trade_date"] < boundary]
    naive_test = frame[frame["trade_date"] >= boundary]
    with pytest.raises(ValueError, match="embargo|重叠"):
        le.assert_embargo_respected(naive_train, naive_test, embargo_days=required)


def test_drop_overlapping_labels_partitions_disjointly(labels, db):
    frame, meta = labels
    cal = market_calendar(db)
    dates = sorted(set(frame["trade_date"]))
    boundary = dates[10]
    parts = le.drop_overlapping_labels(
        frame, boundary_date=boundary, embargo_days=meta["embargo"]["required_embargo_days"], calendar=cal
    )
    assert set(parts["train"]["trade_date"]) < set(dates)
    assert max(parts["train"]["label_end_date"]) < boundary
    # 隔离带非空：靠近边界的样本标签窗口尚未闭合，必须被丢弃而不是静默保留
    assert not parts["embargoed"].empty
    assert min(parts["test"]["trade_date"]) == boundary
    total = len(parts["train"]) + len(parts["embargoed"]) + len(parts["test"])
    assert total == len(frame)
    assert parts["embargo_required"] == le.required_embargo_days()


def test_embargo_file_is_written(labels, tmp_path: Path):
    _, meta = labels
    plan = json.loads(Path(meta["paths"]["embargo"]).read_text(encoding="utf-8"))
    assert plan["required_embargo_days"] >= 6
    assert plan["max_horizon_days"] == 5
    assert plan["dates"] and set(plan["dates"][0]) == {"trade_date", "label_end_date"}
    assert isinstance(plan["folds_example"], list)


# ---------------------------------------------------------------------------
# 分布报告
# ---------------------------------------------------------------------------


def test_distribution_report_structure(labels):
    frame, _ = labels
    report = le.label_distribution_report(frame)
    assert report["sample_count"] == len(frame)
    for col in ("is_1_to_2", "is_continuation", "is_big_loss", "entry_unfilled_A"):
        entry = report["labels"][col]
        assert 0.0 <= entry["missing_rate"] <= 1.0
        assert 0.0 <= entry["positive_rate"] <= 1.0
    assert report["labels"]["ret_1d_A"]["mean"] is not None
    assert report["by_year"] and report["by_board_type"] and report["by_limit_shape"]
    assert report["by_market_regime"].get("NA"), "regime 未接入时应有 NA 分组"
    for value, stats in report["by_board_type"].items():
        assert stats["sample_count"] > 0
        assert isinstance(stats["below_min_samples"], bool)
    assert any("coverage_short_history" in w for w in report["warnings"]), "不足 3 年必须给验收告警"


def test_rare_label_min_sample_protection(tmp_path: Path):
    specs = [{"symbol": "600501", "events": {10: ("turnover", 1)}}]
    db_path = write_db(tmp_path / "tiny.db", specs)
    frame = le.build_label_frame(
        limit_frame=le.load_limit_board_rows(db_path),
        bars_frame=le.load_daily_bars(db_path),
        mode="all_limit_up",
    )
    report = le.label_distribution_report(frame, min_group_samples=50)
    assert report["min_group_samples"] == 50
    observed = {row["label"] for row in report["rare_labels"]}
    assert observed, "稀有标签应被最小样本量保护标记"


def test_report_on_empty_frame():
    report = le.label_distribution_report(pd.DataFrame(columns=["symbol", "trade_date", "label_source"]))
    assert report["sample_count"] == 0
    assert any("empty_dataset" in w for w in report["warnings"])


# ---------------------------------------------------------------------------
# 单位 / 时区 / 板块口径（经由引擎端到端）
# ---------------------------------------------------------------------------


def test_dates_normalized_when_source_uses_yyyymmdd(tmp_path: Path):
    specs = [{"symbol": "600601", "events": {5: ("turnover", 1)}, "date_format": "%Y%m%d"}]
    db_path = write_db(tmp_path / "compact_dates.db", specs)
    frame = le.build_label_frame(
        limit_frame=le.load_limit_board_rows(db_path),
        bars_frame=le.load_daily_bars(db_path),
        mode="all_limit_up",
    )
    assert not frame.empty
    assert frame["trade_date"].str.match(r"^\d{4}-\d{2}-\d{2}$").all()
    assert frame["decision_time"].str.endswith("15:00:00").all()


def test_money_units_and_board_pct_flow_through(db: Path):
    frame = le.build_label_frame(
        limit_frame=le.load_limit_board_rows(db), bars_frame=le.load_daily_bars(db), mode="all_limit_up"
    )
    cyb = frame[frame["symbol"] == "300105"].iloc[0]
    assert cyb["limit_pct"] == 0.20 and cyb["board"] == "创业板"
    bse = frame[frame["symbol"] == "830106"].iloc[0]
    assert bse["limit_pct"] == 0.30 and bse["board"] == "北交所"
    row = frame[frame["symbol"] == "600101"].iloc[0]
    # 元：不做缩放
    assert row["fd_amount"] == 8_000_000.0
    assert row["float_mv_bucket"] == "小盘"
    assert row["limit_price"] == pytest.approx(round(row["prev_close"] * 1.1, 2))
    assert row["pct_chg_convention"] == "percent"


def test_label_only_columns_never_marked_feature_safe(labels):
    frame, meta = labels
    both = set(meta["feature_safe_columns"]) & set(meta["label_only_columns"])
    assert not both
    numeric = [c for c in frame.columns if pd.api.types.is_numeric_dtype(frame[c])]
    leaked = [c for c in numeric if c in LABEL_ONLY_COLUMNS]
    assert leaked, "标签表里应存在仅标签列"
    assert not set(leaked) & FEATURE_SAFE_COLUMNS


def test_symbols_filter_and_date_range_filter(db: Path):
    frame_all = le.build_label_frame(
        limit_frame=le.load_limit_board_rows(db), bars_frame=le.load_daily_bars(db), mode="all"
    )
    frame_one = le.build_label_frame(
        limit_frame=le.load_limit_board_rows(db),
        bars_frame=le.load_daily_bars(db),
        mode="all",
        symbols=["600102"],
    )
    assert set(frame_one["symbol"]) == {"600102"}
    assert len(frame_one) < len(frame_all)
    assert "1进2" in set(frame_one["promotion_stage"])

    lf = le.load_limit_board_rows(db, start_date=TRADE_DATES[40], end_date=TRADE_DATES[70])
    assert lf["trade_date"].min() >= TRADE_DATES[40]
    assert lf["trade_date"].max() <= TRADE_DATES[70]


def test_cli_main_builds_artifacts(db: Path, tmp_path: Path, capsys):
    out = tmp_path / "cli_labels"
    rc = le.main(["--db", str(db), "--out", str(out), "--mode", "continuation", "--horizon-long", "5"])
    assert rc == 0
    captured = capsys.readouterr().out
    assert "[daban-labels]" in captured and "embargo>=6d" in captured
    assert (out / le.LABELS_FILE).exists()
    meta = json.loads((out / le.LABELS_META_FILE).read_text(encoding="utf-8"))
    assert meta["mode"] == "continuation"
    frame = pd.read_csv(out / le.LABELS_FILE)
    assert (frame["limit_times"].astype("float") >= 2).all()


def test_cli_accepts_horizon_and_embargo_flags(db: Path, tmp_path: Path):
    out = tmp_path / "cli2"
    le.main(["--db", str(db), "--out", str(out), "--mode", "all", "--horizon-long", "4", "--embargo-extra", "2"])
    meta = json.loads((out / le.LABELS_META_FILE).read_text(encoding="utf-8"))
    cfg = ExecutableLabelConfig(long_horizon=4, embargo_buffer_days=2)
    assert meta["embargo"]["required_embargo_days"] == cfg.required_embargo_days()
    assert meta["horizons"]["long"] == 4


def test_default_column_order_matches_built_frame(db: Path):
    frame = le.build_label_frame(
        limit_frame=le.load_limit_board_rows(db), bars_frame=le.load_daily_bars(db), mode="all"
    )
    order = le.default_column_order()
    assert order[0] == "symbol" and order[1] == "trade_date"
    assert [c for c in frame.columns if c in order] == [c for c in order if c in frame.columns]


def test_builder_delegates_to_label_engine(db: Path, tmp_path: Path):
    from strategy.first_limit_alpha.data_builder import FirstLimitAlphaDataBuilder

    builder = FirstLimitAlphaDataBuilder(db, name_map={"600101": "测试甲"})
    meta = builder.build_label_dataset(tmp_path / "via_builder", mode="first_board")
    assert meta["sample_count"] > 0
    assert (tmp_path / "via_builder" / le.LABELS_FILE).exists()
