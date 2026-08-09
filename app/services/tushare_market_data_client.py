from __future__ import annotations

import asyncio
import datetime as dt
import os
from typing import Any

import pandas as pd

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
                frame = await asyncio.to_thread(
                    self._fetch_hist_sync, symbol, start_date, end_date, adjust
                )
                if not frame.empty:
                    return self._mark(frame, operation="daily_history", source="tushare")
                fallback_reason = "tushare_empty_response"
            except Exception as exc:
                fallback_reason = f"tushare_error:{type(exc).__name__}"
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
                frame = await asyncio.to_thread(self._fetch_trade_days_sync, min_days)
                if not frame.empty:
                    return self._mark(frame, operation="trade_calendar", source="tushare")
                fallback_reason = "tushare_empty_response"
            except Exception as exc:
                fallback_reason = f"tushare_error:{type(exc).__name__}"
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
                names = await asyncio.to_thread(self._fetch_symbol_names_sync)
                if names:
                    self._last_operation = {
                        "operation": "stock_basic",
                        "source": "tushare",
                        "fallback_reason": "",
                    }
                    return names
                fallback_reason = "tushare_empty_response"
            except Exception as exc:
                fallback_reason = f"tushare_error:{type(exc).__name__}"
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
            frame = await asyncio.to_thread(self._fetch_daily_by_trade_date_sync, trade_date)
            if not frame.empty:
                return self._mark(frame, operation="market_daily", source="tushare")
            reason = "tushare_empty_response"
        except Exception as exc:
            frame = pd.DataFrame()
            reason = f"tushare_error:{type(exc).__name__}"
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
