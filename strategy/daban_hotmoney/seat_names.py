"""席位名称标准化、别名合并与可审计游资名录（issue #34 工程要求）。

为什么需要这一层
----------------
``dragon_tiger_seat_daily.seat_name`` 是券商营业部**原文长文本**，同一家营业部
会有多种写法（全称 / 简称 / 是否带"证券营业部"后缀 / 是否带"股份有限公司"），
直接按原文分组会把同一席位拆成多个样本，画像全部失真。

本模块只做**机械可复现**的标准化，并记录每条规则的可审计来源：

* ``method="special"``  交易所固定席位文本（机构专用 / 沪股通 / 深股通）
* ``method="alias"``    券商简称映射（公开券商名录的机构名缩写，纯字符串规则）
* ``method="pattern"``  通用句式规则（``XX证券…营业部`` → ``XX…营业部``）
* ``method="alias_map"`` 人工别名合并表 :data:`SEAT_ALIAS_RULES`
* ``method="unmatched"`` 无法判定 → 不进入画像分组（计入覆盖率分母）

**不做**的事：不宣称"某营业部 = 某游资个人"。游资人名标签只能通过
:data:`HOT_MONEY_LABELS` 人工名录显式维护，每条必须带 ``source``；名录未覆盖
的席位一律 ``seat_label=None``（issue 原文："不明确的不标注"）。

隐私与合规
----------
营业部与席位名称是交易所公开披露信息；本模块**不接受、不存储**任何自然人
身份、账户、资金账号信息，名录字段里也不允许出现这类内容（``source`` 只写
公开出处与判断依据）。标签属于人工主观判断，输出时必须带上
:data:`LABEL_DISCLAIMER` 免责声明。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable

#: 游资标签免责声明（任何对外展示 / API 返回都必须原样带出）。
LABEL_DISCLAIMER = (
    "游资席位标签为基于公开龙虎榜营业部名称的人工判断，不构成对任何主体身份、"
    "持仓或交易行为的认定，仅供研究，不得作为投资建议或宣传口径。"
)

SEAT_METHOD_SPECIAL = "special"
SEAT_METHOD_ALIAS = "alias"
SEAT_METHOD_PATTERN = "pattern"
SEAT_METHOD_ALIAS_MAP = "alias_map"
SEAT_METHOD_UNMATCHED = "unmatched"
SEAT_METHOD_EMPTY = "empty"

#: 已"标准化成功"的口径（覆盖率分子）。
STANDARDIZED_METHODS = frozenset(
    {SEAT_METHOD_SPECIAL, SEAT_METHOD_ALIAS, SEAT_METHOD_PATTERN, SEAT_METHOD_ALIAS_MAP}
)

KIND_INSTITUTION = "institution"
KIND_NORTHBOUND = "northbound"
KIND_BROKER_BRANCH = "broker_branch"
KIND_TRADE_UNIT = "trade_unit"
KIND_UNKNOWN = "unknown"

# 公司全称噪声：这些片段对区分席位没有任何信息量，统一剥掉。
_NOISE_TOKENS: tuple[str, ...] = (
    "（已注销）",
    "(已注销)",
    "（更名）",
    "(更名)",
    "（原）",
    "(原)",
    "股份有限公司",
    "有限责任公司",
    "有限公司",
)

# 引号 / 空白 / 全角符号先统一（NFKC 会把全角字母数字转半角）。
_STRIPPED_CHARS = re.compile(r"[\s\"'“”‘’,，、]+")

_BRANCH_SUFFIX = re.compile(r"(证券营业部|证券营业部分部|营业部|营业一部|营业二部)$")

_TRADE_UNIT_PATTERN = re.compile(r"(交易单元|席位)")

# 交易所 / 券商披露里的固定文本 → 归一化标准名（不含任何主体身份判断）。
_SPECIAL_TEXTS: tuple[tuple[str, str, str], ...] = (
    ("机构专用", "机构专用", KIND_INSTITUTION),
    ("机构", "机构专用", KIND_INSTITUTION),
    ("沪股通专用", "沪股通", KIND_NORTHBOUND),
    ("沪股通", "沪股通", KIND_NORTHBOUND),
    ("深股通专用", "深股通", KIND_NORTHBOUND),
    ("深股通", "深股通", KIND_NORTHBOUND),
)

#: 券商简称映射表：机构名缩写（纯字符串规则，来源为公开券商名录）。
#: 匹配时按 key 长度**从长到短**，避免"中信建投"被"中信"抢先匹配。
BROKER_SHORT_ALIASES: dict[str, str] = {
    "国泰君安证券": "国泰君安",
    "国泰君安期货": "国泰君安期货",
    "中信建投证券": "中信建投",
    "中信证券": "中信",
    "华泰联合证券": "华泰联合",
    "华泰证券": "华泰",
    "招商证券": "招商",
    "广发证券": "广发",
    "东方财富证券": "东方财富",
    "东方财富拉萨": "东方财富",
    "平安证券": "平安",
    "国信证券": "国信",
    "光大证券": "光大",
    "海通证券": "海通",
    "申万宏源西部证券": "申万宏源西部",
    "申万宏源证券": "申万宏源",
    "中国银河证券": "银河",
    "银河证券": "银河",
    "中金财富证券": "中金财富",
    "中国国际金融": "中金",
    "兴业证券": "兴业",
    "方正证券": "方正",
    "天风证券": "天风",
    "东兴证券": "东兴",
    "红塔证券": "红塔",
    "太平洋证券": "太平洋",
    "浙商证券": "浙商",
    "财通证券": "财通",
    "华鑫证券": "华鑫",
    "西南证券": "西南",
    "西部证券": "西部",
    "国金证券": "国金",
    "东海证券": "东海",
    "东吴证券": "东吴",
    "长江证券": "长江",
    "中泰证券": "中泰",
    "东北证券": "东北",
    "南京证券": "南京",
    "国海证券": "国海",
    "中原证券": "中原",
    "开源证券": "开源",
    "信达证券": "信达",
    "首创证券": "首创",
    "万联证券": "万联",
    "粤开证券": "粤开",
    "国联证券": "国联",
    "江海证券": "江海",
    "国元证券": "国元",
    "华宝证券": "华宝",
    "恒泰证券": "恒泰",
}

#: 按 key 长度降序的匹配序列（确定性，不受 dict 顺序影响）。
_ALIAS_KEYS_SORTED: tuple[str, ...] = tuple(
    sorted(
        (key for key, short in BROKER_SHORT_ALIASES.items() if key and short),
        key=lambda text: (-len(text), text),
    )
)


@dataclass(frozen=True, slots=True)
class SeatAliasRule:
    """人工别名合并规则：把已标准化的 variants 合并到同一标准席位名。"""

    pattern: str
    standard_name: str
    source: str

    def __post_init__(self) -> None:
        if not self.pattern.strip():
            raise ValueError("SeatAliasRule.pattern 不能为空")
        if not self.standard_name.strip():
            raise ValueError("SeatAliasRule.standard_name 不能为空")
        if not self.source.strip():
            raise ValueError("SeatAliasRule.source 必填（可审计来源）")

    def compiled(self) -> re.Pattern[str]:
        return re.compile(self.pattern)


@dataclass(frozen=True, slots=True)
class HotMoneyLabel:
    """人工维护的游资名录条目。``source`` 为空直接拒绝构造（可审计要求）。"""

    standard_name: str
    label: str
    style_hint: str | None
    source: str
    curated_by: str
    curated_at: str
    confidence: float = 0.5

    def __post_init__(self) -> None:
        for attr in ("standard_name", "label", "source", "curated_by", "curated_at"):
            if not str(getattr(self, attr)).strip():
                raise ValueError(f"HotMoneyLabel.{attr} 必填（游资标签来源必须可审计）")
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("HotMoneyLabel.confidence 必须在 [0, 1]")


#: 席位别名合并表（人工维护）。
#: **只允许合并"同一营业部的不同文本写法"**，禁止把两家不同营业部合并成
#: 一个画像样本（那属于游资身份判断，必须走 :data:`HOT_MONEY_LABELS` 名录并
#: 附可审计来源）。这里默认放两条纯文本变体规则作为写法示范。
SEAT_ALIAS_RULES: tuple[SeatAliasRule, ...] = (
    SeatAliasRule(
        pattern=r"^中信北京总部$",
        standard_name="中信北京总部营业部",
        source="公开龙虎榜文本变体：同一席位省略了“证券营业部”后缀（纯文本合并，无身份判断）",
    ),
    SeatAliasRule(
        pattern=r"^华泰深圳益田路荣超商务中心$",
        standard_name="华泰深圳益田路荣超商务中心营业部",
        source="公开龙虎榜文本变体：同一席位省略了“证券营业部”后缀（纯文本合并，无身份判断）",
    ),
)

#: 游资人名名录。默认**为空**：任何未经人工审计的席位都不标注（issue 要求）。
#: 主 agent 若要上线标签，请把条目追加到这里（或从同名 CSV 导入后传入
#: :func:`seat_label` 的 ``labels`` 参数），每条都要写清 ``source``。
HOT_MONEY_LABELS: tuple[HotMoneyLabel, ...] = ()


@dataclass(frozen=True, slots=True)
class SeatName:
    """席位名称标准化结果。"""

    raw: str
    standard_name: str | None
    method: str
    kind: str
    broker: str | None = None
    branch: str | None = None
    noise_removed: tuple[str, ...] = field(default=())

    @property
    def is_standardized(self) -> bool:
        return self.method in STANDARDIZED_METHODS and bool(self.standard_name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seat_name_raw": self.raw,
            "seat_name_std": self.standard_name,
            "seat_name_method": self.method,
            "seat_kind": self.kind,
            "seat_broker": self.broker,
            "seat_branch": self.branch,
            "seat_noise_removed": "|".join(self.noise_removed),
            "seat_name_is_standardized": int(self.is_standardized),
        }


def _prepare(value: Any) -> tuple[str, tuple[str, ...]]:
    raw = str(value).strip() if value is not None else ""
    if not raw:
        return "", ()
    text = unicodedata.normalize("NFKC", raw)
    text = _STRIPPED_CHARS.sub("", text)
    removed: list[str] = []
    for token in _NOISE_TOKENS:
        if token in text:
            text = text.replace(token, "")
            removed.append(token)
    return text, tuple(removed)


def _match_broker(text: str) -> tuple[str, str] | None:
    """返回 ``(broker_short, remainder)``；按最长 key 优先匹配。"""
    for key in _ALIAS_KEYS_SORTED:
        if text.startswith(key):
            return BROKER_SHORT_ALIASES[key], text[len(key) :]
    return None


def _generic_broker(text: str) -> tuple[str, str] | None:
    """通用句式：``XX证券…`` / ``XX期货…`` 前缀 + 营业部/交易单元后缀。"""
    match = re.match(r"^(?P<br>.{2,12}?(证券|期货|资本|投资))(?P<rest>.*)$", text)
    if not match:
        return None
    broker = match.group("br")
    remainder = match.group("rest")
    _BRANCH_RE = re.compile(r"(证券营业部|营业部)$")
    if not remainder:
        return None
    tail = _BRANCH_RE.sub("营业部", remainder)
    if not (tail.endswith(("营业部", "分公司", "总部", "交易单元", "席位"))):
        return None
    return broker, tail


def apply_alias_rules(
    standard_name: str,
    alias_rules: Iterable[SeatAliasRule] = SEAT_ALIAS_RULES,
) -> tuple[str, str | None]:
    """对已标准化名称应用别名合并；返回 ``(标准名, 命中的 pattern 或 None)``。"""
    for rule in alias_rules:
        if rule.compiled().search(standard_name):
            return rule.standard_name, rule.pattern
    return standard_name, None


def normalize_seat_name(
    raw: Any,
    *,
    alias_rules: Iterable[SeatAliasRule] = SEAT_ALIAS_RULES,
) -> SeatName:
    """把一个席位原文名标准化为 :class:`SeatName`。

    * 交易所固定文本（机构专用 / 沪股通 / 深股通）优先级最高；
    * 其次券商简称映射；再退到通用句式规则；
    * 最后应用人工别名合并表（命中记 ``method="alias_map"``）。
    无法判定时 ``standard_name=None``、``method="unmatched"``，
    调用方必须把它当作缺失（不计入画像分组，但计入覆盖率分母）。
    """
    text, noise_removed = _prepare(raw)
    if not text:
        return SeatName(
            raw=str(raw or ""),
            standard_name=None,
            method=SEAT_METHOD_EMPTY,
            kind=KIND_UNKNOWN,
            noise_removed=noise_removed,
        )

    for token, standard, kind in _SPECIAL_TEXTS:
        if token in text:
            merged, rule_pattern = apply_alias_rules(standard, alias_rules)
            method = SEAT_METHOD_ALIAS_MAP if rule_pattern else SEAT_METHOD_SPECIAL
            return SeatName(
                raw=str(raw),
                standard_name=merged,
                method=method,
                kind=kind,
                broker=None,
                branch=None,
                noise_removed=noise_removed,
            )

    broker: str | None = None
    branch: str | None = None
    method = SEAT_METHOD_UNMATCHED
    kind = KIND_UNKNOWN

    matched = _match_broker(text)
    if matched is not None:
        broker, remainder = matched
        tail = _BRANCH_SUFFIX.sub("营业部", remainder)
        branch = tail or remainder or None
        method = SEAT_METHOD_ALIAS
    else:
        generic = _generic_broker(text)
        if generic is not None:
            broker, branch = generic
            method = SEAT_METHOD_PATTERN

    if branch is not None:
        kind = (
            KIND_TRADE_UNIT
            if _TRADE_UNIT_PATTERN.search(branch)
            else (KIND_BROKER_BRANCH if broker else KIND_UNKNOWN)
        )

    if branch is None or broker is None:
        # 形如"某某投资公司（非营业部）"之类的文本：不强行分组，计入覆盖率分母。
        return SeatName(
            raw=str(raw),
            standard_name=None,
            method=SEAT_METHOD_UNMATCHED,
            kind=KIND_UNKNOWN,
            noise_removed=noise_removed,
        )

    standard = f"{broker}{branch}"
    merged, rule_pattern = apply_alias_rules(standard, alias_rules)
    if rule_pattern:
        method = SEAT_METHOD_ALIAS_MAP
    return SeatName(
        raw=str(raw),
        standard_name=merged,
        method=method,
        kind=kind,
        broker=broker,
        branch=branch,
        noise_removed=noise_removed,
    )


def standard_seat_key(
    raw: Any,
    *,
    alias_rules: Iterable[SeatAliasRule] = SEAT_ALIAS_RULES,
) -> str | None:
    """便捷函数：只要标准名（未匹配返回 ``None``）。"""
    return normalize_seat_name(raw, alias_rules=alias_rules).standard_name


def curated_label_map(
    labels: Iterable[HotMoneyLabel] = HOT_MONEY_LABELS,
) -> dict[str, HotMoneyLabel]:
    """把人工名录转成 ``标准席位名 → 标签`` 映射（同名后写覆盖先写）。"""
    mapping: dict[str, HotMoneyLabel] = {}
    for item in labels:
        mapping[item.standard_name.strip()] = item
    return mapping


def seat_label(
    standard_name: str | None,
    *,
    labels: Iterable[HotMoneyLabel] = HOT_MONEY_LABELS,
) -> HotMoneyLabel | None:
    """查人工游资标签；**名录未覆盖就返回 None**（不做任何推断）。"""
    if not standard_name:
        return None
    return curated_label_map(labels).get(standard_name.strip())


def label_audit_rows(
    labels: Iterable[HotMoneyLabel] = HOT_MONEY_LABELS,
) -> list[dict[str, Any]]:
    """名录可审计导出：每条标签连同来源、维护人、维护时间与置信度。"""
    return [
        {
            "seat_name_std": item.standard_name,
            "seat_label": item.label,
            "label_style_hint": item.style_hint,
            "label_source": item.source,
            "label_curated_by": item.curated_by,
            "label_curated_at": item.curated_at,
            "label_confidence": float(item.confidence),
            "label_disclaimer": LABEL_DISCLAIMER,
        }
        for item in labels
    ]


def normalization_report(
    raw_names: Iterable[Any],
    *,
    alias_rules: Iterable[SeatAliasRule] = SEAT_ALIAS_RULES,
    example_limit: int = 10,
) -> dict[str, Any]:
    """席位名称标准化覆盖率（issue 验收项一：覆盖 ≥ 95% 龙虎榜记录）。

    覆盖率按**记录数**（不是去重席位数）计算：同一席位多条记录里只要
    标准化失败就计入分母与未覆盖数，避免"用高频席位掩盖长尾失败"。
    """
    total = 0
    covered = 0
    by_method: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    distinct: dict[str, str] = {}
    unmatched_examples: list[str] = []
    for raw in raw_names:
        text = str(raw or "").strip()
        if not text:
            continue  # 源数据本身为空的行不进入龙虎榜（#22 已过滤）
        total += 1
        seat = normalize_seat_name(text, alias_rules=alias_rules)
        by_method[seat.method] = by_method.get(seat.method, 0) + 1
        by_kind[seat.kind] = by_kind.get(seat.kind, 0) + 1
        if seat.is_standardized:
            covered += 1
            distinct.setdefault(seat.standard_name or "", text)
        elif len(unmatched_examples) < example_limit:
            unmatched_examples.append(text)
    coverage = (covered / total) if total else None
    return {
        "seat_records_total": total,
        "seat_records_standardized": covered,
        "seat_name_coverage": coverage,
        "seat_distinct_standard": len(distinct),
        "seat_name_by_method": by_method,
        "seat_name_by_kind": by_kind,
        "unmatched_examples": unmatched_examples,
        "coverage_target": 0.95,
        "coverage_met": bool(coverage is not None and coverage >= 0.95),
        "coverage_status": (
            "no_data" if coverage is None else ("pass" if coverage >= 0.95 else "fail")
        ),
    }


__all__ = [
    "BROKER_SHORT_ALIASES",
    "HOT_MONEY_LABELS",
    "KIND_BROKER_BRANCH",
    "KIND_INSTITUTION",
    "KIND_NORTHBOUND",
    "KIND_TRADE_UNIT",
    "KIND_UNKNOWN",
    "LABEL_DISCLAIMER",
    "SEAT_ALIAS_RULES",
    "STANDARDIZED_METHODS",
    "HotMoneyLabel",
    "SeatAliasRule",
    "SeatName",
    "apply_alias_rules",
    "curated_label_map",
    "label_audit_rows",
    "normalize_seat_name",
    "normalization_report",
    "seat_label",
    "standard_seat_key",
]
