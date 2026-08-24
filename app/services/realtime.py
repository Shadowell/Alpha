from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from typing import Any

from fastapi import WebSocket


_SEND_TIMEOUT_SECONDS = 5.0


class RealtimeHub:
    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._clients.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self._clients.discard(websocket)

    async def broadcast(self, event: str, payload: dict[str, Any]) -> None:
        if not self._clients:
            return
        msg = json.dumps({"event": event, "data": payload}, ensure_ascii=False)

        # 遍历快照副本：await 期间 connect/disconnect 可能并发修改集合，
        # 直接迭代会触发 "Set changed size during iteration"。
        disconnected: list[WebSocket] = []
        for ws in list(self._clients):
            try:
                # 单个慢/半死客户端不能拖住整个广播（队头阻塞 ticker 循环）
                await asyncio.wait_for(ws.send_text(msg), timeout=_SEND_TIMEOUT_SECONDS)
            except Exception:
                disconnected.append(ws)

        for ws in disconnected:
            self._clients.discard(ws)
            with suppress(Exception):
                await ws.close()
