from __future__ import annotations

import asyncio

import datetime as dt
import os
from dataclasses import dataclass
from typing import Any

import pandas as pd

from app.services.blocking_io import (
    ExternalCircuitOpenError,
    ExternalTimeoutError,
    run_blocking,
)
from app.services.market_data_client import EastmoneyMarketDataClient, _normalize_symbol

try:
    import tushare as _tushare
except Exception:  # pragma: no cover - optional in minimal environments
    _tushare = None


def _enabled_from_env() -> bool:
    return os.getenv("ENABLE_TUSHARE", "true").strip().lower() not in {"0", "false", "no", "off"}


def _compact_date(value: str) -> str:
    text = str(value or "").strip().replace("-", "")
    return text[:8] if len(text) >= 8 and text[:8].isdigit() else ""


def _display_date(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    parsed = pd.to_datetime(text, errors="coerce")
    return "" if pd.isna(parsed) else parsed.date().isoformat()


def _to_ts_code(symbol: str) -> str:
    clean = _normalize_symbol(symbol)
    if clean.startswith(("6", "68")):
        return f"{clean}.SH"
    if clean.startswith(("4", "8")):
        return f"{clean}.BJ"
    return f"{clean}.SZ"


# ── 打板数据底座（issue #22）：接口能力探测 ──────────────────────

# 启动探测覆盖的接口；顺序即能力矩阵顺序。
LIMIT_BOARD_APIS: tuple[str, ...] = (
    "limit_list_d",
    "limit_step",
    "kpl_list",
    "top_list",
    "top_inst",
    "hm_detail",
    "hm_list",
    "hsgt_top10",
)

# 单次调用结果状态（数据层面），与能力状态共同决定同步记录。
RESULT_OK = "ok"
RESULT_EMPTY = "empty"

# 能力状态（对外可见，写入能力矩阵与同步状态表）。
CAPABILITY_AVAILABLE = "available"
CAPABILITY_DENIED = "denied"
CAPABILITY_ERROR = "error"
CAPABILITY_NOT_CONFIGURED = "not_configured"

# 探测时使用的最小化请求：单只股票 + 单日，只为判别权限，不拉全市场。
_PROBE_TS_CODE = "000001.SZ"

# 各接口允许的请求参数（Tushare 对未知参数会直接报错，必须白名单）。
_LIMIT_API_PARAMS: dict[str, tuple[str, ...]] = {
    "limit_list_d": ("trade_date", "ts_code"),
    "limit_step": ("trade_date", "ts_code"),
    "kpl_list": ("trade_date", "ts_code"),
    "top_list": ("trade_date", "ts_code"),
    "top_inst": ("trade_date", "ts_code"),
    "hm_detail": ("trade_date", "ts_code"),
    "hm_list": (),
    "hsgt_top10": ("trade_date", "ts_code"),
}

# 显式 fields：只取打板底座用到的列，减少流量。
# 未列出的接口（limit_step / hm_detail，本机账号无权限）不做裁剪，
# 由字段映射层按别名兼容，避免猜错的 fields 直接把可用账号打成 error。
_LIMIT_API_FIELDS: dict[str, str] = {
    "limit_list_d": (
        "trade_date,ts_code,industry,name,close,pct_chg,amount,limit_amount,"
        "float_mv,total_mv,turnover_ratio,fd_amount,first_time,last_time,"
        "open_times,up_stat,limit_times,limit"
    ),
    "kpl_list": (
        "ts_code,name,trade_date,lu_time,open_time,last_time,lu_desc,tag,"
        "theme,status,amount,turnover_rate,free_float"
    ),
    "top_list": (
        "trade_date,ts_code,name,close,pct_change,turnover_rate,amount,l_sell,"
        "l_buy,l_amount,net_amount,net_rate,amount_rate,float_values,reason"
    ),
    "top_inst": (
        "trade_date,ts_code,exalter,buy,buy_rate,sell,sell_rate,net_buy,side,reason"
    ),
    "hsgt_top10": (
        "trade_date,ts_code,name,close,change,rank,market_type,amount,"
        "net_amount,buy,sell"
    ),
}

_PERMISSION_HINTS = ("权限", "积分", "配额", "permission", "quota", "forbidden")
_EMPTY_HINTS = ("无数据", "not found", "no data")


def classify_tushare_error(exc: BaseException, *, token: str = "") -> tuple[str, str]:
    """把 Tushare 异常归一为 ``(capability_status, safe_reason)``。

    只返回稳定代号，绝不回传原始报文：上游报错里可能带 URL/账号信息，
    而 Token 本身也可能出现在个别 SDK 文案里，因此额外做一次脱敏兜底。
    """
    if isinstance(exc, ExternalCircuitOpenError):
        return CAPABILITY_ERROR, "circuit_open"
    if isinstance(exc, ExternalTimeoutError):
        return CAPABILITY_ERROR, "timeout"
    raw = str(exc)
    lowered = raw.lower()
    if token:
        raw = raw.replace(token, "***")
        lowered = lowered.replace(token.lower(), "***")
    if any(hint in lowered or hint in raw for hint in _PERMISSION_HINTS):
        return CAPABILITY_DENIED, "permission_denied"
    if any(hint in lowered or hint in raw for hint in _EMPTY_HINTS):
        return CAPABILITY_AVAILABLE, "empty_response"
    return CAPABILITY_ERROR, f"api_error:{type(exc).__name__}"


@dataclass(frozen=True)
class LimitApiResult:
    """一次打板接口调用的结果：数据 + 能力状态 + 安全代号。"""

    frame: pd.DataFrame
    status: str
    reason: str

    @property
    def ok(self) -> bool:
        return self.status in {RESULT_OK, RESULT_EMPTY}


class TushareFirstMarketDataClient:
    """Tushare-first daily/reference client with an explicit Eastmoney fallback.

    Tushare is intentionally not used for realtime snapshots because that API
    has a separate permission model. DataFrames carry provenance in ``attrs``;
    the cache service persists it alongside each K-line row.
    """

    def __init__(
        self,
        *,
        fallback: EastmoneyMarketDataClient,
        token: str | None = None,
        enabled: bool | None = None,
        tushare_module: Any = None,
    ) -> None:
        self.fallback = fallback
        self.token = os.getenv("TUSHARE_TOKEN", "").strip() if token is None else token.strip()
        self.enabled = _enabled_from_env() if enabled is None else bool(enabled)
        self.tushare = _tushare if tushare_module is None else tushare_module
        self._pro: Any | None = None
        self._last_operation: dict[str, Any] = {}

    @property
    def is_ready(self) -> bool:
        return bool(self.enabled and self.token and self.tushare is not None)

    def describe(self) -> dict[str, Any]:
        return {
            "primary": "tushare",
            "fallback": "eastmoney/sina",
            "realtime": "eastmoney",
            "enabled": self.enabled,
            "configured": bool(self.token),
            "ready": self.is_ready,
            "last_operation": dict(self._last_operation),
        }

    def _pro_api(self) -> Any:
        if self._pro is None:
            if not self.is_ready:
                raise RuntimeError("Tushare is not configured")
            if hasattr(self.tushare, "set_token"):
                self.tushare.set_token(self.token)
            self._pro = self.tushare.pro_api(self.token)
        return self._pro

    def _mark(
        self,
        frame: pd.DataFrame,
        *,
        operation: str,
        source: str,
        fallback_reason: str = "",
    ) -> pd.DataFrame:
        frame.attrs["data_source"] = source
        frame.attrs["fallback_reason"] = fallback_reason
        self._last_operation = {
            "operation": operation,
            "source": source,
            "fallback_reason": fallback_reason,
        }
        return frame

    async def fetch_spot(self) -> pd.DataFrame:
        frame = await self.fallback.fetch_spot()
        return self._mark(frame, operation="realtime", source="eastmoney")

    async def fetch_hist(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        adjust: str = "qfq",
    ) -> pd.DataFrame:
        fallback_reason = "tushare_not_configured"
        if self.is_ready:
            try:
                frame = await run_blocking(
                    self._fetch_hist_sync,
                    symbol,
                    start_date,
                    end_date,
                    adjust,
                    name="tushare.daily_history",
                )
                if not frame.empty:
                    return self._mark(frame, operation="daily_history", source="tushare")
                fallback_reason = "tushare_empty_response"
            except Exception as exc:
                fallback_reason = (
                    "tushare_timeout" if isinstance(exc, ExternalTimeoutError) else f"tushare_error:{type(exc).__name__}"
                )
        frame = await self.fallback.fetch_hist(symbol, start_date, end_date, adjust=adjust)
        return self._mark(
            frame,
            operation="daily_history",
            source="eastmoney",
            fallback_reason=fallback_reason,
        )

    async def fetch_trade_days(self, min_days: int = 0) -> pd.DataFrame:
        fallback_reason = "tushare_not_configured"
        if self.is_ready:
            try:
                frame = await run_blocking(
                    self._fetch_trade_days_sync, min_days, name="tushare.trade_calendar"
                )
                if not frame.empty:
                    return self._mark(frame, operation="trade_calendar", source="tushare")
                fallback_reason = "tushare_empty_response"
            except Exception as exc:
                fallback_reason = (
                    "tushare_timeout" if isinstance(exc, ExternalTimeoutError) else f"tushare_error:{type(exc).__name__}"
                )
        frame = await self.fallback.fetch_trade_days(min_days=min_days)
        source = str(frame.attrs.get("data_source") or "sina/db")
        return self._mark(
            frame,
            operation="trade_calendar",
            source=source,
            fallback_reason=fallback_reason,
        )

    async def fetch_symbol_names(self) -> dict[str, str]:
        fallback_reason = "tushare_not_configured"
        if self.is_ready:
            try:
                names = await run_blocking(
                    self._fetch_symbol_names_sync, name="tushare.stock_basic"
                )
                if names:
                    self._last_operation = {
                        "operation": "stock_basic",
                        "source": "tushare",
                        "fallback_reason": "",
                    }
                    return names
                fallback_reason = "tushare_empty_response"
            except Exception as exc:
                fallback_reason = (
                    "tushare_timeout" if isinstance(exc, ExternalTimeoutError) else f"tushare_error:{type(exc).__name__}"
                )
        frame = await self.fallback.fetch_spot()
        names = {
            str(row["代码"]): str(row["名称"])
            for _, row in frame.iterrows()
            if row.get("代码") and row.get("名称")
        }
        self._last_operation = {
            "operation": "stock_basic",
            "source": "eastmoney",
            "fallback_reason": fallback_reason,
        }
        return names

    async def fetch_daily_by_trade_date(self, trade_date: str) -> pd.DataFrame:
        if not self.is_ready:
            return self._mark(
                pd.DataFrame(),
                operation="market_daily",
                source="unavailable",
                fallback_reason="tushare_not_configured",
            )
        try:
            frame = await run_blocking(
                self._fetch_daily_by_trade_date_sync, trade_date, name="tushare.market_daily"
            )
            if not frame.empty:
                return self._mark(frame, operation="market_daily", source="tushare")
            reason = "tushare_empty_response"
        except Exception as exc:
            frame = pd.DataFrame()
            reason = (
                "tushare_timeout" if isinstance(exc, ExternalTimeoutError) else f"tushare_error:{type(exc).__name__}"
            )
        return self._mark(
            frame,
            operation="market_daily",
            source="unavailable",
            fallback_reason=reason,
        )

    def _fetch_hist_sync(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        adjust: str,
    ) -> pd.DataFrame:
        start = _compact_date(start_date)
        end = _compact_date(end_date)
        ts_code = _to_ts_code(symbol)
        frame: pd.DataFrame | None = None
        adjustment = str(adjust or "").lower()
        if adjustment in {"qfq", "hfq"} and hasattr(self.tushare, "pro_bar"):
            frame = self.tushare.pro_bar(
                ts_code=ts_code,
                start_date=start,
                end_date=end,
                adj=adjustment,
                freq="D",
            )
        if frame is None or frame.empty:
            frame = self._pro_api().daily(ts_code=ts_code, start_date=start, end_date=end)
        return self._normalize_daily(frame)

    def _fetch_trade_days_sync(self, min_days: int) -> pd.DataFrame:
        end = dt.date.today()
        lookback = max(730, max(0, int(min_days)) * 3)
        start = end - dt.timedelta(days=lookback)
        frame = self._pro_api().trade_cal(
            exchange="SSE",
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
            is_open="1",
            fields="cal_date,is_open",
        )
        if frame is None or frame.empty or "cal_date" not in frame.columns:
            return pd.DataFrame(columns=["trade_date"])
        dates = frame["cal_date"].map(_display_date)
        return pd.DataFrame({"trade_date": sorted({d for d in dates if d})})

    def _fetch_symbol_names_sync(self) -> dict[str, str]:
        frame = self._pro_api().stock_basic(
            exchange="",
            list_status="L",
            fields="ts_code,symbol,name,list_status",
        )
        if frame is None or frame.empty:
            return {}
        names: dict[str, str] = {}
        for _, row in frame.iterrows():
            symbol = _normalize_symbol(str(row.get("symbol") or row.get("ts_code") or ""))
            name = str(row.get("name") or "").strip()
            if symbol and name:
                names[symbol] = name
        return names

    def _fetch_daily_by_trade_date_sync(self, trade_date: str) -> pd.DataFrame:
        compact = _compact_date(trade_date)
        if not compact:
            raise ValueError("trade_date must use YYYY-MM-DD or YYYYMMDD")
        frame = self._pro_api().daily(trade_date=compact)
        normalized = self._normalize_daily(frame)
        if normalized.empty:
            return normalized
        normalized.insert(
            0,
            "代码",
            frame["ts_code"].reset_index(drop=True).map(_normalize_symbol),
        )
        return normalized

    @staticmethod
    def _normalize_daily(frame: pd.DataFrame | None) -> pd.DataFrame:
        columns = ["日期", "开盘", "收盘", "最高", "最低", "成交量", "成交额"]
        if frame is None or frame.empty:
            return pd.DataFrame(columns=columns)
        result = pd.DataFrame(index=frame.index)
        result["日期"] = frame["trade_date"].map(_display_date)
        result["开盘"] = pd.to_numeric(frame["open"], errors="coerce").fillna(0.0)
        result["收盘"] = pd.to_numeric(frame["close"], errors="coerce").fillna(0.0)
        result["最高"] = pd.to_numeric(frame["high"], errors="coerce").fillna(0.0)
        result["最低"] = pd.to_numeric(frame["low"], errors="coerce").fillna(0.0)
        # Tushare daily: vol is lots and amount is thousand RMB.
        result["成交量"] = pd.to_numeric(frame["vol"], errors="coerce").fillna(0.0)
        result["成交额"] = pd.to_numeric(frame["amount"], errors="coerce").fillna(0.0) * 1000.0
        return result[result["日期"].ne("")].sort_values("日期", kind="stable").reset_index(drop=True)

    # ── 日频增强 / 资金流 / 基准指数 / 分钟线 ──

    async def _run_dataset(
        self,
        operation: str,
        sync_fn: Any,
        *args: Any,
    ) -> pd.DataFrame:
        if not self.is_ready:
            return self._mark(
                pd.DataFrame(),
                operation=operation,
                source="unavailable",
                fallback_reason="tushare_not_configured",
            )
        try:
            frame = await asyncio.to_thread(sync_fn, *args)
            if frame is not None and not frame.empty:
                return self._mark(frame, operation=operation, source="tushare")
            reason = "tushare_empty_response"
        except Exception as exc:
            frame = pd.DataFrame()
            reason = f"tushare_error:{type(exc).__name__}"
        return self._mark(
            frame,
            operation=operation,
            source="unavailable",
            fallback_reason=reason,
        )

    async def fetch_daily_basic_by_trade_date(self, trade_date: str) -> pd.DataFrame:
        return await self._run_dataset(
            "daily_basic", self._fetch_daily_basic_sync, trade_date
        )

    async def fetch_price_limits_by_trade_date(self, trade_date: str) -> pd.DataFrame:
        return await self._run_dataset(
            "price_limits", self._fetch_price_limits_sync, trade_date
        )

    async def fetch_adj_factors_by_trade_date(self, trade_date: str) -> pd.DataFrame:
        return await self._run_dataset(
            "adj_factor", self._fetch_adj_factors_sync, trade_date
        )

    async def fetch_moneyflow_by_trade_date(self, trade_date: str) -> pd.DataFrame:
        return await self._run_dataset(
            "moneyflow", self._fetch_moneyflow_sync, trade_date
        )

    async def fetch_moneyflow_hsgt(self, start_date: str, end_date: str) -> pd.DataFrame:
        return await self._run_dataset(
            "moneyflow_hsgt", self._fetch_moneyflow_hsgt_sync, start_date, end_date
        )

    async def fetch_index_daily(
        self,
        trade_date: str,
        ts_codes: list[str] | None = None,
    ) -> pd.DataFrame:
        return await self._run_dataset(
            "index_daily", self._fetch_index_daily_sync, trade_date, ts_codes
        )

    async def fetch_rt_min(self, symbol: str, freq: str = "1MIN") -> pd.DataFrame:
        return await self._run_dataset(
            "rt_min", self._fetch_rt_min_sync, symbol, freq
        )

    async def fetch_realtime_quote(self, symbol: str) -> dict[str, Any] | None:
        if not self.is_ready:
            return None
        try:
            return await asyncio.to_thread(self._fetch_realtime_quote_sync, symbol)
        except Exception:
            return None

    def _fetch_daily_basic_sync(self, trade_date: str) -> pd.DataFrame:
        compact = _compact_date(trade_date)
        if not compact:
            raise ValueError("trade_date must use YYYY-MM-DD or YYYYMMDD")
        frame = self._pro_api().daily_basic(
            trade_date=compact,
            fields=(
                "ts_code,trade_date,turnover_rate,turnover_rate_f,volume_ratio,"
                "pe_ttm,pb,ps_ttm,total_mv,circ_mv"
            ),
        )
        return self._normalize_daily_basic(frame)

    def _fetch_price_limits_sync(self, trade_date: str) -> pd.DataFrame:
        compact = _compact_date(trade_date)
        if not compact:
            raise ValueError("trade_date must use YYYY-MM-DD or YYYYMMDD")
        frame = self._pro_api().stk_limit(
            trade_date=compact,
            fields="ts_code,trade_date,up_limit,down_limit",
        )
        return self._normalize_price_limits(frame)

    def _fetch_adj_factors_sync(self, trade_date: str) -> pd.DataFrame:
        compact = _compact_date(trade_date)
        if not compact:
            raise ValueError("trade_date must use YYYY-MM-DD or YYYYMMDD")
        frame = self._pro_api().adj_factor(
            trade_date=compact,
            fields="ts_code,trade_date,adj_factor",
        )
        return self._normalize_adj_factors(frame)

    def _fetch_moneyflow_sync(self, trade_date: str) -> pd.DataFrame:
        compact = _compact_date(trade_date)
        if not compact:
            raise ValueError("trade_date must use YYYY-MM-DD or YYYYMMDD")
        frame = self._pro_api().moneyflow(
            trade_date=compact,
            fields=(
                "ts_code,trade_date,buy_sm_amount,sell_sm_amount,"
                "buy_md_amount,sell_md_amount,buy_lg_amount,sell_lg_amount,"
                "buy_elg_amount,sell_elg_amount,net_mf_amount"
            ),
        )
        return self._normalize_moneyflow(frame)

    def _fetch_moneyflow_hsgt_sync(self, start_date: str, end_date: str) -> pd.DataFrame:
        start = _compact_date(start_date)
        end = _compact_date(end_date)
        if not start or not end:
            raise ValueError("start_date/end_date must use YYYY-MM-DD or YYYYMMDD")
        frame = self._pro_api().moneyflow_hsgt(
            start_date=start,
            end_date=end,
            fields="trade_date,hgt,sgt,north_money,south_money",
        )
        return self._normalize_moneyflow_hsgt(frame)

    def _fetch_index_daily_sync(
        self,
        trade_date: str,
        ts_codes: list[str] | None,
    ) -> pd.DataFrame:
        compact = _compact_date(trade_date)
        if not compact:
            raise ValueError("trade_date must use YYYY-MM-DD or YYYYMMDD")
        frames: list[pd.DataFrame] = []
        errors: list[str] = []
        for ts_code in ts_codes or ["000001.SH", "399001.SZ", "399006.SZ", "000300.SH"]:
            try:
                frame = self._pro_api().index_daily(
                    ts_code=ts_code,
                    trade_date=compact,
                    fields=(
                        "ts_code,trade_date,open,high,low,close,pre_close,"
                        "change,pct_chg,vol,amount"
                    ),
                )
                normalized = self._normalize_index_daily(frame)
                if not normalized.empty:
                    frames.append(normalized)
            except Exception as exc:
                errors.append(f"{ts_code}:{type(exc).__name__}")
        if not frames:
            if errors:
                raise RuntimeError(",".join(errors))
            return pd.DataFrame()
        result = pd.concat(frames, ignore_index=True)
        if errors:
            result.attrs["partial_errors"] = errors
        return result

    def _fetch_rt_min_sync(self, symbol: str, freq: str) -> pd.DataFrame:
        frame = self._pro_api().rt_min(ts_code=_to_ts_code(symbol), freq=freq)
        return self._normalize_rt_min(frame)

    def _fetch_realtime_quote_sync(self, symbol: str) -> dict[str, Any] | None:
        frame = self.tushare.realtime_quote(ts_code=_to_ts_code(symbol))
        if frame is None or getattr(frame, "empty", True):
            return None
        row = frame.iloc[0]
        price = pd.to_numeric(pd.Series([row.get("PRICE")]), errors="coerce").iloc[0]
        if pd.isna(price):
            return None
        trade_date = _display_date(row.get("DATE"))
        trade_time = str(row.get("TIME") or "").strip()
        bar_time = f"{trade_date} {trade_time}".strip() if trade_date else ""
        return {
            "symbol": _normalize_symbol(str(row.get("TS_CODE") or symbol)),
            "price": float(price),
            "bar_time": bar_time,
            "volume": float(pd.to_numeric(pd.Series([row.get("VOLUME")]), errors="coerce").fillna(0.0).iloc[0]),
            "amount": float(pd.to_numeric(pd.Series([row.get("AMOUNT")]), errors="coerce").fillna(0.0).iloc[0]),
            "source": "tushare.realtime_quote",
        }

    @staticmethod
    def _numeric_column(frame: pd.DataFrame, name: str, scale: float = 1.0) -> pd.Series:
        if name not in frame.columns:
            return pd.Series([0.0] * len(frame), index=frame.index)
        return pd.to_numeric(frame[name], errors="coerce").fillna(0.0) * scale

    @classmethod
    def _normalize_daily_basic(cls, frame: pd.DataFrame | None) -> pd.DataFrame:
        columns = [
            "symbol", "trade_date", "turnover_rate", "turnover_rate_f", "volume_ratio",
            "pe_ttm", "pb", "ps_ttm", "total_mv", "circ_mv",
        ]
        if frame is None or frame.empty:
            return pd.DataFrame(columns=columns)
        result = pd.DataFrame(
            {
                "symbol": frame["ts_code"].map(_normalize_symbol),
                "trade_date": frame["trade_date"].map(_display_date),
                "turnover_rate": cls._numeric_column(frame, "turnover_rate"),
                "turnover_rate_f": cls._numeric_column(frame, "turnover_rate_f"),
                "volume_ratio": cls._numeric_column(frame, "volume_ratio"),
                "pe_ttm": cls._numeric_column(frame, "pe_ttm"),
                "pb": cls._numeric_column(frame, "pb"),
                "ps_ttm": cls._numeric_column(frame, "ps_ttm"),
                # Tushare daily_basic: market value is in 10k RMB.
                "total_mv": cls._numeric_column(frame, "total_mv", 10000.0),
                "circ_mv": cls._numeric_column(frame, "circ_mv", 10000.0),
            }
        )
        return result[result["trade_date"].ne("")].reset_index(drop=True)

    @classmethod
    def _normalize_price_limits(cls, frame: pd.DataFrame | None) -> pd.DataFrame:
        columns = ["symbol", "trade_date", "up_limit", "down_limit"]
        if frame is None or frame.empty:
            return pd.DataFrame(columns=columns)
        result = pd.DataFrame(
            {
                "symbol": frame["ts_code"].map(_normalize_symbol),
                "trade_date": frame["trade_date"].map(_display_date),
                "up_limit": cls._numeric_column(frame, "up_limit"),
                "down_limit": cls._numeric_column(frame, "down_limit"),
            }
        )
        return result[result["trade_date"].ne("")].reset_index(drop=True)

    @classmethod
    def _normalize_adj_factors(cls, frame: pd.DataFrame | None) -> pd.DataFrame:
        columns = ["symbol", "trade_date", "adj_factor"]
        if frame is None or frame.empty:
            return pd.DataFrame(columns=columns)
        result = pd.DataFrame(
            {
                "symbol": frame["ts_code"].map(_normalize_symbol),
                "trade_date": frame["trade_date"].map(_display_date),
                "adj_factor": cls._numeric_column(frame, "adj_factor"),
            }
        )
        return result[result["trade_date"].ne("")].reset_index(drop=True)

    @classmethod
    def _normalize_moneyflow(cls, frame: pd.DataFrame | None) -> pd.DataFrame:
        amount_fields = [
            "buy_sm_amount", "sell_sm_amount", "buy_md_amount", "sell_md_amount",
            "buy_lg_amount", "sell_lg_amount", "buy_elg_amount", "sell_elg_amount",
            "net_mf_amount",
        ]
        columns = ["symbol", "trade_date", *amount_fields]
        if frame is None or frame.empty:
            return pd.DataFrame(columns=columns)
        data: dict[str, Any] = {
            "symbol": frame["ts_code"].map(_normalize_symbol),
            "trade_date": frame["trade_date"].map(_display_date),
        }
        for field in amount_fields:
            # Tushare moneyflow: amounts are in 10k RMB.
            data[field] = cls._numeric_column(frame, field, 10000.0)
        result = pd.DataFrame(data)
        return result[result["trade_date"].ne("")].reset_index(drop=True)

    @classmethod
    def _normalize_moneyflow_hsgt(cls, frame: pd.DataFrame | None) -> pd.DataFrame:
        columns = ["trade_date", "hgt", "sgt", "north_money", "south_money"]
        if frame is None or frame.empty:
            return pd.DataFrame(columns=columns)
        data: dict[str, Any] = {"trade_date": frame["trade_date"].map(_display_date)}
        for field in ("hgt", "sgt", "north_money", "south_money"):
            # Tushare moneyflow_hsgt: amounts are in 10k RMB.
            data[field] = cls._numeric_column(frame, field, 10000.0)
        result = pd.DataFrame(data)
        return result[result["trade_date"].ne("")].reset_index(drop=True)

    @classmethod
    def _normalize_index_daily(cls, frame: pd.DataFrame | None) -> pd.DataFrame:
        columns = [
            "ts_code", "trade_date", "open", "high", "low", "close",
            "pre_close", "change", "pct_chg", "volume", "amount",
        ]
        if frame is None or frame.empty:
            return pd.DataFrame(columns=columns)
        result = pd.DataFrame(
            {
                "ts_code": frame["ts_code"],
                "trade_date": frame["trade_date"].map(_display_date),
                "open": cls._numeric_column(frame, "open"),
                "high": cls._numeric_column(frame, "high"),
                "low": cls._numeric_column(frame, "low"),
                "close": cls._numeric_column(frame, "close"),
                "pre_close": cls._numeric_column(frame, "pre_close"),
                "change": cls._numeric_column(frame, "change"),
                "pct_chg": cls._numeric_column(frame, "pct_chg"),
                # Same convention as daily: vol is lots, amount is thousand RMB.
                "volume": cls._numeric_column(frame, "vol"),
                "amount": cls._numeric_column(frame, "amount", 1000.0),
            }
        )
        return result[result["trade_date"].ne("")].reset_index(drop=True)

    @classmethod
    def _normalize_rt_min(cls, frame: pd.DataFrame | None) -> pd.DataFrame:
        columns = [
            "symbol", "bar_time", "trade_date", "open", "high", "low", "close",
            "volume", "amount", "source",
        ]
        if frame is None or frame.empty or "time" not in frame.columns:
            return pd.DataFrame(columns=columns)
        bar_time = pd.to_datetime(frame["time"], errors="coerce")
        result = pd.DataFrame(
            {
                "symbol": frame["ts_code"].map(_normalize_symbol),
                "bar_time": bar_time.dt.strftime("%Y-%m-%d %H:%M:%S"),
                "trade_date": bar_time.dt.strftime("%Y-%m-%d"),
                "open": cls._numeric_column(frame, "open"),
                "high": cls._numeric_column(frame, "high"),
                "low": cls._numeric_column(frame, "low"),
                "close": cls._numeric_column(frame, "close"),
                # rt_min returns shares and RMB directly.
                "volume": cls._numeric_column(frame, "vol"),
                "amount": cls._numeric_column(frame, "amount"),
                "source": "tushare.rt_min",
            }
        )
        return result[result["bar_time"].ne("") & result["trade_date"].ne("")].reset_index(drop=True)

    # ── 打板数据底座（issue #22）─────────────────────────────

    async def call_limit_api(
        self,
        api: str,
        *,
        trade_date: str = "",
        ts_code: str = "",
        timeout: float | None = None,
    ) -> LimitApiResult:
        """调用一个打板相关接口，返回数据 + 能力状态 + 安全代号。

        与 ``_run_dataset`` 的区别：这里保留可判别的能力状态（denied / error /
        empty），供能力矩阵和 ``enrichment_sync_state`` 使用；调用统一走
        :func:`run_blocking`，因此沿用既有的外部超时与熔断。
        """
        if not self.is_ready:
            return LimitApiResult(pd.DataFrame(), CAPABILITY_NOT_CONFIGURED, "tushare_not_configured")
        params: dict[str, str] = {}
        allowed = _LIMIT_API_PARAMS.get(api, ())
        compact = _compact_date(trade_date)
        if "trade_date" in allowed and compact:
            params["trade_date"] = compact
        clean_code = str(ts_code or "").strip().upper()
        if "ts_code" in allowed and clean_code:
            params["ts_code"] = clean_code
        fields = _LIMIT_API_FIELDS.get(api)
        if fields:
            params["fields"] = fields
        try:
            frame = await run_blocking(
                self._call_limit_api_sync,
                api,
                params,
                timeout=timeout,
                name=f"tushare.{api}",
            )
        except Exception as exc:  # noqa: BLE001 — 单接口失败必须降级而非冒泡
            status, reason = classify_tushare_error(exc, token=self.token)
            self._last_operation = {
                "operation": api,
                "source": "tushare",
                "fallback_reason": reason,
            }
            return LimitApiResult(pd.DataFrame(), status, reason)
        self._last_operation = {"operation": api, "source": "tushare", "fallback_reason": ""}
        if frame is None or frame.empty:
            return LimitApiResult(pd.DataFrame(), RESULT_EMPTY, "empty_response")
        return LimitApiResult(frame, RESULT_OK, "")

    async def probe_limit_capabilities(
        self,
        trade_date: str,
        apis: tuple[str, ...] | list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """逐接口探测权限，输出能力矩阵（不含任何密钥或原始报文）。"""

        matrix: list[dict[str, Any]] = []
        for api in apis or LIMIT_BOARD_APIS:
            result = await self.call_limit_api(api, trade_date=trade_date, ts_code=_PROBE_TS_CODE)
            capability = (
                CAPABILITY_AVAILABLE
                if result.status in {RESULT_OK, RESULT_EMPTY}
                else result.status
            )
            matrix.append(
                {
                    "api": api,
                    "capability": capability,
                    "reason": "ok" if capability == CAPABILITY_AVAILABLE else result.reason,
                    "sample_rows": int(len(result.frame)),
                }
            )
        return matrix

    def _call_limit_api_sync(self, api: str, params: dict[str, str]) -> pd.DataFrame:
        method = getattr(self._pro_api(), api, None)
        if method is None:
            raise RuntimeError(f"tushare_api_missing:{api}")
        frame = method(**params)
        return frame if isinstance(frame, pd.DataFrame) else pd.DataFrame()
