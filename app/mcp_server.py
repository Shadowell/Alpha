"""Alpha MCP Server — 将 Alpha 量化选股系统的核心 API 暴露为 MCP 工具。

通过 stdio 方式运行，供 Hermes Agent 通过 MCP 协议调用。

Usage:
    python app/mcp_server.py
"""
from __future__ import annotations

import json
import os

import httpx
from mcp.server.fastmcp import FastMCP

ALPHA_API_BASE = os.environ.get("ALPHA_API_BASE", "http://127.0.0.1:18888")

mcp = FastMCP(
    name="alpha-quant",
    instructions=(
        "Alpha 量化选股系统工具集。提供漏斗状态查询、策略参数读取、"
        "公告筛选数据、个股详情、K线数据和提案管理能力。"
    ),
)


async def _get(path: str, params: dict | None = None) -> dict:
    async with httpx.AsyncClient(base_url=ALPHA_API_BASE, timeout=30) as client:
        resp = await client.get(path, params=params)
        resp.raise_for_status()
        return resp.json()


async def _post(path: str, body: dict | None = None) -> dict:
    async with httpx.AsyncClient(base_url=ALPHA_API_BASE, timeout=60) as client:
        resp = await client.post(path, json=body or {})
        resp.raise_for_status()
        return resp.json()


# ── 漏斗状态 ──


@mcp.tool()
async def get_funnel_snapshot(trade_date: str | None = None) -> str:
    """获取策略漏斗快照：candidate/focus/buy 三个池子的股票列表和统计。

    Args:
        trade_date: 交易日期 (YYYY-MM-DD)，缺省为最新
    """
    params = {}
    if trade_date:
        params["trade_date"] = trade_date
    data = await _get("/api/funnel", params)
    return json.dumps(data, ensure_ascii=False, indent=2)


@mcp.tool()
async def get_strategy_profile() -> str:
    """获取当前策略配置：所有可调参数及其当前值。"""
    data = await _get("/api/strategy/profile")
    return json.dumps(data, ensure_ascii=False, indent=2)


@mcp.tool()
async def get_stock_detail(symbol: str, kline_days: int = 30) -> str:
    """获取个股详情：基本面指标 + K 线数据。

    Args:
        symbol: 股票代码，如 '603577'
        kline_days: K线天数，默认30
    """
    data = await _get(f"/api/stock/{symbol}/detail", {"kline_days": kline_days})
    return json.dumps(data, ensure_ascii=False, indent=2)


# ── 公告选股 ──


@mcp.tool()
async def get_notice_funnel(trade_date: str | None = None) -> str:
    """获取公告选股漏斗：三个池子的公告股票列表和统计。

    Args:
        trade_date: 日期 (YYYY-MM-DD)，缺省为最新
    """
    params = {}
    if trade_date:
        params["trade_date"] = trade_date
    data = await _get("/api/notice/funnel", params)
    return json.dumps(data, ensure_ascii=False, indent=2)


@mcp.tool()
async def get_notice_keywords() -> str:
    """获取公告选股的关键词规则列表：标签和权重。"""
    data = await _get("/api/notice/keywords")
    return json.dumps(data, ensure_ascii=False, indent=2)


@mcp.tool()
async def get_notice_detail(symbol: str, days: int = 30) -> str:
    """获取公告个股详情：公告内容 + K 线。

    Args:
        symbol: 股票代码
        days: K线天数
    """
    data = await _get(f"/api/notice/{symbol}/detail", {"days": days})
    return json.dumps(data, ensure_ascii=False, indent=2)


# ── K 线与市场数据 ──


@mcp.tool()
async def get_kline(symbol: str, days: int = 30) -> str:
    """获取指定个股的日 K 线数据。

    Args:
        symbol: 股票代码
        days: 历史天数
    """
    data = await _get(f"/api/kline/{symbol}", {"days": days})
    return json.dumps(data, ensure_ascii=False, indent=2)


@mcp.tool()
async def get_kline_sync_status() -> str:
    """获取 K 线缓存同步状态：最后同步时间、覆盖率、进度。"""
    data = await _get("/api/jobs/kline-cache/status")
    return json.dumps(data, ensure_ascii=False, indent=2)


@mcp.tool()
async def get_kline_cache_stats() -> str:
    """获取 K 线缓存统计：总股票数、覆盖天数分布。"""
    data = await _get("/api/jobs/kline-cache/stats")
    return json.dumps(data, ensure_ascii=False, indent=2)


@mcp.tool()
async def get_hot_concepts() -> str:
    """获取当日热门概念板块 Top 10。"""
    data = await _get("/api/market/hot-concepts")
    return json.dumps(data, ensure_ascii=False, indent=2)


@mcp.tool()
async def get_hot_stocks() -> str:
    """获取当日热门个股 Top 30。"""
    data = await _get("/api/market/hot-stocks")
    return json.dumps(data, ensure_ascii=False, indent=2)


# ── Kronos 预测 ──


@mcp.tool()
async def predict_kronos(symbol: str, lookback: int = 30, horizon: int = 3) -> str:
    """调用 Kronos 时序模型预测个股未来 K 线走势。返回历史+预测合并 K 线、预测起始索引、预测明细。

    这是真实的 AI 模型推理结果，任何关于股价走势的预测必须通过本工具获取，禁止自行编造预测数据。

    Args:
        symbol: 股票代码，如 '603577'
        lookback: 回看天数（默认30）
        horizon: 预测天数（默认3）
    """
    try:
        data = await _get(f"/api/predict/{symbol}/kronos", {"lookback": lookback, "horizon": horizon})
        pk = data.get("predicted_kline", [])
        if not pk:
            return json.dumps({"symbol": symbol, "prediction": "无预测结果（可能K线数据不足）"}, ensure_ascii=False)
        summary = []
        for k in pk:
            chg = round((k["close"] - k["open"]) / k["open"] * 100, 2) if k["open"] else 0
            summary.append(f"{k['date']}: 开{k['open']:.2f} 高{k['high']:.2f} 低{k['low']:.2f} 收{k['close']:.2f} 量{k.get('volume',0):.0f} ({'+' if chg>=0 else ''}{chg}%)")
        result = {
            "symbol": symbol,
            "model": "Kronos",
            "horizon": horizon,
            "prediction_summary": summary,
            "predicted_kline": pk,
        }
        return json.dumps(result, ensure_ascii=False, indent=2)
    except httpx.HTTPStatusError as e:
        return json.dumps({"symbol": symbol, "error": f"预测失败({e.response.status_code}): {e.response.text[:200]}"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"symbol": symbol, "error": f"预测异常: {str(e)[:200]}"}, ensure_ascii=False)


@mcp.tool()
async def get_stock_realtime(symbol: str) -> str:
    """获取个股盘中实时行情（当天 OHLCV、涨跌幅、成交额）。

    Args:
        symbol: 股票代码，如 '603577'
    """
    data = await _get(f"/api/stock/{symbol}/realtime")
    return json.dumps(data, ensure_ascii=False, indent=2)


# ── 提案管理 ──


@mcp.tool()
async def propose_rule_patch(
    title: str,
    diff: str,
    reasoning: str,
    expected_impact: str,
    confidence: float = 0.5,
    evidence: str = "[]",
    risk_level: str = "medium",
) -> str:
    """创建策略参数调整提案（rule_patch 类型）。提案创建后处于 pending 状态，等待人工审批。

    Args:
        title: 提案标题
        diff: 参数变更的 JSON 字符串，如 '{"box_range_threshold": {"from": 0.18, "to": 0.20}}'
        reasoning: 推理过程
        expected_impact: 预期影响
        confidence: 置信度 0-1
        evidence: 证据列表的 JSON 字符串
        risk_level: 风险等级 low/medium/high
    """
    diff_obj = json.loads(diff) if isinstance(diff, str) else diff
    evidence_obj = json.loads(evidence) if isinstance(evidence, str) else evidence

    result = await _post("/api/agent/proposals/create", {
        "type": "rule_patch",
        "title": title,
        "risk_level": risk_level,
        "diff": diff_obj,
        "reasoning": reasoning,
        "expected_impact": expected_impact,
        "confidence": confidence,
        "evidence": evidence_obj,
    })
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def propose_notice_rule_patch(
    title: str,
    diff: str,
    reasoning: str,
    expected_impact: str,
    confidence: float = 0.5,
    evidence: str = "[]",
    risk_level: str = "medium",
) -> str:
    """创建公告规则调整提案（notice_rule_patch 类型）。提案创建后处于 pending 状态，等待人工审批。

    Args:
        title: 提案标题
        diff: 建议的 JSON 字符串，如 '{"说明": "将业绩预增权重从14调为16"}'
        reasoning: 推理过程
        expected_impact: 预期影响
        confidence: 置信度 0-1
        evidence: 证据列表的 JSON 字符串
        risk_level: 风险等级 low/medium/high
    """
    diff_obj = json.loads(diff) if isinstance(diff, str) else diff
    evidence_obj = json.loads(evidence) if isinstance(evidence, str) else evidence

    result = await _post("/api/agent/proposals/create", {
        "type": "notice_rule_patch",
        "title": title,
        "risk_level": risk_level,
        "diff": diff_obj,
        "reasoning": reasoning,
        "expected_impact": expected_impact,
        "confidence": confidence,
        "evidence": evidence_obj,
    })
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def list_proposals(status: str | None = None, limit: int = 10) -> str:
    """列出现有提案。

    Args:
        status: 过滤状态：pending/approved/rejected，缺省全部
        limit: 返回数量
    """
    params: dict = {"limit": limit}
    if status:
        params["status"] = status
    data = await _get("/api/agent/proposals", params)
    return json.dumps(data, ensure_ascii=False, indent=2)


@mcp.tool()
async def get_proposal_detail(proposal_id: int) -> str:
    """获取提案详情，包含历史反馈。

    Args:
        proposal_id: 提案 ID
    """
    data = await _get(f"/api/agent/proposals/{proposal_id}")
    return json.dumps(data, ensure_ascii=False, indent=2)


@mcp.tool()
async def approve_proposal(proposal_id: int, note: str = "") -> str:
    """批准一个待处理提案。

    Args:
        proposal_id: 提案 ID
        note: 批准备注
    """
    data = await _post(f"/api/agent/proposals/{proposal_id}/approve", {"note": note})
    return json.dumps(data, ensure_ascii=False, indent=2)


@mcp.tool()
async def reject_proposal(proposal_id: int, note: str = "") -> str:
    """驳回一个待处理提案。

    Args:
        proposal_id: 提案 ID
        note: 驳回原因
    """
    data = await _post(f"/api/agent/proposals/{proposal_id}/reject", {"note": note})
    return json.dumps(data, ensure_ascii=False, indent=2)


@mcp.tool()
async def get_agent_status() -> str:
    """获取 Hermes Agent 运行状态：是否在运行、LLM 可用性、最近任务。"""
    data = await _get("/api/agent/status")
    return json.dumps(data, ensure_ascii=False, indent=2)


@mcp.tool()
async def list_agent_tasks(limit: int = 10) -> str:
    """列出最近的 Agent 任务执行记录。

    Args:
        limit: 返回数量
    """
    data = await _get("/api/agent/tasks", {"limit": limit})
    return json.dumps(data, ensure_ascii=False, indent=2)


# ── 作业触发 ──


@mcp.tool()
async def trigger_eod_screen(trade_date: str | None = None) -> str:
    """触发盘后筛选：从全 A 股中筛选调整期候选个股。

    Args:
        trade_date: 交易日期，缺省为最新交易日
    """
    body = {}
    if trade_date:
        body["trade_date"] = trade_date
    data = await _post("/api/jobs/eod-screen", body)
    return json.dumps(data, ensure_ascii=False, indent=2)


@mcp.tool()
async def trigger_notice_screen(
    notice_date: str | None = None,
    limit: int = 10,
    keywords: str | None = None,
) -> str:
    """触发公告筛选：从当日公告中筛选利好个股。

    Args:
        notice_date: 公告日期，缺省当天
        limit: 每标签取前 N 条
        keywords: 逗号分隔的关键词过滤
    """
    params: dict = {"limit": limit}
    if notice_date:
        params["notice_date"] = notice_date
    if keywords:
        params["keywords"] = keywords
    data = await _post("/api/jobs/notice-screen", params)
    return json.dumps(data, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    mcp.run()
