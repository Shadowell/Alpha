"""打板核心数据底座只读路由（issue #22）。

路由前缀: /api（由 main.py include 时指定）。全部为 GET 只读接口：

* ``/data/tushare-capabilities`` —— Tushare 打板相关接口能力矩阵；
* ``/data/limit-board/status``   —— 数据集状态与覆盖率；
* ``/data/limit-board/{date}``   —— 当日涨停/跌停/炸板 + 连板天梯；
* ``/data/dragon-tiger/{date}``  —— 当日龙虎榜与营业部席位；
* ``/data/hot-money/seats``      —— 游资席位名录。

约定：空数据返回 ``items: []``（不是 500）；参数非法 400；内部异常经
:func:`app.services.error_response.error_detail` 脱敏，只回通用文案 + error_id。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Query

from app.container import ServiceContainer
from app.dependencies import get_container
from app.models import (
    DragonTigerDayOut,
    HotMoneySeatOut,
    LimitBoardDayOut,
    LimitBoardStatusOut,
    TushareCapabilitiesOut,
)
from app.services.error_response import error_detail
from app.services.limit_board_service import LimitBoardService
from app.services.time_utils import now_cn

log = logging.getLogger("routers.limit_board")

router = APIRouter(tags=["打板数据"])

_TRADE_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$|^\d{8}$")
_BOARDS = ("U", "D", "Z")


def _service(container: ServiceContainer) -> LimitBoardService:
    return container.limit_board_service


def _normalize_trade_date(value: str) -> str:
    """YYYYMMDD / YYYY-MM-DD → ISO 日期；非法输入直接 400。"""

    text = str(value or "").strip()
    if not _TRADE_DATE_RE.match(text):
        raise HTTPException(
            status_code=400, detail="trade_date 需为 YYYY-MM-DD 或 YYYYMMDD"
        )
    iso = f"{text[:4]}-{text[4:6]}-{text[6:8]}" if len(text) == 8 else text
    try:
        dt.date.fromisoformat(iso)
    except ValueError:
        raise HTTPException(status_code=400, detail="trade_date 不是有效日期") from None
    return iso


@router.get("/data/tushare-capabilities", response_model=TushareCapabilitiesOut)
async def get_tushare_capabilities(
    refresh: bool = Query(False, description="忽略缓存，逐接口重新探测权限"),
    container: ServiceContainer = Depends(get_container),
):
    service = _service(container)
    if not refresh and not service.capability_snapshot:
        # 未探测时只读返回空矩阵并说明来源，避免一次 GET 就打满上游配额
        payload = {"apis": [], "enabled": service.is_enabled}
    else:
        try:
            payload = dict(await service.probe_capabilities(force=refresh))
        except Exception as exc:  # noqa: BLE001 — 探测失败不得泄露上游报文
            log.warning("tushare capability probe failed: %s", type(exc).__name__)
            return error_detail(
                "能力矩阵暂时不可用", exc, 503, context="tushare capabilities"
            )
    payload["probed"] = bool(payload.get("apis"))
    if not payload["probed"]:
        payload["note"] = (
            "尚未执行探测；可带 refresh=true 调用本接口，或服务启动后自动填充"
        )
    return payload


@router.get("/data/limit-board/status", response_model=LimitBoardStatusOut)
async def get_limit_board_status(container: ServiceContainer = Depends(get_container)):
    service = _service(container)
    try:
        capabilities = service.capability_snapshot or {
            "apis": [],
            "enabled": service.is_enabled,
        }
        payload = await asyncio.to_thread(service.status_payload, capabilities)
    except Exception as exc:  # noqa: BLE001 — 状态端点异常同样脱敏
        log.warning("limit board status failed: %s", type(exc).__name__)
        return error_detail(
            "打板数据状态暂时不可用", exc, 503, context="limit board status"
        )
    payload["server_time"] = now_cn().isoformat()
    return payload


@router.get("/data/limit-board/{trade_date}", response_model=LimitBoardDayOut)
async def get_limit_board_by_date(
    trade_date: str,
    board: str | None = Query(None, description="板块过滤：U=涨停 / D=跌停 / Z=炸板"),
    min_limit_times: int | None = Query(None, ge=1, le=30, description="最小连板数"),
    limit: int = Query(300, ge=1, le=2000, description="最多返回条数"),
    container: ServiceContainer = Depends(get_container),
):
    query_date = _normalize_trade_date(trade_date)
    if board is not None and board not in _BOARDS:
        raise HTTPException(status_code=400, detail="board 仅支持 U / D / Z")
    service = _service(container)
    try:
        items = await asyncio.to_thread(
            service.store.get_limit_board_daily,
            query_date,
            board=board,
            min_limit_times=min_limit_times,
            limit=limit,
        )
        ladder = await asyncio.to_thread(service.store.get_limit_ladder, query_date)
        coverage = await asyncio.to_thread(
            service.store.get_limit_board_coverage, query_date
        )
    except Exception as exc:  # noqa: BLE001 — 查询异常脱敏
        log.warning("limit board query failed: %s", type(exc).__name__)
        return error_detail(
            "涨停板数据暂时不可用", exc, 500, context="limit board query"
        )
    return {
        "trade_date": query_date,
        "board": board,
        "count": len(items),
        "items": items,
        "ladder": ladder,
        "coverage": coverage,
    }


@router.get("/data/dragon-tiger/{trade_date}", response_model=DragonTigerDayOut)
async def get_dragon_tiger_by_date(
    trade_date: str,
    symbol: str | None = Query(None, max_length=12, description="按 6 位代码过滤"),
    seat_limit: int = Query(500, ge=1, le=2000, description="席位最多返回条数"),
    container: ServiceContainer = Depends(get_container),
):
    query_date = _normalize_trade_date(trade_date)
    clean_symbol = str(symbol or "").strip() or None
    if clean_symbol and not re.fullmatch(r"\d{6}", clean_symbol):
        raise HTTPException(status_code=400, detail="symbol 需为 6 位数字代码")
    service = _service(container)
    try:
        items = await asyncio.to_thread(
            service.store.get_dragon_tiger, query_date, clean_symbol
        )
        seats = await asyncio.to_thread(
            service.store.get_dragon_tiger_seats,
            query_date,
            clean_symbol,
            limit=seat_limit,
        )
    except Exception as exc:  # noqa: BLE001 — 查询异常脱敏
        log.warning("dragon tiger query failed: %s", type(exc).__name__)
        return error_detail(
            "龙虎榜数据暂时不可用", exc, 500, context="dragon tiger query"
        )
    return {
        "trade_date": query_date,
        "symbol": clean_symbol,
        "count": len(items),
        "items": items,
        "seats": seats,
    }


@router.get("/data/hot-money/seats", response_model=HotMoneySeatOut)
async def get_hot_money_seats(
    keyword: str | None = Query(
        None, max_length=40, description="按席位名/简介模糊匹配"
    ),
    limit: int = Query(200, ge=1, le=1000),
    container: ServiceContainer = Depends(get_container),
):
    clean_keyword = str(keyword or "").strip() or None
    service = _service(container)
    try:
        items = await asyncio.to_thread(
            service.store.get_hot_money_seats, clean_keyword, limit
        )
    except Exception as exc:  # noqa: BLE001 — 查询异常脱敏
        log.warning("hot money seats query failed: %s", type(exc).__name__)
        return error_detail(
            "游资席位数据暂时不可用", exc, 500, context="hot money seats query"
        )
    return {"count": len(items), "items": items}
