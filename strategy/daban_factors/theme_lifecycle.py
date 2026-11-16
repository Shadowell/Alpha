"""#29 题材板块联动、身位与生命周期因子族（纯计算 + PIT 只读）。

覆盖 5 组因子：
A. 题材热度：``concept_heat_rank``、``concept_heat_score``、``concept_limit_up_count``、
   ``concept_first_board_count``、``concept_continuation_count``、``concept_amount_ratio``、
   ``concept_index_return_1d`` / ``3d`` / ``5d``
B. 身位：``board_position_in_concept``、``concept_leader_gap``、``is_concept_leader``、
   ``is_concept_follower``、``same_concept_limit_up_count``、``ladder_integrity_in_concept``
C. 生命周期：``concept_age_days``、``concept_phase``、``concept_new_theme_flag``、
   ``concept_diffusion``、``concept_persistence``
D. 个股题材属性：``concept_count``、``concept_purity``、``is_main_line``、``cross_concept_count``
E. 交互项：``concept_heat_x_fd_ratio``、``board_position_x_market_regime``、
   ``concept_age_x_continuation_rate``
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from strategy.daban_factors._base import (
    FactorSpec,
    attach_indicators,
)

THEME_LIFECYCLE_SPECS: tuple[FactorSpec, ...] = (
    FactorSpec("concept_heat_rank", "题材热度", "按日题材热度降序排名(1=最热)", "序数", "limit_board_daily+concept", "缺失记NaN", "仅用<=trade_date已披露概念与涨停数据"),
    FactorSpec("concept_heat_score", "题材热度", "主概念涨停数*10+连板数*15+成交占比*100", "无量纲", "limit_board_daily+concept", "缺失记NaN", "仅用<=trade_date截面"),
    FactorSpec("concept_limit_up_count", "题材热度", "所属主概念当日涨停家数", "次", "limit_board_daily+concept", "缺失记NaN", "仅用<=trade_date截面"),
    FactorSpec("concept_first_board_count", "题材热度", "所属主概念当日首板家数", "次", "limit_board_daily+concept", "缺失记NaN", "仅用<=trade_date截面"),
    FactorSpec("concept_continuation_count", "题材热度", "所属主概念当日连板(>=2)家数", "次", "limit_board_daily+concept", "缺失记NaN", "仅用<=trade_date截面"),
    FactorSpec("concept_amount_ratio", "题材热度", "主概念成交额/全市场成交额", "无量纲", "daily_kline+concept", "全市场成交额<=0记NaN", "仅用<=trade_date截面"),
    FactorSpec("concept_index_return_1d", "题材热度", "主概念1日等权平均涨跌幅", "无量纲", "daily_kline+concept", "缺失记NaN", "仅用<=trade_date截面"),
    FactorSpec("concept_index_return_3d", "题材热度", "主概念3日累计等权涨跌幅", "无量纲", "daily_kline+concept", "历史不足3日记NaN", "仅用<=trade_date窗口"),
    FactorSpec("concept_index_return_5d", "题材热度", "主概念5日累计等权涨跌幅", "无量纲", "daily_kline+concept", "历史不足5日记NaN", "仅用<=trade_date窗口"),
    FactorSpec("board_position_in_concept", "身位", "主概念内身位(1=龙头,2=二龙,3=跟风,4=补涨)", "序数", "limit_board_daily", "无概念记NaN", "仅用<=trade_date截面"),
    FactorSpec("concept_leader_gap", "身位", "概念最高连板数-个股连板数", "次", "limit_board_daily", "缺失记NaN", "仅用<=trade_date截面"),
    FactorSpec("is_concept_leader", "身位", "是否主概念最高板/首封龙头(0/1)", "0/1", "limit_board_daily", "缺失记NaN", "仅用<=trade_date截面"),
    FactorSpec("is_concept_follower", "身位", "是否概念跟风/补涨(0/1)", "0/1", "limit_board_daily", "缺失记NaN", "仅用<=trade_date截面"),
    FactorSpec("same_concept_limit_up_count", "身位", "同概念同日涨停家数", "次", "limit_board_daily", "缺失记NaN", "仅用<=trade_date截面"),
    FactorSpec("ladder_integrity_in_concept", "身位", "概念内1/2/3板阶梯完整度(0~1)", "无量纲", "limit_board_daily", "缺失记NaN", "仅用<=trade_date截面"),
    FactorSpec("concept_age_days", "生命周期", "主概念首次活跃至trade_date交易日数", "天", "concept_history", "无历史记NaN", "严格<=trade_date滚动累计"),
    FactorSpec("concept_phase", "生命周期", "生命周期阶段(1启动/2发酵/3高潮/4退潮)", "序数", "concept_history", "缺失记NaN", "仅用<=trade_date状态"),
    FactorSpec("concept_new_theme_flag", "生命周期", "是否新题材首日(age<=1)(0/1)", "0/1", "concept_history", "缺失记NaN", "仅用<=trade_date状态"),
    FactorSpec("concept_diffusion", "生命周期", "主概念涨停家数较前日变化率", "无量纲", "concept_history", "前日为0记NaN", "仅用<=trade_date相邻两日"),
    FactorSpec("concept_persistence", "生命周期", "主概念截至当日连续活跃天数", "天", "concept_history", "缺失记NaN", "严格<=trade_date连续计数"),
    FactorSpec("concept_count", "个股题材属性", "截至trade_date个股所属概念数", "次", "concept_members", "无概念记NaN", "PIT概念成员有效区间"),
    FactorSpec("concept_purity", "个股题材属性", "主概念权重/1或1/concept_count", "无量纲", "concept_members", "无概念记NaN", "PIT概念成员"),
    FactorSpec("is_main_line", "个股题材属性", "是否属于当日主线题材(heat_rank<=3)(0/1)", "0/1", "concept_heat", "缺失记NaN", "仅用<=trade_date截面"),
    FactorSpec("cross_concept_count", "个股题材属性", "个股跨热点概念数(heat_rank<=10)", "次", "concept_members", "缺失记NaN", "仅用<=trade_date截面"),
    FactorSpec("concept_heat_x_fd_ratio", "交互项", "concept_heat_score * fd_amount_ratio", "无量纲", "derived", "任一缺失记NaN", "仅用<=trade_date特征"),
    FactorSpec("board_position_x_market_regime", "交互项", "board_position_in_concept * regime_score", "无量纲", "derived", "任一缺失记NaN", "仅用<=trade_date特征"),
    FactorSpec("concept_age_x_continuation_rate", "交互项", "concept_age_days * (continuation/limit_up)", "天", "derived", "任一缺失记NaN", "仅用<=trade_date特征"),
)

THEME_FACTOR_NAMES: tuple[str, ...] = tuple(spec.name for spec in THEME_LIFECYCLE_SPECS)


def theme_lifecycle_factor_specs() -> tuple[FactorSpec, ...]:
    return THEME_LIFECYCLE_SPECS


def theme_lifecycle_capability() -> dict[str, Any]:
    return {
        "issue": 29,
        "status": "READY",
        "factor_count": len(THEME_LIFECYCLE_SPECS),
        "pit_guaranteed": True,
        "notes": "支持 PIT 概念成员区间过滤与纯内存截面/滚动计算",
    }


def _filter_pit_concepts(
    concept_members: pd.DataFrame | None,
    trade_date: str,
) -> pd.DataFrame:
    if concept_members is None or concept_members.empty:
        return pd.DataFrame(columns=["symbol", "concept", "weight"])
    df = concept_members.copy()
    if "effective_from" in df.columns:
        eff = df["effective_from"].astype(str)
        df = df[(eff == "") | (eff == "nan") | (eff <= trade_date)]
    if "effective_to" in df.columns:
        end = df["effective_to"].astype(str)
        df = df[(end == "") | (end == "nan") | (end >= trade_date)]
    if "weight" not in df.columns:
        df["weight"] = np.nan
    return df[["symbol", "concept", "weight"]].dropna(subset=["symbol", "concept"])


def compute_theme_lifecycle_factors(
    board_df: pd.DataFrame,
    *,
    concept_members: pd.DataFrame | None = None,
    concept_history: pd.DataFrame | None = None,
    trade_date: str | None = None,
    regime_score: float | None = None,
) -> pd.DataFrame:
    """计算 #29 题材板块联动、身位与生命周期因子族（严格 PIT，不读未来行）。"""
    if board_df is None or board_df.empty:
        return pd.DataFrame(columns=["symbol", "trade_date", *THEME_FACTOR_NAMES])

    df = board_df.copy()
    df["symbol"] = df["symbol"].astype(str)
    df["trade_date"] = df["trade_date"].astype(str)
    if trade_date is not None:
        df = df[df["trade_date"] <= str(trade_date)].copy()
        target_dates = [str(trade_date)]
    else:
        target_dates = sorted(df["trade_date"].unique().tolist())

    if df.empty:
        return pd.DataFrame(columns=["symbol", "trade_date", *THEME_FACTOR_NAMES])

    hist = (
        concept_history.copy()
        if concept_history is not None and not concept_history.empty
        else pd.DataFrame()
    )
    if not hist.empty:
        hist["trade_date"] = hist["trade_date"].astype(str)
        if trade_date is not None:
            hist = hist[hist["trade_date"] <= str(trade_date)].copy()

    rows: list[dict[str, Any]] = []
    for dt in target_dates:
        day = df[df["trade_date"] == dt].copy()
        if day.empty:
            continue

        pit_cm = _filter_pit_concepts(concept_members, dt)
        sym_to_concepts: dict[str, list[tuple[str, float]]] = {}
        if not pit_cm.empty:
            for sym, grp in pit_cm.groupby("symbol"):
                sym_to_concepts[str(sym)] = [
                    (str(r["concept"]), float(r["weight"]) if pd.notna(r["weight"]) else np.nan)
                    for _, r in grp.iterrows()
                ]
        elif "concept" in day.columns:
            for _, r in day.iterrows():
                c = r.get("concept")
                if pd.notna(c) and str(c).strip():
                    sym_to_concepts.setdefault(str(r["symbol"]), []).append((str(c).strip(), np.nan))

        # 统计当日概念维度的涨停/首板/连板/成交额
        total_mkt_amt = (
            float(pd.to_numeric(day["amount"], errors="coerce").sum(min_count=1))
            if "amount" in day.columns
            else np.nan
        )
        concept_stats: dict[str, dict[str, Any]] = {}
        for _, r in day.iterrows():
            sym = str(r["symbol"])
            c_list = sym_to_concepts.get(sym, [])
            is_lu = bool(r.get("is_limit_up", True))
            lt = pd.to_numeric(r.get("limit_times", np.nan), errors="coerce")
            amt = pd.to_numeric(r.get("amount", np.nan), errors="coerce")
            pct = pd.to_numeric(r.get("pct_chg", np.nan), errors="coerce")
            for cname, _w in c_list:
                st = concept_stats.setdefault(
                    cname,
                    {"lu": 0, "first": 0, "cont": 0, "amt": 0.0, "has_amt": False, "pcts": [], "lts": []},
                )
                if is_lu:
                    st["lu"] += 1
                    if pd.notna(lt):
                        st["lts"].append(float(lt))
                        if float(lt) <= 1:
                            st["first"] += 1
                        else:
                            st["cont"] += 1
                if pd.notna(amt):
                    st["amt"] += float(amt)
                    st["has_amt"] = True
                if pd.notna(pct):
                    st["pcts"].append(float(pct))

        # 概念热度排序
        for cname, st in concept_stats.items():
            amt_ratio = (
                st["amt"] / total_mkt_amt
                if st["has_amt"] and pd.notna(total_mkt_amt) and total_mkt_amt > 0
                else np.nan
            )
            st["amt_ratio"] = amt_ratio
            st["heat_score"] = (
                st["lu"] * 10.0
                + st["cont"] * 15.0
                + (amt_ratio * 100.0 if pd.notna(amt_ratio) else 0.0)
            )
        ranked_concepts = sorted(
            concept_stats.keys(), key=lambda c: (-concept_stats[c]["heat_score"], c)
        )
        heat_rank_map = {c: idx + 1 for idx, c in enumerate(ranked_concepts)}

        # 历史概念窗口（仅 <= dt）
        hist_le = hist[hist["trade_date"] <= dt] if not hist.empty else pd.DataFrame()
        prev_dates = sorted(df[df["trade_date"] < dt]["trade_date"].unique().tolist())
        prev_dt = prev_dates[-1] if prev_dates else None

        for _, r in day.iterrows():
            sym = str(r["symbol"])
            c_list = sym_to_concepts.get(sym, [])
            lt = pd.to_numeric(r.get("limit_times", np.nan), errors="coerce")
            fd_ratio = pd.to_numeric(r.get("fd_amount_ratio", np.nan), errors="coerce")
            reg_val = (
                float(regime_score)
                if regime_score is not None and np.isfinite(regime_score)
                else pd.to_numeric(r.get("regime_score", np.nan), errors="coerce")
            )

            rec: dict[str, Any] = {"symbol": sym, "trade_date": dt}
            for col in THEME_FACTOR_NAMES:
                rec[col] = np.nan

            if c_list:
                # 选热度最高的概念作为主概念
                primary_c, primary_w = max(
                    c_list,
                    key=lambda item: (
                        concept_stats.get(item[0], {}).get("heat_score", -1.0),
                        item[0],
                    ),
                )
                st = concept_stats.get(primary_c, {})
                hrank = heat_rank_map.get(primary_c, np.nan)
                rec["concept_heat_rank"] = float(hrank) if pd.notna(hrank) else np.nan
                rec["concept_heat_score"] = float(st.get("heat_score", np.nan))
                rec["concept_limit_up_count"] = float(st.get("lu", 0))
                rec["concept_first_board_count"] = float(st.get("first", 0))
                rec["concept_continuation_count"] = float(st.get("cont", 0))
                rec["same_concept_limit_up_count"] = float(st.get("lu", 0))
                rec["concept_amount_ratio"] = st.get("amt_ratio", np.nan)
                if st.get("pcts"):
                    rec["concept_index_return_1d"] = float(np.mean(st["pcts"]))

                # 阶梯完整度（1/2/3板各占1/3）
                lts = st.get("lts", [])
                tiers_present = sum(
                    [
                        any(x == 1 for x in lts),
                        any(x == 2 for x in lts),
                        any(x >= 3 for x in lts),
                    ]
                )
                rec["ladder_integrity_in_concept"] = tiers_present / 3.0

                max_lt = max(lts) if lts else np.nan
                if pd.notna(max_lt) and pd.notna(lt):
                    gap = float(max_lt) - float(lt)
                    rec["concept_leader_gap"] = gap
                    if gap <= 0:
                        pos = 1.0
                    elif gap == 1:
                        pos = 2.0
                    elif float(lt) >= 1:
                        pos = 3.0
                    else:
                        pos = 4.0
                    rec["board_position_in_concept"] = pos
                    rec["is_concept_leader"] = 1.0 if pos == 1.0 else 0.0
                    rec["is_concept_follower"] = 1.0 if pos >= 3.0 else 0.0

                rec["concept_count"] = float(len(c_list))
                rec["concept_purity"] = (
                    float(primary_w)
                    if pd.notna(primary_w)
                    else 1.0 / float(len(c_list))
                )
                rec["is_main_line"] = 1.0 if pd.notna(hrank) and hrank <= 3 else 0.0
                rec["cross_concept_count"] = float(
                    sum(1 for c, _ in c_list if heat_rank_map.get(c, 999) <= 10)
                )

                # 历史滚动：age / persistence / phase / 3d & 5d return / diffusion
                if not hist_le.empty and "concept" in hist_le.columns:
                    ch = hist_le[hist_le["concept"] == primary_c].sort_values("trade_date")
                    if not ch.empty:
                        age = float(len(ch))
                        rec["concept_age_days"] = age
                        rec["concept_new_theme_flag"] = 1.0 if age <= 1.0 else 0.0
                        # 连续活跃天数（从最近日期往回数）
                        if "is_active" in ch.columns:
                            act = ch["is_active"].astype(bool).tolist()
                            pers = 0
                            for flag in reversed(act):
                                if flag:
                                    pers += 1
                                else:
                                    break
                            rec["concept_persistence"] = float(pers)
                        else:
                            rec["concept_persistence"] = age
                        if "pct_chg" in ch.columns:
                            pcts = pd.to_numeric(ch["pct_chg"], errors="coerce").dropna()
                            if len(pcts) >= 3:
                                rec["concept_index_return_3d"] = float(pcts.iloc[-3:].sum())
                            if len(pcts) >= 5:
                                rec["concept_index_return_5d"] = float(pcts.iloc[-5:].sum())
                        if len(ch) >= 2 and "limit_up_count" in ch.columns:
                            prev_lu = pd.to_numeric(ch["limit_up_count"].iloc[-2], errors="coerce")
                            if pd.notna(prev_lu) and prev_lu > 0:
                                rec["concept_diffusion"] = (st.get("lu", 0) - float(prev_lu)) / float(prev_lu)
                        # 生命周期阶段：1启动/2发酵/3高潮/4退潮
                        if age <= 1:
                            phase = 1.0
                        elif pd.notna(rec["concept_diffusion"]) and rec["concept_diffusion"] < -0.3:
                            phase = 4.0
                        elif st.get("lu", 0) >= 5 and st.get("cont", 0) >= 2:
                            phase = 3.0
                        else:
                            phase = 2.0
                        rec["concept_phase"] = phase
                elif prev_dt is None:
                    rec["concept_age_days"] = 1.0
                    rec["concept_new_theme_flag"] = 1.0
                    rec["concept_persistence"] = 1.0
                    rec["concept_phase"] = 1.0

                # 交互项
                if pd.notna(rec["concept_heat_score"]) and pd.notna(fd_ratio):
                    rec["concept_heat_x_fd_ratio"] = float(rec["concept_heat_score"]) * float(fd_ratio)
                if pd.notna(rec["board_position_in_concept"]) and pd.notna(reg_val):
                    rec["board_position_x_market_regime"] = float(rec["board_position_in_concept"]) * float(reg_val)
                if pd.notna(rec["concept_age_days"]) and st.get("lu", 0) > 0:
                    rec["concept_age_x_continuation_rate"] = float(rec["concept_age_days"]) * (
                        float(st.get("cont", 0)) / float(st["lu"])
                    )

            rows.append(attach_indicators(rec, THEME_LIFECYCLE_SPECS))

    return pd.DataFrame(rows)


__all__ = [
    "THEME_FACTOR_NAMES",
    "THEME_LIFECYCLE_SPECS",
    "compute_theme_lifecycle_factors",
    "theme_lifecycle_capability",
    "theme_lifecycle_factor_specs",
]
