"""WebSocket 实时快照路由（保留原 /ws/realtime 路径）。"""
from __future__ import annotations

import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

log = logging.getLogger("routers.ws")

router = APIRouter()


@router.websocket("/ws/realtime")
async def realtime_socket(websocket: WebSocket):
    container = websocket.app.state.container
    hub = container.hub
    await hub.connect(websocket)
    try:
        funnel = await container.funnel_service.get_funnel()
        hot = await container.funnel_service.get_hot_concepts()
        hot_stocks = await container.funnel_service.get_hot_stocks()
        await websocket.send_json(
            {
                "event": "snapshot",
                "data": {
                    "funnel": funnel.model_dump(),
                    "hot_concepts": hot.model_dump(),
                    "hot_stocks": hot_stocks.model_dump(),
                },
            }
        )
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        hub.disconnect(websocket)
    except Exception as exc:  # noqa: BLE001 — 单连接异常隔离
        log.warning("websocket error: %s", exc)
        hub.disconnect(websocket)
