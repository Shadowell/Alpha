from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import re
import time
from typing import Any

import akshare as ak
import pandas as pd


@dataclass
class AkshareDataProvider:
    concept_constituents_cache: dict[str, pd.DataFrame] = field(default_factory=dict)
    concept_snapshot_cache: tuple[datetime, pd.DataFrame] | None = None
    realtime_snapshot_cache: tuple[datetime, pd.DataFrame] | None = None
    hot_stocks_cache: tuple[datetime, pd.DataFrame] | None = None
    symbol_name_cache: tuple[datetime, dict[str, str]] | None = None

    def get_realtime_snapshot(
        self,
        retries: int = 2,
        retry_wait_seconds: float = 1.0,
        cache_ttl_seconds: int = 300,
    ) -> pd.DataFrame:
        return self.get_snapshot_em(
            retries=retries,
            retry_wait_seconds=retry_wait_seconds,
            cache_ttl_seconds=cache_ttl_seconds,
        )

    def _normalize_snapshot(self, df: pd.DataFrame) -> pd.DataFrame:
        if df is None or df.empty:
            return pd.DataFrame()

        code_col = "代码" if "代码" in df.columns else ("symbol" if "symbol" in df.columns else None)
        name_col = "名称" if "名称" in df.columns else ("name" if "name" in df.columns else None)
        if code_col is None or name_col is None:
            return pd.DataFrame()

        payload = pd.DataFrame()
        payload["代码"] = df[code_col].apply(normalize_symbol)
        payload["名称"] = df[name_col].astype(str)
        payload["最新价"] = pd.to_numeric(df["最新价"], errors="coerce").fillna(0.0) if "最新价" in df.columns else 0.0
        payload["涨跌额"] = pd.to_numeric(df["涨跌额"], errors="coerce").fillna(0.0) if "涨跌额" in df.columns else 0.0
        payload["涨跌幅"] = pd.to_numeric(df["涨跌幅"], errors="coerce").fillna(0.0) if "涨跌幅" in df.columns else 0.0
        payload["昨收"] = pd.to_numeric(df["昨收"], errors="coerce").fillna(0.0) if "昨收" in df.columns else 0.0
        payload["今开"] = pd.to_numeric(df["今开"], errors="coerce").fillna(payload["昨收"]) if "今开" in df.columns else 0.0
        payload["最高"] = pd.to_numeric(df["最高"], errors="coerce").fillna(payload["最新价"]) if "最高" in df.columns else payload[
            "最新价"
        ]
        payload["最低"] = pd.to_numeric(df["最低"], errors="coerce").fillna(payload["最新价"]) if "最低" in df.columns else payload[
            "最新价"
        ]
        payload["成交量"] = pd.to_numeric(df["成交量"], errors="coerce").fillna(0.0) if "成交量" in df.columns else 0.0
        payload["成交额"] = pd.to_numeric(df["成交额"], errors="coerce").fillna(0.0) if "成交额" in df.columns else 0.0
        if "总市值" in df.columns:
            payload["总市值"] = pd.to_numeric(df["总市值"], errors="coerce")
        else:
            payload["总市值"] = pd.NA
        return payload.reset_index(drop=True)

    def get_snapshot_em(
        self,
        retries: int = 2,
        retry_wait_seconds: float = 1.0,
        cache_ttl_seconds: int = 300,
    ) -> pd.DataFrame:
        last_exc: Exception | None = None
        for idx in range(retries + 1):
            try:
                df = ak.stock_zh_a_spot_em()
                payload = self._normalize_snapshot(df)
                if not payload.empty:
                    self.realtime_snapshot_cache = (datetime.now(), payload)
                    return payload
            except Exception as exc:
                last_exc = exc
                if idx < retries:
                    time.sleep(retry_wait_seconds * (idx + 1))

        if self.realtime_snapshot_cache is not None:
            ts, cached = self.realtime_snapshot_cache
            age = (datetime.now() - ts).total_seconds()
            if age <= cache_ttl_seconds:
                print("[data_provider] realtime snapshot fallback to cached data")
                return cached.copy()

        if last_exc is not None:
            print(f"[data_provider] get_realtime_snapshot failed: {last_exc}")
        return pd.DataFrame()

    def get_snapshot_spot(
        self,
        retries: int = 1,
        retry_wait_seconds: float = 1.0,
        cache_ttl_seconds: int = 300,
    ) -> pd.DataFrame:
        last_exc: Exception | None = None
        for idx in range(retries + 1):
            try:
                df = ak.stock_zh_a_spot()
                payload = self._normalize_snapshot(df)
                if not payload.empty:
                    self.realtime_snapshot_cache = (datetime.now(), payload.copy())
                    return payload
            except Exception as exc:
                last_exc = exc
                if idx < retries:
                    time.sleep(retry_wait_seconds * (idx + 1))

        if self.realtime_snapshot_cache is not None:
            ts, cached = self.realtime_snapshot_cache
            age = (datetime.now() - ts).total_seconds()
            if age <= cache_ttl_seconds:
                print("[data_provider] stock_zh_a_spot fallback to cached data")
                return cached.copy()
        if last_exc is not None:
            print(f"[data_provider] get_snapshot_spot failed: {last_exc}")
        return pd.DataFrame()

    def get_trade_days(self) -> pd.DataFrame:
        try:
            df = ak.tool_trade_date_hist_sina()
            if df is None or df.empty:
                return pd.DataFrame(columns=["trade_date"])
            return df.copy()
        except Exception as exc:
            print(f"[data_provider] get_trade_days failed: {exc}")
            return pd.DataFrame(columns=["trade_date"])

    def get_hist(self, symbol: str, start_date: str, end_date: str, adjust: str = "qfq") -> pd.DataFrame:
        try:
            df = ak.stock_zh_a_hist(
                symbol=symbol,
                period="daily",
                start_date=start_date,
                end_date=end_date,
                adjust=adjust,
            )
            if df is None or df.empty:
                return pd.DataFrame()
            return df.copy()
        except Exception as exc:
            print(f"[data_provider] get_hist failed for {symbol}: {exc}")
            return pd.DataFrame()

    def get_all_concepts(self, cache_seconds: int = 30) -> pd.DataFrame:
        now = datetime.now()
        if self.concept_snapshot_cache is not None:
            ts, cached = self.concept_snapshot_cache
            if (now - ts).total_seconds() < cache_seconds:
                return cached.copy()

        last_exc: Exception | None = None
        for idx in range(3):
            try:
                df = ak.stock_board_concept_name_em()
                if df is not None and not df.empty:
                    payload = df.copy()
                    payload["数据源"] = "em"
                    self.concept_snapshot_cache = (now, payload)
                    return payload
            except Exception as exc:
                last_exc = exc
                if idx < 2:
                    time.sleep(1.0 * (idx + 1))

        # Fallback: THS concept snapshot for environments where EM endpoint is unstable.
        ths_df = self.get_all_concepts_ths(max_items=20, cache_seconds=600)
        if not ths_df.empty:
            self.concept_snapshot_cache = (now, ths_df.copy())
            return ths_df

        # Stale cache fallback: keep panel usable instead of returning empty payload.
        if self.concept_snapshot_cache is not None:
            _, cached = self.concept_snapshot_cache
            if not cached.empty:
                print("[data_provider] get_all_concepts fallback to stale cached data")
                return cached.copy()

        if last_exc is not None:
            print(f"[data_provider] get_all_concepts failed: {last_exc}")
        return pd.DataFrame()

    def get_all_concepts_ths(self, max_items: int = 40, cache_seconds: int = 600) -> pd.DataFrame:
        now = datetime.now()
        if self.concept_snapshot_cache is not None:
            ts, cached = self.concept_snapshot_cache
            if (now - ts).total_seconds() < cache_seconds and not cached.empty and "数据源" in cached.columns:
                if str(cached["数据源"].iloc[0]) == "ths":
                    return cached.copy()

        try:
            names_df = ak.stock_board_concept_name_ths()
            if names_df is None or names_df.empty or "name" not in names_df.columns:
                return pd.DataFrame()
        except Exception as exc:
            print(f"[data_provider] get_all_concepts_ths names failed: {exc}")
            return pd.DataFrame()

        leader_map: dict[str, str] = {}
        try:
            summary_df = ak.stock_board_concept_summary_ths()
            if summary_df is not None and not summary_df.empty:
                for _, row in summary_df.iterrows():
                    key = str(row.get("概念名称", "")).strip()
                    if key and key not in leader_map:
                        leader_map[key] = str(row.get("龙头股", "")).strip()
        except Exception:
            pass

        items: list[dict[str, Any]] = []
        for name in names_df["name"].astype(str).head(max_items).tolist():
            try:
                info_df = ak.stock_board_concept_info_ths(symbol=name)
            except Exception:
                continue

            if info_df is None or info_df.empty:
                continue
            info_map = {str(row.get("项目", "")).strip(): str(row.get("值", "")).strip() for _, row in info_df.iterrows()}
            change_pct = _parse_percent(info_map.get("板块涨幅", "0"))
            up_count, down_count = _parse_up_down(info_map.get("涨跌家数", "0/0"))
            items.append(
                {
                    "板块名称": name,
                    "涨跌幅": change_pct,
                    "上涨家数": up_count,
                    "下跌家数": down_count,
                    "领涨股票": leader_map.get(name, ""),
                    "涨停家数": 0,
                    "数据源": "ths",
                }
            )

        if not items:
            return pd.DataFrame()
        out = pd.DataFrame(items).sort_values("涨跌幅", ascending=False).reset_index(drop=True)
        return out

    def get_concept_constituents(self, concept_name: str) -> pd.DataFrame:
        if concept_name in self.concept_constituents_cache:
            return self.concept_constituents_cache[concept_name].copy()

        try:
            df = ak.stock_board_concept_cons_em(symbol=concept_name)
        except Exception:
            df = pd.DataFrame()

        self.concept_constituents_cache[concept_name] = df.copy()
        return df

    def get_hot_stocks(
        self,
        top_n: int = 10,
        retries: int = 2,
        retry_wait_seconds: float = 1.0,
        cache_ttl_seconds: int = 300,
    ) -> pd.DataFrame:
        last_exc: Exception | None = None
        for idx in range(retries + 1):
            try:
                raw = ak.stock_hot_rank_em()
                if raw is not None and not raw.empty:
                    normalized = normalize_hot_stocks_df(raw)
                    if not normalized.empty:
                        normalized = normalized.head(top_n).copy()
                        self.hot_stocks_cache = (datetime.now(), normalized)
                        return normalized
            except Exception as exc:
                last_exc = exc
                if idx < retries:
                    time.sleep(retry_wait_seconds * (idx + 1))

        if self.hot_stocks_cache is not None:
            ts, cached = self.hot_stocks_cache
            age = (datetime.now() - ts).total_seconds()
            if age <= cache_ttl_seconds:
                print("[data_provider] hot stocks fallback to cached data")
                return cached.head(top_n).copy()

        if last_exc is not None:
            print(f"[data_provider] get_hot_stocks failed: {last_exc}")
        return pd.DataFrame(columns=["rank", "symbol", "name", "latest_price", "change_amount", "change_pct"])

    def get_symbol_name_map(self, cache_ttl_seconds: int = 3600) -> dict[str, str]:
        now = datetime.now()
        if self.symbol_name_cache is not None:
            ts, payload = self.symbol_name_cache
            if (now - ts).total_seconds() <= cache_ttl_seconds:
                return dict(payload)

        try:
            df = ak.stock_info_a_code_name()
            if df is not None and not df.empty and {"code", "name"}.issubset(set(df.columns)):
                mapping = {
                    normalize_symbol(code): str(name)
                    for code, name in zip(df["code"].tolist(), df["name"].tolist())
                    if str(code).strip()
                }
                self.symbol_name_cache = (now, mapping)
                return mapping
        except Exception as exc:
            print(f"[data_provider] get_symbol_name_map failed: {exc}")

        if self.symbol_name_cache is not None:
            _, payload = self.symbol_name_cache
            return dict(payload)
        return {}



def to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        if isinstance(value, str):
            v = value.replace(",", "").strip()
            if v in {"", "-", "--", "None", "nan", "NaN"}:
                return default
            return float(v)
        return float(value)
    except Exception:
        return default



def to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return default


def _parse_percent(value: Any) -> float:
    raw = str(value or "").strip().replace("%", "")
    return to_float(raw, default=0.0)


def _parse_up_down(value: Any) -> tuple[int, int]:
    raw = str(value or "").strip()
    parts = re.split(r"[\\/]", raw)
    if len(parts) >= 2:
        return to_int(parts[0], 0), to_int(parts[1], 0)
    return 0, 0


def normalize_symbol(value: Any) -> str:
    raw = str(value or "").strip().upper()
    if raw.startswith(("SZ", "SH", "BJ")) and len(raw) > 2:
        raw = raw[2:]
    return raw


def normalize_hot_stocks_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=["rank", "symbol", "name", "latest_price", "change_amount", "change_pct"])

    rank_col = "当前排名" if "当前排名" in df.columns else ("排名" if "排名" in df.columns else None)
    code_col = "代码" if "代码" in df.columns else ("股票代码" if "股票代码" in df.columns else None)
    name_col = "股票名称" if "股票名称" in df.columns else ("名称" if "名称" in df.columns else None)
    price_col = "最新价" if "最新价" in df.columns else ("现价" if "现价" in df.columns else None)
    change_amount_col = "涨跌额" if "涨跌额" in df.columns else None
    change_pct_col = "涨跌幅" if "涨跌幅" in df.columns else None

    if code_col is None or name_col is None:
        return pd.DataFrame(columns=["rank", "symbol", "name", "latest_price", "change_amount", "change_pct"])

    payload = pd.DataFrame()
    payload["rank"] = (
        pd.to_numeric(df[rank_col], errors="coerce").fillna(9999).astype(int)
        if rank_col is not None
        else range(1, len(df) + 1)
    )
    payload["symbol"] = df[code_col].apply(normalize_symbol)
    payload["name"] = df[name_col].astype(str)
    payload["latest_price"] = pd.to_numeric(df[price_col], errors="coerce").fillna(0.0) if price_col else 0.0
    payload["change_amount"] = (
        pd.to_numeric(df[change_amount_col], errors="coerce").fillna(0.0) if change_amount_col else 0.0
    )
    payload["change_pct"] = pd.to_numeric(df[change_pct_col], errors="coerce").fillna(0.0) if change_pct_col else 0.0

    payload = payload[payload["symbol"].str.startswith(("00", "60"))]
    payload = payload[~payload["name"].str.upper().str.contains("ST", na=False)]
    payload = payload.sort_values("rank", ascending=True)
    payload = payload.drop_duplicates(subset=["symbol"], keep="first")
    return payload.reset_index(drop=True)
