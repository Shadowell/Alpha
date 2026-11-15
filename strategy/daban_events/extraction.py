"""公告结构化事件抽取（issue #35「新增结构化事件抽取（JSON schema）」）。

抽取分两条路，永远以规则为底线：

1. :func:`rule_extract` —— 纯关键词 + 金额/比例正则，输出带原文偏移的证据；
2. :func:`llm_extract` —— 现有 OpenAI 兼容接口做结构化 JSON 抽取，但产出必须
   过 :func:`validate_llm_payload` 的**回指校验**：
   ``event_type`` 必须属于固定枚举、``evidence`` 里的每个片段都必须在原文
   真实出现、``amount_text`` 必须能在原文找到同样写法、比例必须 <= 100%。
   任何一条不过就整条丢弃 LLM 结果并退回规则抽取，同时登记
   ``extract_source="rule"`` + ``guard_flags``，绝不"LLM 说了算"。

金额口径
--------
落库统一用**元**（与 #22 v3 底座一致）。中文公告常见的
``1,000万元`` / ``3.5亿元`` / ``不低于2亿元且不超过5亿元`` 都会归一为
``amount_low`` / ``amount_high``；解析不出来时是 ``None`` + ``missing`` 登记，
不做 0 填充。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from .disclosure import DisclosureTiming, resolve_disclosure
from .guard import DEFAULT_TIMEOUT_SECONDS, redact, run_with_timeout
from .taxonomy import (
    EVENT_CODES,
    EVENT_NEUTRAL,
    EVENT_TAXONOMY,
    EventClassification,
    KeywordHit,
    classify_event,
    taxonomy_by_code,
)

EXTRACT_SOURCE_RULE = "rule"
EXTRACT_SOURCE_LLM = "llm_validated"
EXTRACT_SOURCE_LLM_REJECTED = "llm_rejected_fallback_rule"
EXTRACT_SOURCE_LLM_UNAVAILABLE = "llm_unavailable_fallback_rule"

UNIT_YUAN = 1.0
UNIT_WAN = 10_000.0
UNIT_YI = 100_000_000.0

_AMOUNT_RE = re.compile(
    r"(?P<num>\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"\s*(?P<unit>亿元|亿|万元|万|元)?"
)
#: 只有"占……X%"这种明确占比措辞才当比例；"同比增长256%"是增速，不是比例。
_RATIO_RE = re.compile(
    r"占[^。；;%\d]{0,24}?(?:不低于|不超过|约|为|达)?\s*"
    r"(?P<num>\d+(?:\.\d+)?|\.\d+)\s*(?:%|％)"
)
_CN_RANGE_RE = re.compile(
    r"不(?:低|高|少于|超过)于?\s*"
    r"(?P<num>\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"\s*(?P<unit>亿元|亿|万元|万|元)"
)
_KEYWORD_SPLIT_RE = re.compile(r"、|，|,|和|及|与")

LLM_SYSTEM_PROMPT = """你是A股公告事件结构化抽取助手，只做抽取不做推测。
只输出 JSON 对象，字段与取值必须满足：
- event_type: 只能取固定枚举 {types}
- sub_type: 不超过12字的中文短语，必须来自公告原文措辞
- amount_text: 公告原文里出现的金额写法（原样摘录，没有就填 null）
- ratio_text: 公告原文里出现的占比写法（没有就填 null）
- counterparty: 交易对方/中标项目对象名称原文摘录（没有填 null）
- evidence: 1-4 条，每条必须是公告标题或正文里逐字出现过的连续片段
- confidence: 0 到 1 的小数
硬约束：任何片段都不得改写、概括或补写原文没有的信息；
信息不足时 event_type 填 neutral_or_non_substantive、evidence 填空数组。
"""


@dataclass(slots=True)
class RawNotice:
    """公告原始记录（字段与 ``akshare.stock_notice_report`` 对齐）。"""

    symbol: str
    title: str
    body: str = ""
    notice_type: str = ""
    announce_date: str | None = None
    announce_time: str | None = None
    source_url: str | None = None
    name: str = ""

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "RawNotice":
        """兼容 akshare 中文列名与既有公告漏斗的英文字段名。"""

        symbol = str(
            row.get("symbol")
            or row.get("code")
            or row.get("代码")
            or row.get("ts_code")
            or ""
        )
        symbol = symbol.split(".")[0].strip().zfill(6) if symbol else ""
        return cls(
            symbol=symbol,
            title=str(row.get("title") or row.get("公告标题") or ""),
            body=str(row.get("body") or row.get("content") or row.get("公告内容") or ""),
            notice_type=str(row.get("notice_type") or row.get("公告类型") or ""),
            announce_date=str(
                row.get("announce_date")
                or row.get("notice_date")
                or row.get("公告日期")
                or ""
            ).strip()
            or None,
            announce_time=str(row.get("announce_time") or row.get("披露时间") or "").strip()
            or None,
            source_url=str(row.get("url") or row.get("网址") or row.get("source_url") or "").strip()
            or None,
            name=str(row.get("name") or row.get("名称") or ""),
        )


@dataclass(slots=True)
class AmountHit:
    """一次金额/比例命中。"""

    value: float | None
    unit: str
    text: str
    start: int
    end: int
    kind: str = "amount"  # amount | percent

    def as_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "unit": self.unit,
            "text": self.text,
            "start": self.start,
            "end": self.end,
            "kind": self.kind,
        }


@dataclass(slots=True)
class EventExtract:
    """一条公告的结构化抽取结果（落库主体）。"""

    symbol: str
    name: str
    title: str
    notice_type: str
    announce_date: str | None
    source_url: str | None
    raw_text_hash: str
    event_type: str
    event_type_zh: str
    sub_type: str | None
    polarity: int
    confidence: float
    evidence: list[dict[str, Any]] = field(default_factory=list)
    amount_value: float | None = None
    amount_low: float | None = None
    amount_high: float | None = None
    amount_text: str | None = None
    ratio_percent: float | None = None
    counterparty: str | None = None
    non_substantive: bool = False
    risk_flags: list[str] = field(default_factory=list)
    extract_source: str = EXTRACT_SOURCE_RULE
    llm_model: str | None = None
    guard_flags: list[str] = field(default_factory=list)
    needs_manual_review: bool = False
    timing: DisclosureTiming | None = None
    missing: dict[str, str] = field(default_factory=dict)
    # 去重与首次披露标记由 strategy.daban_events.dedupe 回填
    dedupe_key: str | None = None
    event_seq: int | None = None
    is_first_disclosure: bool | None = None
    repeat_count: int | None = None

    @property
    def is_event(self) -> bool:
        return self.event_type != EVENT_NEUTRAL and not self.non_substantive

    @property
    def amount(self) -> float | None:
        """可比的单一金额（元）：有区间时取下限（保守）。"""

        if self.amount_low is not None:
            return self.amount_low
        return self.amount_value

    def with_timing(self, timing: DisclosureTiming) -> "EventExtract":
        self.timing = timing
        if timing.missing:
            self.missing["disclosure_time"] = timing.notes[0] if timing.notes else "unresolved"
        return self

    def as_dict(self) -> dict[str, Any]:
        timing_payload = self.timing.as_dict() if self.timing is not None else {}
        return {
            "symbol": self.symbol,
            "name": self.name,
            "title": self.title,
            "notice_type": self.notice_type,
            "announce_date": self.announce_date,
            "source_url": self.source_url,
            "raw_text_hash": self.raw_text_hash,
            "event_type": self.event_type,
            "event_type_zh": self.event_type_zh,
            "sub_type": self.sub_type,
            "polarity": self.polarity,
            "confidence": round(self.confidence, 4),
            "evidence_json": json.dumps(self.evidence, ensure_ascii=False),
            "amount_value": self.amount_value,
            "amount_low": self.amount_low,
            "amount_high": self.amount_high,
            "amount_text": self.amount_text,
            "ratio_percent": self.ratio_percent,
            "counterparty": self.counterparty,
            "non_substantive": int(self.non_substantive),
            "risk_flags": ";".join(self.risk_flags),
            "extract_source": self.extract_source,
            "llm_model": self.llm_model,
            "guard_flags": ";".join(self.guard_flags),
            "needs_manual_review": int(self.needs_manual_review),
            "dedupe_key": self.dedupe_key,
            "event_seq": self.event_seq,
            "is_first_disclosure": (
                None if self.is_first_disclosure is None else int(self.is_first_disclosure)
            ),
            "repeat_count": self.repeat_count,
            "missing_json": json.dumps(self.missing, ensure_ascii=False),
            **timing_payload,
        }


def raw_text_hash(title: str, body: str = "") -> str:
    """归一化后的正文指纹（折叠空白 + 去全半角差异），用于重复公告识别。"""

    joined = " ".join(f"{title or ''} {body or ''}".split())
    normalized = joined.translate(_FULLWIDTH_MAP)
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()


_FULLWIDTH_MAP = {ord(c): ord(c) - 0xFEE0 for c in "０１２３４５６７８９％，．：；？！＂＇（）"}
_FULLWIDTH_MAP.update({ord("　"): ord(" ")})


# ---------------------------------------------------------------------------
# 金额 / 比例解析
# ---------------------------------------------------------------------------


def _unit_multiplier(unit: str | None) -> tuple[float, str]:
    if unit in ("亿元", "亿"):
        return UNIT_YI, "yi"
    if unit in ("万元", "万"):
        return UNIT_WAN, "wan"
    if unit == "元":
        return UNIT_YUAN, "yuan"
    return UNIT_YUAN, "unknown"


def extract_amounts(text: str, *, limit: int = 12) -> list[AmountHit]:
    """抽取金额写法（元）。只认带"元/万/亿"单位的数字，避免把日期当金额。"""

    if not text:
        return []
    hits: list[AmountHit] = []
    for match in _AMOUNT_RE.finditer(text):
        unit = match.group("unit")
        if not unit:
            continue
        multiplier, kind = _unit_multiplier(unit)
        raw_num = match.group("num").replace(",", "")
        try:
            value = float(raw_num) * multiplier
        except ValueError:  # pragma: no cover - 正则已限定数字
            continue
        hits.append(
            AmountHit(
                value=value,
                unit=kind,
                text=match.group(0),
                start=match.start(),
                end=match.end(),
            )
        )
        if len(hits) >= limit:
            break
    return hits


def extract_ratio(text: str) -> float | None:
    """公告中明确写出的占比（百分数，0-100）；没有明确措辞返回 ``None``。"""

    for match in _RATIO_RE.finditer(text or ""):
        try:
            value = float(match.group("num"))
        except ValueError:  # pragma: no cover - 正则已限定数字
            continue
        if 0.0 <= value <= 100.0:
            return value
    return None


def _amount_bounds(
    hits: Sequence[AmountHit], source: str = ""
) -> tuple[float | None, float | None]:
    """从命中里挑区间：优先"不低于/不超过"显式措辞，否则取最大单值。"""

    lows: list[float] = []
    for match in _CN_RANGE_RE.finditer(source or ""):
        multiplier, _ = _unit_multiplier(match.group("unit"))
        try:
            lows.append(float(match.group("num").replace(",", "")) * multiplier)
        except ValueError:  # pragma: no cover - 正则已限定数字
            continue
    values = [h.value for h in hits if h.value is not None]
    if not values:
        return None, None
    if len(lows) >= 2:
        return min(lows), max(lows)
    biggest = max(values)
    return biggest, biggest


def extract_counterparty(text: str) -> str | None:
    """粗粒度抽取交易对方/中标对象名称（仅摘录，不推断）。"""

    source = text or ""
    for marker in ("交易对方为", "受让方为", "中标对方", "合同对方", "对方为"):
        idx = source.find(marker)
        if idx < 0:
            continue
        tail = source[idx + len(marker) : idx + len(marker) + 40]
        piece = _KEYWORD_SPLIT_RE.split(tail)[0].strip(" 。.;；")
        if piece:
            return piece[:40]
    return None


# ---------------------------------------------------------------------------
# 规则抽取
# ---------------------------------------------------------------------------


def rule_extract(
    notice: RawNotice,
    *,
    calendar=None,
    classification: EventClassification | None = None,
) -> EventExtract:
    """规则兜底抽取：永远能出一个结果，哪怕结果是"非实质性"。"""

    haystack = f"{notice.title} {notice.body}"
    result = classification or classify_event(
        notice.title, notice.body, notice.notice_type
    )
    hits = extract_amounts(haystack)
    amount_low, amount_high = _amount_bounds(hits, haystack)
    ratio = extract_ratio(haystack)
    digest = raw_text_hash(notice.title, notice.body)
    missing: dict[str, str] = {}
    if not notice.announce_date:
        missing["announce_date"] = "source_row_missing_date"
    if not notice.source_url:
        missing["source_url"] = "source_row_missing_url"
    if not hits:
        missing["amount"] = "no_amount_in_text"
    if ratio is None:
        missing["ratio_percent"] = "no_percent_in_text"
    extract = EventExtract(
        symbol=notice.symbol,
        name=notice.name,
        title=notice.title,
        notice_type=notice.notice_type,
        announce_date=notice.announce_date,
        source_url=notice.source_url,
        raw_text_hash=digest,
        event_type=result.code,
        event_type_zh=result.label_zh,
        sub_type=result.sub_type,
        polarity=result.polarity,
        confidence=result.confidence,
        evidence=[h.as_dict() for h in result.hits],
        amount_value=_single_amount(hits),
        amount_low=amount_low,
        amount_high=amount_high,
        amount_text=hits[0].text if hits else None,
        ratio_percent=ratio,
        counterparty=extract_counterparty(haystack),
        non_substantive=result.non_substantive,
        risk_flags=list(result.risk_flags),
        extract_source=EXTRACT_SOURCE_RULE,
        missing=missing,
    )
    extract.with_timing(
        resolve_disclosure(
            announce_date=notice.announce_date,
            announce_time=notice.announce_time,
            calendar=calendar,
        )
    )
    return extract


def _single_amount(hits: Sequence[AmountHit]) -> float | None:
    values = [h.value for h in hits if h.value is not None]
    return max(values) if values else None


# ---------------------------------------------------------------------------
# LLM 抽取（严格回指校验）
# ---------------------------------------------------------------------------

LLM_GUARD_FIELDS = ("event_type", "sub_type", "amount_text", "ratio_text", "counterparty")


def llm_prompt(notice: RawNotice, *, max_body_chars: int = 1200) -> list[dict[str, str]]:
    types = "|".join(EVENT_CODES)
    body = (notice.body or "")[:max_body_chars]
    user = json.dumps(
        {
            "symbol": notice.symbol,
            "title": notice.title,
            "notice_type": notice.notice_type,
            "announce_date": notice.announce_date,
            "body": body,
        },
        ensure_ascii=False,
    )
    return [
        {"role": "system", "content": LLM_SYSTEM_PROMPT.replace("{types}", types)},
        {"role": "user", "content": f"公告：\n{user}"},
    ]


def parse_llm_json(text: str) -> dict[str, Any] | None:
    """从模型输出里抠出 JSON 对象；解析不出来返回 ``None``（不猜测）。"""

    if not text:
        return None
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", stripped)
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", stripped, flags=re.S)
        if match is None:
            return None
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return payload if isinstance(payload, dict) else None


def validate_llm_payload(
    payload: Mapping[str, Any], *, source_text: str
) -> tuple[dict[str, Any], list[str]]:
    """校验 LLM 抽取，返回（可采纳字段，拒绝原因）。

    通过条件（缺一即整条作废）：

    * ``event_type`` 属于固定枚举；
    * ``evidence`` 非空，且每条都是 ``source_text`` 的**逐字子串**；
    * ``amount_text`` / ``ratio_text`` / ``counterparty`` 若给出，同样必须逐字出现；
    * ``confidence`` 落在 0-1。
    """

    flags: list[str] = []
    haystack = _normalize_for_anchor(source_text)
    event_type = str(payload.get("event_type") or "").strip()
    if event_type not in EVENT_CODES:
        flags.append("invalid_event_type")

    evidence = payload.get("evidence")
    anchors: list[dict[str, Any]] = []
    if not isinstance(evidence, list) or not evidence:
        flags.append("missing_evidence")
    else:
        for item in evidence[:4]:
            text = str(item or "").strip()
            if len(text) < 2:
                continue
            position = haystack.find(_normalize_for_anchor(text))
            if position < 0:
                flags.append("hallucinated_evidence")
                continue
            anchors.append(
                {
                    "code": event_type or EVENT_NEUTRAL,
                    "keyword": text[:40],
                    "start": position,
                    "end": position + len(text),
                    "weight": 0,
                    "field": "llm_anchor",
                }
            )

    accepted: dict[str, Any] = {"event_type": event_type, "evidence": anchors}
    for name in LLM_GUARD_FIELDS:
        if name == "event_type":
            continue
        value = payload.get(name)
        if value is None or str(value).strip() in ("", "null", "None"):
            continue
        text = str(value).strip()
        if _normalize_for_anchor(text) not in haystack:
            flags.append(f"unverified_{name}")
            continue
        accepted[name] = text

    try:
        confidence = float(payload.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.0
        flags.append("invalid_confidence")
    if not 0.0 <= confidence <= 1.0:
        confidence = min(1.0, max(0.0, confidence))
        flags.append("confidence_out_of_range")
    accepted["confidence"] = confidence

    if not anchors:
        flags.append("no_verified_anchor")
    return accepted, sorted(set(flags))


def _normalize_for_anchor(text: str) -> str:
    joined = " ".join(str(text or "").split())
    return joined.translate(_FULLWIDTH_MAP).casefold()


def llm_extract(
    notice: RawNotice,
    *,
    client: Callable[[list[dict[str, str]]], str] | None,
    model: str = "",
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[EventExtract | None, list[str]]:
    """调用 LLM 做结构化抽取；任何故障都只返回 (None, 原因)，不抛给调用方。

    ``client`` 是注入式的 ``messages -> 文本`` 可调用，本包不持有 api key，
    因此也就不存在把凭据带进日志的路径。
    """

    if client is None:
        return None, ["llm_client_not_configured"]
    messages = llm_prompt(notice)
    outcome = run_with_timeout(
        lambda: client(messages), source="daban_events_llm", timeout_seconds=timeout_seconds
    )
    if not outcome.ok:
        return None, [f"llm_call_{outcome.status}:{redact(outcome.detail or outcome.reason)}"]
    text = outcome.rows[0] if outcome.rows else ""
    if not isinstance(text, str):
        return None, ["llm_output_not_text"]
    payload = parse_llm_json(text)
    if payload is None:
        return None, ["llm_output_unparsable"]
    accepted, flags = validate_llm_payload(payload, source_text=f"{notice.title} {notice.body}")
    if flags or accepted.get("event_type") not in EVENT_CODES:
        return None, ["llm_rejected", *flags]
    return _from_validated(notice, accepted, model=model), []


def _from_validated(notice: RawNotice, accepted: Mapping[str, Any], *, model: str) -> EventExtract:
    typedef = taxonomy_by_code()[str(accepted["event_type"])]
    anchors = list(accepted.get("evidence") or [])
    amount_text = accepted.get("amount_text")
    hits = extract_amounts(str(amount_text)) if amount_text else []
    amount_low, amount_high = _amount_bounds(hits, str(amount_text or ""))
    ratio_text = accepted.get("ratio_text")
    ratio = extract_ratio(str(ratio_text)) if ratio_text else None
    classification = classify_event(notice.title, notice.body, notice.notice_type)
    return EventExtract(
        symbol=notice.symbol,
        name=notice.name,
        title=notice.title,
        notice_type=notice.notice_type,
        announce_date=notice.announce_date,
        source_url=notice.source_url,
        raw_text_hash=raw_text_hash(notice.title, notice.body),
        event_type=typedef.code,
        event_type_zh=typedef.label_zh,
        sub_type=str(accepted.get("sub_type") or classification.sub_type or "") or None,
        polarity=typedef.polarity,
        confidence=float(accepted.get("confidence", 0.0)),
        evidence=anchors,
        amount_value=_single_amount(hits),
        amount_low=amount_low,
        amount_high=amount_high,
        amount_text=str(amount_text) if amount_text else None,
        ratio_percent=ratio,
        counterparty=str(accepted.get("counterparty")) or None,
        non_substantive=classification.non_substantive,
        risk_flags=list(classification.risk_flags),
        extract_source=EXTRACT_SOURCE_LLM,
        llm_model=model or None,
        missing={},
    )


def extract_event(
    notice: RawNotice,
    *,
    calendar=None,
    llm_client: Callable[[list[dict[str, str]]], str] | None = None,
    model: str = "",
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    manual_review_below: float = 0.35,
) -> EventExtract:
    """抽取入口：LLM 可选，规则永远兜底。

    LLM 结果只有在通过回指校验后才覆盖规则结果；否则保留规则结果并登记
    ``extract_source``，同时把失败原因写进 ``guard_flags`` 供人工抽检。
    """

    base = rule_extract(notice, calendar=calendar)
    missing = dict(base.missing)
    if llm_client is None:
        return base
    validated, reasons = llm_extract(
        notice, client=llm_client, model=model, timeout_seconds=timeout_seconds
    )
    if validated is None:
        base.guard_flags.extend(reasons)
        base.extract_source = (
            EXTRACT_SOURCE_LLM_REJECTED
            if reasons and reasons[0] == "llm_rejected"
            else EXTRACT_SOURCE_LLM_UNAVAILABLE
        )
        base.needs_manual_review = True
        return base
    merged_missing = dict(validated.missing)
    if validated.amount_low is None:
        validated.amount_low = base.amount_low
        validated.amount_high = base.amount_high
        validated.amount_value = base.amount_value
        validated.amount_text = validated.amount_text or base.amount_text
    if validated.ratio_percent is None:
        validated.ratio_percent = base.ratio_percent
    if not validated.evidence:
        validated.evidence = base.evidence
    if not validated.counterparty:
        validated.counterparty = base.counterparty
    for key, reason in missing.items():
        merged_missing.setdefault(key, reason)
    validated.missing = merged_missing
    validated.guard_flags = list(dict.fromkeys(validated.guard_flags))
    validated.needs_manual_review = (
        validated.confidence < manual_review_below or bool(validated.risk_flags)
    )
    return validated


def taxonomy_codes() -> tuple[str, ...]:
    return tuple(t.code for t in EVENT_TAXONOMY)


def anchor_found(text: str, anchor: str) -> bool:
    """公开的"证据是否真在原文里"检查（人工抽检与报表复核用）。"""

    return _normalize_for_anchor(anchor) in _normalize_for_anchor(text)


def keyword_hits_as_dicts(hits: Sequence[KeywordHit]) -> list[dict[str, Any]]:
    return [h.as_dict() for h in hits]
