from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx


log = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是A股公告事件分析助手。请仅基于公告标题和类型判断短期上涨潜力。
输出 JSON 数组，每项包含:
- code: 股票代码
- score: 0-100 分
- reason: 不超过40字
- risk: 不超过30字
要求:
1) 分数体现短线事件驱动强度
2) 若信息不足，给中性分 50 附近
3) 仅输出 JSON，不要额外文字
"""


def _resolve_endpoint() -> tuple[str, str, str]:
    """返回 (url, api_key, model)。未正确配置时 api_key 为空。"""
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/")
    model = os.getenv("HERMES_MODEL", "gpt-4o-mini").strip()
    # 占位 key（如 .env.example 中 Ollama 方案的 "ollama"）视为未配置真实凭据，
    # 但仍允许请求 —— 本地推理服务通常不校验 key。
    return f"{base_url}/responses", api_key, model


async def score_with_llm(
    notices: list[dict[str, Any]],
    timeout_seconds: int = 40,
) -> tuple[dict[str, dict[str, Any]], bool]:
    """LLM 公告打分。

    返回 (scores, llm_enabled)。任何失败都返回 ( {}, False) 并记录日志，
    绝不把故障伪装成"已启用"，避免错误状态被持久化误导下游复盘。
    """
    url, api_key, model = _resolve_endpoint()
    if not api_key:
        return {}, False

    items = [
        {
            "code": n.get("code", ""),
            "name": n.get("name", ""),
            "title": n.get("title", ""),
            "notice_type": n.get("notice_type", ""),
        }
        for n in notices
    ]
    if not items:
        return {}, True

    user_prompt = f"请给以下公告打分:\n{json.dumps(items, ensure_ascii=False)}"
    payload = {
        "model": model,
        "input": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    }

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds, trust_env=False) as client:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
        output_text = _extract_text(data)
        parsed = json.loads(output_text)
        result: dict[str, dict[str, Any]] = {}
        for row in parsed:
            code = str(row.get("code", "")).zfill(6)
            if not code:
                continue
            result[code] = {
                "score": float(row.get("score", 50)),
                "reason": str(row.get("reason", "")),
                "risk": str(row.get("risk", "")),
            }
        return result, True
    except Exception:
        log.exception("[notice_llm] LLM scoring failed (endpoint=%s model=%s)", url, model)
        return {}, False


def _extract_text(payload: dict[str, Any]) -> str:
    if isinstance(payload.get("output_text"), str) and payload.get("output_text"):
        return str(payload["output_text"])

    output = payload.get("output", [])
    chunks: list[str] = []
    for item in output:
        for content in item.get("content", []):
            text = content.get("text")
            if text:
                chunks.append(str(text))
    return "".join(chunks).strip()
