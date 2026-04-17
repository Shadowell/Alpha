from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
import re
from typing import Any

import akshare as ak
import pandas as pd


def _is_trading_hours(dt: datetime | None = None) -> bool:
    """判断当前是否在 A 股连续竞价时段（±5 分钟缓冲）。"""
    cur = dt or datetime.now()
    if cur.weekday() >= 5:
        return False
    t = cur.time()
    return (dtime(9, 25) <= t <= dtime(11, 35)) or (dtime(12, 55) <= t <= dtime(15, 5))


@dataclass
class AkshareDataProvider:
    concept_constituents_cache: dict[str, pd.DataFrame] = field(default_factory=dict)
    concept_snapshot_cache: tuple[datetime, pd.DataFrame] | None = None
    realtime_snapshot_cache: tuple[datetime, pd.DataFrame] | None = None
    realtime_snapshot_source: str = "none"  # em_live / db_fallback / stale_cache / none
    hot_stocks_cache: tuple[datetime, pd.DataFrame] | None = None
    symbol_name_cache: tuple[datetime, dict[str, str]] | None = None
    kline_store: Any = None

    def _snapshot_from_db(self) -> pd.DataFrame:
        """从本地 K 线数据库构建类行情快照（仅用于盘后或实时接口不可用时）。"""
        if self.kline_store is None:
            return pd.DataFrame()
        try:
            rows = self.kline_store.get_latest_snapshot()
            if not rows:
                return pd.DataFrame()
            name_map: dict[str, str] = {}
            if self.symbol_name_cache is not None:
                _, name_map = self.symbol_name_cache
            items = []
            for r in rows:
                s = r["symbol"]
                close = r["close"]
                prev = r["prev_close"]
                pct = ((close / max(prev, 0.01)) - 1) * 100
                items.append({
                    "代码": s,
                    "名称": name_map.get(s, s),
                    "最新价": close,
                    "涨跌额": round(close - prev, 4),
                    "涨跌幅": round(pct, 4),
                    "昨收": prev,
                    "今开": r["open"],
                    "最高": r["high"],
                    "最低": r["low"],
                    "成交量": r["volume"],
                    "成交额": r["amount"],
                    "总市值": pd.NA,
                })
            return pd.DataFrame(items)
        except Exception as exc:
            print(f"[data_provider] snapshot_from_db failed: {exc}")
            return pd.DataFrame()

    async def _fetch_spot_em(self) -> pd.DataFrame:
        """拉取东财全市场 spot 快照（真正的实时接口），失败自动降级到 sina。"""
        try:
            df = await asyncio.to_thread(ak.stock_zh_a_spot_em)
            payload = self._normalize_snapshot(df)
            if not payload.empty:
                await self._ensure_names_from_snapshot(payload)
                return payload
        except Exception as exc:
            print(f"[data_provider] _fetch_spot_em failed: {exc}")
        # 兜底：新浪 spot
        try:
            df = await asyncio.to_thread(ak.stock_zh_a_spot)
            payload = self._normalize_snapshot(df)
            if not payload.empty:
                await self._ensure_names_from_snapshot(payload)
            return payload
        except Exception as exc:
            print(f"[data_provider] _fetch_spot_sina failed: {exc}")
            return pd.DataFrame()

    async def _ensure_names_from_snapshot(self, snapshot: pd.DataFrame) -> None:
        """从 spot snapshot 中自动提取 symbol->name 写入 cache + DB。"""
        try:
            if snapshot.empty or "代码" not in snapshot.columns or "名称" not in snapshot.columns:
                return
            name_map = {
                str(r["代码"]): str(r["名称"])
                for _, r in snapshot.iterrows()
                if r.get("代码") and r.get("名称")
            }
            if not name_map:
                return
            self.symbol_name_cache = (datetime.now(), name_map)
            if self.kline_store is not None:
                try:
                    await asyncio.to_thread(
                        self.kline_store.upsert_symbol_names, name_map, datetime.now().isoformat()
                    )
                except Exception as exc:
                    print(f"[data_provider] persist symbol_names failed: {exc}")
        except Exception as exc:
            print(f"[data_provider] ensure_names_from_snapshot failed: {exc}")

    async def get_realtime_snapshot(
        self,
        retries: int = 2,
        retry_wait_seconds: float = 1.0,
        cache_ttl_seconds: int = 300,
        prefer_live: bool | None = None,
    ) -> pd.DataFrame:
        """全市场实时/准实时行情快照。

        策略：
        - 缓存命中（TTL 内） → 直接返回
        - prefer_live=True（默认盘中自动判断） → 调东财 spot
        - live 失败或盘后 → 从 DB 构建快照
        - DB 也空 → 返回 stale cache / 空
        """
        if prefer_live is None:
            prefer_live = _is_trading_hours()

        # 1) 缓存命中优先
        if self.realtime_snapshot_cache is not None:
            ts, cached = self.realtime_snapshot_cache
            age = (datetime.now() - ts).total_seconds()
            if age <= max(1, cache_ttl_seconds) and not cached.empty:
                return cached.copy()

        # 2) 尝试实时源（带重试）
        if prefer_live:
            last_exc: Exception | None = None
            for idx in range(retries + 1):
                try:
                    df = await self._fetch_spot_em()
                    if not df.empty:
                        self.realtime_snapshot_cache = (datetime.now(), df)
                        self.realtime_snapshot_source = "em_live"
                        return df.copy()
                except Exception as exc:
                    last_exc = exc
                if idx < retries:
                    await asyncio.sleep(retry_wait_seconds * (idx + 1))
            if last_exc is not None:
                print(f"[data_provider] realtime spot failed: {last_exc}")

        # 3) DB fallback
        db_df = self._snapshot_from_db()
        if not db_df.empty:
            self.realtime_snapshot_cache = (datetime.now(), db_df)
            self.realtime_snapshot_source = "db_fallback"
            return db_df.copy()

        # 4) stale cache
        if self.realtime_snapshot_cache is not None:
            _, cached = self.realtime_snapshot_cache
            if not cached.empty:
                self.realtime_snapshot_source = "stale_cache"
                return cached.copy()
        self.realtime_snapshot_source = "none"
        return pd.DataFrame()

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

    async def get_snapshot_em(
        self,
        retries: int = 2,
        retry_wait_seconds: float = 1.0,
        cache_ttl_seconds: int = 300,
    ) -> pd.DataFrame:
        if self.realtime_snapshot_cache is not None:
            ts, cached = self.realtime_snapshot_cache
            age = (datetime.now() - ts).total_seconds()
            if age <= cache_ttl_seconds:
                return cached.copy()

        last_exc: Exception | None = None
        for idx in range(retries + 1):
            try:
                df = await asyncio.to_thread(ak.stock_zh_a_spot_em)
                payload = self._normalize_snapshot(df)
                if not payload.empty:
                    self.realtime_snapshot_cache = (datetime.now(), payload)
                    return payload
            except Exception as exc:
                last_exc = exc
                if idx < retries:
                    await asyncio.sleep(retry_wait_seconds * (idx + 1))

        if self.realtime_snapshot_cache is not None:
            ts, cached = self.realtime_snapshot_cache
            print("[data_provider] realtime snapshot fallback to stale cache")
            return cached.copy()

        if last_exc is not None:
            print(f"[data_provider] get_realtime_snapshot failed: {last_exc}")
        return pd.DataFrame()

    async def get_snapshot_spot(
        self,
        retries: int = 1,
        retry_wait_seconds: float = 1.0,
        cache_ttl_seconds: int = 300,
    ) -> pd.DataFrame:
        return await self.get_realtime_snapshot(cache_ttl_seconds=cache_ttl_seconds)

    async def get_trade_days(self) -> pd.DataFrame:
        if self.kline_store is not None:
            try:
                dates = self.kline_store.get_trade_dates_from_db()
                if dates:
                    return pd.DataFrame({"trade_date": dates})
            except Exception:
                pass
        try:
            df = await asyncio.to_thread(ak.tool_trade_date_hist_sina)
            if df is None or df.empty:
                return pd.DataFrame(columns=["trade_date"])
            return df.copy()
        except Exception as exc:
            print(f"[data_provider] get_trade_days failed: {exc}")
            return pd.DataFrame(columns=["trade_date"])

    async def get_hist(self, symbol: str, start_date: str, end_date: str, adjust: str = "qfq", force_remote: bool = False) -> pd.DataFrame:
        if self.kline_store is not None and not force_remote:
            try:
                import sqlite3
                s_fmt = start_date.replace("-", "")
                e_fmt = end_date.replace("-", "")
                s_iso = f"{s_fmt[:4]}-{s_fmt[4:6]}-{s_fmt[6:8]}" if len(s_fmt) == 8 else start_date
                e_iso = f"{e_fmt[:4]}-{e_fmt[4:6]}-{e_fmt[6:8]}" if len(e_fmt) == 8 else end_date
                conn = sqlite3.connect(str(self.kline_store.db_path))
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """SELECT trade_date, open, high, low, close, volume, amount
                       FROM kline_daily
                       WHERE symbol = ? AND trade_date >= ? AND trade_date <= ?
                       ORDER BY trade_date ASC""",
                    (symbol, s_iso, e_iso),
                ).fetchall()
                conn.close()
                if rows:
                    items = []
                    for r in rows:
                        items.append({
                            "日期": r["trade_date"],
                            "开盘": float(r["open"]),
                            "最高": float(r["high"]),
                            "最低": float(r["low"]),
                            "收盘": float(r["close"]),
                            "成交量": float(r["volume"]),
                            "成交额": float(r["amount"]),
                        })
                    return pd.DataFrame(items)
            except Exception as exc:
                print(f"[data_provider] get_hist DB read failed for {symbol}: {exc}")

        try:
            df = await asyncio.to_thread(
                ak.stock_zh_a_hist,
                symbol=symbol,
                period="daily",
                start_date=start_date,
                end_date=end_date,
                adjust=adjust,
            )
            if df is None or df.empty:
                raise ValueError("empty from stock_zh_a_hist")
            return df.copy()
        except Exception as exc:
            print(f"[data_provider] get_hist failed for {symbol}: {exc}")
        try:
            tx_symbol = _to_tx_symbol(symbol)
            tx_df = await asyncio.to_thread(
                ak.stock_zh_a_hist_tx,
                symbol=tx_symbol,
                start_date=start_date,
                end_date=end_date,
                adjust=adjust,
            )
            if tx_df is None or tx_df.empty:
                return pd.DataFrame()
            payload = tx_df.copy()
            rename_map = {
                "date": "日期",
                "open": "开盘",
                "close": "收盘",
                "high": "最高",
                "low": "最低",
                "amount": "成交量",
            }
            payload = payload.rename(columns=rename_map)
            for col in ["开盘", "收盘", "最高", "最低", "成交量"]:
                if col in payload.columns:
                    payload[col] = pd.to_numeric(payload[col], errors="coerce").fillna(0.0)
            if "成交额" not in payload.columns:
                payload["成交额"] = 0.0
            return payload
        except Exception as tx_exc:
            print(f"[data_provider] get_hist tx fallback failed for {symbol}: {tx_exc}")
            return pd.DataFrame()

    async def get_all_concepts(self, cache_seconds: int = 300) -> pd.DataFrame:
        if self.concept_snapshot_cache is not None:
            ts, cached = self.concept_snapshot_cache
            age = (datetime.now() - ts).total_seconds()
            if age <= cache_seconds and not cached.empty:
                return cached.copy()

        try:
            df = await asyncio.to_thread(ak.stock_board_industry_summary_ths)
            if df is not None and not df.empty:
                result = _normalize_ths_industry(df)
                if not result.empty:
                    self.concept_snapshot_cache = (datetime.now(), result)
                    print(f"[data_provider] concepts via stock_board_industry_summary_ths: {len(result)} rows")
                    return result
        except Exception as e:
            print(f"[data_provider] stock_board_industry_summary_ths failed: {e}")

        if self.concept_snapshot_cache is not None:
            _, cached = self.concept_snapshot_cache
            if not cached.empty:
                return cached.copy()
        return pd.DataFrame()

    async def get_all_concepts_ths(self, max_items: int = 40, cache_seconds: int = 600) -> pd.DataFrame:
        return await self.get_all_concepts(cache_seconds=cache_seconds)

    async def fetch_concept_board_names_em(
        self,
        retries: int = 5,
        base_delay_sec: float = 1.5,
    ) -> tuple[pd.DataFrame, str]:
        """东方财富概念板块一览（含涨跌幅）；连接被远端断开时重试，仍失败则回退同花顺概念名称列表。

        同花顺接口无板块涨跌幅，返回的 ``涨跌幅`` 置为 0，并按交易日种子打乱顺序，
        以便在无法访问东财时仍能覆盖不同概念（非「涨幅榜 Top K」，仅作降级）。

        Returns:
            (dataframe, source) — ``source`` 为 ``em`` 或 ``ths_names``。
        """
        last_exc: Exception | None = None
        for attempt in range(retries):
            try:
                df = await asyncio.to_thread(ak.stock_board_concept_name_em)
                if df is not None and not df.empty:
                    return df.copy(), "em"
            except Exception as exc:
                last_exc = exc
                if attempt < retries - 1:
                    await asyncio.sleep(base_delay_sec * (attempt + 1))
        print(f"[data_provider] stock_board_concept_name_em failed after {retries} tries: {last_exc}")
        try:
            ths = await asyncio.to_thread(ak.stock_board_concept_name_ths)
        except Exception as exc2:
            if last_exc is not None:
                raise last_exc from exc2
            raise
        if ths is None or ths.empty or "name" not in ths.columns:
            if last_exc is not None:
                raise last_exc
            raise RuntimeError("同花顺概念名称列表为空")
        out = ths.rename(columns={"name": "板块名称"}).copy()
        if "涨跌幅" not in out.columns:
            out["涨跌幅"] = 0.0
        seed = int(datetime.now().strftime("%Y%m%d"))
        out = out.sample(frac=1.0, random_state=seed).reset_index(drop=True)
        print(f"[data_provider] concept board list fallback: stock_board_concept_name_ths ({len(out)} rows)")
        return out, "ths_names"

    async def get_concept_constituents(self, concept_name: str, fetch_if_missing: bool = False) -> pd.DataFrame:
        if concept_name in self.concept_constituents_cache:
            return self.concept_constituents_cache[concept_name].copy()
        if not fetch_if_missing:
            return pd.DataFrame()
        df = pd.DataFrame()
        em_attempts = 5
        for attempt in range(em_attempts):
            try:
                df = await asyncio.to_thread(ak.stock_board_concept_cons_em, symbol=concept_name)
                if df is not None and not df.empty:
                    break
            except Exception as exc:
                if attempt == em_attempts - 1:
                    print(f"[data_provider] concept cons em failed ({concept_name}): {exc}")
                await asyncio.sleep(min(8.0, 1.0 * (2**attempt)))
        if df is None or df.empty:
            ind_attempts = 3
            for attempt in range(ind_attempts):
                try:
                    df = await asyncio.to_thread(ak.stock_board_industry_cons_em, symbol=concept_name)
                    if df is not None and not df.empty:
                        break
                except Exception as exc:
                    if attempt == ind_attempts - 1:
                        print(f"[data_provider] industry cons em failed ({concept_name}): {exc}")
                    await asyncio.sleep(min(6.0, 1.0 * (2**attempt)))
        if df is not None and not df.empty:
            self.concept_constituents_cache[concept_name] = df.copy()
            return df.copy()
        return pd.DataFrame()

    async def get_hot_stocks(
        self,
        top_n: int = 30,
        retries: int = 2,
        retry_wait_seconds: float = 1.0,
        cache_ttl_seconds: int = 300,
    ) -> pd.DataFrame:
        if self.hot_stocks_cache is not None:
            ts, cached = self.hot_stocks_cache
            age = (datetime.now() - ts).total_seconds()
            if age <= cache_ttl_seconds and not cached.empty:
                return cached.head(top_n).copy()

        last_exc: Exception | None = None
        for idx in range(retries + 1):
            try:
                df = await self._fetch_hot_stocks_from_spot()
                if not df.empty:
                    self.hot_stocks_cache = (datetime.now(), df)
                    return df.head(top_n).copy()
            except Exception as exc:
                last_exc = exc
                if idx < retries:
                    await asyncio.sleep(retry_wait_seconds * (idx + 1))

        if self.hot_stocks_cache is not None:
            _, cached = self.hot_stocks_cache
            if not cached.empty:
                return cached.head(top_n).copy()

        if last_exc is not None:
            print(f"[data_provider] get_hot_stocks failed: {last_exc}")
        return pd.DataFrame(columns=["rank", "symbol", "name", "latest_price", "change_amount", "change_pct"])

    async def _fetch_hot_stocks_from_spot(self) -> pd.DataFrame:
        """优先用同花顺连续上涨排行，fallback 到创新高排行，最终 fallback 到新浪全市场。"""
        # 1) stock_rank_lxsz_ths — 连续上涨排行
        try:
            df = await asyncio.to_thread(ak.stock_rank_lxsz_ths)
            if df is not None and not df.empty:
                result = _normalize_ths_lxsz(df)
                if not result.empty:
                    print(f"[data_provider] hot stocks via stock_rank_lxsz_ths: {len(result)} rows")
                    return result
        except Exception as e:
            print(f"[data_provider] stock_rank_lxsz_ths failed: {e}")

        # 2) stock_rank_cxg_ths — 创新高排行
        try:
            df = await asyncio.to_thread(ak.stock_rank_cxg_ths)
            if df is not None and not df.empty:
                result = _normalize_ths_cxg(df)
                if not result.empty:
                    print(f"[data_provider] hot stocks via stock_rank_cxg_ths: {len(result)} rows")
                    return result
        except Exception as e:
            print(f"[data_provider] stock_rank_cxg_ths failed: {e}")

        # 3) stock_zh_a_spot — 新浪全市场行情（最终 fallback）
        try:
            df = await asyncio.to_thread(ak.stock_zh_a_spot)
            if df is None or df.empty:
                return pd.DataFrame()
            payload = self._normalize_snapshot(df)
            if payload.empty:
                return pd.DataFrame()
            payload = payload[payload["最新价"] > 0]
            payload = payload[~payload["名称"].str.upper().str.contains("ST", na=False)]
            payload = payload[payload["代码"].str.startswith(("00", "60"))]
            payload = payload.sort_values("涨跌幅", ascending=False)
            self.realtime_snapshot_cache = (datetime.now(), payload)

            result = pd.DataFrame()
            result["rank"] = range(1, len(payload) + 1)
            result["symbol"] = payload["代码"].values
            result["name"] = payload["名称"].values
            result["latest_price"] = payload["最新价"].values
            result["change_amount"] = payload["涨跌额"].values
            result["change_pct"] = payload["涨跌幅"].values
            print(f"[data_provider] hot stocks via stock_zh_a_spot: {len(result)} rows")
            return result.reset_index(drop=True)
        except Exception as exc:
            print(f"[data_provider] _fetch_hot_stocks_from_spot all fallbacks failed: {exc}")
            return pd.DataFrame()

    async def get_symbol_name_map(self, cache_ttl_seconds: int = 3600) -> dict[str, str]:
        """获取全市场 symbol -> name 映射（冷启动自动拉取）。

        顺序：内存缓存 → DB 持久化表 → AkShare 实时 spot → 兜底空 dict
        """
        now = datetime.now()
        if self.symbol_name_cache is not None:
            ts, payload = self.symbol_name_cache
            if (now - ts).total_seconds() <= cache_ttl_seconds:
                return dict(payload)

        # 从 DB 持久化表加载
        if self.kline_store is not None:
            try:
                persisted = await asyncio.to_thread(self.kline_store.load_symbol_names)
                if persisted:
                    self.symbol_name_cache = (now, persisted)
                    # DB 命中后仍尝试后台刷新，但不阻塞
                    asyncio.create_task(self._refresh_names_from_live())
                    return dict(persisted)
            except Exception as exc:
                print(f"[data_provider] load_symbol_names from DB failed: {exc}")

        # 冷启动：统一走 _fetch_spot_em（含 sina 兜底），最多 3 次
        for attempt in range(3):
            payload = await self._fetch_spot_em()
            if not payload.empty:
                name_map = {
                    str(r["代码"]): str(r["名称"])
                    for _, r in payload.iterrows()
                    if r.get("代码") and r.get("名称")
                }
                if name_map:
                    # _ensure_names_from_snapshot 已经在 _fetch_spot_em 里落盘
                    self.realtime_snapshot_cache = (now, payload)
                    self.realtime_snapshot_source = "em_live"
                    return name_map
            await asyncio.sleep(1.5 * (attempt + 1))
        print("[data_provider] get_symbol_name_map cold-start failed after retries, will retry in background")
        asyncio.create_task(self._refresh_names_from_live())

        if self.symbol_name_cache is not None:
            _, payload = self.symbol_name_cache
            return dict(payload)
        return {}

    async def _refresh_names_from_live(self) -> None:
        """后台刷新 symbol 名称映射（DB 命中后异步兜底更新）。"""
        try:
            df = await asyncio.to_thread(ak.stock_zh_a_spot_em)
            payload = self._normalize_snapshot(df)
            if payload.empty:
                return
            await self._ensure_names_from_snapshot(payload)
        except Exception:
            pass



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


def _to_tx_symbol(value: Any) -> str:
    raw = normalize_symbol(value)
    if raw.startswith(("60", "68")):
        return f"sh{raw}"
    if raw.startswith(("00", "30")):
        return f"sz{raw}"
    if raw.startswith(("43", "83", "87", "92")):
        return f"bj{raw}"
    return raw.lower()


def _normalize_ths_industry(df: pd.DataFrame) -> pd.DataFrame:
    """stock_board_industry_summary_ths 返回: 序号, 板块, 涨跌幅, 总成交量, 总成交额, 净流入, 上涨家数, 下跌家数, 均价, 领涨股, 领涨股-涨跌幅。
    映射为 build_concept_heat 需要的字段: 板块名称, 涨跌幅, 上涨家数, 下跌家数, 领涨股票。"""
    if df is None or df.empty:
        return pd.DataFrame()
    name_col = "板块" if "板块" in df.columns else ("板块名称" if "板块名称" in df.columns else None)
    if not name_col:
        return pd.DataFrame()

    out = pd.DataFrame()
    out["板块名称"] = df[name_col].astype(str)
    out["涨跌幅"] = pd.to_numeric(df.get("涨跌幅", pd.Series(dtype=float)), errors="coerce").fillna(0.0)
    out["上涨家数"] = pd.to_numeric(df.get("上涨家数", pd.Series(dtype=float)), errors="coerce").fillna(0).astype(int)
    out["下跌家数"] = pd.to_numeric(df.get("下跌家数", pd.Series(dtype=float)), errors="coerce").fillna(0).astype(int)

    leader_col = "领涨股" if "领涨股" in df.columns else None
    leader_pct_col = "领涨股-涨跌幅" if "领涨股-涨跌幅" in df.columns else None
    if leader_col:
        if leader_pct_col:
            out["领涨股票"] = df[leader_col].astype(str) + "(" + df[leader_pct_col].astype(str) + "%)"
        else:
            out["领涨股票"] = df[leader_col].astype(str)
    else:
        out["领涨股票"] = ""

    return out.reset_index(drop=True)


def _normalize_ths_lxsz(df: pd.DataFrame) -> pd.DataFrame:
    """stock_rank_lxsz_ths 返回: 股票代码, 股票简称, 收盘价, 最高价, 最低价, 连涨天数, 连续涨跌幅, 累计换手率, 所属行业。"""
    if df is None or df.empty:
        return pd.DataFrame()
    code_col = "股票代码" if "股票代码" in df.columns else None
    name_col = "股票简称" if "股票简称" in df.columns else None
    if not code_col or not name_col:
        return pd.DataFrame()

    out = pd.DataFrame()
    out["symbol"] = df[code_col].apply(normalize_symbol)
    out["name"] = df[name_col].astype(str)
    price_col = "收盘价" if "收盘价" in df.columns else ("最新价" if "最新价" in df.columns else None)
    out["latest_price"] = pd.to_numeric(df[price_col], errors="coerce").fillna(0.0) if price_col else 0.0
    pct_col = "连续涨跌幅" if "连续涨跌幅" in df.columns else ("涨跌幅" if "涨跌幅" in df.columns else None)
    out["change_pct"] = pd.to_numeric(df[pct_col], errors="coerce").fillna(0.0) if pct_col else 0.0
    out["change_amount"] = 0.0

    out = out[out["symbol"].str.startswith(("00", "60"))]
    out = out[~out["name"].str.upper().str.contains("ST", na=False)]
    out = out.sort_values("change_pct", ascending=False).reset_index(drop=True)
    out["rank"] = range(1, len(out) + 1)
    return out


def _normalize_ths_cxg(df: pd.DataFrame) -> pd.DataFrame:
    """stock_rank_cxg_ths 返回: 序号, 股票代码, 股票简称, 涨跌幅, 最新价, 成交量, 成交额, 创新高类型。"""
    if df is None or df.empty:
        return pd.DataFrame()
    code_col = "股票代码" if "股票代码" in df.columns else None
    name_col = "股票简称" if "股票简称" in df.columns else None
    if not code_col or not name_col:
        return pd.DataFrame()

    out = pd.DataFrame()
    out["symbol"] = df[code_col].apply(normalize_symbol)
    out["name"] = df[name_col].astype(str)
    out["latest_price"] = pd.to_numeric(df.get("最新价", pd.Series(dtype=float)), errors="coerce").fillna(0.0)
    out["change_pct"] = pd.to_numeric(df.get("涨跌幅", pd.Series(dtype=float)), errors="coerce").fillna(0.0)
    out["change_amount"] = 0.0

    out = out[out["symbol"].str.startswith(("00", "60"))]
    out = out[~out["name"].str.upper().str.contains("ST", na=False)]
    out = out.sort_values("change_pct", ascending=False).reset_index(drop=True)
    out["rank"] = range(1, len(out) + 1)
    return out


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
