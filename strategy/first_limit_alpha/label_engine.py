"""打板可执行标签数据集引擎（issue #23）。

职责：从**已有** SQLite 底座（v3 ``limit_board_daily`` + ``kline_daily``，可选
``kline_minute`` 竞价代理）读取数据，按"决策时点 → 入场时点 → 出场时点"生成
多 horizon / 多任务标签，写入调用方传入的产物目录（不依赖 app container、
不访问网络、不改存储层）。

读取契约（v3，来自 issue #22，单位：**元**，缺失：**NULL**）::

    limit_board_daily(symbol, trade_date, limit_flag[U/D/Z], first_time, last_time,
                      open_times, limit_times, up_stat, fd_amount, amount,
                      limit_amount, turnover_ratio, float_mv, total_mv, close,
                      pct_chg, board_type, theme, limit_reason, source, synced_at)

用法
----
::

    python -m strategy.first_limit_alpha.label_engine \\
        --db data/market_kline.db --out tmp/first_limit_alpha/labels \\
        --mode all_limit_up --start 2023-01-01 --end 2026-06-30

或::

    from strategy.first_limit_alpha.label_engine import build_executable_label_dataset
    meta = build_executable_label_dataset("data/market_kline.db", "tmp/labels")
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import pandas as pd

from .feature_store import ensure_dir, write_dataframe, write_json
from .labeling import (
    LABEL_SOURCE_PROXY,
    LABEL_SOURCE_V3,
    LIMIT_FLAG_BROKEN,
    LIMIT_FLAG_DOWN,
    LIMIT_FLAG_UP,
    ExecutableLabelConfig,
    board_limit_pct,
    build_sample_label,
    label_field_docs,
    norm_trade_date,
)

# ---------------------------------------------------------------------------
# 样本模式
# ---------------------------------------------------------------------------

MODE_FIRST_BOARD = "first_board"
MODE_ALL_LIMIT_UP = "all_limit_up"
MODE_CONTINUATION = "continuation"
MODE_REVERSAL = "reversal"
MODE_ALL = "all"

SAMPLE_MODES = (MODE_FIRST_BOARD, MODE_ALL_LIMIT_UP, MODE_CONTINUATION, MODE_REVERSAL, MODE_ALL)

LABELS_FILE = "labels.csv"
LABELS_META_FILE = "labels_meta.json"
LABEL_SCHEMA_FILE = "label_schema.json"
LABEL_REPORT_FILE = "label_report.json"
LABELS_EMBARGO_FILE = "labels_embargo.json"

LIMIT_BOARD_READ_COLUMNS = (
    "symbol",
    "trade_date",
    "ts_code",
    "name",
    "industry",
    "limit_flag",
    "first_time",
    "last_time",
    "open_times",
    "limit_times",
    "up_stat",
    "fd_amount",
    "amount",
    "limit_amount",
    "turnover_ratio",
    "float_mv",
    "total_mv",
    "close",
    "pct_chg",
    "board_type",
    "theme",
    "limit_reason",
    "source",
)

DAILY_READ_COLUMNS = ("symbol", "trade_date", "open", "high", "low", "close", "volume", "amount")

MODE_DOC = {
    MODE_FIRST_BOARD: "首板样本：收盘封板且 limit_times=1（或前一日未封板）。",
    MODE_ALL_LIMIT_UP: "全部涨停样本：T 日收盘封板（limit_flag=U 或日线代理判定封板）。",
    MODE_CONTINUATION: "连板样本：limit_times>=2，覆盖 1进2/2进3/3进4/高位板。",
    MODE_REVERSAL: "反包 / 炸板修复样本：T 日炸板，或前 N 日内出现炸板/跌停后重新封板。",
    MODE_ALL: "并集：全部涨停 + 反包/炸板修复样本（含炸板未回封，供可成交性建模）。",
}


# ---------------------------------------------------------------------------
# 只读数据访问（直接 SQL，不改存储层）
# ---------------------------------------------------------------------------


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (table,)).fetchone()
    return row is not None


def _available_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _connect_ro(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def load_limit_board_rows(
    db_path: str | Path,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    symbols: Iterable[str] | None = None,
) -> pd.DataFrame:
    """读取 v3 打板主表；表不存在时返回空 DataFrame（调用方据此走日线代理）。"""
    conn = _connect_ro(db_path)
    try:
        if not _table_exists(conn, "limit_board_daily"):
            return pd.DataFrame(columns=list(LIMIT_BOARD_READ_COLUMNS))
        cols = _available_columns(conn, "limit_board_daily")
        read_cols = [c for c in LIMIT_BOARD_READ_COLUMNS if c in cols]
        query = f"SELECT {', '.join(read_cols)} FROM limit_board_daily WHERE 1=1"
        params: list[Any] = []
        if start_date and "trade_date" in cols:
            query += " AND trade_date >= ?"
            params.append(str(start_date))
        if end_date and "trade_date" in cols:
            query += " AND trade_date <= ?"
            params.append(str(end_date))
        if symbols:
            sym_list = [str(s) for s in symbols]
            query += f" AND symbol IN ({','.join('?' for _ in sym_list)})"
            params.extend(sym_list)
        query += " ORDER BY symbol ASC, trade_date ASC"
        frame = pd.read_sql_query(query, conn, params=params)
    finally:
        conn.close()
    if not frame.empty:
        frame["trade_date"] = frame["trade_date"].map(norm_trade_date)
        frame["symbol"] = frame["symbol"].astype(str)
    return frame


def load_daily_bars(
    db_path: str | Path,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    symbols: Iterable[str] | None = None,
) -> pd.DataFrame:
    """读取日线（含标签窗口所需的前置 1 根与未来 H 根）。"""
    conn = _connect_ro(db_path)
    try:
        if not _table_exists(conn, "kline_daily"):
            return pd.DataFrame(columns=list(DAILY_READ_COLUMNS))
        cols = _available_columns(conn, "kline_daily")
        read_cols = [c for c in DAILY_READ_COLUMNS if c in cols]
        query = f"SELECT {', '.join(read_cols)} FROM kline_daily WHERE 1=1"
        params: list[Any] = []
        if start_date:
            query += " AND trade_date >= ?"
            params.append(str(start_date))
        if end_date:
            query += " AND trade_date <= ?"
            params.append(str(end_date))
        if symbols:
            sym_list = [str(s) for s in symbols]
            query += f" AND symbol IN ({','.join('?' for _ in sym_list)})"
            params.extend(sym_list)
        query += " ORDER BY symbol ASC, trade_date ASC"
        frame = pd.read_sql_query(query, conn, params=params)
    finally:
        conn.close()
    if frame.empty:
        return frame
    for col in ("open", "high", "low", "close", "volume", "amount"):
        if col in frame.columns:
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame["trade_date"] = frame["trade_date"].map(norm_trade_date)
    frame["symbol"] = frame["symbol"].astype(str)
    return frame


def load_auction_prices(db_path: str | Path, *, symbols: Iterable[str] | None = None) -> dict[tuple[str, str], float]:
    """从 ``kline_minute`` 取每日 09:25/09:30 首根 bar 的开盘价作为竞价价代理。

    表不存在或无数据时返回空 dict —— 此时场景 B 自动退回 T+1 开盘价并置
    ``auction_proxy=1``（issue #23 风险项：竞价无权限时用开盘价代理）。
    """
    conn = _connect_ro(db_path)
    try:
        if not _table_exists(conn, "kline_minute"):
            return {}
        cols = _available_columns(conn, "kline_minute")
        needed = {"symbol", "trade_date", "open"}
        if not needed.issubset(cols):
            return {}
        time_col = "bar_time" if "bar_time" in cols else None
        if time_col is None:
            return {}
        query = f"""
            SELECT symbol, trade_date, {time_col} AS bar_time, open
            FROM kline_minute
            WHERE substr({time_col}, 12, 5) <= '09:30'
        """
        params: list[Any] = []
        if symbols:
            sym_list = [str(s) for s in symbols]
            query += f" AND symbol IN ({','.join('?' for _ in sym_list)})"
            params.extend(sym_list)
        query += " ORDER BY symbol ASC, trade_date ASC, bar_time ASC"
        frame = pd.read_sql_query(query, conn, params=params)
    finally:
        conn.close()
    out: dict[tuple[str, str], float] = {}
    if frame.empty:
        return out
    frame["trade_date"] = frame["trade_date"].map(norm_trade_date)
    for symbol, date, price in zip(frame["symbol"].astype(str), frame["trade_date"], pd.to_numeric(frame["open"], errors="coerce")):
        if date is None or pd.isna(price) or float(price) <= 0:
            continue
        key = (symbol, str(date))
        out.setdefault(key, float(price))  # 每日本地时间首根 bar 即集合竞价/开盘定价
    return out


# ---------------------------------------------------------------------------
# 样本选择（扩展到首板 / 连板 / 反包 / 炸板）
# ---------------------------------------------------------------------------


def _limit_rows_by_symbol(limit_frame: pd.DataFrame) -> dict[str, dict[str, dict[str, Any]]]:
    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    if limit_frame.empty:
        return grouped
    for record in limit_frame.to_dict("records"):
        symbol = str(record.get("symbol") or "")
        date = record.get("trade_date")
        if not symbol or not date:
            continue
        grouped.setdefault(symbol, {})[str(date)] = record
    return grouped


def _bars_by_symbol(bars_frame: pd.DataFrame) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    if bars_frame.empty:
        return grouped
    for record in bars_frame.to_dict("records"):
        symbol = str(record.get("symbol") or "")
        if not symbol or not record.get("trade_date"):
            continue
        grouped.setdefault(symbol, []).append(record)
    for symbol, rows in grouped.items():
        rows.sort(key=lambda r: str(r.get("trade_date")))
    return grouped


def _is_sealed(bar: Mapping[str, Any], prev_close: float | None, limit_pct: float, eps: float) -> bool:
    """日线口径的"收盘封板"代理判定（仅在 v3 行缺失时使用）。"""
    if prev_close is None or prev_close <= 0:
        return False
    close = bar.get("close")
    if close is None or pd.isna(close):
        return False
    limit_price = round(prev_close * (1.0 + limit_pct) + 1e-9, 2)
    return float(close) >= limit_price * (1.0 - eps)


def select_signal_dates(
    *,
    symbol: str,
    mode: str,
    limit_rows: Mapping[str, Mapping[str, Any]],
    bars: Sequence[Mapping[str, Any]],
    cfg: ExecutableLabelConfig,
    allow_daily_fallback: bool,
    stock_name: str = "",
) -> list[tuple[str, dict[str, Any] | None, str]]:
    """返回 ``[(trade_date, v3 行 or None, sample_mode), ...]``（升序）。

    ``sample_mode`` 可能多于一个模式命中，这里返回该样本**最具体**的模式，
    便于分层统计；``v3 行 is None`` 表示走日线代理（``label_source=daily_proxy_v2``）。
    """
    if mode not in SAMPLE_MODES:
        raise ValueError(f"未知样本模式: {mode}（可选: {', '.join(SAMPLE_MODES)}）")

    bar_dates = [str(b.get("trade_date")) for b in bars]
    date_index = {d: i for i, d in enumerate(bar_dates)}
    picked: list[tuple[str, dict[str, Any] | None, str]] = []
    seen: set[str] = set()

    ordered_dates = sorted(limit_rows.keys())
    for date in ordered_dates:
        row = limit_rows[date]
        flag = str(row.get("limit_flag") or "").strip().upper()
        sample_mode = _mode_for_flag(flag, row, limit_rows=limit_rows, date=date, cfg=cfg)
        if sample_mode is None:
            continue
        if not _mode_matches(mode, sample_mode):
            continue
        if date in date_index:
            picked.append((date, dict(row), sample_mode))
            seen.add(date)
        else:
            # 打板表有记录但日线缺该日（停牌/数据缺口）：仍保留样本（幸存者偏差规避），
            # 由标签层把不可得字段置 None。
            picked.append((date, dict(row), sample_mode))
            seen.add(date)

    if allow_daily_fallback:
        eps = cfg.price_epsilon
        limit_pct = board_limit_pct(symbol, stock_name)
        for i, bar in enumerate(bars):
            date = str(bar.get("trade_date"))
            if date in seen or date in limit_rows:
                continue
            if i < 1:
                continue
            prev_close = bars[i - 1].get("close")
            if prev_close is None or pd.isna(prev_close):
                continue
            if not _is_sealed(bar, float(prev_close), limit_pct, eps):
                continue
            # 日线代理只补"收盘封板"的涨停样本；首板/连板按前一日是否也封板区分
            detected = MODE_CONTINUATION if _prev_sealed(bars, i, limit_pct, eps) else MODE_FIRST_BOARD
            if not _mode_matches(mode, detected):
                continue
            picked.append((date, None, detected))

    picked.sort(key=lambda item: item[0])
    return picked


def _prev_sealed(bars: Sequence[Mapping[str, Any]], i: int, limit_pct: float, eps: float) -> bool:
    if i < 2:
        return False
    prev = bars[i - 1]
    prev_prev_close = bars[i - 2].get("close")
    if prev_prev_close is None or pd.isna(prev_prev_close):
        return False
    return _is_sealed(prev, float(prev_prev_close), limit_pct, eps)


def _mode_for_flag(
    flag: str,
    row: Mapping[str, Any],
    *,
    limit_rows: Mapping[str, Mapping[str, Any]],
    date: str,
    cfg: ExecutableLabelConfig,
) -> str | None:
    """按 ``limit_flag`` + ``limit_times`` 判定样本所属最具体模式。"""
    if flag == LIMIT_FLAG_DOWN:
        return None  # 跌停只作为未来标签输入，不作为打板样本
    times = row.get("limit_times")
    times = int(times) if times is not None and not pd.isna(times) else None
    prev_flags = _recent_flags(limit_rows, date, lookback=cfg.reversal_lookback)
    has_reversal_context = any(f in {LIMIT_FLAG_BROKEN, LIMIT_FLAG_DOWN} for f in prev_flags)
    if flag == LIMIT_FLAG_BROKEN:
        return MODE_REVERSAL
    if flag != LIMIT_FLAG_UP:
        return None
    if has_reversal_context:
        return MODE_REVERSAL
    if times is not None and times >= 2:
        return MODE_CONTINUATION
    return MODE_FIRST_BOARD


def _recent_flags(limit_rows: Mapping[str, Mapping[str, Any]], date: str, *, lookback: int) -> list[str]:
    """取 ``date`` 之前 ``lookback`` 个**已记录**交易日的 limit_flag（不含当日）。"""
    prior = [d for d in sorted(limit_rows.keys()) if d < date][-lookback:]
    flags: list[str] = []
    for d in prior:
        flag = str(limit_rows[d].get("limit_flag") or "").strip().upper()
        flags.append(flag)
    return flags


def _mode_matches(requested: str, detected: str) -> bool:
    if requested == MODE_ALL:
        return True
    if requested == MODE_ALL_LIMIT_UP:
        return detected in {MODE_FIRST_BOARD, MODE_CONTINUATION, MODE_REVERSAL}
    if requested == MODE_FIRST_BOARD:
        return detected == MODE_FIRST_BOARD
    if requested == MODE_CONTINUATION:
        return detected == MODE_CONTINUATION
    if requested == MODE_REVERSAL:
        return detected == MODE_REVERSAL
    return False


# ---------------------------------------------------------------------------
# 数据集构建
# ---------------------------------------------------------------------------


def default_column_order(cfg: ExecutableLabelConfig | None = None) -> list[str]:
    """用最小合成样本推导规范列顺序，保证空数据集也有稳定 schema。"""
    cfg = cfg or ExecutableLabelConfig()
    bars = [
        {"trade_date": "2026-01-05", "open": 10.0, "high": 10.2, "low": 9.9, "close": 10.0, "volume": 1.0, "amount": 10.0},
        {"trade_date": "2026-01-06", "open": 10.0, "high": 11.0, "low": 10.0, "close": 11.0, "volume": 1.0, "amount": 11.0},
    ]
    for k in range(1, cfg.max_horizon + 1):
        bars.append(
            {
                "trade_date": f"2026-01-{6 + k:02d}",
                "open": 11.0,
                "high": 11.5,
                "low": 10.5,
                "close": 11.2,
                "volume": 1.0,
                "amount": 11.0,
            }
        )
    signal = {"symbol": "600000", "name": "合成样本", "limit_flag": "U", "limit_times": 1, "open_times": 1}
    row = build_sample_label(signal=signal, bars=bars, idx=1, cfg=cfg, sample_mode=MODE_FIRST_BOARD)
    if row is None:  # pragma: no cover - 合成样本必然可用
        return []
    return list(row.keys())


def build_label_frame(
    *,
    limit_frame: pd.DataFrame,
    bars_frame: pd.DataFrame,
    cfg: ExecutableLabelConfig | None = None,
    mode: str = MODE_ALL_LIMIT_UP,
    symbols: Iterable[str] | None = None,
    name_map: Mapping[str, str] | None = None,
    auction_map: Mapping[tuple[str, str], float] | None = None,
    allow_daily_fallback: bool = True,
    require_min_history: int = 1,
) -> pd.DataFrame:
    """由已加载的数据帧生成标签表（纯内存，可单测；不碰数据库）。"""
    cfg = cfg or ExecutableLabelConfig()
    limit_by_symbol = _limit_rows_by_symbol(limit_frame)
    bars_by_symbol = _bars_by_symbol(bars_frame)
    wanted = None if symbols is None else {str(s) for s in symbols}
    universe = sorted(set(limit_by_symbol) | set(bars_by_symbol))
    if wanted is not None:
        universe = [s for s in universe if s in wanted]

    rows: list[dict[str, Any]] = []
    for symbol in universe:
        limit_rows = limit_by_symbol.get(symbol, {})
        bars = bars_by_symbol.get(symbol, [])
        if len(bars) <= require_min_history:
            continue
        if auction_map:
            for bar in bars:
                price = auction_map.get((symbol, str(bar.get("trade_date"))))
                if price is not None:
                    bar["auction_price"] = price
        for date, signal_row, sample_mode in select_signal_dates(
            symbol=symbol,
            mode=mode,
            limit_rows=limit_rows,
            bars=bars,
            cfg=cfg,
            allow_daily_fallback=allow_daily_fallback,
            stock_name=(name_map or {}).get(symbol, ""),
        ):
            idx = next((i for i, b in enumerate(bars) if str(b.get("trade_date")) == date), None)
            if idx is None or idx < require_min_history:
                continue
            signal = dict(signal_row or {})
            signal.setdefault("symbol", symbol)
            if not signal.get("name") and name_map:
                signal["name"] = name_map.get(symbol, "")
            prev_date = _prev_recorded_date(limit_rows, date)
            prev_signal = limit_rows.get(prev_date) if prev_date else None
            future_rows = {d: r for d, r in limit_rows.items() if d > date}
            payload = build_sample_label(
                signal=signal,
                bars=bars,
                idx=idx,
                cfg=cfg,
                future_limit_rows=future_rows,
                prev_signal=prev_signal,
                sample_mode=sample_mode,
                label_source_hint=LABEL_SOURCE_V3 if signal_row is not None else LABEL_SOURCE_PROXY,
            )
            if payload is None:
                continue
            rows.append(payload)

    if not rows:
        return pd.DataFrame(columns=default_column_order(cfg))
    frame = pd.DataFrame(rows)
    order = [c for c in default_column_order(cfg) if c in frame.columns]
    extra = [c for c in frame.columns if c not in order]
    return frame[[*order, *sorted(extra)]].reset_index(drop=True)


def _prev_recorded_date(limit_rows: Mapping[str, Any], date: str) -> str | None:
    prior = [d for d in sorted(limit_rows.keys()) if d < date]
    return prior[-1] if prior else None


def build_executable_label_dataset(
    db_path: str | Path,
    output_dir: str | Path | None = None,
    *,
    mode: str = MODE_ALL_LIMIT_UP,
    start_date: str | None = None,
    end_date: str | None = None,
    symbols: Iterable[str] | None = None,
    name_map: Mapping[str, str] | None = None,
    cfg: ExecutableLabelConfig | None = None,
    allow_daily_fallback: bool = True,
    use_auction_proxy: bool = True,
) -> dict[str, Any]:
    """从 SQLite 底座生成可执行标签数据集，并按需写产物文件。

    产物（写入调用方传入的 ``output_dir``，与既有 FirstLimit 样本目录同级）：

    * ``labels.csv``           —— 标签表（含时点、分层、可成交性、多 horizon 标签）
    * ``labels_meta.json``     —— 口径、horizon、embargo 要求、列白名单、覆盖率
    * ``label_schema.json``    —— 每个字段的公式 / 单位 / 缺失处理 / 数据源
    * ``label_report.json``    —— 标签分布报告（正样本率、缺失率、按年/月/板型分层）
    * ``labels_embargo.json``  —— 走查切分（walk-forward + embargo）示例
    """
    cfg = cfg or ExecutableLabelConfig()
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"数据库不存在: {db_path}")

    limit_frame = load_limit_board_rows(db_path, start_date=start_date, end_date=end_date, symbols=symbols)
    # 日线需要覆盖 [start - 1 根, end + H 根]，这里直接放宽窗口再按 signal 过滤
    bars_frame = load_daily_bars(db_path, start_date=None, end_date=None, symbols=symbols)
    auction_map = load_auction_prices(db_path, symbols=symbols) if use_auction_proxy else {}

    frame = build_label_frame(
        limit_frame=limit_frame,
        bars_frame=bars_frame,
        cfg=cfg,
        mode=mode,
        symbols=symbols,
        name_map=name_map,
        auction_map=auction_map,
        allow_daily_fallback=allow_daily_fallback,
    )

    report = label_distribution_report(frame, cfg=cfg)
    meta = build_dataset_meta(frame, cfg=cfg, mode=mode, start_date=start_date, end_date=end_date, report=report)
    calendar = sorted({str(d) for d in bars_frame["trade_date"].dropna().astype(str)}) if not bars_frame.empty else None
    meta["trading_calendar_days"] = int(len(calendar or []))
    meta["sources"] = {
        "db_path": str(db_path),
        "limit_board_rows_loaded": int(len(limit_frame)),
        "daily_bar_rows_loaded": int(len(bars_frame)),
        "auction_bars_loaded": int(len(auction_map)),
        "allow_daily_fallback": bool(allow_daily_fallback),
    }

    if output_dir is not None:
        out_dir = ensure_dir(Path(output_dir))
        paths = {
            "labels": str(write_dataframe(frame, out_dir / LABELS_FILE)),
            "label_schema": str(write_json(label_field_docs(), out_dir / LABEL_SCHEMA_FILE)),
            "label_report": str(write_json(report, out_dir / LABEL_REPORT_FILE)),
            "embargo": str(write_json(embargo_plan(frame, cfg, calendar=calendar), out_dir / LABELS_EMBARGO_FILE)),
        }
        meta["paths"] = paths
        write_json(meta, out_dir / LABELS_META_FILE)
    return meta


def build_dataset_meta(
    frame: pd.DataFrame,
    *,
    cfg: ExecutableLabelConfig,
    mode: str,
    start_date: str | None,
    end_date: str | None,
    report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """数据集元信息：口径、horizon、embargo、列白名单。"""
    from .labeling import FEATURE_SAFE_COLUMNS, LABEL_ONLY_COLUMNS

    dates = sorted({str(d) for d in frame["trade_date"].dropna().tolist()}) if not frame.empty else []
    return {
        "schema_version": "daban-labels-v1",
        "mode": mode,
        "mode_doc": MODE_DOC.get(mode, ""),
        "requested_range": {"start": start_date, "end": end_date},
        "signal_date_range": {"start": dates[0] if dates else None, "end": dates[-1] if dates else None},
        "sample_count": int(len(frame)),
        "symbol_count": int(frame["symbol"].nunique()) if not frame.empty else 0,
        "label_config": cfg.to_dict(),
        "horizons": {
            "continuation": cfg.continuation_horizon,
            "short": cfg.short_horizon,
            "long": cfg.long_horizon,
            "promotion": cfg.promotion_horizon,
            "max": cfg.max_horizon,
        },
        "embargo": {
            "required_embargo_days": cfg.required_embargo_days(),
            "rule": "相邻训练/验证切分之间必须至少隔离 required_embargo_days 个交易日，"
            "否则 3/5 日标签窗口会跨切分重叠造成泄漏。",
            "per_sample_column": "embargo_days / label_end_date",
        },
        "cost_assumptions": {
            "slippage_bps": cfg.slippage_bps,
            "fee_bps_per_side": cfg.fee_bps,
            "applies_to": "场景 B 计入滑点+双边手续费；场景 A 按涨停价排队成交，只扣双边手续费。",
        },
        "units": {"money": "元", "ratio": "小数（0.10 == 10%）", "missing": "NULL / 空"},
        "label_source_coverage": (
            frame["label_source"].value_counts().to_dict() if not frame.empty and "label_source" in frame else {}
        ),
        "feature_safe_columns": sorted(FEATURE_SAFE_COLUMNS),
        "label_only_columns": sorted(LABEL_ONLY_COLUMNS),
        "columns": list(frame.columns),
        "consumers": {
            "#24 特征矩阵": "只允许 join feature_safe_columns；调用 assert_no_label_in_features() 自检",
            "#26 可成交性回测": "entry_status_A / entry_unfilled_A / entry_unfilled_reason_A / entry_price_A / fill_probability / ret_1d_A / ret_3d_A / ret_5d_A",
            "#5 封板质量": "fill_probability / seal_ratio / entry_confidence_A",
        },
    }


# ---------------------------------------------------------------------------
# embargo / walk-forward
# ---------------------------------------------------------------------------


def required_embargo_days(cfg: ExecutableLabelConfig | None = None) -> int:
    """标签重叠所需的最小隔离交易日数。"""
    return (cfg or ExecutableLabelConfig()).required_embargo_days()


def embargo_plan(
    frame: pd.DataFrame,
    cfg: ExecutableLabelConfig | None = None,
    *,
    calendar: Sequence[str] | None = None,
) -> dict[str, Any]:
    """把整表按 decision date 汇总成 embargo 计算所需的最小信息 + 一份走查切分示例。"""
    cfg = cfg or ExecutableLabelConfig()
    if frame.empty:
        return {"required_embargo_days": cfg.required_embargo_days(), "dates": [], "folds": []}
    grouped = frame.groupby("trade_date")["label_end_date"].max().sort_index()
    cal = sorted({str(c) for c in calendar} - {"None", ""}) if calendar is not None else None
    return {
        "required_embargo_days": cfg.required_embargo_days(),
        "max_horizon_days": cfg.max_horizon,
        "embargo_buffer_days": cfg.embargo_buffer_days,
        "calendar_source": "daily_bars" if cal else "label_rows",
        "trading_calendar": cal or [],
        "dates": [
            {"trade_date": str(d), "label_end_date": str(end)} for d, end in zip(grouped.index, grouped.values)
        ],
        "folds_example": walk_forward_folds(
            frame,
            n_folds=3,
            test_days=20,
            min_train_days=60,
            embargo_days=cfg.required_embargo_days(),
            calendar=cal,
        ),
    }


def walk_forward_folds(
    frame: pd.DataFrame,
    *,
    n_folds: int = 3,
    test_days: int = 20,
    min_train_days: int = 60,
    embargo_days: int | None = None,
    date_col: str = "trade_date",
    end_col: str = "label_end_date",
    calendar: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """展开式 walk-forward 切分，自动插入 embargo 隔离带。

    对每个切点 ``cut``：

    * ``train = dates[:cut]``
    * 隔离带 ``embargo = dates[cut:first_test]`` —— 这些交易日的样本**必须丢弃**，
      因为它们的标签窗口延伸到测试区；
    * ``test = dates[first_test : first_test + test_days]``，其中 ``first_test`` 是
      第一个"晚于训练区最后一个样本 ``label_end_date`` 至少 ``embargo_days`` 个交易日"的日期。

    ``calendar`` 应传**完整市场交易日日历**（如 ``kline_daily`` 的全部 trade_date）：
    涨停事件只发生在部分交易日，用样本自身日期数间隔会低估真实交易日跨度。
    返回每折的日期清单（调用方据此过滤样本），并给出隔离带长度便于审计。
    """
    if frame.empty:
        return []
    cfg = ExecutableLabelConfig()
    embargo = int(embargo_days if embargo_days is not None else cfg.required_embargo_days())
    sub = frame[[date_col, end_col]].dropna()
    if sub.empty:
        return []
    latest_end: dict[str, str] = {}
    for d, e in zip(sub[date_col].astype(str), sub[end_col].astype(str)):
        latest_end[d] = max(latest_end.get(d, e), e)
    dates = sorted(latest_end)
    cal = sorted({str(c) for c in (calendar if calendar is not None else dates)})
    if not cal or cal[-1] < dates[-1]:
        cal = sorted(set(cal) | set(dates))
    if len(dates) < min_train_days + 1:
        return []

    folds: list[dict[str, Any]] = []
    first_cut = min_train_days
    last_cut = len(dates) - 1
    if last_cut < first_cut:
        return []
    step = max(1, (last_cut - first_cut) // max(1, n_folds - 1)) if n_folds > 1 else 1
    cuts = list(range(first_cut, last_cut + 1, step))[: max(1, n_folds)]
    for cut in cuts:
        train_dates = dates[:cut]
        train_label_end = max(latest_end[d] for d in train_dates)
        # 隔离带：训练区标签窗口尚未闭合的日期全部丢弃
        test_start = cut
        while test_start < len(dates):
            if _trading_gap(cal, train_label_end, dates[test_start]) >= embargo:
                break
            test_start += 1
        if test_start >= len(dates):
            continue
        test_dates = dates[test_start : test_start + test_days]
        if not test_dates:
            continue
        folds.append(
            {
                "fold": len(folds) + 1,
                "train_dates": train_dates,
                "embargo_dates": dates[cut:test_start],
                "test_dates": test_dates,
                "train_label_end_max": train_label_end,
                "embargo_gap_days": _trading_gap(cal, train_label_end, test_dates[0]),
                "embargo_required": embargo,
            }
        )
    return folds


def _trading_gap(dates: Sequence[str], start: str, end: str) -> int:
    """``start`` 与 ``end`` 之间隔着多少个已知交易日（``end<=start`` 时为负/0）。"""
    idx = {d: i for i, d in enumerate(dates)}
    if start in idx and end in idx:
        return idx[end] - idx[start]
    # 边界日期不在交易日历里时退化为顺序计数
    return sum(1 for d in dates if start < d <= end)


def assert_embargo_respected(
    train: pd.DataFrame,
    test: pd.DataFrame,
    *,
    embargo_days: int | None = None,
    date_col: str = "trade_date",
    end_col: str = "label_end_date",
    calendar: Sequence[str] | None = None,
) -> None:
    """校验切分没有标签重叠；违规直接抛 ``ValueError``（供 #24 训练前调用）。"""
    required = int(embargo_days if embargo_days is not None else required_embargo_days())
    if train.empty or test.empty:
        return
    train_end = str(train[end_col].astype(str).max())
    test_start = str(test[date_col].astype(str).min())
    overlap = train[train[end_col].astype(str) >= test_start]
    if not overlap.empty:
        raise ValueError(
            f"标签窗口跨切分重叠：train 的 label_end_date 最大 {train_end} >= test 最早决策日 {test_start}，"
            f"有 {len(overlap)} 条训练样本的未来窗口侵入测试区，需要至少 {required} 个交易日 embargo。"
        )
    train_dates = {str(d) for d in train[date_col].astype(str)}
    test_dates = {str(d) for d in test[date_col].astype(str)}
    if train_dates & test_dates:
        raise ValueError(f"决策日重叠：{sorted(train_dates & test_dates)[:5]}")
    cal = sorted({str(c) for c in (calendar if calendar is not None else train_dates | test_dates)} | train_dates | test_dates)
    gap = _trading_gap(cal, train_end, test_start)
    if gap < required:
        raise ValueError(
            f"embargo 不足：实际隔离 {gap} 个交易日（label 窗口闭合日 {train_end} → 测试首日 {test_start}），"
            f"需要至少 {required} 个交易日。"
        )


def drop_overlapping_labels(
    frame: pd.DataFrame,
    *,
    boundary_date: str,
    embargo_days: int | None = None,
    date_col: str = "trade_date",
    end_col: str = "label_end_date",
    calendar: Sequence[str] | None = None,
) -> dict[str, pd.DataFrame]:
    """按边界日切分，并把"标签窗口尚未闭合 / 落在隔离带内"的训练样本一并丢弃。

    ``train`` 只保留 ``label_end_date`` 距 ``boundary_date`` 至少 ``embargo_days`` 个
    交易日的样本，因此结果可直接通过 :func:`assert_embargo_respected` 校验；
    被隔离带吃掉的样本放在 ``embargoed`` 里供审计，不会静默消失。

    ``calendar`` 传完整市场交易日（建议用 ``kline_daily`` 的全部 trade_date）；
    不传则退化为用标签表里出现的日期估算，隔离带会被低估。
    """
    required = int(embargo_days if embargo_days is not None else required_embargo_days())
    boundary = str(boundary_date)
    if frame.empty:
        empty = frame.copy()
        return {"train": empty, "embargoed": empty.copy(), "test": empty.copy(), "embargo_required": required}
    before = frame[frame[date_col].astype(str) < boundary]
    after = frame[frame[date_col].astype(str) >= boundary]
    cal = sorted({str(c) for c in (calendar if calendar is not None else _frame_dates(frame, date_col, end_col))})
    if boundary not in cal:
        cal = sorted(set(cal) | {boundary})
    keep_mask = pd.Series(True, index=before.index)
    if not before.empty:
        keep_mask = before[end_col].astype(str).map(lambda d: _trading_gap(cal, d, boundary) >= required)
    crossing = before[~keep_mask.astype(bool)]
    kept_before = before[keep_mask.astype(bool)]
    return {
        "train": kept_before.reset_index(drop=True),
        "embargoed": crossing.reset_index(drop=True),
        "test": after.reset_index(drop=True),
        "embargo_required": required,
    }


def _frame_dates(frame: pd.DataFrame, date_col: str, end_col: str) -> set[str]:
    return set(frame[date_col].astype(str)) | set(frame[end_col].astype(str))


# ---------------------------------------------------------------------------
# 分布报告
# ---------------------------------------------------------------------------

_BINARY_LABELS = (
    "is_1_to_2",
    "is_2_to_3",
    "is_3_to_4",
    "is_continuation",
    "is_broken_board",
    "is_break_streak_next",
    "is_next_limit_down",
    "is_next_close_down",
    "is_big_loss",
    "is_strong_3d",
    "entry_unfilled_A",
    "entry_unfilled_B",
    "auction_proxy",
    "label_complete",
)
_RETURN_LABELS = (
    "ret_1d_A",
    "ret_3d_A",
    "ret_5d_A",
    "ret_1d_B",
    "ret_3d_B",
    "ret_5d_B",
    "fwd_ret_3d",
    "fwd_ret_5d",
    "next_open_premium",
    "next_close_return",
    "next_high_premium",
    "next_auction_premium",
    "max_drawdown_3d",
    "max_runup_3d",
    "max_adverse_offset_3d",
)


def label_distribution_report(
    frame: pd.DataFrame,
    *,
    cfg: ExecutableLabelConfig | None = None,
    min_group_samples: int | None = None,
) -> dict[str, Any]:
    """标签分布报告：正样本率、缺失率、按年/月/regime/板型分层 + 稀有标签最小样本量保护。"""
    cfg = cfg or ExecutableLabelConfig()
    floor = int(min_group_samples if min_group_samples is not None else 30)
    total = int(len(frame))
    out: dict[str, Any] = {
        "sample_count": total,
        "symbol_count": int(frame["symbol"].nunique()) if total else 0,
        "date_range": {
            "start": str(frame["trade_date"].min()) if total else None,
            "end": str(frame["trade_date"].max()) if total else None,
        },
        "min_group_samples": floor,
        "labels": {},
        "by_label_source": {},
        "by_year": {},
        "by_month": {},
        "by_board_type": {},
        "by_limit_shape": {},
        "by_float_mv_bucket": {},
        "by_market_regime": {},
        "rare_labels": [],
        "warnings": [],
    }
    if total == 0:
        out["warnings"].append("empty_dataset: 无任何样本，先确认 v3 打板表是否已同步（#22）。")
        return out

    for col in (*_BINARY_LABELS, *_RETURN_LABELS):
        if col not in frame.columns:
            continue
        series = pd.to_numeric(frame[col], errors="coerce")
        observed = int(series.notna().sum())
        entry = {
            "missing_count": total - observed,
            "missing_rate": round((total - observed) / total, 6),
        }
        if observed:
            if col in _BINARY_LABELS:
                entry["positive_count"] = int((series.dropna() > 0).sum())
                entry["positive_rate"] = round(float((series.dropna() > 0).mean()), 6)
            else:
                entry["mean"] = round(float(series.mean()), 6)
                entry["median"] = round(float(series.median()), 6)
                entry["p05"] = round(float(series.quantile(0.05)), 6)
                entry["p95"] = round(float(series.quantile(0.95)), 6)
            if col in _BINARY_LABELS and observed < floor:
                out["rare_labels"].append({"label": col, "observed_count": observed, "min_group_samples": floor})
        out["labels"][col] = entry

    for source, group in frame.groupby("label_source", dropna=False):
        out["by_label_source"][str(source)] = {
            "sample_count": int(len(group)),
            "share": round(len(group) / total, 6),
            "entry_unfilled_A_rate": _rate(group, "entry_unfilled_A"),
            "label_complete_rate": _rate(group, "label_complete"),
        }

    date_str = frame["trade_date"].astype(str)
    frame_with_period = frame.assign(_year=date_str.str.slice(0, 4), _month=date_str.str.slice(0, 7))
    for key, col in (("by_year", "_year"), ("by_month", "_month")):
        for value, group in frame_with_period.groupby(col, dropna=False):
            out[key][str(value)] = _group_stats(group, floor)
    for key, col in (
        ("by_board_type", "board_type"),
        ("by_limit_shape", "limit_shape"),
        ("by_float_mv_bucket", "float_mv_bucket"),
        ("by_market_regime", "market_regime"),
    ):
        if col not in frame.columns:
            continue
        for value, group in frame.groupby(col, dropna=False):
            label = str(value) if value is not None and not pd.isna(value) else "NA"
            out[key][label] = _group_stats(group, floor)

    proxy_share = float((frame["label_source"] == LABEL_SOURCE_PROXY).mean())
    if proxy_share > 0.5:
        out["warnings"].append(
            f"daily_proxy_dominant: {proxy_share:.1%} 样本来自日线代理，v3 打板表覆盖率不足，"
            "标签口径偏弱，待 #22 同步上线后需重跑验收。"
        )
    conflict = frame.get("flag_price_conflict")
    if conflict is not None and float(pd.to_numeric(conflict, errors="coerce").fillna(0).sum()) > 0:
        out["warnings"].append(
            f"flag_price_conflict_rows: {int(pd.to_numeric(conflict, errors='coerce').fillna(0).sum())} 条样本的 "
            "limit_flag 与收盘价矛盾，属数据质量问题，建议排查同步层。"
        )
    unfilled = _rate(frame, "entry_unfilled_A")
    if unfilled is not None and unfilled["rate"] > 0.6:
        out["warnings"].append(
            f"entry_unfilled_dominant: 场景 A 不可成交占比 {unfilled['rate']:.1%}，"
            "打板可成交样本偏少，分层模型需按 board_type 加权或放宽板型口径。"
        )
    years = sorted(k for k in out["by_year"] if k.isdigit())
    if len(years) < 3:
        out["warnings"].append(
            f"coverage_short_history: 仅覆盖 {len(years)} 个年份，issue #23 验收要求 >=3 年分布报告，"
            "需待 #22 打板表全量同步后重跑。"
        )
    return out


def _rate(group: pd.DataFrame, col: str) -> dict[str, Any] | None:
    if col not in group.columns or group.empty:
        return None
    series = pd.to_numeric(group[col], errors="coerce")
    observed = int(series.notna().sum())
    if observed == 0:
        return {"rate": None, "observed_count": 0}
    return {"rate": round(float((series > 0).mean()), 6), "observed_count": observed}


def _group_stats(group: pd.DataFrame, floor: int) -> dict[str, Any]:
    stats: dict[str, Any] = {"sample_count": int(len(group)), "below_min_samples": bool(len(group) < floor)}
    for col in ("is_1_to_2", "is_continuation", "is_broken_board", "is_big_loss", "is_next_limit_down", "entry_unfilled_A"):
        rate = _rate(group, col)
        if rate is not None:
            stats[col] = rate
    for col in ("ret_1d_B", "next_close_return"):
        if col in group.columns:
            series = pd.to_numeric(group[col], errors="coerce").dropna()
            if not series.empty:
                stats[f"{col}_mean"] = round(float(series.mean()), 6)
    return stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m strategy.first_limit_alpha.label_engine",
        description="生成打板可执行标签数据集（issue #23）。只读 SQLite 底座，不访问网络。",
    )
    parser.add_argument("--db", required=True, help="market_kline.db 路径（含 v3 limit_board_daily / kline_daily）")
    parser.add_argument("--out", required=True, help="产物目录（调用方传入，例如 tmp/first_limit_alpha/labels）")
    parser.add_argument("--mode", default=MODE_ALL_LIMIT_UP, choices=SAMPLE_MODES, help="样本范围")
    parser.add_argument("--start", default=None, help="信号日起（YYYY-MM-DD）")
    parser.add_argument("--end", default=None, help="信号日止（YYYY-MM-DD，含）")
    parser.add_argument("--symbols", default=None, help="逗号分隔的股票代码白名单（默认全市场）")
    parser.add_argument("--horizon-long", type=int, default=5, help="长 horizon（交易日）")
    parser.add_argument("--horizon-short", type=int, default=3, help="短 horizon（交易日）")
    parser.add_argument("--embargo-extra", type=int, default=1, help="embargo 额外隔离交易日数（默认 1）")
    parser.add_argument("--slippage-bps", type=float, default=10.0)
    parser.add_argument("--fee-bps", type=float, default=8.0)
    parser.add_argument("--no-daily-fallback", action="store_true", help="禁用日线代理降级（只用 v3）")
    parser.add_argument("--no-auction-proxy", action="store_true", help="禁用 kline_minute 竞价价代理")
    parser.add_argument("--print-meta", action="store_true", help="把 meta 打到 stdout")
    return parser


def config_from_args(args: argparse.Namespace) -> ExecutableLabelConfig:
    return ExecutableLabelConfig(
        short_horizon=int(args.horizon_short),
        long_horizon=int(args.horizon_long),
        slippage_bps=float(args.slippage_bps),
        fee_bps=float(args.fee_bps),
        embargo_buffer_days=int(args.embargo_extra),
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    cfg = config_from_args(args)
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()] if args.symbols else None
    meta = build_executable_label_dataset(
        args.db,
        args.out,
        mode=args.mode,
        start_date=args.start,
        end_date=args.end,
        symbols=symbols,
        cfg=cfg,
        allow_daily_fallback=not args.no_daily_fallback,
        use_auction_proxy=not args.no_auction_proxy,
    )
    if args.print_meta:
        print(json.dumps(meta, ensure_ascii=False, indent=2))
    else:
        print(
            f"[daban-labels] mode={meta['mode']} samples={meta['sample_count']} "
            f"symbols={meta['symbol_count']} range={meta['signal_date_range']['start']}~{meta['signal_date_range']['end']} "
            f"embargo>={meta['embargo']['required_embargo_days']}d -> {args.out}"
        )
    return 0


__all__ = [
    "ExecutableLabelConfig",
    "LABELS_FILE",
    "MODE_ALL",
    "MODE_ALL_LIMIT_UP",
    "MODE_CONTINUATION",
    "MODE_DOC",
    "MODE_FIRST_BOARD",
    "MODE_REVERSAL",
    "SAMPLE_MODES",
    "assert_embargo_respected",
    "build_arg_parser",
    "build_dataset_meta",
    "build_executable_label_dataset",
    "build_label_frame",
    "default_column_order",
    "drop_overlapping_labels",
    "embargo_plan",
    "label_distribution_report",
    "load_auction_prices",
    "load_daily_bars",
    "load_limit_board_rows",
    "main",
    "required_embargo_days",
    "select_signal_dates",
    "walk_forward_folds",
]
