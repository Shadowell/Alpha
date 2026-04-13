from __future__ import annotations

from typing import Any

import pandas as pd

from app.config import TAG_COLORS
from app.services.data_provider import AkshareDataProvider, to_float, to_int


def _safe_col(df: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
    if col not in df.columns:
        return pd.Series([default] * len(df), index=df.index)
    return pd.to_numeric(df[col], errors="coerce").fillna(default)


async def build_concept_heat(
    provider: AkshareDataProvider,
    top_n: int = 120,
    concepts_df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    raw = concepts_df.copy() if concepts_df is not None else await provider.get_all_concepts()
    if raw.empty:
        return pd.DataFrame()

    df = raw.copy()
    if "板块名称" not in df.columns:
        return pd.DataFrame()

    df["涨跌幅"] = _safe_col(df, "涨跌幅", 0.0)
    df["上涨家数"] = _safe_col(df, "上涨家数", 0)
    df["下跌家数"] = _safe_col(df, "下跌家数", 0)

    df = df.sort_values("涨跌幅", ascending=False).head(top_n).copy()

    if "涨停家数" in df.columns:
        df["涨停家数"] = _safe_col(df, "涨停家数", 0).astype(int)
    else:
        limit_counts: list[int] = []
        for _, row in df.iterrows():
            name = str(row.get("板块名称", ""))
            cons = await provider.get_concept_constituents(name)
            if cons.empty or "涨跌幅" not in cons.columns:
                limit_counts.append(0)
                continue
            cnt = int((pd.to_numeric(cons["涨跌幅"], errors="coerce").fillna(0) >= 9.8).sum())
            limit_counts.append(cnt)
        df["涨停家数"] = limit_counts

    pct_rank = df["涨跌幅"].rank(pct=True, method="max")
    limit_rank = df["涨停家数"].rank(pct=True, method="max")
    total = (df["上涨家数"] + df["下跌家数"]).replace(0, 1)
    up_ratio = df["上涨家数"] / total

    df["heat"] = 0.5 * pct_rank + 0.3 * limit_rank + 0.2 * up_ratio
    df = df.sort_values(["heat", "涨停家数"], ascending=False)
    return df


async def map_stock_concepts(
    provider: AkshareDataProvider,
    symbols: set[str],
    concept_heat_df: pd.DataFrame,
) -> dict[str, list[dict[str, Any]]]:
    stock_map: dict[str, list[dict[str, Any]]] = {s: [] for s in symbols}
    if not symbols or concept_heat_df.empty:
        return stock_map

    for _, row in concept_heat_df.iterrows():
        concept_name = str(row.get("板块名称", ""))
        if not concept_name:
            continue
        cons = await provider.get_concept_constituents(concept_name)
        if cons.empty or "代码" not in cons.columns:
            continue

        cons_codes = set(cons["代码"].astype(str).tolist())
        matched = symbols.intersection(cons_codes)
        if not matched:
            continue

        payload = {
            "name": concept_name,
            "heat": round(to_float(row.get("heat")), 4),
            "change_pct": to_float(row.get("涨跌幅")),
            "limit_up_count": to_int(row.get("涨停家数")),
            "up_count": to_int(row.get("上涨家数")),
            "down_count": to_int(row.get("下跌家数")),
            "leader": str(row.get("领涨股票", "")),
        }
        for symbol in matched:
            stock_map[symbol].append(payload)

    for symbol in stock_map:
        stock_map[symbol].sort(key=lambda x: (x["heat"], x["limit_up_count"]), reverse=True)

    return stock_map


def build_top_tags(stock_concepts: list[dict[str, Any]], top_k: int = 3) -> list[dict[str, Any]]:
    tags: list[dict[str, Any]] = []
    for rank, concept in enumerate(stock_concepts[:top_k], start=1):
        tags.append(
            {
                "name": concept["name"],
                "rank": rank,
                "color": TAG_COLORS.get(rank, "#6b7280"),
                "heat": concept["heat"],
                "change_pct": concept["change_pct"],
                "limit_up_count": concept["limit_up_count"],
                "up_count": concept["up_count"],
                "down_count": concept["down_count"],
                "leader": concept.get("leader", ""),
            }
        )
    return tags


def build_hot_concepts_payload(
    concept_heat_df: pd.DataFrame,
    selected_symbols: set[str],
    stock_concepts_map: dict[str, list[dict[str, Any]]],
    top_n: int = 20,
) -> list[dict[str, Any]]:
    if concept_heat_df.empty:
        return []

    selected_count_map: dict[str, int] = {}
    for symbol in selected_symbols:
        for concept in stock_concepts_map.get(symbol, []):
            selected_count_map[concept["name"]] = selected_count_map.get(concept["name"], 0) + 1

    items: list[dict[str, Any]] = []
    for _, row in concept_heat_df.head(top_n).iterrows():
        name = str(row.get("板块名称", ""))
        items.append(
            {
                "name": name,
                "heat": round(to_float(row.get("heat")), 4),
                "change_pct": round(to_float(row.get("涨跌幅")), 2),
                "limit_up_count": to_int(row.get("涨停家数")),
                "up_count": to_int(row.get("上涨家数")),
                "down_count": to_int(row.get("下跌家数")),
                "leader": str(row.get("领涨股票", "")),
                "selected_count": selected_count_map.get(name, 0),
            }
        )
    return items
