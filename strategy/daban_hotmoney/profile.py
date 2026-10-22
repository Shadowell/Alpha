"""龙虎榜席位画像（issue #34「席位画像」小节）。

口径
----
* 统计单元 = **席位事件** = ``(标准席位名, T 日, 个股)``；同一席位同一天的
  买/卖两侧合并成一条事件（``sides`` 同时含 buy/sell 即"买卖同席"，是风险信号）。
* 滚动窗口：只取 ``[window_start, as_of]``（默认 250 个交易日）的榜单，
  窗口外历史行为不参与评分（游资风格会切换，issue 风险项要求定期重估）。
* 结果字段只在**观察日 <= as_of** 时进入统计（见
  :func:`strategy.daban_hotmoney.source.compute_event_outcome`），因此 T 日
  榜单的席位事件要等 T+1（甚至 T+3）收盘后才成为样本 —— 杜绝前视泄漏。
* 收缩：比率走 Beta-Binomial（伪样本 ``prior_strength``），均值走经验贝叶斯；
  先验取**同窗口全体席位的事件级中枢**，小样本席位被拉回市场中枢，
  不会出现"1 次上榜 = 100% 胜率"。
* 样本量门槛：可用样本 ``< min_events`` 的席位 ``is_reliable=0``、
  ``seat_alpha_score=None``、``seat_risk_score=None`` 并给出拒绝原因。

字段定义
--------
``next_open_premium``  T 收盘 → T+1 开盘溢价（无真实竞价数据，故为竞价强度代理）
``next_close_premium`` T 收盘 → T+1 收盘溢价
``ret_3d``             T 收盘 → T+3 收盘收益
``win_rate``           ``next_close_premium > 0`` 的频率（买方样本）
``buy_face_rate``      买方事件次日大跌率（"买入即大面"）
``sell_dump_rate``     卖方事件次日大跌率（"出货砸盘"）
``relay_success_rate`` T 日 ``limit_times >= relay_min_boards`` 买入后 T+1 再板率
``first_promotion_rate`` T 日首板买入后 T+1 晋级率
"""

from __future__ import annotations

import math
import sqlite3
import statistics
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from strategy.daban_hotmoney.seat_names import (
    HOT_MONEY_LABELS,
    KIND_INSTITUTION,
    KIND_NORTHBOUND,
    SEAT_ALIAS_RULES,
    HotMoneyLabel,
    SeatAliasRule,
    normalize_seat_name,
    seat_label,
)
from strategy.daban_hotmoney.source import (
    EventOutcome,
    PitPriceBook,
    build_price_book,
    compute_event_outcome,
    data_availability,
    label_source_flags,
    load_seat_events,
    open_readonly,
)

MV_BUCKET_EDGES: tuple[float, ...] = (20e8, 50e8, 100e8, 300e8)
PRICE_BUCKET_EDGES: tuple[float, ...] = (5.0, 10.0, 30.0, 60.0)

STYLE_INSTITUTION = "institution"
STYLE_NORTHBOUND = "northbound"
STYLE_RELAY = "relay"
STYLE_FIRST_BOARD = "first_board"
STYLE_DUMP = "dump"
STYLE_MIXED = "mixed"
STYLE_UNCLEAR = "unclear"

REASON_INSUFFICIENT_SAMPLE = "insufficient_sample"
REASON_NO_OUTCOME_SAMPLE = "no_settled_outcome_sample"
REASON_NO_SCORE = "score_not_computed"

#: 风格判定阈值（写死在配置里并由报告导出，便于复现 / 复盘）
DUMP_STYLE_THRESHOLD = 0.30
RELAY_STYLE_THRESHOLD = 0.50
FIRST_BOARD_STYLE_THRESHOLD = 0.70


@dataclass(frozen=True, slots=True)
class SeatProfileConfig:
    """画像超参（全部可配置，随报告导出）。"""

    window_days: int = 250
    min_events: int = 20
    prior_strength: float = 20.0
    z: float = 1.96
    dump_threshold: float = -0.05
    relay_min_boards: int = 2
    alpha_scale: float = 0.05
    top_theme_n: int = 3
    top_partner_n: int = 5
    include_institution: bool = True
    include_northbound: bool = True


@dataclass(frozen=True, slots=True)
class SeatEvent:
    """合并买卖两侧后的席位事件（榜单原文 → 标准席位）。"""

    seat_name_std: str
    trade_date: str
    symbol: str
    sides: tuple[str, ...]
    buy_amount: float | None
    sell_amount: float | None
    net_amount: float | None
    seat_count: int
    kinds: tuple[str, ...]
    raw_names: tuple[str, ...]
    reason: str | None

    @property
    def has_buy(self) -> bool:
        return "buy" in self.sides

    @property
    def has_sell(self) -> bool:
        return "sell" in self.sides

    @property
    def is_conflict(self) -> bool:
        """买卖同席：同一席位当天既是买方又是卖方（对倒 / 出货嫌疑）。"""
        return self.has_buy and self.has_sell


@dataclass(slots=True)
class EnrichedEvent:
    """事件 + PIT 结果 + T 日板行快照（板行本身在 T 日盘后可知，无未来信息）。"""

    event: SeatEvent
    outcome: EventOutcome
    board: dict[str, Any] | None
    limit_times: int | None

    @property
    def settled(self) -> bool:
        return self.outcome.usable


# ─── 收缩与置信区间（纯函数，单测直接覆盖）─────────────────────────


def beta_shrunk_rate(
    successes: int, n: int, prior_rate: float, k: float
) -> float | None:
    """Beta-Binomial 收缩 ``(x + k*p0) / (n + k)``；``n=0`` 返回 None（不造数）。"""
    if n < 0 or successes < 0 or successes > n:
        raise ValueError(f"非法比率样本: successes={successes} n={n}")
    if k < 0:
        raise ValueError("prior_strength 必须 >= 0")
    if n == 0:
        return None
    return (successes + k * prior_rate) / (n + k)


def mean_shrunk(
    mean: float | None, n: int, prior_mean: float, k: float
) -> float | None:
    """均值经验贝叶斯收缩 ``(n*mean + k*prior) / (n + k)``；无样本返回 None。"""
    if k < 0:
        raise ValueError("prior_strength 必须 >= 0")
    if mean is None or n <= 0:
        return None
    return (n * mean + k * prior_mean) / (n + k)


def wilson_interval(
    successes: int, n: int, z: float = 1.96
) -> tuple[float, float] | None:
    """比率的 Wilson 置信区间；``n=0`` 返回 None。"""
    if n <= 0:
        return None
    phat = successes / n
    z2 = z * z
    centre = (phat + z2 / (2 * n)) / (1.0 + z2 / n)
    half = (z / (1.0 + z2 / n)) * math.sqrt(phat * (1 - phat) / n + z2 / (4 * n * n))
    return (max(0.0, centre - half), min(1.0, centre + half))


def normal_mean_interval(
    mean: float | None, values: Sequence[float | None], z: float = 1.96
) -> tuple[float, float] | None:
    """均值的正态近似区间（``n>=2`` 才有样本标准差）。"""
    data = [float(v) for v in values if v is not None]
    if mean is None or len(data) < 2:
        return None
    se = statistics.stdev(data) / math.sqrt(len(data))
    return (mean - z * se, mean + z * se)


def premium_component(shrunk_premium: float | None, scale: float) -> float:
    """把收缩后的次日收盘溢价压到 [0,1]：``0.5 + 0.5*tanh(p/scale)``。"""
    if shrunk_premium is None:
        raise ValueError("premium_component 需要非空收缩溢价")
    if scale <= 0:
        raise ValueError("alpha_scale 必须为正")
    return _clip01(0.5 + 0.5 * math.tanh(shrunk_premium / scale))


def _clip01(value: float) -> float:
    return min(1.0, max(0.0, value))


def _mean_std(values: Iterable[float | None]) -> tuple[float | None, float | None, int]:
    data = [float(v) for v in values if v is not None]
    if not data:
        return None, None, 0
    if len(data) == 1:
        return data[0], None, 1
    return statistics.fmean(data), statistics.stdev(data), len(data)


# ─── 事件装载与合并 ───────────────────────────────────────────────


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(out) else out


def build_seat_events(
    rows: Iterable[dict[str, Any]],
    *,
    alias_rules: Iterable[SeatAliasRule] = SEAT_ALIAS_RULES,
) -> tuple[list[SeatEvent], dict[str, Any]]:
    """把 ``dragon_tiger_seat_daily`` 行合并成席位事件，并统计标准化情况。

    标准化失败的行**不参与分组**（不伪造席位名），但计入覆盖率分母 ——
    这正是 issue 验收项"标准化覆盖 ≥ 95%"的口径。
    """
    merged: dict[tuple[str, str, str], dict[str, Any]] = {}
    raw_by_standard: dict[str, set[str]] = defaultdict(set)
    skipped_unmatched = 0
    total_rows = 0
    for row in rows:
        total_rows += 1
        raw = str(row.get("seat_name") or "").strip()
        trade_date = str(row.get("trade_date") or "").strip()
        symbol = str(row.get("symbol") or "").strip().zfill(6)
        side = str(row.get("side") or "").strip().lower()
        seat = normalize_seat_name(raw, alias_rules=alias_rules)
        if not seat.is_standardized or not seat.standard_name:
            skipped_unmatched += 1
            continue
        if not trade_date or not symbol:
            skipped_unmatched += 1
            continue
        standard = seat.standard_name
        raw_by_standard[standard].add(raw)
        item = merged.setdefault(
            (standard, trade_date, symbol),
            {
                "sides": set(),
                "buy_amount": None,
                "sell_amount": None,
                "net_amount": None,
                "seat_count": 0,
                "kinds": set(),
                "reason": None,
            },
        )
        item["seat_count"] += int(row.get("seat_count") or 1)
        item["kinds"].add(seat.kind)
        buy = _num(row.get("buy_amount"))
        sell = _num(row.get("sell_amount"))
        if side == "buy":
            item["sides"].add("buy")
            if buy is not None:
                item["buy_amount"] = (_num(item["buy_amount"]) or 0.0) + buy
            if sell:  # top_inst 的买入行也可能带 symmetric_sell 金额
                item["sides"].add("sell")
                item["sell_amount"] = (_num(item["sell_amount"]) or 0.0) + sell
        elif side == "sell":
            item["sides"].add("sell")
            if sell is not None:
                item["sell_amount"] = (_num(item["sell_amount"]) or 0.0) + sell
        net = _num(row.get("net_amount"))
        if net is not None:
            item["net_amount"] = (_num(item["net_amount"]) or 0.0) + net
        if item["reason"] is None:
            reason = str(row.get("reason") or "").strip()
            item["reason"] = reason or None

    events: list[SeatEvent] = []
    for (standard, trade_date, symbol), item in merged.items():
        net = item["net_amount"]
        if net is None:
            net = (_num(item["buy_amount"]) or 0.0) - (_num(item["sell_amount"]) or 0.0)
        events.append(
            SeatEvent(
                seat_name_std=standard,
                trade_date=trade_date,
                symbol=symbol,
                sides=tuple(sorted(item["sides"])),
                buy_amount=item["buy_amount"],
                sell_amount=item["sell_amount"],
                net_amount=net,
                seat_count=item["seat_count"],
                kinds=tuple(sorted(item["kinds"])),
                raw_names=tuple(sorted(raw_by_standard[standard])),
                reason=item["reason"],
            )
        )
    events.sort(key=lambda e: (e.trade_date, e.symbol, e.seat_name_std))
    coverage = {
        "seat_rows_total": total_rows,
        "seat_rows_unmatched": skipped_unmatched,
        "seat_events_total": len(events),
        "seat_distinct_standard": len(raw_by_standard),
    }
    return events, coverage


def enrich_events(
    book: PitPriceBook,
    events: Sequence[SeatEvent],
    config: SeatProfileConfig,
    *,
    notes: Counter | None = None,
) -> list[EnrichedEvent]:
    """给每个事件挂上 PIT 结果与 T 日板行；``settled=False`` 的事件不进统计。"""
    counter = notes if notes is not None else Counter()
    enriched: list[EnrichedEvent] = []
    for event in events:
        outcome = compute_event_outcome(
            book, event.symbol, event.trade_date, dump_threshold=config.dump_threshold
        )
        if not outcome.usable:
            counter[f"outcome_skipped:{outcome.reason}"] += 1
        board = book.board_row(event.symbol, event.trade_date)
        raw_times = board.get("limit_times") if board else None
        limit_times = int(raw_times) if raw_times is not None else None
        if board is None:
            counter["board_row_missing"] += 1
        enriched.append(
            EnrichedEvent(
                event=event, outcome=outcome, board=board, limit_times=limit_times
            )
        )
    return enriched


# ─── 偏好分桶 ─────────────────────────────────────────────────────


def bucket_edges(value: float | None, edges: tuple[float, ...]) -> str | None:
    """连续值按边界切成 ``b0..bN``；缺失返回 None（绝不填 0）。"""
    if value is None:
        return None
    for idx, edge in enumerate(edges):
        if value < edge:
            return f"b{idx}"
    return f"b{len(edges)}"


def bucket_labels(edges: tuple[float, ...]) -> list[str]:
    """桶编号 → 可读区间（金额单位为元）。"""
    names = [f"<{edges[0]:.0e}"]
    for i in range(len(edges) - 1):
        names.append(f"{edges[i]:.0e}-{edges[i + 1]:.0e}")
    names.append(f">={edges[-1]:.0e}")
    return names


def board_position_label(limit_times: int | None, *, relay_min: int) -> str | None:
    if limit_times is None:
        return None
    if limit_times <= 1:
        return "first"
    if limit_times < relay_min + 2:
        return "low_relay"
    return "high_relay"


def _share_map(counter: Counter[str], total: int) -> dict[str, float]:
    if not total:
        return {}
    return {key: round(value / total, 6) for key, value in counter.most_common()}


# ─── 统计聚合 ─────────────────────────────────────────────────────


@dataclass(slots=True)
class SeatStats:
    seat_name_std: str
    listed_events: int = 0
    buy_events: int = 0
    sell_events: int = 0
    conflict_events: int = 0
    first_seen_date: str | None = None
    last_seen_date: str | None = None
    buy_outcome_n: int = 0
    sell_outcome_n: int = 0
    wins: int = 0
    buy_faces: int = 0
    sell_dumps: int = 0
    relay_total: int = 0
    relay_success: int = 0
    first_total: int = 0
    first_success: int = 0
    open_premiums: list[float] = field(default_factory=list)
    close_premiums: list[float] = field(default_factory=list)
    ret_3d: list[float] = field(default_factory=list)
    mv_counter: Counter = field(default_factory=Counter)
    price_counter: Counter = field(default_factory=Counter)
    board_type_counter: Counter = field(default_factory=Counter)
    theme_counter: Counter = field(default_factory=Counter)
    position_counter: Counter = field(default_factory=Counter)
    bucket_missing: int = 0
    bucket_total: int = 0
    partners: Counter = field(default_factory=Counter)
    kinds: set[str] = field(default_factory=set)
    raw_names: set[str] = field(default_factory=set)
    active_dates: set[str] = field(default_factory=set)
    net_buy_sum: float = 0.0


def aggregate_stats(
    items: Sequence[EnrichedEvent], config: SeatProfileConfig
) -> dict[str, SeatStats]:
    """一次遍历把所有席位事件聚成 :class:`SeatStats`（含协同网络与偏好）。"""
    stats: dict[str, SeatStats] = {}
    buyers_by_key: dict[tuple[str, str], set[str]] = defaultdict(set)
    for item in items:
        buyers_by_key[(item.event.trade_date, item.event.symbol)].add(
            item.event.seat_name_std
        )
    for item in items:
        event = item.event
        seat = stats.setdefault(
            event.seat_name_std, SeatStats(seat_name_std=event.seat_name_std)
        )
        seat.listed_events += 1
        seat.first_seen_date = min(
            filter(None, [seat.first_seen_date, event.trade_date])
        )
        seat.last_seen_date = max(filter(None, [seat.last_seen_date, event.trade_date]))
        seat.active_dates.add(event.trade_date)
        seat.kinds.update(event.kinds)
        seat.raw_names.update(event.raw_names)
        if event.has_buy:
            seat.buy_events += 1
        if event.has_sell:
            seat.sell_events += 1
        if event.is_conflict:
            seat.conflict_events += 1
        if event.net_amount:
            seat.net_buy_sum += float(event.net_amount)
        for other in buyers_by_key[(event.trade_date, event.symbol)]:
            if other != event.seat_name_std and event.has_buy:
                seat.partners[other] += 1
        if event.has_buy:
            _add_preferences(seat, item, config)
        outcome = item.outcome
        if event.has_buy and outcome.usable:
            seat.buy_outcome_n += 1
            if outcome.next_open_premium is not None:
                seat.open_premiums.append(outcome.next_open_premium)
            if outcome.next_close_premium is not None:
                seat.close_premiums.append(outcome.next_close_premium)
                if outcome.next_close_premium > 0:
                    seat.wins += 1
                if outcome.next_day_drop == 1.0:
                    seat.buy_faces += 1
            if outcome.ret_3d is not None:
                seat.ret_3d.append(outcome.ret_3d)
            if item.limit_times is not None and outcome.next_is_limit_up is not None:
                if item.limit_times >= config.relay_min_boards:
                    seat.relay_total += 1
                    seat.relay_success += 1 if outcome.next_is_limit_up else 0
                elif item.limit_times == 1:
                    seat.first_total += 1
                    seat.first_success += 1 if outcome.next_is_limit_up else 0
        if event.has_sell and outcome.usable:
            seat.sell_outcome_n += 1
            if outcome.next_day_drop == 1.0:
                seat.sell_dumps += 1
    return stats


def _add_preferences(
    seat: SeatStats, item: EnrichedEvent, config: SeatProfileConfig
) -> None:
    """ "它喜欢买什么"：只用 T 日盘后已知的板行字段，不需要未来数据。"""
    board = item.board
    seat.bucket_total += 1
    mv = _num(board.get("float_mv")) if board else None
    price = _num(board.get("close")) if board else None
    if board is None or (mv is None and price is None):
        seat.bucket_missing += 1
    seat.mv_counter[bucket_edges(mv, MV_BUCKET_EDGES) or "missing"] += 1
    seat.price_counter[bucket_edges(price, PRICE_BUCKET_EDGES) or "missing"] += 1
    theme = str(board.get("theme") or "").strip() if board else ""
    board_type = str(board.get("board_type") or "").strip() if board else ""
    seat.theme_counter[theme or "missing"] += 1
    seat.board_type_counter[board_type or "missing"] += 1
    seat.position_counter[
        board_position_label(item.limit_times, relay_min=config.relay_min_boards)
        or "missing"
    ] += 1


def pooled_priors(
    items: Sequence[EnrichedEvent], config: SeatProfileConfig
) -> dict[str, float]:
    """同窗口**事件级**市场中枢：所有席位收缩时共用这份先验。"""
    buy_close: list[float] = []
    buy_open: list[float] = []
    buy_r3: list[float] = []
    relay_total = relay_success = 0
    sell_total = sell_dump = 0
    for item in items:
        outcome = item.outcome
        if not outcome.usable:
            continue
        if item.event.has_buy:
            if outcome.next_close_premium is not None:
                buy_close.append(outcome.next_close_premium)
            if outcome.next_open_premium is not None:
                buy_open.append(outcome.next_open_premium)
            if outcome.ret_3d is not None:
                buy_r3.append(outcome.ret_3d)
            if (
                item.limit_times is not None
                and item.limit_times >= config.relay_min_boards
                and outcome.next_is_limit_up is not None
            ):
                relay_total += 1
                relay_success += 1 if outcome.next_is_limit_up else 0
        if item.event.has_sell:
            sell_total += 1
            sell_dump += 1 if outcome.next_day_drop == 1.0 else 0
    n = len(buy_close)
    return {
        "buy_outcome_events": float(n),
        "sell_outcome_events": float(sell_total),
        "mean_open_premium": statistics.fmean(buy_open) if buy_open else 0.0,
        "mean_close_premium": statistics.fmean(buy_close) if buy_close else 0.0,
        "mean_ret_3d": statistics.fmean(buy_r3) if buy_r3 else 0.0,
        "win_rate": (sum(1 for v in buy_close if v > 0) / n) if n else 0.5,
        "buy_face_rate": (sum(1 for v in buy_close if v <= config.dump_threshold) / n)
        if n
        else 0.0,
        "sell_dump_rate": (sell_dump / sell_total) if sell_total else 0.0,
        "relay_success_rate": (relay_success / relay_total) if relay_total else 0.0,
        "relay_pool_events": float(relay_total),
    }


# ─── 单席位画像行 ─────────────────────────────────────────────────


def profile_row(
    seat: str,
    stats: SeatStats,
    priors: dict[str, float],
    config: SeatProfileConfig,
    *,
    as_of_date: str,
    window_start: str | None,
    labels: Sequence[HotMoneyLabel] = HOT_MONEY_LABELS,
    label_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把 :class:`SeatStats` + 市场中枢 → 画像行（含收缩、CI、评分、风格）。"""
    meta = label_meta or {}
    k = float(config.prior_strength)
    z = float(config.z)
    close_mean, close_std, close_n = _mean_std(stats.close_premiums)
    open_mean, _, open_n = _mean_std(stats.open_premiums)
    r3_mean, _, r3_n = _mean_std(stats.ret_3d)

    shrunk_close = mean_shrunk(close_mean, close_n, priors["mean_close_premium"], k)
    shrunk_open = mean_shrunk(open_mean, open_n, priors["mean_open_premium"], k)
    shrunk_r3 = mean_shrunk(r3_mean, r3_n, priors["mean_ret_3d"], k)
    shrunk_win = beta_shrunk_rate(stats.wins, close_n, priors["win_rate"], k)
    shrunk_face = beta_shrunk_rate(stats.buy_faces, close_n, priors["buy_face_rate"], k)
    shrunk_dump = beta_shrunk_rate(
        stats.sell_dumps, stats.sell_outcome_n, priors["sell_dump_rate"], k
    )
    shrunk_relay = beta_shrunk_rate(
        stats.relay_success, stats.relay_total, priors["relay_success_rate"], k
    )
    first_prior = priors["relay_success_rate"]
    shrunk_first = beta_shrunk_rate(
        stats.first_success, stats.first_total, first_prior, k
    )

    reliable = close_n >= config.min_events
    reject_reasons: list[str] = []
    if close_n == 0:
        reject_reasons.append(REASON_NO_OUTCOME_SAMPLE)
    if not reliable:
        reject_reasons.append(
            f"{REASON_INSUFFICIENT_SAMPLE}(n={close_n}<min_events={config.min_events})"
        )

    alpha = risk = None
    if (
        reliable
        and shrunk_close is not None
        and shrunk_win is not None
        and shrunk_face is not None
    ):
        sample_component = close_n / (close_n + k)
        alpha = _clip01(
            0.55 * premium_component(shrunk_close, config.alpha_scale)
            + 0.30 * shrunk_win
            + 0.15 * sample_component
        )
    if reliable and shrunk_dump is not None and shrunk_face is not None:
        risk = _clip01(0.70 * shrunk_dump + 0.30 * shrunk_face)
    if alpha is None and not reject_reasons:
        reject_reasons.append(REASON_NO_SCORE)

    style, style_note = classify_style(
        stats, shrunk_relay, shrunk_dump, reliable, close_n, priors
    )
    label = seat_label(seat, labels=labels)
    win_rate = (stats.wins / close_n) if close_n else None
    ci_close = normal_mean_interval(close_mean, stats.close_premiums, z)
    ci_win = wilson_interval(stats.wins, close_n, z)

    row: dict[str, Any] = {
        "seat_name_std": seat,
        "seat_raw_names": sorted(stats.raw_names),
        "seat_kind": sorted(stats.kinds),
        "as_of_date": as_of_date,
        "window_start_date": window_start,
        "window_days": config.window_days,
        "first_seen_date": stats.first_seen_date,
        "last_seen_date": stats.last_seen_date,
        "active_days": len(stats.active_dates),
        "listed_events": stats.listed_events,
        "buy_events": stats.buy_events,
        "sell_events": stats.sell_events,
        "conflict_events": stats.conflict_events,
        "conflict_ratio": (
            round(stats.conflict_events / stats.listed_events, 6)
            if stats.listed_events
            else None
        ),
        "net_buy_sum_cny": round(stats.net_buy_sum, 2),
        "seat_sample_events": close_n,
        "sell_sample_events": stats.sell_outcome_n,
        "next_open_premium": _round(open_mean),
        "next_open_premium_shrunk": _round(shrunk_open),
        "next_close_premium": _round(close_mean),
        "next_close_premium_shrunk": _round(shrunk_close),
        "next_close_premium_ci_low": _round(ci_close[0] if ci_close else None),
        "next_close_premium_ci_high": _round(ci_close[1] if ci_close else None),
        "ret_3d": _round(r3_mean),
        "ret_3d_shrunk": _round(shrunk_r3),
        "win_rate": _round(win_rate),
        "win_rate_shrunk": _round(shrunk_win),
        "win_rate_ci_low": _round(ci_win[0] if ci_win else None),
        "win_rate_ci_high": _round(ci_win[1] if ci_win else None),
        "buy_face_rate": _round((stats.buy_faces / close_n) if close_n else None),
        "buy_face_rate_shrunk": _round(shrunk_face),
        "sell_dump_rate": _round(
            (stats.sell_dumps / stats.sell_outcome_n) if stats.sell_outcome_n else None
        ),
        "sell_dump_rate_shrunk": _round(shrunk_dump),
        "relay_success_rate": _round(
            (stats.relay_success / stats.relay_total) if stats.relay_total else None
        ),
        "relay_success_rate_shrunk": _round(shrunk_relay),
        "relay_sample_events": stats.relay_total,
        "first_promotion_rate": _round(
            (stats.first_success / stats.first_total) if stats.first_total else None
        ),
        "first_promotion_rate_shrunk": _round(shrunk_first),
        "first_sample_events": stats.first_total,
        "pref_mv_share": _share_map(stats.mv_counter, stats.bucket_total),
        "pref_price_share": _share_map(stats.price_counter, stats.bucket_total),
        "pref_board_type_share": _share_map(
            stats.board_type_counter, stats.bucket_total
        ),
        "pref_theme_share": _share_map(stats.theme_counter, stats.bucket_total),
        "pref_position_share": _share_map(stats.position_counter, stats.bucket_total),
        "pref_missing_ratio": _round(
            (stats.bucket_missing / stats.bucket_total) if stats.bucket_total else None
        ),
        "top_themes": [
            {"theme": name, "events": count}
            for name, count in stats.theme_counter.most_common(config.top_theme_n)
        ],
        "partners": [
            {"seat_name_std": name, "co_events": count}
            for name, count in stats.partners.most_common(config.top_partner_n)
        ],
        "seat_alpha_score": _round(alpha),
        "seat_risk_score": _round(risk),
        "seat_style": style,
        "seat_style_note": style_note,
        "is_reliable": int(reliable),
        "insufficient_sample": int(not reliable),
        "reject_reasons": reject_reasons,
        "prior_event_count": int(priors["buy_outcome_events"]),
        "prior_win_rate": _round(priors["win_rate"]),
        "prior_close_premium": _round(priors["mean_close_premium"]),
        "prior_relay_success_rate": _round(priors["relay_success_rate"]),
        "hot_money_label": label.label if label else None,
        "seat_label_is_proxy": int(meta.get("hot_money_label_is_proxy", 1)),
        "seat_label_source": meta.get("label_source", "missing"),
        "label_audit": (
            {
                "source": label.source,
                "curated_by": label.curated_by,
                "curated_at": label.curated_at,
                "confidence": float(label.confidence),
            }
            if label
            else None
        ),
        "disclaimer": meta.get("disclaimer"),
    }
    if close_std is None:
        row["premium_std_is_missing"] = 1
    return row


def _round(value: float | None, digits: int = 6) -> float | None:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    return round(float(value), digits)


def classify_style(
    stats: SeatStats,
    shrunk_relay: float | None,
    shrunk_dump: float | None,
    reliable: bool,
    n: int,
    priors: dict[str, float],
) -> tuple[str, str]:
    """风格判定：机构 / 北向（文本判定）→ 数据不足不判定 → 砸盘 → 接力 → 首板 → 混合。"""
    kinds = set(stats.kinds)
    if KIND_INSTITUTION in kinds:
        return STYLE_INSTITUTION, "交易所机构专用席位（文本判定，非游资身份标签）"
    if KIND_NORTHBOUND in kinds:
        return STYLE_NORTHBOUND, "沪深股通席位（文本判定，非游资身份标签）"
    if not reliable or n == 0:
        return STYLE_UNCLEAR, f"次日结果样本不足（n={n}），不判定风格"
    pos = _share_map(stats.position_counter, stats.bucket_total)
    relay_share = pos.get("low_relay", 0.0) + pos.get("high_relay", 0.0)
    first_share = pos.get("first", 0.0)
    if shrunk_dump is not None and shrunk_dump >= DUMP_STYLE_THRESHOLD:
        return (
            STYLE_DUMP,
            f"卖出后次日大跌收缩率 {shrunk_dump:.2%} ≥ {DUMP_STYLE_THRESHOLD:.0%}",
        )
    if (
        shrunk_relay is not None
        and relay_share >= RELAY_STYLE_THRESHOLD
        and shrunk_relay >= max(RELAY_STYLE_THRESHOLD, priors["relay_success_rate"])
    ):
        return (
            STYLE_RELAY,
            f"连板买入占比 {relay_share:.2%}，晋级收缩率 {shrunk_relay:.2%}",
        )
    if first_share >= FIRST_BOARD_STYLE_THRESHOLD:
        return STYLE_FIRST_BOARD, f"首板买入占比 {first_share:.2%}"
    return STYLE_MIXED, "风格分散，未达单一风格判定阈值"


# ─── 公开入口 ─────────────────────────────────────────────────────


@dataclass(slots=True)
class SeatProfileReport:
    """画像报告（JSON 可序列化，直接给 API / 研究脚本用）。"""

    as_of_date: str
    window_start_date: str | None
    config: dict[str, Any]
    profiles: list[dict[str, Any]]
    summary: dict[str, Any]
    normalization: dict[str, Any]
    availability: dict[str, Any]
    label_meta: dict[str, Any]
    notes: dict[str, int]

    def by_seat(self) -> dict[str, dict[str, Any]]:
        return {row["seat_name_std"]: row for row in self.profiles}

    def reliable(self) -> list[dict[str, Any]]:
        return [row for row in self.profiles if row["is_reliable"]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_seat_profiles(
    db_path: str | Path | None = None,
    *,
    conn: sqlite3.Connection | None = None,
    as_of_date: str,
    config: SeatProfileConfig | None = None,
    labels: Sequence[HotMoneyLabel] = HOT_MONEY_LABELS,
    alias_rules: Sequence[SeatAliasRule] = SEAT_ALIAS_RULES,
) -> SeatProfileReport:
    """构建 PIT 席位画像。``db_path`` 与 ``conn`` 二选一（均以只读方式访问）。"""
    cfg = config or SeatProfileConfig()
    if conn is not None:
        connection, own_conn = conn, False
    elif db_path is not None:
        connection, own_conn = open_readonly(db_path), True
    else:
        raise ValueError("必须提供 db_path 或 conn 之一")
    try:
        availability = data_availability(connection)
        label_meta = label_source_flags(connection)
        label_meta["disclaimer"] = _disclaimer()
        window_start = _window_start(connection, as_of_date, cfg.window_days)
        book = build_price_book(
            connection, as_of_date=as_of_date, start_date=window_start
        )
        raw_events, coverage = build_seat_events(
            load_seat_events(connection, start_date=window_start, upto_date=as_of_date),
            alias_rules=alias_rules,
        )
        events = [
            event
            for event in raw_events
            if (cfg.include_institution or KIND_INSTITUTION not in event.kinds)
            and (cfg.include_northbound or KIND_NORTHBOUND not in event.kinds)
        ]
        notes: Counter = Counter()
        items = enrich_events(book, events, cfg, notes=notes)
        stats = aggregate_stats(items, cfg)
        priors = pooled_priors(items, cfg)
        rows = [
            profile_row(
                seat,
                stats[seat],
                priors,
                cfg,
                as_of_date=as_of_date,
                window_start=window_start,
                labels=labels,
                label_meta=label_meta,
            )
            for seat in sorted(stats)
        ]
        summary = {
            "seats_total": len(rows),
            "seats_reliable": sum(1 for row in rows if row["is_reliable"]),
            "seats_insufficient": sum(1 for row in rows if not row["is_reliable"]),
            "events_total": len(events),
            "window_trading_days": len(book.calendar),
            "pool_prior_event_count": int(priors["buy_outcome_events"]),
            "pool_prior_win_rate": _round(priors["win_rate"]),
            "pool_prior_close_premium": _round(priors["mean_close_premium"]),
            "data_usable": bool(availability["usable_for_profiling"]),
            "min_events": cfg.min_events,
        }
        return SeatProfileReport(
            as_of_date=as_of_date,
            window_start_date=window_start,
            config=asdict(cfg),
            profiles=rows,
            summary=summary,
            normalization=coverage,
            availability=availability,
            label_meta=label_meta,
            notes=dict(notes),
        )
    finally:
        if own_conn:
            connection.close()


def _disclaimer() -> str:
    from strategy.daban_hotmoney.seat_names import LABEL_DISCLAIMER

    return LABEL_DISCLAIMER


def _window_start(
    conn: sqlite3.Connection, as_of_date: str, window_days: int
) -> str | None:
    """滚动窗口起点：``kline_daily`` 日历上 ``as_of`` 之前最近 ``window_days`` 个交易日。"""
    if not data_availability(conn)["kline_daily"]["rows"]:
        return None
    rows = conn.execute(
        "SELECT DISTINCT trade_date FROM kline_daily WHERE trade_date <= ? "
        "ORDER BY trade_date DESC LIMIT ?",
        (as_of_date, max(1, int(window_days))),
    ).fetchall()
    dates = sorted(str(r["trade_date"]) for r in rows)
    return dates[0] if dates else None


def profiles_frame(report: SeatProfileReport) -> Any:
    """画像 → ``pandas.DataFrame``（嵌套字段序列化为 JSON 文本，便于落 CSV）。"""
    import json

    import pandas as pd

    records = []
    for row in report.profiles:
        item = dict(row)
        for key, value in item.items():
            if isinstance(value, (dict, list)):
                item[key] = json.dumps(value, ensure_ascii=False, sort_keys=True)
        records.append(item)
    return pd.DataFrame(records)


__all__ = [
    "MV_BUCKET_EDGES",
    "PRICE_BUCKET_EDGES",
    "REASON_INSUFFICIENT_SAMPLE",
    "REASON_NO_OUTCOME_SAMPLE",
    "REASON_NO_SCORE",
    "STYLE_DUMP",
    "STYLE_FIRST_BOARD",
    "STYLE_INSTITUTION",
    "STYLE_MIXED",
    "STYLE_NORTHBOUND",
    "STYLE_RELAY",
    "STYLE_UNCLEAR",
    "SeatEvent",
    "SeatProfileConfig",
    "SeatProfileReport",
    "SeatStats",
    "aggregate_stats",
    "beta_shrunk_rate",
    "board_position_label",
    "build_seat_events",
    "build_seat_profiles",
    "bucket_edges",
    "bucket_labels",
    "classify_style",
    "enrich_events",
    "mean_shrunk",
    "normal_mean_interval",
    "pooled_priors",
    "premium_component",
    "profile_row",
    "profiles_frame",
    "wilson_interval",
]
