"""另类数据源合规 / 可行性评估（issue #33 验收项 1）。

评估维度固定为：覆盖股票数、历史长度、更新频率、获取方式、合规风险、成本、
结论（adopt / conditional / reject）。结论随代码走，避免文档漂移。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

ADOPT = "adopt"
CONDITIONAL = "conditional"
REJECT = "reject"


@dataclass(frozen=True)
class SourceAssessment:
    key: str
    title: str
    channel: str
    access: str
    coverage: str
    history: str
    frequency: str
    compliance_risk: str
    cost: str
    decision: str
    factors: tuple[str, ...]
    reason: str
    akshare_functions: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


SOURCE_ASSESSMENTS: tuple[SourceAssessment, ...] = (
    SourceAssessment(
        key="eastmoney_attention",
        title="东财千股千评 · 市场关注度",
        channel="data.eastmoney.com 数据中心公开 API（经 AkShare 封装）",
        access="akshare.stock_comment_em（全市场单页翻页）/ stock_comment_detail_scrd_focus_em（单票约 30 交易日）",
        coverage="全部 A 股（约 5000+ 标的，天然覆盖涨停候选池 ≥80%）",
        history="快照口径：接口只提供最新交易日；单票关注指数约 30 个交易日；自有落库后逐日累积",
        frequency="每交易日一次（收盘后），本包把可见时间锁定在交易日 15:30",
        compliance_risk="低：公开数据中心的结构化 API，无需登录、不绕反爬、不含个人信息",
        cost="0（免费公开接口）；单次全市场抓取约 10+ 个分页请求",
        decision=ADOPT,
        factors=(
            "attention_index",
            "attention_score",
            "attention_rank",
            "attention_rank_delta",
            "attention_index_growth_7d",
            "alt_attention_score",
        ),
        reason="覆盖度、稳定性、可回溯性都满足验收；无需登录与反爬对抗",
        akshare_functions=("stock_comment_em", "stock_comment_detail_scrd_focus_em"),
    ),
    SourceAssessment(
        key="interactive_qa",
        title="互动易问答（巨潮资讯）",
        channel="irm.cninfo.com.cn 官方投资者互动渠道（经 AkShare 封装）",
        access="akshare.stock_irm_cninfo(symbol)，单票最多 10 页 × 1000 条",
        coverage="披露义务公司为主；小市值 / 长期不回复的公司条目稀疏，实测覆盖涨停池约 60%~80%",
        history="单票可回溯数年提问列表，但接口只返回当前列表快照，历史需自建行级快照",
        frequency="交易日日间更新；问答时间戳为 Asia/Shanghai",
        compliance_risk="低-中：公开问答，问题文本可能含提问者昵称，本包不落库提问者字段，只保留问题正文与时间",
        cost="0；逐票抓取，需要限并发与限额",
        decision=ADOPT,
        factors=(
            "interactive_qa_count_1d",
            "interactive_qa_count_3d",
            "interactive_qa_count_7d",
            "interactive_reply_rate_7d",
            "interactive_reply_speed_hours_7d",
        ),
        reason="官方渠道、时间戳可信、能提前反映题材发酵；回复速度需二次详情接口，样本不足时显式缺失",
        akshare_functions=("stock_irm_cninfo", "stock_irm_ans_cninfo"),
    ),
    SourceAssessment(
        key="eastmoney_news",
        title="东财个股新闻",
        channel="so.eastmoney.com 搜索接口（AkShare 封装内部携带浏览器 Cookie / 指纹头）",
        access="akshare.stock_news_em(symbol)，仅最近约 100 条",
        coverage="有被报道的标的；冷门股常为空，涨停池覆盖不稳定",
        history="只有近期窗口，无法回溯 2 年，不满足完整周期验证",
        frequency="日间滚动",
        compliance_risk="中：调用形态接近网页抓取（伪装浏览器 / 携带 Cookie），与「不绕反爬」红线存在冲突",
        cost="0，但接口不保证 SLA",
        decision=CONDITIONAL,
        factors=(
            "social_mention_count_1d",
            "social_mention_count_3d",
            "social_mention_count_7d",
            "social_mention_growth",
            "news_count_1d",
            "news_sentiment",
            "news_sentiment_confidence",
            "news_novelty",
            "social_sentiment",
            "social_sentiment_change",
        ),
        reason=(
            "文本抽取管线可复用（与 #14 共享），但默认关闭：需要显式 "
            "ALPHA_ALT_NEWS_ENABLED=true 才抓取；拿到书面授权后再转 adopt"
        ),
        akshare_functions=("stock_news_em",),
    ),
    SourceAssessment(
        key="search_index",
        title="百度 / 微信 / 头条搜索指数",
        channel="各家指数平台",
        access="需要商业授权或开放平台申请，AkShare 仅有单日热搜榜",
        coverage="热搜榜只覆盖当日上榜标的，非全市场",
        history="公开渠道无长历史；授权接口价格高",
        frequency="分钟-日",
        compliance_risk="中-高：数据授权与转存限制不明确",
        cost="商业接口按调用/按量计费",
        decision=REJECT,
        factors=("search_index", "search_index_growth"),
        reason="未获得合规授权前不接入；因子清单保留但恒为显式缺失（绝不填 0）",
        akshare_functions=("stock_hot_search_baidu",),
    ),
    SourceAssessment(
        key="guba_posts",
        title="股吧发帖量 / 阅读量 / 评论量",
        channel="guba.eastmoney.com 列表页",
        access="需要逐帖翻页抓取列表与正文，存在反爬与登录墙",
        coverage="热票密集、冷票稀疏，抽样偏差大",
        history="公开接口不提供历史回溯",
        frequency="实时",
        compliance_risk="高：属于 issue 明确禁止的「需要登录 / 绕反爬」抓取；帖内昵称涉及个人信息",
        cost="0 但维护成本高、易失效",
        decision=REJECT,
        factors=("social_post_count", "social_read_count", "social_comment_count"),
        reason="合规红线优先，不实现页面爬虫；关注度用千股千评公开指数替代",
        akshare_functions=("stock_hot_keyword_em", "stock_hot_up_em"),
    ),
    SourceAssessment(
        key="analyst_reports",
        title="研报评级 / 目标价 / 首次覆盖",
        channel="券商研报与 Wind / 东财 Choice 数据",
        access="需要商业数据订阅与版权授权",
        coverage="覆盖机构关注的中大盘，小盘打板标的稀疏",
        history="商业库可回溯，但本仓库无授权",
        frequency="事件驱动",
        compliance_risk="高：研报版权与再分发限制",
        cost="订阅费用高，ROI 未验证",
        decision=REJECT,
        factors=("analyst_rating_change", "target_price_upside"),
        reason="无合规来源，因子保持显式缺失，等授权后再评估",
        akshare_functions=(),
    ),
    SourceAssessment(
        key="longhu_hotmoney",
        title="龙虎榜 / 游资社区讨论",
        channel="交易所龙虎榜公开数据 + 第三方社区",
        access="榜单可由公开披露获取（已由 Tushare/AkShare 覆盖）；社区讨论无合规来源",
        coverage="榜单仅覆盖异动标的，约涨停池的 20%~40%",
        history="榜单可回溯多年；社区数据无",
        frequency="交易日盘后",
        compliance_risk="榜单低；社区帖子高（爬虫 + 个人信息）",
        cost="0",
        decision=REJECT,
        factors=("hotmoney_discussion",),
        reason="龙虎榜结构化数据属于 #13/#14 范畴，本 issue 不重复接入；社区讨论不接入",
        akshare_functions=("stock_lhb_detail_em",),
    ),
)

FACTOR_TO_SOURCE: dict[str, str] = {
    factor: assessment.key
    for assessment in SOURCE_ASSESSMENTS
    for factor in assessment.factors
}


def assessments_by_decision(decision: str) -> list[SourceAssessment]:
    return [item for item in SOURCE_ASSESSMENTS if item.decision == decision]


def adopted_sources() -> tuple[str, ...]:
    return tuple(item.key for item in assessments_by_decision(ADOPT))


def build_assessment_report() -> str:
    """生成数据源评估报告（Markdown），供 docs 与 API 复用同一份事实。"""
    lines: list[str] = ["# 另类数据源评估报告（issue #33）", ""]
    lines.append(
        "评估口径：覆盖股票数、历史长度、更新频率、获取方式、合规风险、成本、结论。"
    )
    lines.append("")
    lines.append("| 结论 | 数量 |")
    lines.append("| --- | --- |")
    for label, decision in (
        ("可用 adopt", ADOPT),
        ("有条件 conditional", CONDITIONAL),
        ("不接入 reject", REJECT),
    ):
        lines.append(f"| {label} | {len(assessments_by_decision(decision))} |")
    lines.append("")
    for item in SOURCE_ASSESSMENTS:
        lines.append(f"## {item.title}")
        lines.append("")
        lines.append(f"- 结论：**{item.decision}**")
        lines.append(f"- 渠道：{item.channel}")
        lines.append(f"- 获取方式：{item.access}")
        lines.append(f"- 覆盖：{item.coverage}")
        lines.append(f"- 历史：{item.history}")
        lines.append(f"- 频率：{item.frequency}")
        lines.append(f"- 合规风险：{item.compliance_risk}")
        lines.append(f"- 成本：{item.cost}")
        lines.append(f"- 产出因子：{', '.join(item.factors)}")
        lines.append(f"- 理由：{item.reason}")
        if item.akshare_functions:
            lines.append(
                "- 相关接口：" + ", ".join(f"`{fn}`" for fn in item.akshare_functions)
            )
        lines.append("")
    lines.append("## 全局结论")
    lines.append("")
    lines.append(
        "1. 首期只接入 **千股千评关注度**（全市场快照，天然满足 ≥80% 涨停候选覆盖）"
        "与 **互动易问答** 两个可回溯、无需登录的合规来源。"
    )
    lines.append(
        "2. 个股新闻走同一套文本抽取管线，但因接口形态接近网页抓取，默认关闭，"
        "需显式开关 `ALPHA_ALT_NEWS_ENABLED=true`。"
    )
    lines.append(
        "3. 搜索指数、股吧发帖、研报目标价、游资社区在拿到书面授权前一律不接入，"
        "对应因子保持显式缺失（`None`），禁止用 0 代替。"
    )
    lines.append(
        "4. 所有因子上线前必须先过离线 IC / 分层收益与泄漏校验（见 `evaluation.py`），"
        "无增量就记录并停止。"
    )
    lines.append("")
    return "\n".join(lines)
