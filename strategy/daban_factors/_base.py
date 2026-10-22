"""打板 P2 因子族公共基础设施（issues #28 / #29 / #30 / #31 共用）。

本模块只提供 **纯计算 + 只读取数** 的通用件，不含任何写库、调度、API 注册：

* :class:`FactorSpec` —— 因子注册表条目（公式、单位、数据源、缺失规则、PIT 说明）；
* 单位口径集中转换 :func:`convert_units` —— 因子层内部统一为
  **元 / 股 / 无量纲比率**，禁止各因子自行猜测单位；
* PIT 时间工具 —— :func:`parse_ts`、:func:`is_visible`、:func:`bar_is_visible`；
* 缺失 / 代理指示列 —— :func:`attach_indicators`（缺失恒为 ``NaN`` + ``*_is_missing=1``，
  **绝不 fillna(0)**；代理口径必须打 ``*_is_proxy=1``）；
* 只读 SQLite 连接 —— :func:`ro_connection`（``mode=ro``，绝不写仓库 ``data/*.db``）。

时间约定：所有 ``as_of_time`` / ``trade_date`` / ``bar_time`` 均为 **A 股本地时间
（Asia/Shanghai，UTC+8）的朴素日期时间字符串**，与 #22/#24/#25 现有链路一致；
带时区的 ISO 串会被规范化为本地墙钟时间后比较。
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

# ─── 因子注册表 ──────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class FactorSpec:
    """单个因子的口径说明（供文档、schema 校验与验收报告使用）。"""

    name: str
    group: str
    formula: str
    unit: str
    source: str
    missing_rule: str
    pit_note: str
    #: 该因子最早可计算的墙钟时刻（``"HH:MM"``）；日频盘后因子用 ``"post_close"``。
    visible_from: str = "post_close"
    is_proxy_capable: bool = False


def specs_as_dicts(specs: Sequence[FactorSpec]) -> list[dict[str, Any]]:
    """因子注册表的可序列化视图（issue 验收报告与文档校验用）。"""
    return [
        {
            "name": spec.name,
            "group": spec.group,
            "formula": spec.formula,
            "unit": spec.unit,
            "source": spec.source,
            "missing_rule": spec.missing_rule,
            "pit_note": spec.pit_note,
            "visible_from": spec.visible_from,
            "is_proxy_capable": spec.is_proxy_capable,
        }
        for spec in specs
    ]


# ─── 单位口径（集中转换，单测锁定）──────────────────────────────────

#: 源单位 → 内部规范单位的乘数。内部规范：金额=元、量=股、比率=无量纲小数。
UNIT_SCALE: dict[str, float] = {
    "yuan": 1.0,  # 元（#22 映射层已换算，moneyflow / 龙虎榜 / 市值列均为元）
    "wan_yuan": 1e4,  # 万元（Tushare 原始口径，接入方未换算时使用）
    "yi_yuan": 1e8,  # 亿元
    "hand": 100.0,  # 手 → 股（1 手 = 100 股）
    "share": 1.0,  # 股
    "pct": 0.01,  # 百分数 → 小数
    "ratio": 1.0,  # 已是无量纲小数
    "count": 1.0,  # 计数（户数 / 次数）
    "day": 1.0,  # 自然日
    "minute": 1.0,  # 交易分钟
    "price": 1.0,  # 元/股
    "ms": 1.0,  # 毫秒时间戳（不参与缩放，仅登记）
}

#: 因子层输出的单位标记（文档 + 单测断言用）。
OUTPUT_UNITS: tuple[str, ...] = (
    "元",
    "股",
    "无量纲",
    "次",
    "天",
    "分钟",
    "序数",
    "文本",
    "0/1",
)


def convert_units(frame: pd.DataFrame, mapping: Mapping[str, str]) -> pd.DataFrame:
    """按声明的源单位把金额/量/比率列集中换算为内部规范单位。

    ``mapping`` 为 ``{列名或 "单位标记": 源单位 token}``；未在 :data:`UNIT_SCALE`
    中登记的 token 直接抛 :class:`ValueError`（宁可乐死也不静默猜单位）。
    仅做乘法缩放，NaN / None 原样保留（缺失不许填 0）。
    """

    if frame is None or frame.empty:
        return pd.DataFrame(columns=list(frame.columns) if frame is not None else [])
    unknown = sorted({token for token in mapping.values() if token not in UNIT_SCALE})
    if unknown:
        raise ValueError(f"未登记的资金/量单位口径: {unknown}")
    result = frame.copy()
    for column, token in mapping.items():
        if column not in result.columns:
            continue
        scale = UNIT_SCALE[token]
        numeric = pd.to_numeric(result[column], errors="coerce")
        result[column] = numeric.astype("float64") * scale if scale != 1.0 else numeric
    return result


# ─── 数值 / 时间工具 ─────────────────────────────────────────────────


def num(value: Any) -> float | None:
    """把任意标量安全转 float；空值 / NaN / inf 一律视为缺失（返回 None）。"""
    if value is None:
        return None
    if isinstance(value, (str, bytes)):
        text = value.decode() if isinstance(value, bytes) else value
        text = text.strip()
        if not text or text.lower() in {"nan", "none", "null", "na", "-", "--"}:
            return None
        try:
            value = float(text.replace(",", ""))
        except ValueError:
            return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def cell(value: float | None) -> Any:
    """把 ``None`` 缺失规范成 ``numpy.nan``（DataFrame 里保持 NaN，不填 0）。"""
    return np.nan if value is None else value


def safe_div(numerator: Any, denominator: Any) -> float | None:
    """安全除法：任一为空、分母为 0 → None（缺失），不做 0 填充。"""
    top = num(numerator)
    bottom = num(denominator)
    if top is None or bottom is None or bottom == 0.0:
        return None
    return top / bottom


def is_missing(value: Any) -> bool:
    """因子值是否视为缺失（None / NaN / 空串）。"""
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    if isinstance(value, str) and not value.strip():
        return True
    return False


def text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    stripped = str(value).strip()
    return stripped or None


_CLOCK_CACHE: dict[str, datetime | None] = {}


def parse_ts(value: Any) -> datetime | None:
    """解析 ``YYYY-MM-DD[ /T][HH:MM[:SS]]``（可带时区）为 **朴素本地墙钟时间**。

    支持仅日期（视为 00:00）。解析失败返回 None（调用方按缺失处理，绝不"猜"）。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        naive = (
            value.astimezone(timezone.utc).replace(tzinfo=None)
            if value.tzinfo
            else value
        )
        return naive
    key = str(value).strip()
    if key in _CLOCK_CACHE:
        return _CLOCK_CACHE[key]
    parsed = _parse_ts_uncached(key)
    if len(_CLOCK_CACHE) < 4096:
        _CLOCK_CACHE[key] = parsed
    return parsed


def _parse_ts_uncached(key: str) -> datetime | None:
    if not key or key.lower() in {"nan", "none", "null", "na"}:
        return None
    normalized = key.replace("/", "-")
    if len(normalized) == 8 and normalized.isdigit():  # YYYYMMDD
        normalized = f"{normalized[:4]}-{normalized[4:6]}-{normalized[6:]}"
    if len(normalized) == 14 and normalized.isdigit():  # YYYYMMDDHHMMSS
        normalized = f"{normalized[:10]} {normalized[10:12]}:{normalized[12:14]}:00"
    candidate = normalized.replace("T", " ")
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    for fmt in (
        None,
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%H:%M:%S",
        "%H:%M",
    ):
        try:
            if fmt is None:
                parsed = datetime.fromisoformat(candidate)
            else:
                parsed = datetime.strptime(candidate, fmt)
        except ValueError:
            continue
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        if fmt in ("%H:%M:%S", "%H:%M"):
            # 纯时刻：用 1970-01-01 占位，调用方只比较 HH:MM 维度。
            return datetime(1970, 1, 1, parsed.hour, parsed.minute, parsed.second)
        return parsed
    return None


def date_key(value: Any) -> str:
    """把日期 / 日期时间规范成 ``YYYY-MM-DD``；无法解析返回空串。"""
    if isinstance(value, str) and len(value) >= 10 and value[4] == "-":
        return value[:10]
    parsed = parse_ts(value)
    return parsed.strftime("%Y-%m-%d") if parsed else ""


def clock_minutes(value: Any) -> float | None:
    """把 ``HH:MM:SS`` / ``HHMMSS`` 时刻转成当日墙钟分钟数（0..1439.98）。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if ":" in text:
        parts = text.split(":")
        if len(parts) < 2:
            return None
        hour, minute = parts[0], parts[1]
    elif len(text) >= 4 and text[:4].isdigit():
        hour, minute = text[:2], text[2:4]
    else:
        return None
    try:
        h, m = int(hour), int(minute)
    except ValueError:
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    seconds = 0
    if len(text.split(":")) == 3:
        try:
            seconds = int(text.split(":")[2])
        except ValueError:
            seconds = 0
    return float(h * 60 + m + seconds / 60.0)


def bar_wall_minutes(bar: datetime) -> float:
    """分钟 K 线 bar 的墙钟分钟数（当日 0 点起）。"""
    return float(bar.hour * 60 + bar.minute + bar.second / 60.0)


def session_minutes_from_clock(clock: Any) -> float | None:
    """连续竞价起点（09:30）起的交易分钟数；口径与 #24 一致。"""
    minutes = clock_minutes(clock)
    if minutes is None:
        return None
    if minutes <= 570.0:  # 09:30 之前（含集合竞价）记 0
        return 0.0
    if minutes <= 690.0:  # 11:30
        return minutes - 570.0
    if minutes < 780.0:  # 午休段钳到 120
        return 120.0
    return min(240.0, 120.0 + minutes - 780.0)


def is_visible(available_at: datetime | None, as_of_time: datetime | None) -> bool:
    """PIT 判定：数据的"实际可见时刻"必须不晚于决策时刻。

    任一方无法解析 → 视为 **不可见**（宁缺毋漏，未来公告 / 延迟披露都走这里）。
    """
    if available_at is None or as_of_time is None:
        return False
    return available_at <= as_of_time


# ─── 指示列装配 ──────────────────────────────────────────────────────


def attach_indicators(
    row: dict[str, Any],
    specs: Sequence[FactorSpec],
    *,
    proxy_flags: Mapping[str, int] | None = None,
    proxy_factors: Sequence[str] = (),
    text_factors: Sequence[str] = (),
) -> dict[str, Any]:
    """为单行补齐 ``*_is_missing`` / ``*_is_proxy`` 指示列。

    缺失值统一改写为 ``numpy.nan``，保证下游拿到的是显式 NaN 而不是 0 / None 混型。
    """
    flags = proxy_flags or {}
    for spec in specs:
        value = row.get(spec.name)
        if spec.name in text_factors:
            missing = text_or_none(value) is None
        else:
            missing = is_missing(value)
        row[f"{spec.name}_is_missing"] = int(bool(missing))
        if not missing and spec.name not in text_factors:
            row[spec.name] = (
                value if isinstance(value, (int, np.integer)) else float(value)
            )  # type: ignore[arg-type]
        elif missing:
            row[spec.name] = np.nan if spec.name not in text_factors else value
    for name in proxy_factors:
        row[f"{name}_is_proxy"] = int(bool(flags.get(name, 0)))
    return row


def output_columns(
    specs: Sequence[FactorSpec],
    *,
    raw_columns: Sequence[str],
    proxy_factors: Sequence[str] = (),
) -> list[str]:
    """完整输出列顺序：原始列 → 因子列 → 缺失指示列 → 代理指示列。"""
    columns = list(raw_columns)
    columns.extend(spec.name for spec in specs)
    columns.extend(f"{spec.name}_is_missing" for spec in specs)
    columns.extend(f"{name}_is_proxy" for name in proxy_factors)
    return columns


def project_columns(frame: pd.DataFrame, columns: Sequence[str]) -> pd.DataFrame:
    """补齐缺失列（NaN）并按注册表顺序投影，保证 schema 稳定。"""
    result = frame.copy()
    for column in columns:
        if column not in result.columns:
            result[column] = np.nan
    return result[list(columns)]


def empty_frame(columns: Sequence[str]) -> pd.DataFrame:
    return pd.DataFrame(columns=list(columns))


# ─── 截面统计 ────────────────────────────────────────────────────────


def percentile_rank(values: Mapping[str, float | None]) -> dict[str, float]:
    """把 ``{key: value}`` 转成 0..1 的截面百分位（并列取平均排名）。

    None / NaN 不参与排名，返回时该 key 不出现在结果里（由调用方标缺失）。
    """
    items = [
        (key, float(value)) for key, value in values.items() if not is_missing(value)
    ]
    if len(items) < 2:
        return {}
    items.sort(key=lambda item: item[1])
    ranks: dict[str, float] = {}
    idx = 0
    total = len(items)
    while idx < total:
        j = idx
        while j + 1 < total and items[j + 1][1] == items[idx][1]:
            j += 1
        avg_rank = (idx + j) / 2.0
        for k in range(idx, j + 1):
            ranks[items[k][0]] = avg_rank / (total - 1)
        idx = j + 1
    return ranks


def consecutive_count(flags: Iterable[bool | None]) -> int | None:
    """从序列尾部（最近一天）往前数连续为真的天数。

    首个元素即为最近一日；遇到 False 立即停止；遇到 None（缺数据）→ 返回 None
    （视为"无法判断"，不返回 0，避免把缺数当"没有连续流入"）。
    """
    count = 0
    seen_any = False
    for flag in flags:
        seen_any = True
        if flag is None:
            return None
        if not flag:
            return count
        count += 1
    return count if seen_any else None


# ─── 只读取数 ────────────────────────────────────────────────────────


def ro_connection(db_path: str | Path) -> sqlite3.Connection:
    """以 **只读** 模式打开 SQLite（绝不写仓库 ``data/*.db``）。"""
    path = Path(db_path)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def query_frame(
    conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()
) -> pd.DataFrame:
    """执行只读查询并返回 DataFrame；表不存在时返回空表（不抛，由覆盖率报告说明）。"""
    try:
        return pd.read_sql_query(sql, conn, params=list(params))
    except sqlite3.Error:
        return pd.DataFrame()


def chunked(items: Sequence[str], size: int = 400) -> Iterable[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


# ─── 质量 / 验收报告 ─────────────────────────────────────────────────


def factor_distribution(
    frame: pd.DataFrame, specs: Sequence[FactorSpec]
) -> dict[str, dict[str, float]]:
    """数值因子的样本分布（count/mean/median/p90/min/max）；文本因子跳过。"""
    distribution: dict[str, dict[str, float]] = {}
    for spec in specs:
        if spec.name not in frame.columns:
            continue
        series = pd.to_numeric(frame[spec.name], errors="coerce")
        clean = series.replace([np.inf, -np.inf], np.nan).dropna()
        if clean.empty:
            continue
        distribution[spec.name] = {
            "count": int(clean.shape[0]),
            "mean": float(clean.mean()),
            "median": float(clean.median()),
            "p90": float(clean.quantile(0.9)),
            "min": float(clean.min()),
            "max": float(clean.max()),
        }
    return distribution


def indicator_rates(
    frame: pd.DataFrame, specs: Sequence[FactorSpec], kind: str
) -> dict[str, float]:
    """``*_is_missing`` / ``*_is_proxy`` 的平均比例（无样本时按 1.0 / 0.0 兜底）。"""
    total = int(len(frame))
    rates: dict[str, float] = {}
    default = 1.0 if kind == "is_missing" else 0.0
    for spec in specs:
        column = f"{spec.name}_{kind}"
        if total and column in frame.columns:
            rates[spec.name] = float(
                pd.to_numeric(frame[column], errors="coerce").fillna(0.0).mean()
            )
        else:
            rates[spec.name] = default if kind == "is_missing" else 0.0
    return rates


def ic_report_placeholder(target: str = "单因子 IC / 分层收益") -> dict[str, str]:
    """IC / 分层 / 模型增量报告位 —— 线上 #22 表尚未回填时必须显式 PARTIAL。"""
    return {
        f"{target}_report": (
            "PARTIAL：#22 数据底座表线上为空（盘后调度才回填），"
            "IC / 分层收益 / 样本外增量需待数据回填后由 #27 基线产出，禁止提前填数"
        )
    }
