"""确定性合成龙虎榜 / 行情 fixture（issue #34 单测与研究 CLI 共用）。

这里的标的代码与营业部名称**全部是虚构占位数据**（``测试XX证券…营业部``、
``MOCK标的``），刻意不复用任何真实券商营业部或真实股票名称，避免把合成数据
伪装成真实席位画像。生成过程只用固定 seed 的 ``random.Random``，同参数必得
同一份数据，便于回归与复现。

用法
----
>>> rows = make_hotmoney_fixture(days=90, symbols=12, seed=7)
>>> path = write_fixture_db(tmp_path / "market_kline.db", rows)

写库只发生在调用方给的 ``tmp_path`` 上（用 #22 的 ``KlineSQLiteStore`` 建表），
**绝不碰仓库 ``data/*.db``**；读侧（画像 / 因子 / 回测）一律只读。

植入结构（用于验证"画像能不能发现增量"）
----------------------------------------
* ``GOOD_SEATS``（前两个营业部）买入的个股 → 次日溢价被人为抬高 +3%；
* ``DUMP_SEATS``（第五个营业部）卖出的个股 → 次日溢价被压到 -6% 左右；
* 其余席位无信息（纯噪声），画像里应表现为"不可靠 / 中性"。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

CN_TZ = timezone(timedelta(hours=8))
SYNC_AT = "2026-01-01T17:00:00+08:00"

#: 虚构营业部（可被 :mod:`seat_names` 的通用句式规则标准化）。
MOCK_SEATS: tuple[str, ...] = (
    "测试第一大证券股份有限公司上海张杨路证券营业部",
    "测试第二大证券股份有限公司深圳科技园证券营业部",
    "测试第三大证券股份有限公司杭州文二西路证券营业部",
    "测试第四大证券股份有限公司成都天府大道证券营业部",
    "测试第五大证券股份有限公司北京中关村大街证券营业部",
    "测试第六大证券股份有限公司广州珠江新城证券营业部",
    "机构专用",
    "沪股通专用",
    "测试未知文本咨询公司",
)

GOOD_SEAT_COUNT = 2
DUMP_SEAT_INDEX = 5  # 无信息席位之外的"砸盘席位"下标
MOCK_THEMES: tuple[str, ...] = ("测试算力", "测试机器人", "测试医药")
MOCK_BOARD_TYPES: tuple[str, ...] = ("一字板", "T字板", "换手板")
GOOD_PREMIUM_BONUS = 0.03
DUMP_PREMIUM_LEVEL = -0.065


@dataclass(slots=True)
class FixtureRows:
    """合成数据集合：日线、涨停板行、龙虎榜行、席位行、游资名录行。"""

    dates: list[str]
    symbols: list[str]
    klines: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    boards: list[dict[str, Any]] = field(default_factory=list)
    themes: list[dict[str, Any]] = field(default_factory=list)
    dragon_tiger: list[dict[str, Any]] = field(default_factory=list)
    seats: list[dict[str, Any]] = field(default_factory=list)
    hot_money_seats: list[dict[str, Any]] = field(default_factory=list)
    #: ``(symbol, trade_date) -> 植入的次日收盘溢价``，用于断言与复现。
    premium_plan: dict[tuple[str, str], float] = field(default_factory=dict)

    def as_of(self) -> str:
        return self.dates[-1]


def _trade_dates(days: int, start: str = "2026-01-05") -> list[str]:
    cursor = datetime.strptime(start, "%Y-%m-%d").date()
    out: list[str] = []
    while len(out) < days:
        if cursor.weekday() < 5:
            out.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return out


def make_hotmoney_fixture(
    *,
    days: int = 90,
    symbols: int = 12,
    seed: int = 7,
    limit_up_prob: float = 0.35,
    buyers_per_event: int = 2,
    sellers_per_event: int = 2,
    good_seat_prob: float = 0.6,
) -> FixtureRows:
    """生成一份可复现的合成市场（涨停 → 龙虎榜 → 席位 → 次日结果）。"""
    rng = random.Random(seed)
    dates = _trade_dates(days)
    tickers = [f"6{idx:05d}" for idx in range(symbols)]
    rows = FixtureRows(dates=dates, symbols=tickers)

    close_state = {t: 10.0 for t in tickers}
    boards_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for day_idx, date in enumerate(dates):
        for ticker in tickers:
            prev = close_state[ticker]
            limit_up = day_idx > 0 and rng.random() < limit_up_prob
            if limit_up:
                close = round(prev * 1.10, 2)
                open_ = close if rng.random() < 0.5 else round(prev * 1.03, 2)
                high, low = close, round(min(open_, prev), 2)
            else:
                close = round(max(0.5, prev * (1 + rng.uniform(-0.05, 0.04))), 2)
                open_ = round(prev * (1 + rng.uniform(-0.02, 0.02)), 2)
                high = round(max(open_, close) * 1.01, 2)
                low = round(min(open_, close) * 0.99, 2)
            volume = 1_000_000.0
            rows.klines.setdefault(ticker, []).append(
                {
                    "trade_date": date,
                    "open": open_,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": volume,
                    "amount": round(volume * close, 2),
                }
            )
            if limit_up:
                prev_board = boards_by_key.get((ticker, dates[day_idx - 1]))
                prev_times = (
                    int(prev_board.get("limit_times") or 0) if prev_board else 0
                )
                board = {
                    "symbol": ticker,
                    "trade_date": date,
                    "limit_flag": "U",
                    "first_time": rng.choice(["09:35:00", "10:20:00", "14:10:00"]),
                    "last_time": "14:30:00",
                    "open_times": rng.choice([0, 0, 1, 2]),
                    "limit_times": prev_times + 1,
                    "up_stat": f"{prev_times + 1}/{prev_times + 1}",
                    "fd_amount": round(rng.uniform(0.5e8, 5e8), 2),
                    "amount": round(volume * close, 2),
                    "limit_amount": round(rng.uniform(0.5e8, 5e8), 2),
                    "turnover_ratio": round(rng.uniform(1.0, 30.0), 4),
                    "float_mv": round(rng.uniform(1.5e9, 3e10), 2),
                    "total_mv": round(rng.uniform(2e9, 5e10), 2),
                    "close": close,
                    "pct_chg": 10.0,
                }
                rows.boards.append(board)
                boards_by_key[(ticker, date)] = board
                rows.themes.append(
                    {
                        "symbol": ticker,
                        "trade_date": date,
                        "theme": rng.choice(MOCK_THEMES),
                        "board_type": rng.choice(MOCK_BOARD_TYPES),
                        "limit_reason": "测试题材",
                    }
                )
            close_state[ticker] = close

    for (ticker, date), board in sorted(
        boards_by_key.items(), key=lambda kv: (kv[0][1], kv[0][0])
    ):
        if date == dates[-1]:
            continue  # 最后一天的结果尚未落地（PIT：观察日必须在 as_of 之前）
        day_idx = dates.index(date)
        good_buyer = rng.random() < good_seat_prob
        buyers = _pick_seats(rng, count=buyers_per_event, force_good=good_buyer)
        sellers = _pick_seats(rng, count=sellers_per_event)
        dump_seller = MOCK_SEATS[DUMP_SEAT_INDEX] in sellers
        premium = rng.uniform(-0.045, 0.045)
        if good_buyer:
            premium += GOOD_PREMIUM_BONUS
        if dump_seller:
            premium = DUMP_PREMIUM_LEVEL + rng.uniform(-0.01, 0.01)
        rows.premium_plan[(ticker, date)] = premium
        next_bar = rows.klines[ticker][day_idx + 1]
        prev_close = board["close"]
        nxt_open = round(max(0.01, prev_close * (1 + premium * 0.5)), 2)
        nxt_close = round(max(0.01, prev_close * (1 + premium)), 2)
        next_bar.update(
            {
                "open": nxt_open,
                "close": nxt_close,
                "high": round(max(nxt_open, nxt_close) * 1.005, 2),
                "low": round(min(nxt_open, nxt_close) * 0.995, 2),
            }
        )
        buy_amount = round(rng.uniform(2e7, 3e8), 2)
        net = round(buy_amount * rng.uniform(0.3, 0.9), 2)
        rows.dragon_tiger.append(
            {
                "symbol": ticker,
                "trade_date": date,
                "reason": "测试上榜",
                "close": board["close"],
                "pct_change": 10.0,
                "amount": board["amount"],
                "l_buy": buy_amount,
                "l_sell": round(buy_amount - net, 2),
                "net_amount": net,
                "net_rate": round(net / max(buy_amount, 1.0) * 100, 4),
                "float_values": board["float_mv"],
                "name": "MOCK标的",
            }
        )
        for seat in buyers:
            rows.seats.append(
                {
                    "trade_date": date,
                    "symbol": ticker,
                    "seat_name": seat,
                    "side": "buy",
                    "reason": "测试上榜",
                    "stock_name": "MOCK标的",
                    "buy_amount": round(buy_amount / max(1, len(buyers)), 2),
                    "sell_amount": 0.0,
                    "net_amount": round(net / max(1, len(buyers)), 2),
                    "seat_count": 1,
                }
            )
        for seat in sellers:
            rows.seats.append(
                {
                    "trade_date": date,
                    "symbol": ticker,
                    "seat_name": seat,
                    "side": "sell",
                    "reason": "测试上榜",
                    "stock_name": "MOCK标的",
                    "buy_amount": 0.0,
                    "sell_amount": round((buy_amount - net) / max(1, len(sellers)), 2),
                    "net_amount": -round((buy_amount - net) / max(1, len(sellers)), 2),
                    "seat_count": 1,
                }
            )

    rows.hot_money_seats = [
        {
            "seat_name": name,
            "org_code": "",
            "category": None,
            "description": None,
            "orgs": [name],
            "appear_count": 0,
            "last_trade_date": dates[-1],
            "source": "proxy.top_inst",
        }
        for name in MOCK_SEATS
    ]
    return rows


def _pick_seats(
    rng: random.Random, *, count: int, force_good: bool = False
) -> list[str]:
    """从虚构席位池挑 ``count`` 个不重复席位；``force_good`` 保证含高分席位。"""
    pool = list(MOCK_SEATS)
    chosen: list[str] = []
    if force_good:
        chosen.append(rng.choice(pool[:GOOD_SEAT_COUNT]))
    while len(chosen) < count:
        candidate = rng.choice(pool)
        if candidate not in chosen:
            chosen.append(candidate)
    rng.shuffle(chosen)
    return chosen


def write_fixture_db(
    path: str | Path, rows: FixtureRows, *, synced_at: str = SYNC_AT
) -> str:
    """把合成数据写进调用方指定的**新**数据库（通常是 ``tmp_path``），返回路径。"""
    from app.services.kline_store import KlineSQLiteStore

    store = KlineSQLiteStore(str(Path(path)))
    for ticker, bars in rows.klines.items():
        store.upsert_symbol_klines(ticker, bars, synced_at)
    if rows.boards:
        store.upsert_limit_board_daily(rows.boards, synced_at)
    if rows.themes:
        store.update_limit_board_theme(rows.themes, synced_at)
    if rows.dragon_tiger:
        store.upsert_dragon_tiger(rows.dragon_tiger, synced_at)
    if rows.seats:
        store.upsert_dragon_tiger_seats(rows.seats, synced_at)
    if rows.hot_money_seats:
        store.upsert_hot_money_seats(rows.hot_money_seats, synced_at)
    return str(store.db_path)


__all__ = [
    "DUMP_SEAT_INDEX",
    "GOOD_SEAT_COUNT",
    "MOCK_SEATS",
    "FixtureRows",
    "make_hotmoney_fixture",
    "write_fixture_db",
]
