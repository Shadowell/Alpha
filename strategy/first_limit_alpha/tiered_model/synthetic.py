"""合成标签 / 因子面板 (GitHub issue #27)。

线上 ``limit_board_daily`` 目前为空表（#22 盘后日线调度才回填），因此分层模型的
**所有单元测试必须用合成数据**，且合成数据要能复现真实的数据病态：

* ``limit_times`` 缺失 → 未分层（``board_tier == ""``）；
* 场景 A 一字板不可成交（``entry_unfilled_A = 1``）→ 该样本 ``ret_*_A`` /
  ``fwd_ret_*`` 为 ``None``；
* 出场日跌停（``future_limit_flag_d{h} = 'D'``）→ 卖不出；
* 因子表刻意**不给**某些 ``(symbol, date)`` → 触发 PIT as-of 回看与
  ``*_is_missing``；
* 因子表刻意给若干 ``as_of_date > max(trade_date)`` 的行 → 验证它们被丢弃计数。

生成过程只用"决策时点 T 及之前"的随机数驱动标签，因此分层模型有**真实但有限**
的信号（IC 为正），同时保留一个纯噪声因子和一个只在未来窗口才有用的"泄漏因子"
供泄漏防线测试。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from ..microstructure_features import microstructure_output_columns

#: 只带"过去信息"的可用因子（取自 #24 的真实输出列名，便于接线时口径一致）。
USABLE_FACTORS: tuple[str, ...] = (
    "fd_ratio_float_mv",
    "first_seal_minute",
    "seal_duration_min",
    "open_times",
    "on_board_amount_ratio",
    "seal_quality_proxy",
    "limit_up_count_250d",
    "first_board_next_day_premium_avg",
    "continuation_success_rate",
    "broken_board_rate",
)
NOISE_FACTORS: tuple[str, ...] = ("limit_up_freq_250d", "up_stat_limit_density")
#: 合成场景里刻意全缺失的因子（验证缺数显式缺失，而不是被填 0）。
ALWAYS_MISSING_FACTORS: tuple[str, ...] = ("max_consecutive_boards_250d",)

FACTOR_COLUMNS: tuple[str, ...] = (*USABLE_FACTORS, *NOISE_FACTORS)


@dataclass(slots=True)
class SyntheticTierConfig:
    n_dates: int = 90
    n_symbols: int = 14
    seed: int = 20270727
    #: 每天抽多少标的涨停（模拟梯队宽度）
    boards_per_day: int = 5
    #: 一字板（场景 A 买不到）占比
    one_word_ratio: float = 0.22
    #: 出场跌停（卖不出）占比
    limit_down_ratio: float = 0.06
    #: 首板 / 2板 / 3板 / 高位板 / 反包 的日频构成权重。
    ladder_weights: tuple[float, float, float, float, float] = (
        0.45,
        0.20,
        0.12,
        0.11,
        0.12,
    )
    #: 信号强度：0 时纯噪声，1 时几乎确定
    signal_strength: float = 0.9
    #: 因子表缺失占比（某些 (symbol,date) 完全没有因子行）
    factor_gap_ratio: float = 0.15
    #: 注入多少"未来因子行"用于 PIT 丢弃测试
    future_factor_rows: int = 12
    label_horizon: int = 3

    def as_dict(self) -> dict[str, object]:
        return {
            "n_dates": self.n_dates,
            "n_symbols": self.n_symbols,
            "seed": self.seed,
            "boards_per_day": self.boards_per_day,
            "one_word_ratio": self.one_word_ratio,
            "limit_down_ratio": self.limit_down_ratio,
            "ladder_weights": list(self.ladder_weights),
            "signal_strength": self.signal_strength,
            "factor_gap_ratio": self.factor_gap_ratio,
            "future_factor_rows": self.future_factor_rows,
            "label_horizon": self.label_horizon,
        }


def _business_dates(n: int) -> list[str]:
    stamps = pd.date_range("2025-01-06", periods=n, freq="B")
    return [s.date().isoformat() for s in stamps]


def _pick_ladder(
    rng: np.random.Generator, weights: tuple[float, ...]
) -> tuple[str, int]:
    """Return ``(board_type_zh, limit_times)`` for one sampled board."""

    if len(weights) != 5:
        raise ValueError("ladder_weights must cover 首板/2板/3板/高位板/反包")
    draw = float(rng.random())
    cuts = np.cumsum(np.asarray(weights, dtype=float) / float(sum(weights)))
    if draw <= cuts[0]:
        return "首板", 1
    if draw <= cuts[1]:
        return "二连板", 2
    if draw <= cuts[2]:
        return "三连板", 3
    if draw <= cuts[3]:
        return "高位板", int(rng.integers(4, 7))
    return "反包", int(rng.integers(1, 4))


def make_synthetic_labels(
    cfg: SyntheticTierConfig | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, object]]:
    """Build a ``#23``-shaped label frame plus a ``#24``-shaped factor frame.

    Returns ``(label_frame, factor_frame, truth)``.  ``truth`` keeps the latent
    drivers so a test can assert the model recovers *some* of them (and that a
    noise-only variant recovers none).
    """

    cfg = cfg or SyntheticTierConfig()
    rng = np.random.default_rng(cfg.seed)
    dates = _business_dates(cfg.n_dates)
    symbols = [f"{600000 + i * 7:06d}" for i in range(cfg.n_symbols)]

    # latent per (date, symbol) driver, only ever *revealed* through factors at T
    latent = rng.normal(size=(cfg.n_dates, len(symbols)))
    rows: list[dict[str, object]] = []
    factor_rows: list[dict[str, object]] = []

    for t, date in enumerate(dates):
        chosen = rng.choice(len(symbols), size=cfg.boards_per_day, replace=False)
        for si in chosen:
            symbol = symbols[si]
            board_type, limit_times = _pick_ladder(rng, cfg.ladder_weights)
            driver = float(latent[t, si])
            # 只有部分形态能被 T 日因子解释：留出真实噪声余量
            quality = 0.5 + cfg.signal_strength * 0.5 * driver
            quality = float(np.clip(quality, -1.5, 1.5))
            promoted = rng.random() < (1.0 / (1.0 + math.exp(-1.6 * quality)))

            one_word = rng.random() < cfg.one_word_ratio
            sealed = board_type in {"首板", "二连板", "三连板", "高位板", "反包"}
            exit_down = rng.random() < cfg.limit_down_ratio
            prev_limit_times = max(0, limit_times - 1)
            limit_times_next = limit_times + 1 if promoted else 0

            fwd_ret_3d = (
                math.nan
                if one_word
                else round(0.02 + 0.05 * quality + 0.01 * rng.normal(), 6)
            )
            next_auction_premium = (
                round(0.01 * quality + 0.004 * rng.normal(), 6)
                if promoted
                else round(-0.02 + 0.01 * quality + 0.004 * rng.normal(), 6)
            )
            rows.append(
                {
                    "symbol": symbol,
                    "trade_date": date,
                    "name": f"合成{symbol}",
                    "sample_mode": (
                        "reversal"
                        if board_type == "反包"
                        else ("first_board" if limit_times == 1 else "continuation")
                    ),
                    "board_type": board_type,
                    "limit_shape": "一字" if one_word else "换手",
                    "limit_flag": "U" if sealed else "Z",
                    "limit_times": (
                        None if rng.random() < 0.05 else int(limit_times)
                    ),
                    "open_times": 0 if one_word else int(rng.integers(0, 3)),
                    "prev_limit_times": prev_limit_times,
                    "promotion_stage": (
                        {1: "1进2", 2: "2进3", 3: "3进4"}.get(limit_times, "4进5+")
                    ),
                    "market_regime": str(
                        rng.choice(["冰点", "修复", "发酵", "高潮", "分歧", "退潮"])
                    ),
                    "float_mv": float(rng.uniform(1e9, 3e10)),
                    "float_mv_bucket": "小盘(<50亿)",
                    "price_bucket": "中价(10-30)",
                    "board": "主板",
                    "is_st": 0,
                    "decision_time": f"{date} 15:00:00",
                    "entry_status_A": "unfilled" if one_word else "filled",
                    "entry_unfilled_A": 1 if one_word else 0,
                    "entry_unfilled_reason_A": "one_word_limit_up_unbuyable" if one_word else "",
                    "entry_status_B": "filled",
                    "entry_unfilled_B": 0,
                    "entry_unfilled_reason_B": "",
                    "label_horizon_days": cfg.label_horizon,
                    "embargo_days": cfg.label_horizon + 1,
                    "label_end_date": dates[min(t + cfg.label_horizon, len(dates) - 1)],
                    "label_status": "ok",
                    "label_complete": 1,
                    "fwd_ret_3d": fwd_ret_3d,
                    "fwd_ret_5d": fwd_ret_3d,
                    "next_auction_premium": next_auction_premium,
                    "is_1_to_2": (1 if promoted else 0) if limit_times == 1 else None,
                    "is_2_to_3": (1 if promoted else 0) if limit_times == 2 else None,
                    "is_3_to_4": (1 if promoted else 0) if limit_times == 3 else None,
                    "is_continuation": 1 if promoted else 0,
                    "is_strong_3d": 1 if (promoted and rng.random() < 0.7) else 0,
                    "is_break_streak_next": 0 if promoted else 1,
                    "is_big_loss": 1 if (not promoted and rng.random() < 0.35) else 0,
                    "is_next_limit_down": 1 if exit_down else 0,
                    "future_limit_flag_d3": "D" if exit_down else ("U" if promoted else ""),
                    "future_limit_times_d3": limit_times_next,
                    "next_limit_times": limit_times_next or None,
                    "promotion_level_next": limit_times_next or None,
                    "_latent_driver": quality,
                    "_promoted": int(promoted),
                }
            )

        for si, symbol in enumerate(symbols):
            if rng.random() < cfg.factor_gap_ratio:
                continue  # 该 (symbol, date) 完全没有因子行 -> as-of 回看
            record: dict[str, object] = {
                "symbol": symbol,
                "as_of_date": date,
            }
            driver = float(latent[t, si])
            for name in USABLE_FACTORS:
                record[name] = round(0.6 * driver + 0.4 * rng.normal(), 6)
            for name in NOISE_FACTORS:
                record[name] = round(rng.normal(), 6)
            for name in ALWAYS_MISSING_FACTORS:
                record[name] = None  # 列存在但恒缺 -> NaN + is_missing
            factor_rows.append(record)

    label_frame = pd.DataFrame(rows)
    factor_frame = pd.DataFrame(factor_rows)
    if cfg.future_factor_rows > 0:
        last = dates[-1]
        future = [
            {
                "symbol": symbols[int(rng.integers(0, len(symbols)))],
                "as_of_date": f"2099-12-{i + 1:02d}",
                **{name: float(rng.normal()) for name in FACTOR_COLUMNS},
            }
            for i in range(cfg.future_factor_rows)
        ]
        factor_frame = pd.concat([factor_frame, pd.DataFrame(future)], ignore_index=True)
        del last
    truth = {
        "n_dates": len(dates),
        "n_symbols": len(symbols),
        "first_date": dates[0],
        "last_date": dates[-1],
        "cfg": cfg.as_dict(),
        "positive_rate_all": round(float(label_frame["_promoted"].mean()), 6),
    }
    return label_frame, factor_frame, truth


def drop_synthetic_only_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """Remove the ``_latent_driver`` / ``_promoted`` helpers before modelling."""

    return frame.loc[:, [c for c in frame.columns if not str(c).startswith("_")]]


def label_columns_subset() -> list[str]:
    """The #23 columns the synthetic frame intentionally reproduces."""

    return [
        "symbol",
        "trade_date",
        "board_type",
        "sample_mode",
        "limit_shape",
        "limit_times",
        "promotion_stage",
        "entry_unfilled_A",
        "entry_unfilled_B",
        "fwd_ret_3d",
        "next_auction_premium",
        "is_1_to_2",
        "is_2_to_3",
        "is_3_to_4",
        "is_continuation",
        "is_strong_3d",
        "is_break_streak_next",
        "is_big_loss",
        "is_next_limit_down",
        "future_limit_flag_d3",
    ]


def real_factor_columns() -> list[str]:
    """Sanity helper: the synthetic factor names really exist in #24's output."""

    catalogue = set(microstructure_output_columns())
    missing = [c for c in (*FACTOR_COLUMNS, *ALWAYS_MISSING_FACTORS) if c not in catalogue]
    if missing:
        raise AssertionError(
            f"synthetic factor names drift from microstructure_output_columns(): {missing}"
        )
    return list(FACTOR_COLUMNS)


__all__ = [
    "ALWAYS_MISSING_FACTORS",
    "FACTOR_COLUMNS",
    "NOISE_FACTORS",
    "USABLE_FACTORS",
    "SyntheticTierConfig",
    "drop_synthetic_only_columns",
    "label_columns_subset",
    "make_synthetic_labels",
    "real_factor_columns",
]
