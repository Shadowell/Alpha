"""飞书 Webhook 通知 — 支持文本与交互式卡片两种形式。

## 使用方式

```python
from app.services.feishu_notify import send_feishu_card, CardBuilder

await send_feishu_card(
    CardBuilder(title="📊 K 线同步完成", template="green")
    .add_kv_grid([("成功", "3012"), ("失败", "12")])
    .add_markdown("耗时 **42.3s**")
    .build()
)
```

卡片风格统一为"简洁信息卡"：标题带色带 + KV 网格 + 可选段落 + 可选按钮。
"""
from __future__ import annotations

import os
from typing import Iterable

import httpx

_DEFAULT_WEBHOOK = (
    "https://open.feishu.cn/open-apis/bot/v2/hook/"
    "186eaf03-826f-4793-ab8f-c9f2d9149482"
)


def _webhook_url() -> str:
    return os.environ.get("FEISHU_WEBHOOK_URL") or os.environ.get("FEISHU_WEBHOOK") or _DEFAULT_WEBHOOK


_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=15)
    return _client


async def send_feishu_text(text: str) -> dict:
    """通过 Webhook 发送纯文本消息（保留用于极简场景）。"""
    payload = {"msg_type": "text", "content": {"text": text}}
    try:
        resp = await _get_client().post(_webhook_url(), json=payload)
        return resp.json()
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def send_feishu_card(card: dict) -> dict:
    """通过 Webhook 发送交互式卡片。

    `card` 为完整的飞书卡片 JSON（i18n_elements 或 elements 结构），
    建议使用 `CardBuilder` 构建以保持视觉统一。
    """
    payload = {"msg_type": "interactive", "card": card}
    try:
        resp = await _get_client().post(_webhook_url(), json=payload)
        return resp.json()
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ────────────────────────────────────────────────────────────────
# CardBuilder — 简洁卡片构建器
# ────────────────────────────────────────────────────────────────

# 飞书卡片支持的 header 模板色
_TEMPLATE_COLORS = {
    "blue", "wathet", "turquoise", "green", "yellow", "orange",
    "red", "carmine", "violet", "purple", "indigo", "grey",
}


class CardBuilder:
    """最小化的飞书卡片构建器 — 保持简洁、视觉一致。"""

    def __init__(self, title: str, *, subtitle: str = "", template: str = "blue") -> None:
        if template not in _TEMPLATE_COLORS:
            template = "blue"
        self._title = title
        self._subtitle = subtitle
        self._template = template
        self._elements: list[dict] = []

    # —— 基础组件 ——
    def add_markdown(self, content: str) -> "CardBuilder":
        """追加一段 markdown 正文。"""
        if content:
            self._elements.append({"tag": "markdown", "content": content})
        return self

    def add_hr(self) -> "CardBuilder":
        self._elements.append({"tag": "hr"})
        return self

    def add_note(self, text: str) -> "CardBuilder":
        """底部灰色备注（时间戳、环境等）。"""
        if text:
            self._elements.append(
                {
                    "tag": "note",
                    "elements": [{"tag": "plain_text", "content": text}],
                }
            )
        return self

    # —— KV 网格（2-4 列自适应） ——
    def add_kv_grid(self, items: Iterable[tuple[str, str]], *, cols: int = 2) -> "CardBuilder":
        """把 (label, value) 对排成网格。"""
        items = list(items)
        if not items:
            return self
        cols = max(1, min(4, cols))
        fields: list[dict] = []
        for k, v in items:
            fields.append(
                {
                    "is_short": cols > 1,
                    "text": {
                        "tag": "lark_md",
                        "content": f"**{k}**\n{v}",
                    },
                }
            )
        self._elements.append({"tag": "div", "fields": fields})
        return self

    # —— 单段 K-V（横向） ——
    def add_kv_inline(self, items: Iterable[tuple[str, str]]) -> "CardBuilder":
        parts = [f"**{k}** {v}" for k, v in items]
        if parts:
            self._elements.append({"tag": "markdown", "content": "  ·  ".join(parts)})
        return self

    # —— 按钮行（跳转链接） ——
    def add_link_button(self, text: str, url: str, *, primary: bool = False) -> "CardBuilder":
        btn_type = "primary" if primary else "default"
        self._elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": text},
                        "url": url,
                        "type": btn_type,
                    }
                ],
            }
        )
        return self

    # —— 构建 ——
    def build(self) -> dict:
        title_el = {
            "tag": "plain_text",
            "content": self._title,
        }
        header: dict = {"title": title_el, "template": self._template}
        if self._subtitle:
            header["subtitle"] = {"tag": "plain_text", "content": self._subtitle}

        return {
            "config": {"wide_screen_mode": True, "update_multi": True},
            "header": header,
            "elements": self._elements,
        }


# ────────────────────────────────────────────────────────────────
# 预制卡片（各业务模块调用）
# ────────────────────────────────────────────────────────────────

async def notify_sync_complete(
    trade_date: str,
    success_count: int,
    failed_count: int,
    total: int,
    elapsed_sec: float,
    mode: str = "全量",
) -> dict:
    """K 线同步完成通知 — 卡片样式。"""
    success_rate = (success_count / total * 100) if total else 0
    template = "green" if failed_count == 0 else ("yellow" if success_rate >= 95 else "red")
    card = (
        CardBuilder(title=f"📊 K 线{mode}同步完成", subtitle=f"交易日 {trade_date}", template=template)
        .add_kv_grid(
            [
                ("✅ 成功", f"{success_count} 只"),
                ("❌ 失败", f"{failed_count} 只"),
                ("📈 总计", f"{total} 只"),
                ("⏱ 耗时", f"{elapsed_sec:.1f}s"),
            ],
            cols=2,
        )
        .add_note(f"Alpha · {mode}同步 · 成功率 {success_rate:.1f}%")
        .build()
    )
    return await send_feishu_card(card)


async def notify_predict_top(
    trade_date: str,
    top_entries: list[dict],
    meta: dict,
) -> dict:
    """预测选股 Top 推送 — 卡片样式。"""
    lines = []
    for i, e in enumerate(top_entries[:10], start=1):
        boards = "、".join(b["name"] for b in e.get("boards", [])[:2]) or "-"
        lines.append(
            f"`{i:>2}` **{e['name']}**({e['symbol']})  高 +{e['pred_max_high_pct']:.1f}%  "
            f"收 +{e['pred_last_close_pct']:.1f}%  · {boards}"
        )
    elapsed = meta.get("elapsed_sec", 0)
    boards_used = meta.get("boards_used", 0)
    stocks_scanned = meta.get("stocks_scanned", 0)

    card = (
        CardBuilder(title="🔮 预测选股 Top 10", subtitle=f"交易日 {trade_date}", template="purple")
        .add_kv_inline(
            [
                ("板块", f"{boards_used}"),
                ("个股", f"{stocks_scanned}"),
                ("耗时", f"{elapsed}s"),
            ]
        )
        .add_hr()
        .add_markdown("\n".join(lines) if lines else "_无预测结果_")
        .add_note(f"Kronos · {meta.get('kronos_device', 'unknown')}")
        .build()
    )
    return await send_feishu_card(card)
