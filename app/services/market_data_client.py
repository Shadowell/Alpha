from __future__ import annotations

import asyncio
import datetime as _dt
import re
from typing import Any

import httpx
import pandas as pd


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        if isinstance(value, str):
            text = value.replace(",", "").strip()
            if text in {"", "-", "--", "None", "nan", "NaN"}:
                return default
            return float(text)
        return float(value)
    except Exception:
        return default


def _normalize_symbol(symbol: str) -> str:
    digits = "".join(ch for ch in str(symbol or "") if ch.isdigit())
    return digits[-6:].zfill(6) if digits else ""


def _yyyymmdd(value: str) -> str:
    text = str(value or "").strip().replace("-", "")
    if len(text) >= 8 and text[:8].isdigit():
        return text[:8]
    return ""


def _iso_date(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        return ""
    return parsed.date().isoformat()


class EastmoneyMarketDataClient:
    """Async, timeout-bound market data client for K-line cache sync.

    This client intentionally avoids AkShare's blocking requests path for the
    data-center sync workflow. Every network call goes through httpx with
    connect/read/write/pool timeouts so a bad upstream cannot strand a Python
    thread in OpenSSL/socket forever.
    """

    HIST_URLS = (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get",
        "https://82.push2his.eastmoney.com/api/qt/stock/kline/get",
    )
    SPOT_URLS = (
        "https://82.push2.eastmoney.com/api/qt/clist/get",
        "https://push2.eastmoney.com/api/qt/clist/get",
    )
    TRADE_DAYS_URL = "https://finance.sina.com.cn/realstock/company/klc_td_sh.txt"

    def __init__(
        self,
        *,
        store: Any | None = None,
        timeout: httpx.Timeout | None = None,
        retries: int = 2,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.store = store
        self.timeout = timeout or httpx.Timeout(connect=2.0, read=8.0, write=2.0, pool=2.0)
        self.retries = max(0, int(retries))
        self.transport = transport
        self._headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            "Accept": "application/json,text/plain,*/*",
        }

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self.timeout,
            headers=self._headers,
            trust_env=False,
            follow_redirects=True,
            transport=self.transport,
        )

    async def _get_json(self, client: httpx.AsyncClient, url: str, params: dict[str, Any]) -> dict[str, Any]:
        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                payload = resp.json()
                return payload if isinstance(payload, dict) else {}
            except Exception as exc:
                last_exc = exc
                if attempt < self.retries:
                    await asyncio.sleep(0.25 * (attempt + 1))
        print(f"[market_data_client] GET json failed: {url} ({last_exc})")
        return {}

    async def _get_text(self, client: httpx.AsyncClient, url: str) -> str:
        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                return resp.text
            except Exception as exc:
                last_exc = exc
                if attempt < self.retries:
                    await asyncio.sleep(0.25 * (attempt + 1))
        print(f"[market_data_client] GET text failed: {url} ({last_exc})")
        return ""

    async def fetch_hist(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        adjust: str = "qfq",
    ) -> pd.DataFrame:
        clean_symbol = _normalize_symbol(symbol)
        if not clean_symbol:
            return pd.DataFrame()
        market_code = "1" if clean_symbol.startswith(("6", "68")) else "0"
        adjust_dict = {"qfq": "1", "hfq": "2", "": "0", None: "0"}
        params = {
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f116",
            "ut": "7eea3edcaed734bea9cbfc24409ed989",
            "klt": "101",
            "fqt": adjust_dict.get(adjust, "1"),
            "secid": f"{market_code}.{clean_symbol}",
            "beg": _yyyymmdd(start_date),
            "end": _yyyymmdd(end_date),
        }
        if not params["beg"] or not params["end"]:
            return pd.DataFrame()

        payload: dict[str, Any] = {}
        async with self._client() as client:
            for url in self.HIST_URLS:
                payload = await self._get_json(client, url, params)
                data = payload.get("data") if isinstance(payload, dict) else None
                klines = data.get("klines") if isinstance(data, dict) else None
                if klines:
                    break

        data = payload.get("data") if isinstance(payload, dict) else None
        klines = data.get("klines") if isinstance(data, dict) else None
        if not klines:
            return pd.DataFrame()

        rows: list[list[str]] = []
        for item in klines:
            parts = str(item).split(",")
            if len(parts) >= 7:
                rows.append(parts)
        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows)
        out = pd.DataFrame()
        out["日期"] = pd.to_datetime(df[0], errors="coerce").dt.date.astype(str)
        out["开盘"] = pd.to_numeric(df[1], errors="coerce").fillna(0.0)
        out["收盘"] = pd.to_numeric(df[2], errors="coerce").fillna(0.0)
        out["最高"] = pd.to_numeric(df[3], errors="coerce").fillna(0.0)
        out["最低"] = pd.to_numeric(df[4], errors="coerce").fillna(0.0)
        out["成交量"] = pd.to_numeric(df[5], errors="coerce").fillna(0.0)
        out["成交额"] = pd.to_numeric(df[6], errors="coerce").fillna(0.0)
        out = out[out["日期"].ne("NaT")]
        return out.reset_index(drop=True)

    async def fetch_spot(self) -> pd.DataFrame:
        for url in self.SPOT_URLS:
            result = await self._fetch_spot_from_url(url)
            if not result.empty:
                return result
        return pd.DataFrame()

    async def _fetch_spot_from_url(self, url: str) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        page_size = 500
        async with self._client() as client:
            for page in range(1, 30):
                params = {
                    "pn": str(page),
                    "pz": str(page_size),
                    "po": "1",
                    "np": "1",
                    "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                    "fltt": "2",
                    "invt": "2",
                    "fid": "f12",
                    "fs": "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23,m:0 t:81 s:2048",
                    "fields": (
                        "f2,f3,f4,f5,f6,f12,f14,f15,f16,f17,f18,f20"
                    ),
                }
                payload = await self._get_json(client, url, params)
                data = payload.get("data") if isinstance(payload, dict) else None
                diff = data.get("diff") if isinstance(data, dict) else None
                if isinstance(diff, dict):
                    diff_rows = list(diff.values())
                elif isinstance(diff, list):
                    diff_rows = diff
                else:
                    diff_rows = []
                if not diff_rows:
                    break
                rows.extend([r for r in diff_rows if isinstance(r, dict)])
                total = int(_to_float(data.get("total"), 0)) if isinstance(data, dict) else 0
                if total and len(rows) >= total:
                    break
        return self._normalize_spot_rows(rows)

    @staticmethod
    def _normalize_spot_rows(rows: list[dict[str, Any]]) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame()
        items: list[dict[str, Any]] = []
        for row in rows:
            symbol = _normalize_symbol(str(row.get("f12") or ""))
            name = str(row.get("f14") or "").strip()
            if not symbol or not name:
                continue
            latest = _to_float(row.get("f2"))
            items.append(
                {
                    "代码": symbol,
                    "名称": name,
                    "最新价": latest,
                    "涨跌幅": _to_float(row.get("f3")),
                    "涨跌额": _to_float(row.get("f4")),
                    "成交量": _to_float(row.get("f5")),
                    "成交额": _to_float(row.get("f6")),
                    "最高": _to_float(row.get("f15"), latest),
                    "最低": _to_float(row.get("f16"), latest),
                    "今开": _to_float(row.get("f17"), latest),
                    "昨收": _to_float(row.get("f18"), latest),
                    "总市值": _to_float(row.get("f20"), 0.0),
                }
            )
        return pd.DataFrame(items)

    async def fetch_trade_days(self, min_days: int = 0) -> pd.DataFrame:
        db_dates: list[str] = []
        if self.store is not None:
            try:
                db_dates = self.store.get_trade_dates_from_db() or []
            except Exception:
                db_dates = []
        if db_dates and (min_days <= 0 or len(db_dates) >= min_days):
            return pd.DataFrame({"trade_date": db_dates})

        async with self._client() as client:
            text = await self._get_text(client, self.TRADE_DAYS_URL)
        dates = self._parse_sina_trade_days(text)
        if dates:
            return pd.DataFrame({"trade_date": dates})
        if db_dates:
            return pd.DataFrame({"trade_date": db_dates})
        return pd.DataFrame(columns=["trade_date"])

    @staticmethod
    def _parse_sina_trade_days(text: str) -> list[str]:
        if not text:
            return []
        dates: list[str] = []
        try:
            from akshare.stock.cons import hk_js_decode
            from py_mini_racer import py_mini_racer

            encoded = text.split("=", 1)[1].split(";", 1)[0].replace('"', "")
            js_code = py_mini_racer.MiniRacer()
            js_code.eval(hk_js_decode)
            decoded = js_code.call("d", encoded)
            dates = [_iso_date(str(item)) for item in decoded]
        except Exception:
            matches = re.findall(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{8}", text)
            dates = [_iso_date(item) for item in matches]

        parsed = sorted({d for d in dates if d})
        parsed.append(_dt.date(1992, 5, 4).isoformat())
        return sorted(set(parsed))
