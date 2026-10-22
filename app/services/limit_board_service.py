"""打板核心数据底座服务（issue #22）。

职责边界：

1. **能力探测** —— 启动/健康只读能力矩阵，逐接口标记
   ``available / denied / error / not_configured``，只输出安全代号。
2. **日频同步** —— 每个数据集独立尝试、失败隔离、指数退避；
   只允许收盘定型后写入，盘中绝不落库污染封账。
3. **口径集中** —— 字段映射与单位换算全部集中在本模块的 ``FIELD_MAPS``，
   金额统一为**元**；缺失值保持 ``None``，绝不填 0（因子层必须显式处理缺失）。
4. **降级路径** —— ``limit_list_d`` 无权限时用 ``kline_daily`` + ``stk_limit``
   自算涨停/跌停/炸板；``limit_step`` 无权限时由 ``limit_list_d`` 聚合天梯；
   ``hm_list`` 无权限时用龙虎榜营业部名做游资席位代理。

调用方只依赖本模块与 :class:`KlineSQLiteStore`，不直接碰 Tushare SDK。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import re
import time
from typing import Any, Callable, Iterable

import pandas as pd

from app.services.market_data_client import _normalize_symbol
from app.services.tushare_market_data_client import (
    CAPABILITY_DENIED,
    CAPABILITY_ERROR,
    CAPABILITY_NOT_CONFIGURED,
    LIMIT_BOARD_APIS,
    RESULT_EMPTY,
    RESULT_OK,
    LimitApiResult,
)
from app.services.time_utils import now_cn

log = logging.getLogger("limit_board")

# 数据集 → 状态表（enrichment_sync_state.dataset）里的键。
DATASET_LIMIT_BOARD = "limit_board"
DATASET_LIMIT_LADDER = "limit_ladder"
DATASET_LIMIT_THEME = "limit_board_theme"
DATASET_DRAGON_TIGER = "dragon_tiger"
DATASET_DRAGON_TIGER_SEAT = "dragon_tiger_seat"
DATASET_HOT_MONEY_SEAT = "hot_money_seat"
DATASET_HSGT_TOP10 = "hsgt_top10"

# 同步顺序：涨停板 → 题材 → 天梯 → 龙虎榜 → 席位 → 游资 → 北向十大。
DATASET_ORDER: tuple[str, ...] = (
    DATASET_LIMIT_BOARD,
    DATASET_LIMIT_THEME,
    DATASET_LIMIT_LADDER,
    DATASET_DRAGON_TIGER,
    DATASET_DRAGON_TIGER_SEAT,
    DATASET_HOT_MONEY_SEAT,
    DATASET_HSGT_TOP10,
)

# 数据集 → 主源接口（能力矩阵与降级判定共用这一份映射）。
DATASET_PRIMARY_API: dict[str, str] = {
    DATASET_LIMIT_BOARD: "limit_list_d",
    DATASET_LIMIT_THEME: "kpl_list",
    DATASET_LIMIT_LADDER: "limit_step",
    DATASET_DRAGON_TIGER: "top_list",
    DATASET_DRAGON_TIGER_SEAT: "top_inst",
    DATASET_HOT_MONEY_SEAT: "hm_list",
    DATASET_HSGT_TOP10: "hsgt_top10",
}

DATASET_LABELS: dict[str, str] = {
    DATASET_LIMIT_BOARD: "涨停/跌停/炸板",
    DATASET_LIMIT_THEME: "涨停题材/板型",
    DATASET_LIMIT_LADDER: "连板天梯",
    DATASET_DRAGON_TIGER: "龙虎榜",
    DATASET_DRAGON_TIGER_SEAT: "龙虎榜席位",
    DATASET_HOT_MONEY_SEAT: "游资席位",
    DATASET_HSGT_TOP10: "北向十大成交股",
}

# 降级路径说明（对外可见，避免"静默失败"）。
DEGRADATION_HINTS: dict[str, str] = {
    DATASET_LIMIT_BOARD: "self_calc:kline_daily+stk_limit",
    DATASET_LIMIT_THEME: "none",
    DATASET_LIMIT_LADDER: "aggregate:limit_list_d",
    DATASET_DRAGON_TIGER: "none",
    DATASET_DRAGON_TIGER_SEAT: "fallback:hm_detail",
    DATASET_HOT_MONEY_SEAT: "proxy:dragon_tiger_seat_daily",
    DATASET_HSGT_TOP10: "none",
}

# 终态：这些状态代表"本轮已给出结论"，不会因为下一轮自动重试再打外部接口。
# ``degraded`` 表示已按 issue 约定的降级路径产出可用数据（封板质量字段缺失），
# 主 K 线与其它数据集不受影响。
TERMINAL_STATUSES = frozenset({"success", "skipped", "not_configured", "degraded"})

_EOD_SETTLE_HHMM = "15:10"
_TICK_EPSILON = 0.005  # 价格最小变动 0.01 的一半，容差判等


# ── 归一化小工具（缺失一律 None，不填 0）───────────────────────


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "nat", "null"}:
        return None
    return text


def clean_number(value: Any, scale: float = 1.0) -> float | None:
    text = clean_text(value)
    if text is None:
        return None
    try:
        number = float(text.replace(",", ""))
    except ValueError:
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return round(number * scale, 6)


def clean_int(value: Any) -> int | None:
    number = clean_number(value)
    return None if number is None else int(round(number))


def clean_date(value: Any) -> str | None:
    text = clean_text(value)
    if text is None:
        return None
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    parsed = pd.to_datetime(text, errors="coerce")
    return None if pd.isna(parsed) else str(parsed.date())


def clean_clock(value: Any) -> str | None:
    """Tushare 封板时间 ``92500`` / ``145433`` → ``09:25:00``。"""

    text = clean_text(value)
    if text is None:
        return None
    if ":" in text:
        parts = text.split(":")
        if len(parts) == 3 and all(p.isdigit() for p in parts):
            hh, mm, ss = (int(p) for p in parts)
            if 0 <= hh < 24 and 0 <= mm < 60 and 0 <= ss < 60:
                return f"{hh:02d}:{mm:02d}:{ss:02d}"
        return None
    digits = re.sub(r"\D", "", text)
    if len(digits) == 6:
        hh, mm, ss = int(digits[:2]), int(digits[2:4]), int(digits[4:6])
        if 0 <= hh < 24 and 0 <= mm < 60 and 0 <= ss < 60:
            return f"{hh:02d}:{mm:02d}:{ss:02d}"
        return None
    if len(digits) == 5:
        return clean_clock(f"0{digits}")
    if len(digits) in (3, 4):  # HHMM
        padded = digits.zfill(4)
        return f"{padded[:2]}:{padded[2:4]}:00"
    return None


def clean_reason(value: Any) -> str:
    """上榜原因是主键的一部分：缺失必须归一为空串，不能落 NULL。"""
    return clean_text(value) or ""


def clean_symbol(value: Any) -> str | None:
    text = clean_text(value)
    if text is None:
        return None
    symbol = _normalize_symbol(text)
    return symbol or None


def clean_side(value: Any) -> str | None:
    """龙虎榜买卖方向：Tushare ``0``=买入、``1``=卖出；也接受英文/中文写法。"""

    text = (clean_text(value) or "").lower()
    if text in {"0", "buy", "b", "买", "买入"}:
        return "buy"
    if text in {"1", "sell", "s", "卖", "卖出"}:
        return "sell"
    return None


# ── 字段映射（集中口径：目标列 → (源列别名…, kind, scale)）──────

# kind: text / symbol / date / clock / number / int / side
FIELD_MAPS: dict[str, tuple[tuple[str, tuple[str, ...], str, float], ...]] = {
    # limit_list_d 的 fd_amount / amount / limit_amount / float_mv / total_mv
    # 实测（2026-10-21）已经是**元**，scale 保持 1.0；如上游改口径只需改这里。
    "limit_list_d": (
        ("symbol", ("ts_code", "symbol"), "symbol", 1.0),
        ("trade_date", ("trade_date",), "date", 1.0),
        ("ts_code", ("ts_code",), "text", 1.0),
        ("name", ("name",), "text", 1.0),
        ("industry", ("industry",), "text", 1.0),
        ("limit", ("limit", "limit_flag"), "text", 1.0),
        ("first_time", ("first_time", "fs_time"), "clock", 1.0),
        ("last_time", ("last_time",), "clock", 1.0),
        ("open_times", ("open_times",), "int", 1.0),
        ("limit_times", ("limit_times", "lu_times"), "int", 1.0),
        ("up_stat", ("up_stat", "limit_stat"), "text", 1.0),
        ("fd_amount", ("fd_amount", "fund"), "number", 1.0),
        ("amount", ("amount",), "number", 1.0),
        ("limit_amount", ("limit_amount",), "number", 1.0),
        ("turnover_ratio", ("turnover_ratio", "turnover_rate"), "number", 1.0),
        ("float_mv", ("float_mv", "circ_mv"), "number", 1.0),
        ("total_mv", ("total_mv",), "number", 1.0),
        ("close", ("close",), "number", 1.0),
        ("pct_chg", ("pct_chg", "pct_change"), "number", 1.0),
    ),
    "kpl_list": (
        ("symbol", ("ts_code", "symbol"), "symbol", 1.0),
        ("trade_date", ("trade_date",), "date", 1.0),
        ("board_type", ("status", "tag"), "text", 1.0),
        ("theme", ("theme",), "text", 1.0),
        ("limit_reason", ("lu_desc", "desc"), "text", 1.0),
    ),
    "top_list": (
        ("symbol", ("ts_code", "symbol"), "symbol", 1.0),
        ("trade_date", ("trade_date",), "date", 1.0),
        ("reason", ("reason",), "reason", 1.0),
        ("ts_code", ("ts_code",), "text", 1.0),
        ("name", ("name",), "text", 1.0),
        ("close", ("close",), "number", 1.0),
        ("pct_change", ("pct_change", "pct_chg"), "number", 1.0),
        ("turnover_rate", ("turnover_rate",), "number", 1.0),
        ("amount", ("amount",), "number", 1.0),
        ("l_buy", ("l_buy",), "number", 1.0),
        ("l_sell", ("l_sell",), "number", 1.0),
        ("l_amount", ("l_amount",), "number", 1.0),
        ("net_amount", ("net_amount",), "number", 1.0),
        ("net_rate", ("net_rate",), "number", 1.0),
        ("amount_rate", ("amount_rate",), "number", 1.0),
        ("float_values", ("float_values",), "number", 1.0),
    ),
    # top_inst / hm_detail 共用：金额单位元。
    "top_inst": (
        ("trade_date", ("trade_date",), "date", 1.0),
        ("symbol", ("ts_code", "symbol"), "symbol", 1.0),
        ("ts_code", ("ts_code",), "text", 1.0),
        ("stock_name", ("name", "stock_name"), "text", 1.0),
        ("seat_name", ("exalter", "seat_name", "name"), "text", 1.0),
        ("side", ("side",), "side", 1.0),
        ("reason", ("reason",), "reason", 1.0),
        ("buy_amount", ("buy", "buy_amount"), "number", 1.0),
        ("buy_rate", ("buy_rate",), "number", 1.0),
        ("sell_amount", ("sell", "sell_amount"), "number", 1.0),
        ("sell_rate", ("sell_rate",), "number", 1.0),
        ("net_amount", ("net_buy", "net_amount"), "number", 1.0),
    ),
    "hm_detail": (
        ("trade_date", ("trade_date",), "date", 1.0),
        ("symbol", ("ts_code", "symbol"), "symbol", 1.0),
        ("ts_code", ("ts_code",), "text", 1.0),
        ("stock_name", ("name", "stock_name"), "text", 1.0),
        ("seat_name", ("exalter", "seat_name", "org_name"), "text", 1.0),
        ("side", ("side",), "side", 1.0),
        ("reason", ("reason",), "reason", 1.0),
        ("buy_amount", ("buy", "buy_amount"), "number", 1.0),
        ("sell_amount", ("sell", "sell_amount"), "number", 1.0),
        ("net_amount", ("net_buy", "net_amount"), "number", 1.0),
    ),
    "hm_list": (
        ("seat_name", ("name", "seat_name", "org_name"), "text", 1.0),
        ("org_code", ("org_code", "ts_code"), "text", 1.0),
        ("category", ("category", "tag"), "text", 1.0),
        ("description", ("desc", "description"), "text", 1.0),
        ("orgs", ("orgs", "org_list"), "json", 1.0),
    ),
    "hsgt_top10": (
        ("trade_date", ("trade_date",), "date", 1.0),
        ("symbol", ("ts_code", "symbol"), "symbol", 1.0),
        ("ts_code", ("ts_code",), "text", 1.0),
        ("name", ("name",), "text", 1.0),
        ("market_type", ("market_type",), "text", 1.0),
        ("rank_no", ("rank", "rank_no"), "int", 1.0),
        ("close", ("close",), "number", 1.0),
        ("change", ("change",), "number", 1.0),
        ("amount", ("amount",), "number", 1.0),
        ("net_amount", ("net_amount",), "number", 1.0),
        ("buy_amount", ("buy", "buy_amount"), "number", 1.0),
        ("sell_amount", ("sell", "sell_amount"), "number", 1.0),
    ),
}

_KIND_CASTS: dict[str, Callable[[Any], Any]] = {
    "text": clean_text,
    "symbol": clean_symbol,
    "date": clean_date,
    "clock": clean_clock,
    "number": clean_number,
    "int": clean_int,
    "side": clean_side,
    "reason": clean_reason,
    "json": lambda v: v,
}


def normalize_rows(api: str, frame: pd.DataFrame | None) -> list[dict[str, Any]]:
    """把 Tushare 原始 DataFrame 按 ``FIELD_MAPS`` 归一为目标字段字典列表。

    - 缺失/无法解析 → ``None``（不是 0）；
    - 金额按映射表 scale 统一为元；
    - 没有 ``symbol``（或 ``trade_date``）的行直接丢弃，避免造出无法主键化的脏数据。
    """

    spec = FIELD_MAPS.get(api)
    if spec is None or frame is None or frame.empty:
        return []
    records: list[dict[str, Any]] = []
    columns = {str(c) for c in frame.columns}
    for raw in frame.to_dict("records"):
        row: dict[str, Any] = {}
        for target, sources, kind, scale in spec:
            value: Any = None
            for source in sources:
                if source in columns and raw.get(source) is not None:
                    value = raw.get(source)
                    if clean_text(value) is not None:
                        break
            cast = _KIND_CASTS.get(kind, clean_text)
            if kind == "number":
                row[target] = clean_number(value, scale)
            elif kind == "json":
                row[target] = _parse_json_list(value)
            else:
                row[target] = cast(value)
        if not row.get("symbol") and api not in {"hm_list"}:
            continue
        if api not in {"hm_list"} and not row.get("trade_date"):
            continue
        records.append(row)
    return records


def _parse_json_list(value: Any) -> list[str]:
    """``hm_list`` 的 ``orgs`` 是 JSON 数组字符串，元素可能是对象或字符串。"""

    text = clean_text(value)
    if text is None:
        return []
    try:
        loaded = json.loads(text)
    except (TypeError, ValueError):
        return [text]
    if isinstance(loaded, dict):
        loaded = [loaded]
    if not isinstance(loaded, list):
        return [str(loaded)] if str(loaded or "").strip() else []
    names: list[str] = []
    for item in loaded:
        if isinstance(item, dict):
            name = item.get("org_name") or item.get("name") or item.get("title")
            if name is None:
                continue
            text_name = clean_text(name)
        else:
            text_name = clean_text(item)
        if text_name:
            names.append(text_name)
    return names


def _sum_optional(left: float | None, right: float | None) -> float | None:
    if left is None and right is None:
        return None
    return round((left or 0.0) + (right or 0.0), 6)


def dedupe_rows(
    rows: Iterable[dict[str, Any]], keys: tuple[str, ...]
) -> list[dict[str, Any]]:
    """按业务主键去重（保留最后一条），并把同键的金额合并求和。

    ``top_inst`` 同一 ``(date, symbol, seat, side, reason)`` 会出现多条
    （典型是"机构专用"多个席位同名），这里聚合金额并记录 ``seat_count``。
    """

    merged: dict[tuple[Any, ...], dict[str, Any]] = {}
    sums = ("buy_amount", "sell_amount", "buy_rate", "sell_rate", "net_amount")
    for row in rows:
        key = tuple(row.get(k) for k in keys)
        if any(part is None for part in key):
            continue
        existing = merged.get(key)
        if existing is None:
            item = dict(row)
            item["seat_count"] = 1
            merged[key] = item
            continue
        for field in sums:
            if field in row or field in existing:
                existing[field] = _sum_optional(existing.get(field), row.get(field))
        existing["seat_count"] = int(existing.get("seat_count") or 1) + 1
        for field, value in row.items():
            if (
                field not in sums
                and existing.get(field) in (None, "")
                and value not in (None, "")
            ):
                existing[field] = value
    return list(merged.values())


# ── 自算降级（limit_list_d 无权限时）──────────────────────────


def derive_board_from_daily(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """用日线收盘 + 涨跌停价自算 U/D/Z。

    只能给出封板质量以外的字段：``first_time``/``fd_amount``/``open_times``
    等保持 ``None``，由能力矩阵标记为降级，因子层必须显式处理缺失。
    """

    out: list[dict[str, Any]] = []
    for row in rows:
        symbol = clean_symbol(row.get("symbol"))
        trade_date = clean_date(row.get("trade_date"))
        close = clean_number(row.get("close"))
        high = clean_number(row.get("high"))
        low = clean_number(row.get("low"))
        open_price = clean_number(row.get("open"))
        up_limit = clean_number(row.get("up_limit"))
        down_limit = clean_number(row.get("down_limit"))
        if not symbol or not trade_date or close is None:
            continue
        limit_flag: str | None = None
        if up_limit is not None and close + _TICK_EPSILON >= up_limit:
            limit_flag = "U"
        elif down_limit is not None and close - _TICK_EPSILON <= down_limit:
            limit_flag = "D"
        elif (
            up_limit is not None
            and high is not None
            and high + _TICK_EPSILON >= up_limit
        ):
            limit_flag = "Z"
        if limit_flag is None:
            continue
        board_type = None
        if (
            limit_flag == "U"
            and open_price is not None
            and high is not None
            and low is not None
            and up_limit is not None
            and abs(open_price - up_limit) <= _TICK_EPSILON
            and abs(high - up_limit) <= _TICK_EPSILON
            and abs(low - up_limit) <= _TICK_EPSILON
        ):
            board_type = "一字板"
        out.append(
            {
                "symbol": symbol,
                "trade_date": trade_date,
                "ts_code": None,
                "name": clean_text(row.get("name")),
                "industry": None,
                "limit": limit_flag,
                "first_time": None,
                "last_time": None,
                "open_times": None,
                "limit_times": None,
                "up_stat": None,
                "fd_amount": None,
                "amount": clean_number(row.get("amount")),
                "limit_amount": None,
                "turnover_ratio": clean_number(row.get("turnover_rate")),
                "float_mv": clean_number(row.get("circ_mv")),
                "total_mv": clean_number(row.get("total_mv")),
                "close": close,
                "pct_chg": None,
                "board_type": board_type,
                "theme": None,
                "limit_reason": None,
                "source": "self_calc.daily",
            }
        )
    return out


def build_ladder(
    board_rows: list[dict[str, Any]],
    *,
    prev_counts: dict[int, int] | None = None,
    source: str = "limit_list_d",
) -> list[dict[str, Any]]:
    """按连板高度聚合当日天梯，并在有前一日数据时计算晋级率。"""

    by_level: dict[int, list[dict[str, Any]]] = {}
    for row in board_rows:
        if row.get("limit") != "U":
            continue
        level = clean_int(row.get("limit_times")) or 1
        by_level.setdefault(level, []).append(row)
    prev = prev_counts or {}
    ladder: list[dict[str, Any]] = []
    trade_date = board_rows[0]["trade_date"] if board_rows else ""
    for level, items in sorted(by_level.items(), key=lambda kv: -kv[0]):
        fd_values = [clean_number(i.get("fd_amount")) for i in items]
        amounts = [clean_number(i.get("amount")) for i in items]
        opens = [clean_int(i.get("open_times")) for i in items]
        prev_count = prev.get(level - 1)
        ladder.append(
            {
                "trade_date": trade_date,
                "ladder_level": level,
                "stock_count": len(items),
                "symbols": [
                    {"symbol": i.get("symbol"), "name": i.get("name")}
                    for i in items[:200]
                ],
                "fd_amount_sum": (
                    round(sum(v for v in fd_values if v is not None), 6)
                    if any(v is not None for v in fd_values)
                    else None
                ),
                "amount_sum": (
                    round(sum(v for v in amounts if v is not None), 6)
                    if any(v is not None for v in amounts)
                    else None
                ),
                "avg_open_times": (
                    round(sum(v for v in opens if v is not None) / len(opens), 4)
                    if any(v is not None for v in opens)
                    else None
                ),
                "prev_level_count": prev_count,
                "promotion_ratio": (
                    round(len(items) / prev_count, 4) if prev_count else None
                ),
                "source": source,
            }
        )
    return ladder


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(str(raw).strip())
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(float(str(raw).strip()))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in {"0", "false", "no", "off", ""}


def _settle_time() -> dt.time:
    raw = os.getenv("ALPHA_LIMIT_BOARD_SETTLE_AFTER", _EOD_SETTLE_HHMM).strip()
    try:
        hour, minute = raw.split(":")
        return dt.time(int(hour), int(minute))
    except ValueError:
        return dt.time.fromisoformat(_EOD_SETTLE_HHMM)


class LimitBoardService:
    """打板数据底座的同步与查询入口（全部只依赖 store + client）。"""

    def __init__(
        self,
        *,
        store: Any,
        market_data_client: Any,
        enabled: bool | None = None,
    ) -> None:
        self.store = store
        self.client = market_data_client
        self._enabled = (
            _env_bool("ENABLE_LIMIT_BOARD_DATA", True)
            if enabled is None
            else bool(enabled)
        )
        self._retry_attempts = max(
            1, min(_env_int("ALPHA_LIMIT_BOARD_RETRY_ATTEMPTS", 3), 5)
        )
        self._retry_base_seconds = max(
            0.05, min(_env_float("ALPHA_LIMIT_BOARD_RETRY_BASE_SECONDS", 1.0), 10.0)
        )
        self._capability_ttl = max(
            60.0,
            min(
                _env_float("ALPHA_LIMIT_BOARD_CAPABILITY_TTL_SECONDS", 1800.0), 86400.0
            ),
        )
        self._capability: dict[str, Any] | None = None
        self._capability_lock = asyncio.Lock()

    # ── 基本状态 ──

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    @property
    def capability_snapshot(self) -> dict[str, Any] | None:
        return dict(self._capability) if self._capability else None

    # ── 能力矩阵 ──

    async def probe_capabilities(
        self, *, trade_date: str | None = None, force: bool = False
    ) -> dict[str, Any]:
        """探测各接口权限，输出能力矩阵（结果按 TTL 缓存）。"""

        if not self._enabled:
            return {"enabled": False, "reason": "limit_board_disabled", "apis": []}
        cached = self._capability
        if (
            not force
            and cached is not None
            and (time.monotonic() - float(cached.get("probed_at_mono") or 0.0))
            < self._capability_ttl
        ):
            return dict(cached)
        async with self._capability_lock:
            cached = self._capability
            if (
                not force
                and cached is not None
                and (time.monotonic() - float(cached.get("probed_at_mono") or 0.0))
                < self._capability_ttl
            ):
                return dict(cached)

            target = trade_date or await self._resolve_trade_date()
            ready = bool(getattr(self.client, "is_ready", False))
            if not ready or not target:
                matrix = [
                    {
                        "api": api,
                        "capability": CAPABILITY_NOT_CONFIGURED,
                        "reason": (
                            "tushare_not_configured"
                            if not ready
                            else "trade_date_unresolved"
                        ),
                        "sample_rows": 0,
                    }
                    for api in LIMIT_BOARD_APIS
                ]
            else:
                probe = getattr(self.client, "probe_limit_capabilities", None)
                if probe is None:  # 兼容最小 fake client
                    matrix = [
                        {
                            "api": api,
                            "capability": CAPABILITY_ERROR,
                            "reason": "probe_unsupported",
                            "sample_rows": 0,
                        }
                        for api in LIMIT_BOARD_APIS
                    ]
                else:
                    matrix = list(await probe(target, LIMIT_BOARD_APIS))
            payload = {
                "enabled": True,
                "trade_date": target,
                "probed_at": now_cn().isoformat(),
                "probed_at_mono": time.monotonic(),
                "degradations": dict(DEGRADATION_HINTS),
                "apis": matrix,
            }
            self._capability = dict(payload)
            return payload

    def capability_lookup(self) -> dict[str, dict[str, Any]]:
        cached = self._capability or {}
        return {
            str(item.get("api")): dict(item)
            for item in (cached.get("apis") or [])
            if isinstance(item, dict)
        }

    # ── 同步 ──

    async def sync_trade_date(
        self,
        trade_date: str | None = None,
        *,
        datasets: Iterable[str] | None = None,
        trigger_mode: str = "auto",
        force: bool = False,
    ) -> dict[str, Any]:
        """收盘后同步打板数据集；逐数据集隔离，绝不抛异常打断主 K 线封账。"""

        if not self._enabled:
            return {"success": False, "message": "打板数据同步已关闭", "datasets": []}
        target = trade_date or await self._resolve_trade_date()
        if not target:
            return {
                "success": False,
                "message": "无法确定交易日",
                "trade_date": "",
                "datasets": [],
            }
        if not force and not self.date_settled(target):
            return {
                "success": False,
                "skipped_intraday": True,
                "message": f"{target} 尚未收盘定型（{_settle_time().strftime('%H:%M')} 后），跳过打板数据",
                "trade_date": target,
                "datasets": [],
            }
        wanted = [
            d
            for d in (list(datasets) if datasets else list(DATASET_ORDER))
            if d in DATASET_ORDER
        ]
        capabilities = self.capability_lookup()
        results: list[dict[str, Any]] = []
        for dataset in wanted:
            try:
                results.append(
                    await self._sync_dataset(dataset, target, capabilities=capabilities)
                )
            except Exception as exc:  # noqa: BLE001 — 单数据集异常也必须隔离
                log.warning(
                    "limit board dataset %s failed: %s", dataset, type(exc).__name__
                )
                results.append(
                    {
                        "dataset": dataset,
                        "status": "failed",
                        "rows": 0,
                        "reason": f"api_error:{type(exc).__name__}",
                        "degraded": False,
                    }
                )
            await asyncio.sleep(0.05)  # 轻微间隔，避免瞬时并发触发限频
        success = all(item["status"] in TERMINAL_STATUSES for item in results)
        return {
            "success": success,
            "trade_date": target,
            "trigger_mode": trigger_mode,
            "datasets": results,
        }

    async def _sync_dataset(
        self,
        dataset: str,
        trade_date: str,
        *,
        capabilities: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        handler: Callable[[str, dict[str, dict[str, Any]]], Any] = {
            DATASET_LIMIT_BOARD: self._sync_limit_board,
            DATASET_LIMIT_THEME: self._sync_limit_theme,
            DATASET_LIMIT_LADDER: self._sync_limit_ladder,
            DATASET_DRAGON_TIGER: self._sync_dragon_tiger,
            DATASET_DRAGON_TIGER_SEAT: self._sync_dragon_tiger_seats,
            DATASET_HOT_MONEY_SEAT: self._sync_hot_money_seats,
            DATASET_HSGT_TOP10: self._sync_hsgt_top10,
        }[dataset]
        return await handler(trade_date, capabilities or self.capability_lookup())

    async def _call(
        self,
        api: str,
        trade_date: str,
        capabilities: dict[str, dict[str, Any]] | None = None,
    ) -> LimitApiResult:
        """调用接口；已知 denied/not_configured 且矩阵仍新鲜时直接短路。

        短路只为省掉重复的必然失败请求（Tushare 有分钟级限频）；矩阵过期后
        仍会真实请求一次，好让"后来开通的权限"能自动生效。
        """
        cached_status, cached_reason = self._capability_reason(capabilities or {}, api)
        if (
            cached_status in {CAPABILITY_DENIED, CAPABILITY_NOT_CONFIGURED}
            and self._matrix_fresh()
        ):
            return LimitApiResult(
                pd.DataFrame(), cached_status, cached_reason or cached_status
            )
        call = getattr(self.client, "call_limit_api", None)
        if call is None:
            if not getattr(self.client, "is_ready", False):
                return LimitApiResult(
                    pd.DataFrame(), CAPABILITY_NOT_CONFIGURED, "tushare_not_configured"
                )
            return LimitApiResult(pd.DataFrame(), CAPABILITY_ERROR, "call_unsupported")
        result: LimitApiResult = await call(api, trade_date=trade_date)
        # 只有 error 才重试（denied / not_configured 重试没有意义）。
        attempt = 0
        while result.status == CAPABILITY_ERROR and attempt < self._retry_attempts - 1:
            attempt += 1
            await asyncio.sleep(self._retry_base_seconds * (2 ** (attempt - 1)))
            result = await call(api, trade_date=trade_date)
        return result

    def _record(
        self,
        dataset: str,
        trade_date: str,
        *,
        status: str,
        rows: int,
        message: str,
    ) -> dict[str, Any]:
        updated_at = now_cn().isoformat()
        self.store.set_enrichment_state(
            dataset,
            trade_date=trade_date,
            status=status,
            row_count=int(rows),
            message=message[:300],
            updated_at=updated_at,
        )
        return {
            "dataset": dataset,
            "status": status,
            "rows": int(rows),
            "reason": message,
            "degraded": status in {"skipped", "degraded"}
            or message.startswith("degraded"),
        }

    def _matrix_fresh(self) -> bool:
        cached = self._capability
        if not cached:
            return False
        return (
            time.monotonic() - float(cached.get("probed_at_mono") or 0.0)
        ) < self._capability_ttl

    def _capability_reason(
        self, capabilities: dict[str, dict[str, Any]], api: str
    ) -> tuple[str, str]:
        """从能力矩阵里取某接口的状态与安全代号（用于降级说明与短路）。"""
        item = capabilities.get(api) or {}
        return str(item.get("capability") or ""), str(item.get("reason") or "")

    async def _sync_limit_board(
        self, trade_date: str, capabilities: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        api = "limit_list_d"
        result = await self._call(api, trade_date, capabilities)
        if result.status == RESULT_OK:
            rows = normalize_rows(api, result.frame)
            written = await asyncio.to_thread(
                self.store.upsert_limit_board_daily, rows, now_cn().isoformat()
            )
            return self._record(
                DATASET_LIMIT_BOARD,
                trade_date,
                status="success",
                rows=written,
                message="ok",
            )
        if result.status in {
            CAPABILITY_DENIED,
            CAPABILITY_NOT_CONFIGURED,
            CAPABILITY_ERROR,
        }:
            return await self._sync_limit_board_self_calc(
                trade_date,
                reason=str(result.reason or result.status),
            )
        if result.status == RESULT_EMPTY:
            # 接口可用但当日无数据（非交易日 / 无涨跌停），不算失败也不算降级。
            return self._record(
                DATASET_LIMIT_BOARD,
                trade_date,
                status="success",
                rows=0,
                message="empty_response",
            )
        return self._record(
            DATASET_LIMIT_BOARD,
            trade_date,
            status="failed",
            rows=0,
            message=f"unexpected_status({result.status})",
        )

    async def _sync_limit_board_self_calc(
        self, trade_date: str, *, reason: str
    ) -> dict[str, Any]:
        """降级自算：日线 + 涨跌停价判定 U/D/Z（封板质量字段保持缺失）。"""
        rows = await asyncio.to_thread(self.store.get_daily_with_limits, trade_date)
        derived = derive_board_from_daily(rows)
        written = 0
        if derived:
            written = await asyncio.to_thread(
                self.store.upsert_limit_board_daily, derived, now_cn().isoformat()
            )
        return self._record(
            DATASET_LIMIT_BOARD,
            trade_date,
            status="degraded" if written else "skipped",
            rows=written,
            message=f"degraded:self_calc({reason}) fd_amount/first_time/open_times unavailable",
        )

    async def _sync_limit_theme(
        self, trade_date: str, capabilities: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        api = "kpl_list"
        result = await self._call(api, trade_date, capabilities)
        if result.status != RESULT_OK:
            status = (
                "not_configured"
                if result.status == CAPABILITY_NOT_CONFIGURED
                else "skipped"
            )
            return self._record(
                DATASET_LIMIT_THEME,
                trade_date,
                status=status,
                rows=0,
                message=f"degraded:kpl_unavailable({result.reason or result.status})",
            )
        rows = normalize_rows(api, result.frame)
        updated = await asyncio.to_thread(
            self.store.update_limit_board_theme, rows, now_cn().isoformat()
        )
        return self._record(
            DATASET_LIMIT_THEME,
            trade_date,
            status="success",
            rows=updated,
            message="ok" if updated else "no_matching_board_rows",
        )

    async def _sync_limit_ladder(
        self, trade_date: str, capabilities: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        api = "limit_step"
        result = await self._call(api, trade_date, capabilities)
        if result.status == RESULT_OK:
            ladder = self._ladder_from_step(result.frame, trade_date)
            written = await asyncio.to_thread(
                self.store.upsert_limit_ladder, ladder, now_cn().isoformat()
            )
            return self._record(
                DATASET_LIMIT_LADDER,
                trade_date,
                status="success",
                rows=written,
                message="ok",
            )
        board_rows = await asyncio.to_thread(
            self.store.get_limit_board_daily, trade_date
        )
        if not board_rows:
            return self._record(
                DATASET_LIMIT_LADDER,
                trade_date,
                status="skipped",
                rows=0,
                message="degraded:no_board_rows",
            )
        prev_counts = await asyncio.to_thread(self._previous_level_counts, trade_date)
        # 天梯来源跟随涨停板数据的来源（接口原样 or 自算降级），
        # 而不是 limit_step 自身的权限状态，避免 Batch 1 误判数据可信度。
        board_sources = {str(row.get("source") or "") for row in board_rows}
        ladder_source = (
            "aggregate.self_calc"
            if board_sources and board_sources.issubset({"self_calc.daily"})
            else "aggregate.limit_list_d"
        )
        ladder = build_ladder(
            [{**row, "limit": row.get("limit_flag")} for row in board_rows],
            prev_counts=prev_counts,
            source=ladder_source,
        )
        written = await asyncio.to_thread(
            self.store.upsert_limit_ladder, ladder, now_cn().isoformat()
        )
        return self._record(
            DATASET_LIMIT_LADDER,
            trade_date,
            status="degraded" if written else "skipped",
            rows=written,
            message=f"degraded:aggregate_from_limit_board({result.reason or result.status})",
        )

    @staticmethod
    def _ladder_from_step(frame: pd.DataFrame, trade_date: str) -> list[dict[str, Any]]:
        """``limit_step`` 有权限时按 (连板数, 股票) 聚合为天梯。"""

        rows = frame.to_dict("records")
        by_level: dict[int, list[dict[str, Any]]] = {}
        for raw in rows:
            level = clean_int(
                raw.get("num_times") or raw.get("times") or raw.get("limit_times")
            )
            if level is None or level <= 0:
                continue
            by_level.setdefault(level, []).append(raw)
        target = clean_date(trade_date) or ""
        ladder = []
        for level, items in sorted(by_level.items(), key=lambda kv: -kv[0]):
            ladder.append(
                {
                    "trade_date": target,
                    "ladder_level": level,
                    "stock_count": len(items),
                    "symbols": [
                        {
                            "symbol": clean_symbol(i.get("ts_code")),
                            "name": clean_text(i.get("name")),
                        }
                        for i in items[:200]
                    ],
                    "fd_amount_sum": None,
                    "amount_sum": None,
                    "avg_open_times": None,
                    "prev_level_count": None,
                    "promotion_ratio": None,
                    "source": "limit_step",
                }
            )
        return ladder

    def _previous_level_counts(self, trade_date: str) -> dict[int, int]:
        prev_date = self._previous_board_trade_date(trade_date)
        if not prev_date:
            return {}
        return {
            int(item["ladder_level"]): int(item.get("stock_count") or 0)
            for item in self.store.get_limit_ladder(prev_date)
        }

    def _previous_board_trade_date(self, trade_date: str) -> str:
        """最近一个"已有涨停板数据"的早于 trade_date 的交易日（由 SQL 直接取）。"""

        getter = getattr(self.store, "previous_board_trade_date", None)
        if getter is None:
            return ""
        return str(getter(trade_date) or "")

    async def _sync_dragon_tiger(
        self, trade_date: str, capabilities: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        api = "top_list"
        result = await self._call(api, trade_date, capabilities)
        if result.status != RESULT_OK:
            status = (
                "not_configured"
                if result.status == CAPABILITY_NOT_CONFIGURED
                else "skipped"
            )
            return self._record(
                DATASET_DRAGON_TIGER,
                trade_date,
                status=status,
                rows=0,
                message=f"top_list_unavailable({result.reason or result.status})",
            )
        rows = normalize_rows(api, result.frame)
        written = await asyncio.to_thread(
            self.store.upsert_dragon_tiger, rows, now_cn().isoformat()
        )
        return self._record(
            DATASET_DRAGON_TIGER,
            trade_date,
            status="success",
            rows=written,
            message="ok",
        )

    async def _sync_dragon_tiger_seats(
        self, trade_date: str, capabilities: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        for api in ("top_inst", "hm_detail"):
            result = await self._call(api, trade_date, capabilities)
            if result.status != RESULT_OK:
                continue
            rows = dedupe_rows(
                normalize_rows(api, result.frame),
                ("trade_date", "symbol", "seat_name", "side", "reason"),
            )
            for row in rows:
                # 来源必须可追溯：top_inst 是权威口径，hm_detail 是降级口径。
                row["source"] = f"tushare.{api}"
            written = await asyncio.to_thread(
                self.store.upsert_dragon_tiger_seats, rows, now_cn().isoformat()
            )
            record = self._record(
                DATASET_DRAGON_TIGER_SEAT,
                trade_date,
                status="success",
                rows=written,
                message="ok" if api == "top_inst" else f"degraded:source={api}",
            )
            if api == "hm_detail":
                record["degraded"] = True
            return record
        status = (
            "not_configured"
            if getattr(self.client, "is_ready", False) is False
            else "skipped"
        )
        return self._record(
            DATASET_DRAGON_TIGER_SEAT,
            trade_date,
            status=status,
            rows=0,
            message="seat_sources_unavailable(top_inst,hm_detail)",
        )

    async def _sync_hot_money_seats(
        self, trade_date: str, capabilities: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        api = "hm_list"
        result = await self._call(api, trade_date, capabilities)
        if result.status == RESULT_OK:
            rows = normalize_rows(api, result.frame)
            written = await asyncio.to_thread(
                self.store.upsert_hot_money_seats, rows, now_cn().isoformat()
            )
            return self._record(
                DATASET_HOT_MONEY_SEAT,
                trade_date,
                status="success",
                rows=written,
                message="ok",
            )
        # 降级：用龙虎榜营业部名做游资席位代理（issue 明确的降级路径）。
        seats = await asyncio.to_thread(self.store.get_dragon_tiger_seats, trade_date)
        names: dict[str, dict[str, Any]] = {}
        for seat in seats:
            name = clean_text(seat.get("seat_name"))
            if not name:
                continue
            item = names.setdefault(
                name,
                {
                    "seat_name": name,
                    "org_code": "",
                    "category": None,
                    "description": None,
                    "orgs": [name],
                    "appear_count": 0,
                    "last_trade_date": trade_date,
                    "source": "proxy.top_inst",
                },
            )
            item["appear_count"] = int(item["appear_count"]) + 1
        written = await asyncio.to_thread(
            self.store.upsert_hot_money_seats,
            list(names.values()),
            now_cn().isoformat(),
        )
        return self._record(
            DATASET_HOT_MONEY_SEAT,
            trade_date,
            status="degraded" if written else "skipped",
            rows=written,
            message=f"degraded:seat_name_proxy({result.reason or result.status})",
        )

    async def _sync_hsgt_top10(
        self, trade_date: str, capabilities: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        api = "hsgt_top10"
        result = await self._call(api, trade_date, capabilities)
        if result.status != RESULT_OK:
            status = (
                "not_configured"
                if result.status == CAPABILITY_NOT_CONFIGURED
                else "skipped"
            )
            return self._record(
                DATASET_HSGT_TOP10,
                trade_date,
                status=status,
                rows=0,
                message=f"hsgt_top10_unavailable({result.reason or result.status})",
            )
        rows = normalize_rows(api, result.frame)
        written = await asyncio.to_thread(
            self.store.upsert_hsgt_top10, rows, now_cn().isoformat()
        )
        return self._record(
            DATASET_HSGT_TOP10, trade_date, status="success", rows=written, message="ok"
        )

    # ── 查询（只读 API 使用；全部同步方法，由路由放线程池执行）──

    def status_payload(
        self, capabilities: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        overview = self.store.get_enrichment_overview()
        boards = [item for item in overview if item["dataset"] in DATASET_ORDER]
        coverage_date = self.store.latest_board_trade_date()
        return {
            "enabled": self._enabled,
            "latest_trade_date": coverage_date,
            "coverage": (
                self.store.get_limit_board_coverage(coverage_date)
                if coverage_date
                else None
            ),
            "datasets": boards,
            "capabilities": capabilities or self.capability_snapshot or {"apis": []},
            "primary_apis": dict(DATASET_PRIMARY_API),
            "degradations": dict(DEGRADATION_HINTS),
        }

    @staticmethod
    def date_settled(trade_date: str, now: dt.datetime | None = None) -> bool:
        """日频打板数据只允许收盘定型后落库（默认 15:10，可用环境变量调整）。"""

        current = now or now_cn()
        text = str(trade_date or "").strip()
        if len(text) == 8 and text.isdigit():
            text = f"{text[:4]}-{text[4:6]}-{text[6:8]}"
        if text < current.date().isoformat():
            return True
        return current.timetz().replace(tzinfo=None) >= _settle_time()

    async def _resolve_trade_date(self) -> str:
        latest = await asyncio.to_thread(self.store.latest_board_trade_date)
        if latest:
            return latest
        resolver = getattr(self.client, "fetch_trade_days", None)
        if resolver is not None and getattr(self.client, "is_ready", False):
            try:
                frame = await resolver(min_days=1)
                dates = [clean_date(d) for d in list(frame.get("trade_date", []))]
                settled = [d for d in dates if d and self.date_settled(d)]
                if settled:
                    return max(settled)
            except Exception as exc:  # noqa: BLE001 — 日历兜底失败继续走本地
                log.debug("trade calendar resolve failed: %s", type(exc).__name__)
        today = now_cn().date()
        if self.date_settled(today.isoformat()):
            return today.isoformat()
        for _ in range(10):
            today -= dt.timedelta(days=1)
            if today.weekday() < 5 and self.date_settled(today.isoformat()):
                return today.isoformat()
        return ""
