"""事件类型体系（issue #35「事件类型」章节）。

类型口径严格来自 issue 原文列出的 7 类事件，外加一个"非实质性/中性"兜底类，
用于承接标题党与重复披露公告 —— 兜底类**不是一种事件**，它的作用是
"把噪声显式留在样本里并标成中性"，而不是被静默丢弃。

与既有公告漏斗的口径对齐
------------------------
``app/services/notice_service.py`` 的 ``BULLISH_RULES`` 关键词标签通过
:func:`from_notice_tag` 映射到本表（只读对齐，不 import app 包，避免
研究层与服务层互相绑定）。``融资获批`` 不在 issue #35 的事件清单内，
映射到 :data:`EVENT_NEUTRAL`（保留但不给事件强度），防止悄悄扩范围。

关键词命中是**带偏移**的：每个命中都记录 ``(keyword, start, end, weight)``，
后续 LLM 校验与人工抽检都靠这些 span 回指原文，这是 issue 里
「事件抽取必须可追溯到原文」的实现基础。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

# ---------------------------------------------------------------------------
# 事件类型代号（ascii code，落库/因子列名用；label_zh 只用于展示）
# ---------------------------------------------------------------------------

EVENT_RESTRUCTURING = "restructuring"
EVENT_EARNINGS_UP = "earnings_up"
EVENT_MAJOR_CONTRACT = "major_contract"
EVENT_SHAREHOLDER_ACTION = "shareholder_action"
EVENT_NEW_BUSINESS = "new_business"
EVENT_REGULATORY_NEGATIVE = "regulatory_negative"
EVENT_DIVIDEND = "dividend"
EVENT_NEUTRAL = "neutral_or_non_substantive"

#: 事件类型全集（顺序稳定，用于因子 one-hot 与报表分组）。
EVENT_CODES: tuple[str, ...] = (
    EVENT_RESTRUCTURING,
    EVENT_EARNINGS_UP,
    EVENT_MAJOR_CONTRACT,
    EVENT_SHAREHOLDER_ACTION,
    EVENT_NEW_BUSINESS,
    EVENT_REGULATORY_NEGATIVE,
    EVENT_DIVIDEND,
    EVENT_NEUTRAL,
)

#: 实质性事件（不含兜底类）；「事件首板 vs 非事件首板」对比只用这一组。
SUBSTANTIVE_EVENT_CODES: tuple[str, ...] = tuple(
    code for code in EVENT_CODES if code != EVENT_NEUTRAL
)

POLARITY_POSITIVE = 1
POLARITY_NEGATIVE = -1
POLARITY_NEUTRAL = 0


@dataclass(frozen=True, slots=True)
class EventTypeDef:
    """一类事件的定义。

    * ``keywords``：``(关键词, 权重)``；权重是"标题级"证据强度，
      越具体（如"控制权变更"）越高，越泛（如"签订"）越低；
    * ``body_keywords``：只出现在正文（而非标题）里的关键词，证据强度更低，
      单独命中不足以判定事件类型，需要与 ``keywords`` 或金额共同出现；
    * ``base_strength``：规则强度起点（0-100）；
    * ``requires_amount``：该类事件的实质性主要由金额决定（合同/回购/分红），
      金额缺失时强度封顶；
    * ``risk_keywords``：命中即扣减强度并登记风险标记（减持/终止/立案调查等）。
    """

    code: str
    label_zh: str
    polarity: int
    base_strength: float
    keywords: tuple[tuple[str, int], ...]
    sub_types: tuple[str, ...] = ()
    body_keywords: tuple[str, ...] = ()
    requires_amount: bool = False
    note_zh: str = ""

    def keyword_set(self) -> frozenset[str]:
        return frozenset(k for k, _ in self.keywords)

    def max_weight(self) -> int:
        return max((w for _, w in self.keywords), default=0)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "label_zh": self.label_zh,
            "polarity": self.polarity,
            "base_strength": self.base_strength,
            "keywords": [[k, w] for k, w in self.keywords],
            "sub_types": list(self.sub_types),
            "body_keywords": list(self.body_keywords),
            "requires_amount": self.requires_amount,
            "note_zh": self.note_zh,
        }


# 风险/噪声标记：不新增事件类型，只用于强度分级与人工抽检提示。
RISK_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("减持", "shareholder_reduction"),
    ("终止", "plan_terminated"),
    ("延期", "plan_postponed"),
    ("补充", "supplement_notice"),
    ("更正", "correction_notice"),
    ("澄清", "clarification_notice"),
    ("风险提示", "risk_reminder"),
    ("违约", "default"),
    ("逾期", "overdue"),
    ("立案", "investigation"),
    ("问询", "inquiry_letter"),
    ("监管函", "regulatory_letter"),
    ("处罚", "penalty"),
    ("预减", "earnings_down"),
    ("亏损", "loss"),
    ("退市", "delisting_risk"),
    ("停牌", "suspension"),
    ("第一次回复", "repeat_disclosure"),
    ("进展", "progress_update"),
)

#: 明显"非实质性"的标题特征（问询回复/澄清/风险提示性公告等）。
NON_SUBSTANTIVE_PATTERNS: tuple[str, ...] = (
    "风险提示性公告",
    "澄清公告",
    "补充公告",
    "更正公告",
    "说明公告",
    "回访",
    "问答",
)


EVENT_TAXONOMY: tuple[EventTypeDef, ...] = (
    EventTypeDef(
        code=EVENT_RESTRUCTURING,
        label_zh="并购重组",
        polarity=POLARITY_POSITIVE,
        base_strength=62.0,
        keywords=(
            ("控制权变更", 30),
            ("实际控制人变更", 29),
            ("要约收购", 26),
            ("重大资产购买", 25),
            ("重大资产重组", 25),
            ("资产注入", 24),
            ("借壳", 24),
            ("股权转让", 22),
            ("股份转让", 21),
            ("协议转让", 21),
            ("吸收合并", 20),
            ("战略投资", 18),
            ("重组", 14),
            ("收购", 13),
            ("并购", 13),
        ),
        sub_types=(
            "并购重组",
            "资产注入",
            "股权转让",
            "控制权变更",
            "要约收购",
            "吸收合并",
        ),
        body_keywords=("交易对方", "标的资产", "发行股份购买"),
        requires_amount=True,
        note_zh="实质性由交易金额/是否导致控制权变更决定；晚间披露常见次日一字板。",
    ),
    EventTypeDef(
        code=EVENT_EARNINGS_UP,
        label_zh="业绩预增/扭亏/超预期",
        polarity=POLARITY_POSITIVE,
        base_strength=58.0,
        keywords=(
            ("业绩超预期", 30),
            ("扭亏为盈", 26),
            ("预增", 22),
            ("大幅上升", 22),
            ("大幅增长", 21),
            ("净利润增长", 20),
            ("业绩预告", 16),
            ("业绩快报", 16),
            ("预盈", 15),
            ("同比增长", 14),
            ("盈利", 6),
        ),
        sub_types=("预增", "扭亏", "超预期", "业绩快报", "定期报告"),
        body_keywords=("归属于上市公司股东的净利润", "扣除非经常性损益"),
        note_zh="预告区间下限才是可交易信息；快报与正式报告需区分披露时间。",
    ),
    EventTypeDef(
        code=EVENT_MAJOR_CONTRACT,
        label_zh="重大合同/中标/订单",
        polarity=POLARITY_POSITIVE,
        base_strength=55.0,
        keywords=(
            ("中标", 28),
            ("重大合同", 26),
            ("签署合同", 22),
            ("签订合同", 22),
            ("框架协议", 16),
            ("定点", 16),
            ("订单", 15),
            ("采购协议", 15),
            ("供货", 14),
            ("中选", 14),
            ("签订", 8),
        ),
        sub_types=("中标", "重大合同", "订单", "框架协议", "定点"),
        body_keywords=("合同金额", "履约期限", "占最近一期经审计"),
        requires_amount=True,
        note_zh="金额/市值比是核心；框架/意向类协议无金额时按低强度处理。",
    ),
    EventTypeDef(
        code=EVENT_SHAREHOLDER_ACTION,
        label_zh="回购/增持/股权激励",
        polarity=POLARITY_POSITIVE,
        base_strength=48.0,
        keywords=(
            ("回购股份", 24),
            ("以集中竞价", 20),
            ("增持计划", 20),
            ("控股股东增持", 20),
            ("回购", 16),
            ("增持", 15),
            ("股权激励", 15),
            ("员工持股", 15),
            ("限制性股票", 13),
            ("股票期权", 12),
        ),
        sub_types=("回购", "增持", "股权激励", "员工持股"),
        body_keywords=("回购金额", "增持金额", "行权条件"),
        requires_amount=True,
        note_zh="真实资金承诺（含金额下限）才算实质；"
        "回购专用账户变现/减持式增持要扣分。",
    ),
    EventTypeDef(
        code=EVENT_NEW_BUSINESS,
        label_zh="新产品/新业务/技术突破",
        polarity=POLARITY_POSITIVE,
        base_strength=45.0,
        keywords=(
            ("技术突破", 24),
            ("临床试验", 20),
            ("获批上市", 19),
            ("产品注册", 18),
            ("量产", 17),
            ("投产", 16),
            ("新品发布", 16),
            ("商业化", 15),
            ("专利授权", 14),
            ("中试", 13),
            ("新业务", 12),
            ("数据中心", 10),
            ("算力", 10),
        ),
        sub_types=("新产品", "新业务", "技术突破", "资质获批"),
        body_keywords=("预计贡献收入", "通过审评审批", "取得注册证"),
        note_zh="题材相关度靠 #8 题材热度衡量；纯概念无收入贡献要降级。",
    ),
    EventTypeDef(
        code=EVENT_REGULATORY_NEGATIVE,
        label_zh="监管函/问询函/立案",
        polarity=POLARITY_NEGATIVE,
        base_strength=30.0,
        keywords=(
            ("立案告知书", 30),
            ("立案调查", 29),
            ("行政处罚", 25),
            ("监管函", 22),
            ("问询函", 20),
            ("关注函", 18),
            ("纪律处分", 18),
            ("警示函", 16),
            ("责令改正", 15),
            ("立案", 12),
        ),
        sub_types=("立案调查", "行政处罚", "问询函", "监管函", "关注函"),
        note_zh="负面事件不做多头信号，只用于断板/大面风险分层。",
    ),
    EventTypeDef(
        code=EVENT_DIVIDEND,
        label_zh="分红/送转",
        polarity=POLARITY_POSITIVE,
        base_strength=35.0,
        keywords=(
            ("高送转", 24),
            ("每10股送", 22),
            ("转增", 18),
            ("现金分红", 16),
            ("派息", 14),
            ("利润分配", 12),
            ("权益分派", 12),
            ("分红", 10),
        ),
        sub_types=("现金分红", "送转", "特别分红"),
        body_keywords=("每股送", "每股派"),
        requires_amount=True,
        note_zh="短线炒作看填权预期，除息本身不产生 alpha。",
    ),
    EventTypeDef(
        code=EVENT_NEUTRAL,
        label_zh="非实质性/中性",
        polarity=POLARITY_NEUTRAL,
        base_strength=20.0,
        keywords=(),
        sub_types=("非实质性", "重复披露", "常规公告"),
        note_zh="兜底类：不进入多头事件信号，只做噪声与重复披露登记。",
    ),
)

_TAXONOMY_BY_CODE: dict[str, EventTypeDef] = {t.code: t for t in EVENT_TAXONOMY}

#: 与 ``app/services/notice_service.BULLISH_RULES`` 标签的只读口径对齐。
NOTICE_TAG_MAP: Mapping[str, str] = {
    "业绩预增": EVENT_EARNINGS_UP,
    "高额分红": EVENT_DIVIDEND,
    "股份回购": EVENT_SHAREHOLDER_ACTION,
    "重大合同": EVENT_MAJOR_CONTRACT,
    "资产重组": EVENT_RESTRUCTURING,
    "产品突破": EVENT_NEW_BUSINESS,
    # 融资获批不属于 issue #35 的事件清单 -> 保留但不计事件强度。
    "融资获批": EVENT_NEUTRAL,
}


@dataclass(frozen=True, slots=True)
class KeywordHit:
    """一次关键词命中，带原文偏移，供追溯与人工抽检。"""

    code: str
    keyword: str
    start: int
    end: int
    weight: int
    field: str = "title"  # title | body | notice_type

    def evidence(self, text: str) -> str:
        return text[self.start : self.end]

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "keyword": self.keyword,
            "start": self.start,
            "end": self.end,
            "weight": self.weight,
            "field": self.field,
        }


@dataclass(slots=True)
class EventClassification:
    """规则分类结果（不含金额解析，金额在 extraction 层补齐）。"""

    code: str
    label_zh: str
    polarity: int
    confidence: float
    hits: list[KeywordHit] = field(default_factory=list)
    sub_type: str | None = None
    risk_flags: list[str] = field(default_factory=list)
    non_substantive: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def is_substantive(self) -> bool:
        return self.code != EVENT_NEUTRAL and not self.non_substantive

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.code,
            "event_type_zh": self.label_zh,
            "polarity": self.polarity,
            "confidence": round(self.confidence, 4),
            "sub_type": self.sub_type,
            "evidence": [h.as_dict() for h in self.hits],
            "risk_flags": list(self.risk_flags),
            "non_substantive": self.non_substantive,
            "notes": list(self.notes),
        }


def taxonomy_by_code() -> dict[str, EventTypeDef]:
    return dict(_TAXONOMY_BY_CODE)


def event_type_doc() -> dict[str, dict[str, Any]]:
    """事件类型字典（写入报表 / 供 TopK 解释"因为什么事件被选中"）。"""

    return {t.code: t.as_dict() for t in EVENT_TAXONOMY}


def from_notice_tag(tag: str | None) -> str:
    """把既有公告漏斗的关键词标签映射到事件类型代号（未知 -> 兜底类）。"""

    return NOTICE_TAG_MAP.get(str(tag or "").strip(), EVENT_NEUTRAL)


def _find_all(text: str, needle: str) -> list[int]:
    """返回 needle 在 text 中所有起始偏移（大小写无关，兼容全角空格）。"""

    if not text or not needle:
        return []
    haystack = text.casefold()
    target = needle.casefold()
    spans: list[int] = []
    start = 0
    while True:
        idx = haystack.find(target, start)
        if idx < 0:
            break
        spans.append(idx)
        start = idx + 1
    return spans


def match_keywords(
    title: str,
    body: str = "",
    notice_type: str = "",
    *,
    codes: Sequence[str] | None = None,
) -> list[KeywordHit]:
    """在所有事件类型上做关键词命中（标题证据权重最高）。"""

    selected = [t for t in EVENT_TAXONOMY if codes is None or t.code in set(codes)]
    hits: list[KeywordHit] = []
    fields = (
        ("title", title or ""),
        ("body", body or ""),
        ("notice_type", notice_type or ""),
    )
    for typedef in selected:
        for keyword, weight in typedef.keywords:
            for field_name, text in fields:
                if not text:
                    continue
                for start in _find_all(text, keyword):
                    hits.append(
                        KeywordHit(
                            code=typedef.code,
                            keyword=keyword,
                            start=start,
                            end=start + len(keyword),
                            weight=weight
                            if field_name == "title"
                            else max(1, weight // 2),
                            field=field_name,
                        )
                    )
    return sorted(hits, key=lambda h: (-h.weight, h.field != "title", h.code))


_NON_SUBSTANTIVE_RE = re.compile(
    "|".join(re.escape(p) for p in NON_SUBSTANTIVE_PATTERNS)
)


def detect_risk_flags(text: str) -> list[str]:
    """风险/噪声标记（不做事件类型，只做强度分级与抽检提示）。"""

    flags: list[str] = []
    for keyword, flag in RISK_KEYWORDS:
        if keyword in text and flag not in flags:
            flags.append(flag)
    return flags


def classify_event(
    title: str,
    body: str = "",
    notice_type: str = "",
    *,
    extra_terms: Sequence[str] = (),
) -> EventClassification:
    """规则兜底分类：无命中一律返回兜底类，绝不"猜"一个事件类型。

    ``extra_terms`` 允许调用方（如已上线的 LLM 抽取结果）声明"我在原文里
    还看到了这些词"，本函数只在词真的出现在原文时才采纳 —— 这是
    「不允许 LLM 编造事件」的第一道闸。
    """

    haystack = f"{title} {body} {notice_type}"
    hits = match_keywords(title, body, notice_type)
    risk_flags = detect_risk_flags(haystack)
    non_substantive = bool(_NON_SUBSTANTIVE_RE.search(title or ""))

    accepted: list[KeywordHit] = list(hits)
    for term in extra_terms:
        term = str(term or "").strip()
        if not term:
            continue
        found = False
        for typedef in EVENT_TAXONOMY:
            for keyword, weight in typedef.keywords:
                if term == keyword:
                    for start in _find_all(haystack, term):
                        accepted.append(
                            KeywordHit(
                                code=typedef.code,
                                keyword=keyword,
                                start=start,
                                end=start + len(keyword),
                                weight=weight,
                                field="body",
                            )
                        )
                        found = True
        if not found:
            risk_flags.append("unmatched_claimed_term")

    if not accepted:
        return EventClassification(
            code=EVENT_NEUTRAL,
            label_zh=_TAXONOMY_BY_CODE[EVENT_NEUTRAL].label_zh,
            polarity=POLARITY_NEUTRAL,
            confidence=0.0,
            hits=[],
            sub_type="常规公告",
            risk_flags=risk_flags,
            non_substantive=non_substantive,
            notes=["no_keyword_hit"],
        )

    best_code = _best_code(accepted)
    typedef = _TAXONOMY_BY_CODE[best_code]
    code_hits = [h for h in accepted if h.code == best_code]
    title_weight = sum(h.weight for h in code_hits if h.field == "title")
    body_weight = sum(h.weight for h in code_hits if h.field != "title")
    raw = title_weight + 0.5 * body_weight
    confidence = min(1.0, raw / max(1.0, typedef.max_weight() * 1.5))
    if non_substantive:
        confidence *= 0.5
    return EventClassification(
        code=best_code,
        label_zh=typedef.label_zh,
        polarity=typedef.polarity,
        confidence=round(confidence, 4),
        hits=code_hits,
        sub_type=_pick_sub_type(typedef, haystack, code_hits),
        risk_flags=risk_flags,
        non_substantive=non_substantive,
        notes=[f"matched:{h.keyword}" for h in code_hits[:6]],
    )


def _best_code(hits: Sequence[KeywordHit]) -> str:
    """按（标题命中数, 权重和, 类型优先级）选主事件类型。"""

    priority = {code: i for i, code in enumerate(EVENT_CODES)}
    scores: dict[str, tuple[int, int]] = {}
    for hit in hits:
        n, w = scores.get(hit.code, (0, 0))
        bump = 1 if hit.field == "title" else 0
        scores[hit.code] = (n + bump, w + hit.weight)
    if not scores:
        return EVENT_NEUTRAL
    ranked = sorted(
        scores.items(),
        key=lambda kv: (-kv[1][0], -kv[1][1], priority.get(kv[0], len(priority))),
    )
    return ranked[0][0]


def _pick_sub_type(
    typedef: EventTypeDef, haystack: str, hits: Sequence[KeywordHit]
) -> str | None:
    for sub in typedef.sub_types:
        if sub and sub in haystack:
            return sub
    if hits:
        return hits[0].keyword
    return None
