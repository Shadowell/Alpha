"""飞书 Webhook 通知 — 用于盘后同步完成后推送消息到飞书群。"""
from __future__ import annotations

import asyncio
import httpx

FEISHU_WEBHOOK_URL = (
    "https://open.feishu.cn/open-apis/bot/v2/hook/"
    "186eaf03-826f-4793-ab8f-c9f2d9149482"
)

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=15)
    return _client


async def send_feishu_text(text: str) -> dict:
    """通过 Webhook 发送纯文本消息到飞书群。"""
    payload = {"msg_type": "text", "content": {"text": text}}
    try:
        resp = await _get_client().post(FEISHU_WEBHOOK_URL, json=payload)
        return resp.json()
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def notify_sync_complete(
    trade_date: str,
    success_count: int,
    failed_count: int,
    total: int,
    elapsed_sec: float,
    mode: str = "全量",
) -> dict:
    """同步完成后发送汇总通知。"""
    lines = [
        f"📊 Alpha K线{mode}同步完成",
        f"━━━━━━━━━━━━━━━━━━",
        f"📅 交易日：{trade_date}",
        f"✅ 成功：{success_count} 只",
        f"❌ 失败：{failed_count} 只",
        f"📈 总计：{total} 只",
        f"⏱️ 耗时：{elapsed_sec:.1f} 秒",
    ]
    return await send_feishu_text("\n".join(lines))
