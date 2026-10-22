"""打板可执行标签体系（issue #23）。

本模块只做**纯函数标签计算**：不连数据库、不访问网络、不依赖 container。
数据读取与样本装配在 ``label_engine.py``。

决策时点 / 入场时点 / 出场时点
--------------------------------
* ``decision_time`` = T 日 15:00（Asia/Shanghai，收盘后决策）。
  T 日当天已知的打板事实（``limit_flag`` / ``limit_times`` / ``first_time`` /
  ``open_times`` / ``fd_amount`` 等）只用于**分层与可成交性判定**，
  这些字段在 T 日收盘后即可确定，不是未来函数。
* 场景 A（``*_A``）：T 日以涨停价排队打板。入场价 = T 日涨停价；
  一字板 / T 字板 / 涨停开不了板 **不可成交** → ``entry_unfilled``，
  收益类标签一律置 ``None``，绝不写 0。
* 场景 B（``*_B``）：T+1 集合竞价或开盘接力。入场价 = 竞价价（可用时）
  或 T+1 开盘价代理（``auction_proxy=1``），并按配置计入滑点/手续费。
* 出场时点 = 各 horizon 对应的 T+k 交易日收盘价。

严格 PIT
--------
标签只允许使用 ``T+1 … T+H`` 的数据（``bars[idx+1:]``）；
T 日及以前的数据只用于 T 日事实与入场价。因子侧的 ``shift(1)`` 约定由
``features.py`` 负责，本模块产出的列**不应**进入因子矩阵，
可用 ``LABEL_COLUMNS`` / ``is_label_column()`` 做列白名单校验。

金额单位：元（v3 底座口径）；比例类字段：小数（0.10 == 10%）；缺失值：``None``。
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

# ---------------------------------------------------------------------------
# 常量与配置
# ---------------------------------------------------------------------------

LABEL_SOURCE_V3 = "limit_board_daily_v3"
"""标签来自 Tushare ``limit_list_d`` 落库的 v3 打板主表。"""

LABEL_SOURCE_PROXY = "daily_proxy_v2"
"""v3 打板表当日缺失时，退化为 ``kline_daily`` 日线代理（口径较弱，必须显式区分）。"""

TZ_LABEL = "Asia/Shanghai"
"""所有日期/时间字符串均为 A 股市场本地时区（东八区），无 DST。"""

LIMIT_FLAG_UP = "U"
LIMIT_FLAG_DOWN = "D"
LIMIT_FLAG_BROKEN = "Z"

STATUS_FILLED = "filled"
STATUS_UNFILLED = "unfilled"
STATUS_UNKNOWN = "unknown"

# 不可成交原因代号（#5 可成交性模型直接消费该列）
UNFILLED_ONE_WORD = "one_word"
UNFILLED_T_WORD = "t_word"
UNFILLED_NEVER_OPENED = "never_opened"
UNFILLED_NOT_LIMIT_UP = "not_limit_up"
UNFILLED_LIMIT_DOWN = "limit_down_at_close"
UNFILLED_FLAG_CONFLICT = "flag_price_conflict"
UNFILLED_MISSING_DATA = "missing_data"
UNFILLED_NO_NEXT_BAR = "suspended_or_no_next_bar"
UNFILLED_NEXT_ONE_WORD_LIMIT_UP = "next_day_one_word_limit_up"
UNFILLED_UNUSABLE_PRICE = "non_positive_price"

# 标签状态代号（写入 ``label_status`` / ``*_status`` 列）
LS_OK = "ok"
LS_INSUFFICIENT_HORIZON = "insufficient_horizon"
LS_MISSING_FUTURE_DATA = "missing_future_data"
LS_UNFILLED = "unfilled_entry"
LS_UNKNOWN_ENTRY = "unknown_entry"


@dataclass(slots=True)
class ExecutableLabelConfig:
    """可执行标签口径配置（全部可单测复现）。

    阈值口径与 issue #23 一致；horizon 单位为**交易日**（不是自然日）。
    """

    continuation_horizon: int = 2
    """``is_continuation`` 观察窗口：T+1…T+N 是否再次涨停。"""
    short_horizon: int = 3
    """``ret_3d`` / ``max_drawdown_3d`` / ``max_runup_3d`` 等 3 日窗口。"""
    long_horizon: int = 5
    """``ret_5d`` 长窗口。"""
    promotion_horizon: int = 2
    """晋级标签（1进2/2进3/3进4）观察 T+1…T+N。"""

    strong_threshold: float = 0.12
    """``is_strong_3d``：3 日最高溢价阈值。"""
    break_drawdown_threshold: float = -0.07
    """``is_big_loss`` 的最大不利偏移触发线。"""
    big_loss_close_threshold: float = -0.05
    """``is_big_loss`` 的 T+1 收盘（相对入场价）触发线。"""
    next_close_down_threshold: float = -0.05
    """``is_next_close_down``：T+1 收盘相对 **T 日收盘** 的跌幅线（issue 原文口径）。"""

    slippage_bps: float = 10.0
    fee_bps: float = 8.0
    """场景 B 的滑点/单边手续费（基点，1bp = 0.01%）。场景 A 排队按涨停价成交不计滑点。"""

    price_epsilon: float = 0.0025
    """判定"价格等于涨停价"的相对容差（0.25%）。"""
    late_seal_seconds: int = 14 * 3600 + 30 * 60
    """``last_time`` 不早于此秒数视为尾盘板（默认 14:30:00）。"""

    high_board_min_times: int = 3
    """``limit_times >=`` 该值归类为高位板。"""
    reversal_lookback: int = 5
    """反包/炸板修复样本向前查找炸板或跌停的最大交易日数。"""

    st_name_keywords: tuple[str, ...] = ("ST", "*ST", "SST")
    large_mv_threshold: float = 20_000_000_000.0
    mid_mv_threshold: float = 5_000_000_000.0
    embargo_buffer_days: int = 1
    """在最长 horizon 之外额外隔离的交易日数（防边界标签窗口重叠的保险量）。"""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    # ---- horizon 相关（embargo 依赖） ------------------------------------
    @property
    def max_horizon(self) -> int:
        return max(
            self.continuation_horizon,
            self.short_horizon,
            self.long_horizon,
            self.promotion_horizon,
        )

    def required_embargo_days(self) -> int:
        """标签窗口重叠所需的最小隔离交易日数（含保险量）。"""
        return self.max_horizon + max(0, int(self.embargo_buffer_days))


# ---------------------------------------------------------------------------
# 字段口径文档（写入 label_schema.json，供 #24/#26 与人工核对）
# ---------------------------------------------------------------------------


def label_field_docs() -> dict[str, str]:
    """每个标签字段的公式 / 单位 / 缺失处理 / 数据源。"""
    return {
        # --- 标识与时点 ---
        "symbol": "股票 6 位代码（不带交易所后缀）。来源：limit_board_daily.symbol。",
        "name": "股票简称（透传 v3 name 或调用方 name_map）；仅用于 ST 识别与展示，不作为数值特征。",
        "trade_date": "信号日 T（= 决策日），格式 YYYY-MM-DD，Asia/Shanghai。",
        "decision_time": "T 日 15:00:00（收盘后决策），Asia/Shanghai。",
        "entry_time_A": "场景 A 入场时点：T 日首次封板时间 first_time（缺失则记 T 日 09:30）。",
        "entry_time_B": "场景 B 入场时点：T+1 09:25:00（竞价）或 09:30:00（开盘代理）。",
        "exit_time_short": "3 日窗口出场时点：T+short_horizon 收盘。",
        "exit_time_long": "5 日窗口出场时点：T+long_horizon 收盘。",
        "label_end_date": "本样本最后一个被读取的未来交易日（embargo 计算用）。",
        "label_horizon_days": "标签窗口长度（交易日）= max_horizon。",
        "embargo_days": "该样本至少需要隔离的交易日数 = max_horizon + embargo_buffer_days。",
        "label_source": f"{LABEL_SOURCE_V3} | {LABEL_SOURCE_PROXY}；标签/分层的取数来源。",
        "label_status": f"ok | {LS_INSUFFICIENT_HORIZON} | {LS_MISSING_FUTURE_DATA} | {LS_UNFILLED} | {LS_UNKNOWN_ENTRY}",
        "label_complete": "1=所有 horizon 标签齐备；0=窗口不足或部分缺失（收益列为 None）。",
        "market_regime": "冰点/修复/发酵/高潮/分歧/退潮，来自 issue #4；当前未接入 → None。",
        "market_regime_available": "0/1，标记 #4 regime 是否已接入（本期恒为 0）。",
        # --- 分层 ---
        "sample_mode": "样本所属模式：first_board | all_limit_up | continuation | reversal（由 label_engine 选定）。",
        "board_type": "T 日在连板梯队中的位置：首板 | 二连板 | 三连板 | 高位板 | 连板 | 反包 | 炸板 | 炸板修复 | 地天板 | 跌停 | 其他。"
        "（下注台阶 1进2/2进3/3进4 用 promotion_stage 表达，与本列区分，见 classify_board_type 文档）",
        "limit_shape": "一字 | T字 | 换手 | 尾盘 | 炸板未回封 | 未知；由 OHLC+limit_flag+open_times+last_time 判定。",
        "board": "主板 | 创业板 | 科创板 | 北交所（按代码前缀）。",
        "limit_times": "T 日连板数（Tushare limit_times，缺失为 None）。",
        "open_times": "T 日开板（炸板）次数；None=缺失，0=一次未开。",
        "limit_flag": "Tushare limit 落库列（U=收盘封板 / D=跌停 / Z=炸板未回封）；空或异常值归一为 None。",
        "up_stat": "涨停统计原样透传（Tushare up_stat，形如 3天/2板），缺失为 None。",
        "limit_pct": "该股涨停幅度（小数：主板 0.10 / 创业板科创板 0.20 / 北交所 0.30 / ST 0.05）。",
        "limit_price": "T 日涨停价 = round(prev_close*(1+limit_pct), 2)。",
        "prev_close": "T-1 收盘价（元）；优先取日线，缺失时用 close/(1+pct_chg) 反推并置 prev_close_derived=1。",
        "prev_close_derived": "1=prev_close 由 close/pct_chg 反推（弱口径），0=直接取日线。",
        "pct_chg_frac": "T 日涨跌幅（统一为小数）。对 %/小数两种单位自适应，并见 pct_chg_convention。",
        "pct_chg_convention": "percent | fraction | unknown：v3 pct_chg 原始单位的判定结果。",
        "float_mv_bucket": "小盘(<50亿) | 中盘(50-200亿) | 大盘(>200亿) | 未知（float_mv 单位：元）。",
        "price_bucket": "低价(<10) | 中价(10-30) | 高价(30-100) | 超高价(>=100) | 未知。",
        "is_st": "1=名称含 ST/*ST（涨跌幅 5%）。",
        # --- 场景 A：T 日涨停价排队 ---
        "entry_status_A": f"{STATUS_FILLED} | {STATUS_UNFILLED} | {STATUS_UNKNOWN}。一字/T字/涨停开不了板 → unfilled。",
        "entry_unfilled_A": "1=场景 A 不可成交（此时 A 收益类标签全为 None，禁止当 0 用）。",
        "entry_unfilled_reason_A": "不可成交原因代号，见 UNFILLED_* 常量。",
        "entry_price_A": "场景 A 入场价（元）= T 日涨停价；unfilled/unknown → None。",
        "fill_probability": "T 日涨停排队成交概率（0-1 启发式估计，正式模型见 #5）；输入缺失 → None。",
        "seal_ratio": "封单/板内成交额 = fd_amount / COALESCE(limit_amount, amount)（元/元）。",
        "ret_1d_A": "T+1 收盘 / entry_price_A - 1（含手续费，扣滑点=0）。unfilled → None。",
        "ret_3d_A": "T+short_horizon（默认 3）收盘净收益 = T+3 收盘/entry_price_A - 1 - 2*fee_bps/1e4。unfilled → None。",
        "ret_5d_A": "T+long_horizon（默认 5）收盘净收益，口径同 ret_3d_A。窗口不足 → None。",
        "mfe_3d_A": "3 日内最高价相对 entry_price_A 的最大有利偏移（MFE，未扣成本）。",
        "mao_3d_A": "3 日内最低价相对 entry_price_A 的最大不利偏移（MAO，<=0，未扣成本）。",
        # --- 场景 B：T+1 竞价/开盘接力 ---
        "entry_status_B": f"{STATUS_FILLED} | {STATUS_UNFILLED} | {STATUS_UNKNOWN}。",
        "entry_unfilled_B": "1=场景 B 不可成交（T+1 停牌/无数据/一字涨停买不到）。",
        "entry_unfilled_reason_B": "不可成交原因代号，见 UNFILLED_* 常量。",
        "auction_price": "T+1 集合竞价成交价（元）；不可得 → None。",
        "auction_proxy": "1=竞价价缺失、用 T+1 开盘价代理；0=使用真实竞价价。",
        "entry_price_B": "场景 B 入场价（元）= raw*(1+slippage_bps/1e4)。",
        "ret_1d_B": "T+1 收盘净收益（扣双边手续费与滑点）。",
        "ret_3d_B": "T+short_horizon（默认 3）收盘净收益 = T+3 收盘/entry_price_B - 1 - 2*fee_bps/1e4。",
        "ret_5d_B": "T+long_horizon（默认 5）收盘净收益，口径同 ret_3d_B。",
        "mfe_3d_B": "3 日最大有利偏移（相对 entry_price_B，未扣成本）。",
        "mao_3d_B": "3 日最大不利偏移（相对 entry_price_B，<=0，未扣成本）。",
        # --- 溢价类（相对 T 日收盘，无成本） ---
        "next_auction_premium": "T+1 竞价价 / T 日收盘 - 1；竞价缺失 → None（见 next_open_premium 代理）。",
        "next_open_premium": "T+1 开盘价 / T 日收盘 - 1。",
        "next_close_return": "T+1 收盘价 / T 日收盘 - 1（次日隔日溢价）。",
        "next_high_premium": "T+1 最高价 / T 日收盘 - 1。",
        "next_limit_up_premium": "T+1 涨停价 / T 日收盘 - 1（次日理论最大溢价）。",
        # --- 晋级 / 风险 0-1 标签 ---
        "is_1_to_2": "仅对 limit_times=1 的样本赋值：晋级窗口内出现 2 连板→1，窗口齐备但未封板→0；"
        "窗口缺失（停牌/尾部不足）或非该台阶样本→None。",
        "is_2_to_3": "仅对 limit_times=2 的样本赋值：窗口内出现 3 连板→1，否则 0；不可判定→None。",
        "is_3_to_4": "仅对 limit_times=3 的样本赋值：窗口内出现 4 连板→1，否则 0；不可判定→None。",
        "promotion_level_next": "晋级到的连板高度（None=未晋级或无数据）。",
        "promotion_source": "limit_board_daily_v3 | kline_daily_seal_proxy | no_promotion_observed | none：晋级判定取数来源。",
        "is_continuation": f"T+1…T+{ExecutableLabelConfig.continuation_horizon} 是否再次涨停。",
        "is_broken_board": "炸板：T 日盘中触及涨停但收盘未封（limit_flag=Z）或 open_times>0。",
        "is_break_streak_next": "断板：T 日封板且窗口内第一个交易日未能封板。",
        "is_next_limit_down": "T+1 收盘跌停（limit_flag=D 或价格<=跌停价）。",
        "is_next_close_down": "T+1 收盘相对 T 日收盘 <= next_close_down_threshold（默认 -5%）。",
        "is_big_loss": "大面：场景基准入场价下，T+1 收盘 <= big_loss_close_threshold 或 3 日最大不利偏移 <= break_drawdown_threshold。",
        "is_limit_up_at_close": "T 日收盘封板（limit_flag=U 且 close≈涨停价）。",
        "flag_price_conflict": "1=limit_flag 与收盘价/涨停价矛盾（数据质量问题，需人工/告警复核）。",
        # --- 多 horizon 收益（默认按场景 A 基准，unfilled 时改用 T 收盘基准并标 status）---
        "fwd_ret_3d": "3 日**前瞻**收盘收益（基准价见 ret_benchmark_source）。"
        "issue #23 原文称 ret_3d；这里刻意改名 fwd_ret_3d，因为 features.py 已把 ret_3d/ret_5d "
        "用作**回溯**收益率（close/close.shift(3)-1），同名 join 会互相覆盖造成静默泄漏。",
        "fwd_ret_5d": "5 日**前瞻**收盘收益（同 fwd_ret_3d，原名 ret_5d）。",
        "ret_benchmark_price": "fwd_ret_3d/fwd_ret_5d/drawdown/runup 使用的基准价（元）。",
        "ret_benchmark_source": "entry_price_A | close_T：基准价来源说明。",
        "max_drawdown_3d": "T+1…T+3 收盘序列的峰谷最大回撤（<=0）。",
        "max_runup_3d": "T+1…T+3 最高价相对基准价的最大涨幅。",
        "max_adverse_offset_3d": "T+1…T+3 最低价相对基准价的最大不利偏移（<=0）。",
        # --- 未来原始价格（仅供标签复现与调试，禁止进因子矩阵）---
        "d1_open … d5_close": "T+1…T+5 交易日 OHLC（元）。仅用于标签复现与调试，禁止进因子矩阵。",
        "future_limit_flag_d1…d5": "T+1…T+5 的 limit_flag（U/D/Z，空=非涨跌停或当日无记录）。",
        "future_limit_times_d1…d5": "T+1…T+5 的连板数。",
        "open": "T 日开盘价（元，kline_daily 口径，不复权）。",
        "high": "T 日最高价（元）。",
        "low": "T 日最低价（元）。",
        "close": "T 日收盘价（元）。标签基准价与涨停价校验均用它。",
        "first_time": "T 日首次封板时间（HH:MM:SS，Asia/Shanghai）；由 v3 first_time 归一，缺失为 None。",
        "last_time": "T 日最后封板时间（HH:MM:SS）；用于尾盘板判定（>= late_seal_seconds）。",
        "limit_down_price": "T 日跌停价 = round(prev_close*(1-limit_pct), 2)（元）。",
        "fd_amount": "封单金额（元，v3 原值透传）；缺失为 None，参与 seal_ratio / fill_probability。",
        "limit_amount": "板内（涨停时段）成交额（元）；缺失为 None。",
        "amount": "当日成交额（元）；优先取 v3 amount，缺失时回退 kline_daily.amount。",
        "turnover_ratio": "换手率（v3 原值透传，单位按 Tushare 口径）；缺失为 None。",
        "float_mv": "流通市值（元）；缺失为 None。",
        "total_mv": "总市值（元）；缺失为 None。",
        "theme": "题材（v3 原值透传，由 #22 主题回填）。",
        "industry": "所属行业（v3 原值透传）。",
        "limit_reason": "涨停原因（v3 原值透传）。",
        "is_strong_3d": "3 日强势：mfe_3d_A >= strong_threshold（不可得时退用 max_runup_3d）。",
        "volume": "T 日成交量（手/股沿用 kline_daily 口径，标签计算不使用它）。",
        "next_limit_times": "T+1 的连板数（v3 口径，None=当日无打板记录）。",
        "board_type_raw": "透传 v3 limit_board_daily.board_type（Tushare 板块/类型原值），与本项目计算的 board_type 分层不同名以免混淆。",
        "limit_flag_inferred": "1=limit_flag 由日线价格推断（v3 该行缺失或为空），0=直接取 v3。",
        "promotion_stage": "该样本下注的晋级台阶：1进2 / 2进3 / 3进4 / 4进5+ / 空（limit_times 不可用）。",
        "entry_confidence_A": "场景 A 成交判定依据：v3_open_times（有开板次数）| ohlc_inferred（日线推断）| unknown。",
    }


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _num(value: Any) -> float | None:
    """把任意输入安全转 float；NaN/inf/空串/None → None（绝不落到 0）。"""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        value = text
    if isinstance(value, bool):
        return float(value)
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def _r(value: float | None, digits: int = 6) -> float | None:
    return None if value is None else round(float(value), digits)


def _norm_flag(value: Any) -> str | None:
    """归一化 ``limit_flag``；非 U/D/Z（含空串、异常值）一律视为 ``None``。"""
    if value in (None, ""):
        return None
    text = str(value).strip().upper()
    return text if text in {LIMIT_FLAG_UP, LIMIT_FLAG_DOWN, LIMIT_FLAG_BROKEN} else None


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator - 1.0


def _first_not_none(*values: float | None) -> float | None:
    for value in values:
        if value is not None:
            return value
    return None


def norm_trade_date(value: Any) -> str | None:
    """归一化交易日为 ``YYYY-MM-DD``（同时兼容 ``YYYYMMDD``）。"""
    if value is None:
        return None
    if isinstance(value, (datetime,)):
        return value.strftime("%Y-%m-%d")
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text[: len(text)], fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    digits = text.replace("-", "")[:8]
    if len(digits) == 8 and digits.isdigit():
        return f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}"
    return None


def parse_seal_seconds(value: Any) -> int | None:
    """解析封板时间为"当日 0 点起秒数"。

    同时兼容 Tushare 的 ``HH:MM:SS`` 与 ``HHMMSS`` 两种写法；空值 → None。
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.upper() in {"NONE", "NAN", "-"}:
        return None
    if ":" in text:
        parts = text.split(":")
    else:
        digits = "".join(ch for ch in text if ch.isdigit())
        if len(digits) < 4:
            return None
        digits = digits.zfill(6)
        parts = [digits[0:2], digits[2:4], digits[4:6]]
    try:
        hh = int(parts[0])
        mm = int(parts[1])
        ss = int(parts[2]) if len(parts) > 2 else 0
    except (TypeError, ValueError):
        return None
    if not (0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 59):
        return None
    return hh * 3600 + mm * 60 + ss


def normalize_time_str(value: Any, default: str | None = None) -> str | None:
    """把封板时间归一为 ``HH:MM:SS``；无法解析时返回 ``default``。"""
    seconds = parse_seal_seconds(value)
    if seconds is None:
        return default
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def market_board(symbol: Any) -> str:
    """按代码前缀返回板块：主板 / 创业板 / 科创板 / 北交所 / 其他。"""
    code = str(symbol or "").split(".")[0]
    if not code:
        return "其他"
    if code.startswith(("688", "689")) or code.startswith("68"):
        return "科创板"
    if code.startswith("30"):
        return "创业板"
    if code.startswith(("43", "83", "87", "88", "92")):
        return "北交所"
    if code.startswith(("60", "00", "90", "20")):
        return "主板"
    return "其他"


def is_st_name(name: Any) -> bool:
    text = str(name or "").upper()
    return "ST" in text


def board_limit_pct(symbol: Any, name: Any = "", *, st_pct: float = 0.05, bse_pct: float = 0.30) -> float:
    """涨停幅度（小数）。北交所 30%、创业板/科创板 20%、主板 10%、ST 5%。"""
    if is_st_name(name):
        return st_pct
    board = market_board(symbol)
    if board == "北交所":
        return bse_pct
    if board in {"创业板", "科创板"}:
        return 0.20
    return 0.10


def round_price(value: float | None) -> float | None:
    return None if value is None else round(float(value) + 1e-9, 2)


def limit_up_price(prev_close: float | None, limit_pct: float) -> float | None:
    return None if prev_close is None else round_price(prev_close * (1.0 + limit_pct))


def limit_down_price(prev_close: float | None, limit_pct: float) -> float | None:
    return None if prev_close is None else round_price(prev_close * (1.0 - limit_pct))


def normalize_pct_chg(value: Any) -> tuple[float | None, str]:
    """v3 ``pct_chg`` 单位自适应 → (小数涨跌幅, 单位代号)。

    Tushare 惯例是百分数（9.98 表示 +9.98%），但落库映射层尚未定稿，
    因此对两种约定都做兜底：``|v| > 1.5`` 判为百分数，否则判为小数。
    """
    raw = _num(value)
    if raw is None:
        return None, "unknown"
    if abs(raw) > 1.5:
        return raw / 100.0, "percent"
    return raw, "fraction"


def classify_float_mv_bucket(value: float | None, cfg: ExecutableLabelConfig) -> str:
    if value is None or value <= 0:
        return "未知"
    if value > cfg.large_mv_threshold:
        return "大盘"
    if value > cfg.mid_mv_threshold:
        return "中盘"
    return "小盘"


def classify_price_bucket(value: float | None) -> str:
    if value is None or value <= 0:
        return "未知"
    if value < 10.0:
        return "低价"
    if value < 30.0:
        return "中价"
    if value < 100.0:
        return "高价"
    return "超高价"


# ---------------------------------------------------------------------------
# 分层：板型 / 形态 / 可成交性
# ---------------------------------------------------------------------------


def classify_limit_shape(
    *,
    limit_flag: str | None,
    sealed_at_close: bool,
    open_times: int | None,
    open_p: float | None,
    high: float | None,
    low: float | None,
    close: float | None,
    limit_price: float | None,
    last_time_seconds: int | None,
    cfg: ExecutableLabelConfig,
) -> str:
    """一字 / T字 / 换手 / 尾盘 / 炸板未回封 / 未知。"""
    eps = cfg.price_epsilon
    if None in (open_p, high, low, close) or limit_price is None or limit_price <= 0:
        return "未知"
    at_limit = lambda p: p is not None and p >= limit_price * (1.0 - eps)  # noqa: E731
    if not at_limit(close):
        return "炸板未回封" if (limit_flag == LIMIT_FLAG_BROKEN or at_limit(high)) else "未知"
    if sealed_at_close or limit_flag == LIMIT_FLAG_UP:
        if at_limit(open_p) and abs((high or 0) - (low or 0)) <= max(limit_price * eps, 1e-9):
            return "一字"
        if at_limit(open_p) and at_limit(high):
            return "T字"
        if last_time_seconds is not None and last_time_seconds >= cfg.late_seal_seconds:
            return "尾盘"
        return "换手"
    return "换手"


def classify_board_type(
    *,
    limit_flag: str | None,
    limit_times: int | None,
    sealed_at_close: bool,
    prev_flag: str | None,
    prev_limit_times: int | None,
    touched_limit_down: bool = False,
    cfg: ExecutableLabelConfig,
) -> str:
    """T 日在连板梯队中的位置：首板 / 二连板 / 三连板 / 高位板 / 反包 / 炸板 / 炸板修复 / 地天板 / 跌停 / 其他。

    以 ``limit_flag`` + ``limit_times`` 为主口径：
    * ``limit_times`` 是 T 日**已达成**的连板高度，因此 ``board_type`` 描述
      "T 日是几板"，而 "N进N+1" 这类下注台阶由 :func:`classify_promotion_stage`
      与 ``is_1_to_2`` / ``is_2_to_3`` / ``is_3_to_4`` 标签表达。这样拆是为避免
      issue 里"首板"与"1进2"两个分层名互相覆盖（首板样本赌的就是 1进2）。
    * 前一日炸板/跌停/断板 → ``反包``；盘中触及跌停但收盘封板 → ``地天板``。
    """
    if limit_flag == LIMIT_FLAG_DOWN:
        return "跌停"
    if touched_limit_down and (sealed_at_close or limit_flag == LIMIT_FLAG_UP):
        return "地天板"
    reversal = _is_reversal_context(prev_flag=prev_flag, prev_limit_times=prev_limit_times)
    if limit_flag == LIMIT_FLAG_BROKEN:
        return "炸板修复" if reversal else "炸板"
    times = limit_times if isinstance(limit_times, int) else None
    if sealed_at_close or limit_flag == LIMIT_FLAG_UP:
        if reversal:
            return "反包"
        if times is None:
            return "连板"
        if times >= cfg.high_board_min_times:
            return "高位板" if times > 3 else "三连板"
        if times == 1:
            return "首板"
        if times == 2:
            return "二连板"
        return f"{times}连板"
    return "其他"


def classify_promotion_stage(limit_times: int | None) -> str:
    """该样本下注的晋级台阶：1进2 / 2进3 / 3进4 / 4进5+ / 空串（无法判定）。"""
    if not isinstance(limit_times, int) or limit_times < 1:
        return ""
    if limit_times == 1:
        return "1进2"
    if limit_times == 2:
        return "2进3"
    if limit_times == 3:
        return "3进4"
    return "4进5+"


def _is_reversal_context(*, prev_flag: str | None, prev_limit_times: int | None) -> bool:
    """前一日炸板或跌停 → 反包语境。"""
    if prev_flag == LIMIT_FLAG_BROKEN or prev_flag == LIMIT_FLAG_DOWN:
        return True
    return False


def estimate_fill_probability(
    *,
    limit_shape: str,
    limit_flag: str | None,
    open_times: int | None,
    seal_ratio: float | None,
    first_time_seconds: int | None,
    cfg: ExecutableLabelConfig,
) -> float | None:
    """T 日涨停排队成交概率的启发式估计（正式模型见 issue #5）。

    口径：
    * 形态基线：炸板最易成交 0.95 > 尾盘/换手 0.75/0.70 > T字 0.10 > 一字 0.02 > 从未开板 0.15；
    * 封单越厚越难成交：``× 1/(1+max(seal_ratio,0))``；
    * 首封越晚越易成交：线性 0.85→1.10（09:30→15:00）。
    输入不足时返回 ``None``（不猜 0）。
    """
    if limit_flag == LIMIT_FLAG_DOWN:
        return 0.0
    if limit_shape in {"未知", None} and limit_flag not in {LIMIT_FLAG_UP, LIMIT_FLAG_BROKEN}:
        return None
    base_map = {
        "一字": 0.02,
        "T字": 0.10,
        "换手": 0.70,
        "尾盘": 0.75,
        "炸板未回封": 0.95,
    }
    base = base_map.get(limit_shape)
    if base is None:
        return None
    if limit_flag == LIMIT_FLAG_UP and open_times == 0 and limit_shape in {"一字", "T字"}:
        base = min(base, 0.15)
    prob = base
    if seal_ratio is not None:
        prob *= 1.0 / (1.0 + max(seal_ratio, 0.0))
    if first_time_seconds is not None:
        day_start, day_end = 9 * 3600 + 30 * 60, 15 * 3600
        span = max(day_end - day_start, 1)
        progress = min(max((first_time_seconds - day_start) / span, 0.0), 1.0)
        prob *= 0.85 + 0.25 * progress
    return max(0.0, min(1.0, prob))


# ---------------------------------------------------------------------------
# 场景可成交性
# ---------------------------------------------------------------------------


def decide_entry_A(
    *,
    limit_flag: str | None,
    limit_shape: str,
    sealed_at_close: bool,
    open_times: int | None,
    flag_price_conflict: bool,
    limit_price: float | None,
    cfg: ExecutableLabelConfig,
) -> tuple[str, str | None]:
    """场景 A（T 日涨停价排队）是否可成交 → ``(status, reason)``。"""
    if flag_price_conflict:
        return STATUS_UNKNOWN, UNFILLED_FLAG_CONFLICT
    if limit_price is None or limit_price <= 0:
        return STATUS_UNKNOWN, UNFILLED_MISSING_DATA
    if limit_flag == LIMIT_FLAG_DOWN:
        return STATUS_UNFILLED, UNFILLED_LIMIT_DOWN
    if limit_flag == LIMIT_FLAG_BROKEN:
        # 盘中曾封板、排队的买单在开板时会成交 —— 这正是"炸板大面"的来源。
        return STATUS_FILLED, None
    if limit_flag != LIMIT_FLAG_UP and not sealed_at_close:
        return STATUS_UNFILLED, UNFILLED_NOT_LIMIT_UP
    if limit_shape == "一字":
        return STATUS_UNFILLED, UNFILLED_ONE_WORD
    if limit_shape == "T字":
        return STATUS_UNFILLED, UNFILLED_T_WORD
    if open_times is None:
        return STATUS_UNKNOWN, UNFILLED_MISSING_DATA
    if open_times == 0:
        return STATUS_UNFILLED, UNFILLED_NEVER_OPENED
    return STATUS_FILLED, None


def decide_entry_B(
    *,
    next_bar: Mapping[str, Any] | None,
    next_limit_price: float | None,
    cfg: ExecutableLabelConfig,
) -> tuple[str, str | None, float | None, int]:
    """场景 B（T+1 集合竞价/开盘接力）→ ``(status, reason, raw_entry_price, auction_proxy)``。

    * ``next_bar`` 为 ``None`` → T+1 停牌/退市/数据缺失：不可成交，收益标签置 ``None``。
    * 入场价优先取 ``auction_price``（真实竞价成交价），缺失则用 T+1 开盘价并置
      ``auction_proxy=1``。
    * T+1 一字/T字涨停（整天没跌下涨停价）→ 挂涨停也买不到 → 不可成交。
    """
    if next_bar is None:
        return STATUS_UNFILLED, UNFILLED_NO_NEXT_BAR, None, 0
    open_p = _num(next_bar.get("open"))
    low = _num(next_bar.get("low"))
    auction = _num(next_bar.get("auction_price"))
    proxy = 0
    raw = auction
    if raw is None:
        raw = open_p
        proxy = 1
    if raw is None or raw <= 0:
        return STATUS_UNKNOWN, UNFILLED_UNUSABLE_PRICE, None, proxy
    eps = cfg.price_epsilon
    if next_limit_price and next_limit_price > 0:
        entry_at_limit = raw >= next_limit_price * (1.0 - eps)
        never_opened_below = low is not None and low >= next_limit_price * (1.0 - eps)
        # 次日一字/T字（整天没跌下涨停价）→ 集合竞价/开盘挂涨停也买不到。
        if entry_at_limit and never_opened_below:
            return STATUS_UNFILLED, UNFILLED_NEXT_ONE_WORD_LIMIT_UP, raw, proxy
    return STATUS_FILLED, None, raw, proxy


# ---------------------------------------------------------------------------
# 核心：单样本标签
# ---------------------------------------------------------------------------


def _future_flags_and_times(
    dates: Sequence[str],
    future_limit_rows: Mapping[str, Mapping[str, Any]] | None,
) -> list[tuple[str | None, int | None]]:
    rows = future_limit_rows or {}
    out: list[tuple[str | None, int | None]] = []
    for d in dates:
        row = rows.get(d) or {}
        times = _num(row.get("limit_times"))
        out.append((_norm_flag(row.get("limit_flag")), None if times is None else int(times)))
    return out


def build_sample_label(
    *,
    signal: Mapping[str, Any],
    bars: Sequence[Mapping[str, Any]],
    idx: int,
    cfg: ExecutableLabelConfig | None = None,
    future_limit_rows: Mapping[str, Mapping[str, Any]] | None = None,
    prev_signal: Mapping[str, Any] | None = None,
    sample_mode: str | None = None,
    label_source_hint: str | None = None,
) -> dict[str, Any] | None:
    """计算单个 (symbol, T) 样本的可执行标签。

    参数
    ----
    * ``signal``：T 日 v3 打板行（字段名同 ``limit_board_daily``）；传 ``{}`` 表示
      当日无 v3 记录 → 走日线代理口径（``label_source=daily_proxy_v2``）。
    * ``bars``：该 symbol **升序**日线序列（字段 ``trade_date/open/high/low/close/volume/amount``）；
      ``bars[idx]`` 即 T 日。若某根 bar 带 ``auction_price`` 字段，则用于场景 B 竞价价。
    * ``idx``：T 日在 ``bars`` 中的下标。
    * ``future_limit_rows``：``{trade_date: v3 行}``，只允许包含 **T 日之后** 的日期；
      早于等于 T 日的日期会被忽略（防未来函数）。
    * ``prev_signal``：T-1 日 v3 行（可为 None），用于反包/地天板语境判定。
    * ``label_source_hint``：调用方已知取数来源时显式传入，优先级高于推断。

    返回 ``None`` 只发生在 T 日数据本身不可用（无 bar / 非正价）或 T 日与涨停无关时；
    未来窗口不足不会丢样本，而是把对应标签置 ``None`` 并写 ``label_status``。
    """
    cfg = cfg or ExecutableLabelConfig()
    if idx < 0 or idx >= len(bars):
        return None
    bar = bars[idx]
    t_date = norm_trade_date(bar.get("trade_date"))
    close_t = _num(bar.get("close"))
    open_t = _num(bar.get("open"))
    high_t = _num(bar.get("high"))
    low_t = _num(bar.get("low"))
    if t_date is None or close_t is None or close_t <= 0:
        return None

    symbol = str(signal.get("symbol") or bar.get("symbol") or "")
    name = signal.get("name") or bar.get("name") or ""
    limit_pct = board_limit_pct(symbol, name)

    # ---- prev_close：优先日线 T-1，缺失则用 close/(1+pct_chg) 反推 ----
    prev_close: float | None = None
    prev_close_derived = 0
    if idx >= 1:
        prev_close = _num(bars[idx - 1].get("close"))
    # v3 pct_chg 的单位口径始终判定一次（即使 prev_close 来自日线、不需要反推）
    raw_pct_frac, pct_conv = normalize_pct_chg(signal.get("pct_chg"))
    if raw_pct_frac is None:
        pct_conv = "not_available"
    if prev_close is None or prev_close <= 0:
        if raw_pct_frac is not None and raw_pct_frac > -1.0:
            prev_close = close_t / (1.0 + raw_pct_frac)
            prev_close_derived = 1
    pct_frac = close_t / prev_close - 1.0 if prev_close and prev_close > 0 else raw_pct_frac

    limit_price = limit_up_price(prev_close, limit_pct)
    down_price = limit_down_price(prev_close, limit_pct)
    eps = cfg.price_epsilon
    sealed_at_close = limit_price is not None and close_t >= limit_price * (1.0 - eps)

    limit_flag = _norm_flag(signal.get("limit_flag"))
    limit_flag_inferred = 0
    if limit_flag is None:
        # 拿不到 v3 ``limit_flag``：用日线价格推断（收盘封板=U，仅盘中摸板=Z）。
        touched_limit = limit_price is not None and high_t is not None and high_t >= limit_price * (1.0 - eps)
        if not sealed_at_close and not touched_limit:
            # 既没有涨停记录、也没摸到涨停价 → 不是打板样本。
            return None
        limit_flag = LIMIT_FLAG_UP if sealed_at_close else LIMIT_FLAG_BROKEN
        limit_flag_inferred = 1
    label_source = label_source_hint or (
        LABEL_SOURCE_PROXY if limit_flag_inferred else LABEL_SOURCE_V3
    )

    open_times_raw = _num(signal.get("open_times"))
    open_times = None if open_times_raw is None else int(open_times_raw)
    limit_times_raw = _num(signal.get("limit_times"))
    limit_times = None if limit_times_raw is None else int(limit_times_raw)
    first_seconds = parse_seal_seconds(signal.get("first_time"))
    last_seconds = parse_seal_seconds(signal.get("last_time"))

    flag_price_conflict = bool(
        limit_flag == LIMIT_FLAG_UP and limit_price is not None and not sealed_at_close
    ) or bool(
        limit_flag in {LIMIT_FLAG_UP, LIMIT_FLAG_BROKEN} and down_price is not None and close_t <= down_price * (1.0 + eps)
    ) or bool(
        # T 字形态（开盘即封、盘中跌下过涨停价）却记 open_times=0"从未开板"——自相矛盾的脏数据。
        # 注意：开盘在涨停价之下、之后拉板封死的票 open_times=0 是合理的，不算矛盾。
        limit_flag == LIMIT_FLAG_UP and open_times == 0 and low_t is not None and open_t is not None
        and limit_price is not None
        and open_t >= limit_price * (1.0 - eps)
        and low_t < limit_price * (1.0 - eps)
    )

    limit_shape = classify_limit_shape(
        limit_flag=limit_flag,
        sealed_at_close=sealed_at_close,
        open_times=open_times,
        open_p=open_t,
        high=high_t,
        low=low_t,
        close=close_t,
        limit_price=limit_price,
        last_time_seconds=last_seconds,
        cfg=cfg,
    )

    prev_flag: str | None = None
    prev_times: int | None = None
    if prev_signal:
        prev_flag = _norm_flag(prev_signal.get("limit_flag"))
        ptv = _num(prev_signal.get("limit_times"))
        prev_times = None if ptv is None else int(ptv)
    touched_limit_down = bool(down_price is not None and low_t is not None and low_t <= down_price * (1.0 + eps))
    board_type = classify_board_type(
        limit_flag=limit_flag,
        limit_times=limit_times,
        sealed_at_close=sealed_at_close,
        prev_flag=prev_flag,
        prev_limit_times=prev_times,
        touched_limit_down=touched_limit_down,
        cfg=cfg,
    )
    promotion_stage = classify_promotion_stage(limit_times)

    seal_ratio = _ratio(
        _num(signal.get("fd_amount")),
        _num(signal.get("limit_amount")) or _num(signal.get("amount")) or _num(bar.get("amount")),
    )
    fill_probability = estimate_fill_probability(
        limit_shape=limit_shape,
        limit_flag=limit_flag,
        open_times=open_times,
        seal_ratio=seal_ratio,
        first_time_seconds=first_seconds,
        cfg=cfg,
    )

    # ---------------- 场景 A ----------------
    status_a, reason_a = decide_entry_A(
        limit_flag=limit_flag,
        limit_shape=limit_shape,
        sealed_at_close=sealed_at_close,
        open_times=open_times,
        flag_price_conflict=flag_price_conflict,
        limit_price=limit_price,
        cfg=cfg,
    )
    if status_a == STATUS_UNKNOWN and reason_a == UNFILLED_MISSING_DATA and open_times is None and limit_price:
        # 拿不到 ``open_times``（日线代理或字段缺失）时，只能用日线 OHLC 做保守推断：
        # 一字板整天没跌下涨停价 → 确定买不到；收盘封板且盘中确实跌下过涨停价 →
        # 说明板开过、排队可成交；其余情况保持 unknown，绝不臆造成交。
        if limit_shape == "一字":
            status_a, reason_a = STATUS_UNFILLED, UNFILLED_ONE_WORD
        elif sealed_at_close and low_t is not None and low_t < limit_price * (1.0 - eps):
            status_a, reason_a = STATUS_FILLED, None
    if open_times is not None:
        entry_confidence = "v3_open_times"
    elif status_a in {STATUS_FILLED, STATUS_UNFILLED}:
        entry_confidence = "ohlc_inferred"
    else:
        entry_confidence = "unknown"
    entry_price_a = limit_price if status_a == STATUS_FILLED else None

    # ---------------- 未来窗口（严格 PIT：只允许 T 日之后的交易日） ----------------
    horizon = cfg.max_horizon
    raw_future = list(bars[idx + 1 : idx + 1 + horizon])
    future = [b for b in raw_future if (norm_trade_date(b.get("trade_date")) or "") > t_date]
    future_dates = [norm_trade_date(b.get("trade_date")) for b in future]
    allowed_future = [str(d) for d in future_dates]
    flags_times = _future_flags_and_times(allowed_future, future_limit_rows)
    ft_map = dict(zip(allowed_future, flags_times))

    next_bar = future[0] if future else None
    next_date = future_dates[0] if future_dates else None
    next_flag, next_times = ft_map.get(next_date, (None, None)) if next_date else (None, None)
    next_limit_price = limit_up_price(close_t, limit_pct)

    status_b, reason_b, raw_b, auction_proxy = decide_entry_B(
        next_bar=next_bar,
        next_limit_price=next_limit_price,
        cfg=cfg,
    )
    entry_price_b = None
    if status_b == STATUS_FILLED and raw_b is not None:
        entry_price_b = raw_b * (1.0 + cfg.slippage_bps / 10_000.0)

    def close_at(offset: int) -> float | None:
        if offset < 1 or offset > len(future):
            return None
        return _num(future[offset - 1].get("close"))

    def net_return(exit_price: float | None, entry_price: float | None) -> float | None:
        if exit_price is None or entry_price is None or entry_price <= 0:
            return None
        gross = exit_price / entry_price - 1.0
        return gross - (cfg.fee_bps * 2.0) / 10_000.0

    ret_1d_a = net_return(close_at(1), entry_price_a)
    ret_short_a = net_return(close_at(cfg.short_horizon), entry_price_a)
    ret_long_a = net_return(close_at(cfg.long_horizon), entry_price_a)
    mfe_short_a = mao_short_a = None
    if entry_price_a and entry_price_a > 0:
        highs = [_num(b.get("high")) for b in future[: cfg.short_horizon]]
        lows = [_num(b.get("low")) for b in future[: cfg.short_horizon]]
        highs = [h for h in highs if h is not None]
        lows = [v for v in lows if v is not None]
        if highs:
            mfe_short_a = max(highs) / entry_price_a - 1.0
        if lows:
            mao_short_a = min(lows) / entry_price_a - 1.0

    ret_1d_b = net_return(close_at(1), entry_price_b)
    ret_short_b = net_return(close_at(cfg.short_horizon), entry_price_b)
    ret_long_b = net_return(close_at(cfg.long_horizon), entry_price_b)
    mfe_short_b = mao_short_b = None
    if entry_price_b and entry_price_b > 0:
        highs = [v for v in (_num(b.get("high")) for b in future[: cfg.short_horizon]) if v is not None]
        lows = [v for v in (_num(b.get("low")) for b in future[: cfg.short_horizon]) if v is not None]
        if highs:
            mfe_short_b = max(highs) / entry_price_b - 1.0
        if lows:
            mao_short_b = min(lows) / entry_price_b - 1.0

    # 基准价：优先场景 A 入场价；A 不可成交则退到 T 日收盘（明确标注来源，便于 #24 过滤）
    benchmark_price, benchmark_source = (
        (entry_price_a, "entry_price_A")
        if entry_price_a
        else (close_t, "close_T")
    )
    fwd_ret_3d = net_return(close_at(cfg.short_horizon), benchmark_price)
    fwd_ret_5d = net_return(close_at(cfg.long_horizon), benchmark_price)
    closes = [v for v in (_num(b.get("close")) for b in future[: cfg.short_horizon]) if v is not None]
    max_drawdown_3d = None
    if benchmark_price and closes:
        peak = benchmark_price
        worst = 0.0
        for c in closes:
            peak = max(peak, c)
            worst = min(worst, c / peak - 1.0)
        max_drawdown_3d = worst
    runup_vals = [v for v in (_num(b.get("high")) for b in future[: cfg.short_horizon]) if v is not None]
    adverse_vals = [v for v in (_num(b.get("low")) for b in future[: cfg.short_horizon]) if v is not None]
    max_runup_3d = (max(runup_vals) / benchmark_price - 1.0) if runup_vals and benchmark_price else None
    max_adverse_offset_3d = (min(adverse_vals) / benchmark_price - 1.0) if adverse_vals and benchmark_price else None

    # ---------------- 溢价类（相对 T 日收盘，无成本） ----------------
    open_1 = _num(next_bar.get("open")) if next_bar else None
    high_1 = _num(next_bar.get("high")) if next_bar else None
    close_1 = close_at(1)
    auction_1 = _num(next_bar.get("auction_price")) if next_bar else None
    next_auction_premium = _ratio(auction_1, close_t)
    next_open_premium = _ratio(open_1, close_t)
    next_close_return = _ratio(close_1, close_t)
    next_high_premium = _ratio(high_1, close_t)
    next_limit_up_premium = _ratio(next_limit_price, close_t)

    # ---------------- 晋级（1进2 / 2进3 / 3进4） ----------------
    promo_days = allowed_future[: cfg.promotion_horizon]
    promo_flags = flags_times[: cfg.promotion_horizon]
    promotion_source = "none"
    promotion_level: int | None = None
    observed_no_promotion = False
    for offset, (f_, t_) in enumerate(promo_flags, start=1):
        if f_ != LIMIT_FLAG_UP:
            continue
        if t_ is not None:
            if limit_times is None or t_ > limit_times:
                promotion_level = t_
                promotion_source = LABEL_SOURCE_V3
                break
        else:
            # v3 明确记了"次日封板"但连板数为空：按 T 日高度 + 间隔日数推算台阶
            promotion_level = (limit_times or 1) + offset
            promotion_source = LABEL_SOURCE_V3
            break
    if promotion_level is None:
        # 日线封板代理：未来窗口内 close 触及涨停价
        for offset, b in enumerate(future[: cfg.promotion_horizon], start=1):
            pc = close_at(offset - 1) if offset > 1 else close_t
            lp = limit_up_price(pc, limit_pct)
            c = _num(b.get("close"))
            if lp and c and c >= lp * (1.0 - eps):
                promotion_level = (limit_times or 1) + offset
                promotion_source = "kline_daily_seal_proxy"
                break
    # 窗口齐备但没有任何封板证据 → 判"未晋级"（0）；窗口本身缺失（停牌/尾部数据不足）
    # → None，绝不能把"看不到数据"当成"没晋级"或收益 0。
    if promotion_level is None and len(promo_days) >= cfg.promotion_horizon:
        observed_no_promotion = True
        promotion_source = "no_promotion_observed"

    base_times = limit_times
    is_1_to_2 = is_2_to_3 = is_3_to_4 = None
    if base_times is not None and (promotion_level is not None or observed_no_promotion):
        reached = promotion_level if promotion_level is not None else base_times
        # 每个台阶标签只对该样本"在赌的那一级"赋值，其余保持 None，
        # 便于 #26 按台阶做 masked 多任务训练（避免结构性 0 稀释正样本率）。
        if base_times == 1:
            is_1_to_2 = int(reached >= 2)
        elif base_times == 2:
            is_2_to_3 = int(reached >= 3)
        elif base_times == 3:
            is_3_to_4 = int(reached >= 4)

    cont_window = list(zip(allowed_future[: cfg.continuation_horizon], flags_times[: cfg.continuation_horizon]))
    is_continuation = None
    if cont_window:
        any_up = any(f_ == LIMIT_FLAG_UP for f_, _ in cont_window)
        if not any_up:
            for offset, b in enumerate(future[: cfg.continuation_horizon], start=1):
                pc = close_at(offset - 1) if offset > 1 else close_t
                lp = limit_up_price(pc, limit_pct)
                c = _num(b.get("close"))
                if lp and c and c >= lp * (1.0 - eps):
                    any_up = True
                    break
        is_continuation = int(any_up)

    is_broken_board = int(
        limit_flag == LIMIT_FLAG_BROKEN
        or bool(open_times and open_times > 0)
        or (sealed_at_close is False and high_t is not None and limit_price and high_t >= limit_price * (1.0 - eps))
    )
    is_limit_up_at_close = int(bool(sealed_at_close) and limit_flag in {LIMIT_FLAG_UP, None})
    is_break_streak_next = None
    if next_date and sealed_at_close:
        nf = next_flag
        nc = close_at(1)
        nlp = next_limit_price
        sealed_next = bool(nf == LIMIT_FLAG_UP) or (
            nlp is not None and nc is not None and nc >= nlp * (1.0 - eps)
        )
        is_break_streak_next = int(not sealed_next)
    is_next_limit_down = None
    if next_date:
        nc = close_at(1)
        ndp = limit_down_price(close_t, limit_pct)
        is_next_limit_down = int(
            next_flag == LIMIT_FLAG_DOWN
            or (ndp is not None and nc is not None and nc <= ndp * (1.0 + eps))
        )
    is_next_close_down = None
    if next_close_return is not None:
        is_next_close_down = int(next_close_return <= cfg.next_close_down_threshold)

    loss_ref_a = ret_1d_a if ret_1d_a is not None else None
    loss_ref_b = ret_1d_b if ret_1d_b is not None else None
    is_big_loss = None
    ref_ret = loss_ref_a if loss_ref_a is not None else loss_ref_b
    ref_mao = mao_short_a if mao_short_a is not None else mao_short_b
    if ref_ret is not None or ref_mao is not None:
        hit_close = ref_ret is not None and ref_ret <= cfg.big_loss_close_threshold
        hit_dd = ref_mao is not None and ref_mao <= cfg.break_drawdown_threshold
        is_big_loss = int(bool(hit_close or hit_dd))
    elif next_close_return is not None or max_adverse_offset_3d is not None:
        hit_close = next_close_return is not None and next_close_return <= cfg.big_loss_close_threshold
        hit_dd = max_adverse_offset_3d is not None and max_adverse_offset_3d <= cfg.break_drawdown_threshold
        is_big_loss = int(bool(hit_close or hit_dd))

    # ---------------- 状态汇总 ----------------
    status_parts: list[str] = []
    if len(future) < horizon:
        status_parts.append(LS_INSUFFICIENT_HORIZON)
    missing_required = any(
        v is None for v in (ret_short_a, ret_long_a) if status_a == STATUS_FILLED
    )
    if status_a == STATUS_UNFILLED:
        status_parts.append(LS_UNFILLED)
    elif status_a == STATUS_UNKNOWN:
        status_parts.append(LS_UNKNOWN_ENTRY)
    elif missing_required:
        status_parts.append(LS_MISSING_FUTURE_DATA)
    label_status = "|".join(status_parts) if status_parts else LS_OK
    label_complete = int(
        len(future) >= horizon
        and status_a == STATUS_FILLED
        and ret_short_a is not None
        and ret_long_a is not None
    )

    exit_short_date = future_dates[cfg.short_horizon - 1] if len(future) >= cfg.short_horizon else None
    exit_long_date = future_dates[cfg.long_horizon - 1] if len(future) >= cfg.long_horizon else None
    payload: dict[str, Any] = {
        "symbol": symbol,
        "trade_date": t_date,
        "name": str(name or ""),
        "sample_mode": sample_mode or "",
        "decision_time": f"{t_date} 15:00:00",
        "entry_time_A": f"{t_date} {normalize_time_str(signal.get('first_time'), '09:30:00')}",
        "entry_time_B": (
            f"{next_date} {'09:25:00' if (auction_1 is not None) else '09:30:00'}" if next_date else None
        ),
        "exit_time_short": f"{exit_short_date} 15:00:00" if exit_short_date else None,
        "exit_time_long": f"{exit_long_date} 15:00:00" if exit_long_date else None,
        "label_end_date": allowed_future[-1] if allowed_future else t_date,
        "label_horizon_days": horizon,
        "embargo_days": cfg.required_embargo_days(),
        "label_source": label_source,
        "label_status": label_status,
        "label_complete": label_complete,
        "market_regime": None,
        "market_regime_available": 0,
        # 分层
        "board_type": board_type,
        "promotion_stage": promotion_stage,
        "limit_shape": limit_shape,
        "board": market_board(symbol),
        "is_st": int(is_st_name(name)),
        "limit_flag": limit_flag,
        "limit_flag_inferred": limit_flag_inferred,
        "limit_times": limit_times,
        "open_times": open_times,
        "up_stat": signal.get("up_stat"),
        "first_time": normalize_time_str(signal.get("first_time")),
        "last_time": normalize_time_str(signal.get("last_time")),
        "limit_pct": limit_pct,
        "limit_price": _r(limit_price, 4),
        "limit_down_price": _r(down_price, 4),
        "prev_close": _r(prev_close, 4),
        "prev_close_derived": prev_close_derived,
        "open": _r(open_t, 4),
        "high": _r(high_t, 4),
        "low": _r(low_t, 4),
        "close": _r(close_t, 4),
        "volume": _num(bar.get("volume")),
        "pct_chg_frac": _r(pct_frac),
        "pct_chg_convention": pct_conv,
        "fd_amount": _num(signal.get("fd_amount")),
        "limit_amount": _num(signal.get("limit_amount")),
        "amount": _first_not_none(_num(signal.get("amount")), _num(bar.get("amount"))),
        "turnover_ratio": _num(signal.get("turnover_ratio")),
        "float_mv": _num(signal.get("float_mv")),
        "float_mv_bucket": classify_float_mv_bucket(_num(signal.get("float_mv")), cfg),
        "total_mv": _num(signal.get("total_mv")),
        "price_bucket": classify_price_bucket(close_t),
        "theme": signal.get("theme"),
        "industry": signal.get("industry"),
        "limit_reason": signal.get("limit_reason"),
        "board_type_raw": signal.get("board_type"),
        "next_limit_times": next_times,
        # 可成交性
        "fill_probability": _r(fill_probability),
        "seal_ratio": _r(seal_ratio),
        "flag_price_conflict": int(flag_price_conflict),
        "entry_status_A": status_a,
        "entry_confidence_A": entry_confidence,
        "entry_unfilled_A": int(status_a != STATUS_FILLED),
        "entry_unfilled_reason_A": reason_a,
        "entry_price_A": _r(entry_price_a, 4),
        "entry_status_B": status_b,
        "entry_unfilled_B": int(status_b != STATUS_FILLED),
        "entry_unfilled_reason_B": reason_b,
        "auction_price": _r(auction_1, 4),
        "auction_proxy": auction_proxy,
        "entry_price_B": _r(entry_price_b, 4),
        # 收益（场景 A）
        "ret_1d_A": _r(ret_1d_a),
        "ret_3d_A": _r(ret_short_a),
        "ret_5d_A": _r(ret_long_a),
        "mfe_3d_A": _r(mfe_short_a),
        "mao_3d_A": _r(mao_short_a),
        # 收益（场景 B）
        "ret_1d_B": _r(ret_1d_b),
        "ret_3d_B": _r(ret_short_b),
        "ret_5d_B": _r(ret_long_b),
        "mfe_3d_B": _r(mfe_short_b),
        "mao_3d_B": _r(mao_short_b),
        # 溢价
        "next_auction_premium": _r(next_auction_premium),
        "next_open_premium": _r(next_open_premium),
        "next_close_return": _r(next_close_return),
        "next_high_premium": _r(next_high_premium),
        "next_limit_up_premium": _r(next_limit_up_premium),
        # 0-1 标签
        "is_1_to_2": is_1_to_2,
        "is_2_to_3": is_2_to_3,
        "is_3_to_4": is_3_to_4,
        "promotion_level_next": promotion_level,
        "promotion_source": promotion_source,
        "is_continuation": is_continuation,
        "is_broken_board": is_broken_board,
        "is_break_streak_next": is_break_streak_next,
        "is_next_limit_down": is_next_limit_down,
        "is_next_close_down": is_next_close_down,
        "is_big_loss": is_big_loss,
        "is_limit_up_at_close": is_limit_up_at_close,
        "is_strong_3d": int(mfe_short_a >= cfg.strong_threshold) if mfe_short_a is not None else (
            int(max_runup_3d >= cfg.strong_threshold) if max_runup_3d is not None else None
        ),
        # 多 horizon（基准价口径）
        "ret_benchmark_price": _r(benchmark_price, 4),
        "ret_benchmark_source": benchmark_source,
        "fwd_ret_3d": _r(fwd_ret_3d),
        "fwd_ret_5d": _r(fwd_ret_5d),
        "max_drawdown_3d": _r(max_drawdown_3d),
        "max_runup_3d": _r(max_runup_3d),
        "max_adverse_offset_3d": _r(max_adverse_offset_3d),
    }

    # 旧口径兼容列（同名字段语义不变，便于既有回归平滑迁移；由 data_builder 的 legacy 通道产生）
    for offset in range(1, horizon + 1):
        if offset <= len(future):
            b = future[offset - 1]
            for field_name in ("open", "high", "low", "close"):
                payload[f"d{offset}_{field_name}"] = _r(_num(b.get(field_name)), 4)
            payload[f"future_limit_flag_d{offset}"] = (ft_map.get(future_dates[offset - 1]) or (None, None))[0]
            payload[f"future_limit_times_d{offset}"] = (ft_map.get(future_dates[offset - 1]) or (None, None))[1]
    return payload


# ---------------------------------------------------------------------------
# 列白名单（供 #24/#26 做"标签不得进因子矩阵"校验）
# ---------------------------------------------------------------------------

# 列分类（供 #24 特征侧做**列白名单**，杜绝标签泄漏）
# ---------------------------------------------------------------------------
#
# 标签产物里的列分三类：
# 1. ``META_COLUMNS``：主键 / 时点 / 口径说明，既不是特征也不是标签。
# 2. ``FEATURE_SAFE_COLUMNS``：**T 日收盘即已知**的事实（板型、形态、封单、
#    可成交性判定等），#24 可以直接当特征用（仍建议 shift 语义由因子层保证）。
# 3. ``LABEL_ONLY_COLUMNS``：只用 **T+1…T+H** 数据算出来的标签，**禁止**进因子矩阵。

META_COLUMNS = {
    "symbol",
    "trade_date",
    "name",
    "sample_mode",
    "decision_time",
    "entry_time_A",
    "entry_time_B",
    "exit_time_short",
    "exit_time_long",
    "label_end_date",
    "label_source",
    "label_status",
    "label_horizon_days",
    "embargo_days",
    "promotion_source",
    "ret_benchmark_source",
    "ret_benchmark_price",
    "pct_chg_convention",
    "prev_close_derived",
    "limit_flag_inferred",
    "entry_confidence_A",
    "board_type_raw",
}

FEATURE_SAFE_COLUMNS = {
    "board_type",
    "promotion_stage",
    "limit_shape",
    "board",
    "is_st",
    "limit_flag",
    "limit_times",
    "open_times",
    "up_stat",
    "first_time",
    "last_time",
    "limit_pct",
    "limit_price",
    "limit_down_price",
    "prev_close",
    "close",
    "open",
    "high",
    "low",
    "volume",
    "amount",
    "pct_chg_frac",
    "fd_amount",
    "limit_amount",
    "turnover_ratio",
    "float_mv",
    "float_mv_bucket",
    "total_mv",
    "price_bucket",
    "theme",
    "industry",
    "limit_reason",
    "market_regime",
    "market_regime_available",
    "seal_ratio",
    "flag_price_conflict",
    "fill_probability",
    "entry_status_A",
    "entry_unfilled_A",
    "entry_unfilled_reason_A",
    "entry_price_A",
    "is_broken_board",
    "is_limit_up_at_close",
}

LABEL_ONLY_COLUMNS = {
    # 场景 A 收益
    "ret_1d_A",
    "ret_3d_A",
    "ret_5d_A",
    "mfe_3d_A",
    "mao_3d_A",
    # 场景 B 收益
    "entry_status_B",
    "entry_unfilled_B",
    "entry_unfilled_reason_B",
    "entry_price_B",
    "auction_price",
    "auction_proxy",
    "ret_1d_B",
    "ret_3d_B",
    "ret_5d_B",
    "mfe_3d_B",
    "mao_3d_B",
    # 溢价
    "next_auction_premium",
    "next_open_premium",
    "next_close_return",
    "next_high_premium",
    "next_limit_up_premium",
    # 晋级 / 风险
    "is_1_to_2",
    "is_2_to_3",
    "is_3_to_4",
    "promotion_level_next",
    "is_continuation",
    "is_break_streak_next",
    "is_next_limit_down",
    "is_next_close_down",
    "is_big_loss",
    "is_strong_3d",
    "next_limit_times",
    # 多 horizon
    "fwd_ret_3d",
    "fwd_ret_5d",
    "max_drawdown_3d",
    "max_runup_3d",
    "max_adverse_offset_3d",
    # 可用性元数据（依赖未来是否存在 → 也不得进因子）
    "label_complete",
}

_FUTURE_LABEL_PREFIXES = (
    "d1_",
    "d2_",
    "d3_",
    "d4_",
    "d5_",
    "future_",
    "ret_",
    "fwd_ret_",
    "mao_",
    "mfe_",
    "max_",
    "next_",
)


def is_label_column(col: str) -> bool:
    """该列只含 **T+1 及以后** 的信息，禁止进入因子矩阵。"""
    text = str(col)
    if text in FEATURE_SAFE_COLUMNS or text in META_COLUMNS:
        return False
    if text in LABEL_ONLY_COLUMNS:
        return True
    if text.startswith(_FUTURE_LABEL_PREFIXES):
        return True
    return text in {"auction_price", "auction_proxy", "label_complete"}


def is_feature_safe_column(col: str) -> bool:
    """T 日收盘即可知的列：#24 可以作为特征使用。"""
    return str(col) in FEATURE_SAFE_COLUMNS


def label_columns(columns: Iterable[str]) -> list[str]:
    """从一批列名里挑出"标签列"（禁止进因子矩阵的那些）。"""
    return [c for c in columns if is_label_column(c)]


def feature_safe_columns(columns: Iterable[str]) -> list[str]:
    """从一批列名里挑出 T 日已知的可作特征列。"""
    return [c for c in columns if is_feature_safe_column(c)]


def assert_no_label_in_features(feature_names: Iterable[str]) -> None:
    """因子矩阵混入标签列时直接报错（#24/#26 接入时调用）。"""
    leaked = label_columns(feature_names)
    if leaked:
        raise ValueError(f"标签列泄漏进因子矩阵，请从特征中移除: {sorted(leaked)[:20]}")


# ---------------------------------------------------------------------------
# 旧口径（次日开盘价代理）—— 保留以兼容既有 samples.csv / 回归
# ---------------------------------------------------------------------------


def compute_sample_labels(frame: Any, idx: int, label_cfg: Any) -> dict[str, Any] | None:
    """[已弃用] 旧"次日开盘价"代理标签。

    它衡量的是"次日开盘买入"的收益，不是打板真实成交，且一字板会被
    误算成可成交。新代码请使用 :func:`build_sample_label`；本函数仅为
    兼容 :class:`FirstLimitAlphaDataBuilder.build_dataset` 既有输出而保留，
    并显式打上 ``label_source = daily_proxy_v2`` 标记。
    """
    max_needed = max(
        label_cfg.continuation_horizon,
        label_cfg.strong_horizon,
        label_cfg.break_horizon,
        label_cfg.evaluation_horizon,
    )
    future = frame.iloc[idx + 1 : idx + 1 + max_needed]
    if len(future) < max_needed:
        return None

    current = frame.iloc[idx]
    entry = future.iloc[0]
    entry_open = float(entry["open"])
    if entry_open <= 0:
        return None

    cont_window = future.head(label_cfg.continuation_horizon)
    strong_window = future.head(label_cfg.strong_horizon)
    break_window = future.head(label_cfg.break_horizon)
    eval_window = future.head(label_cfg.evaluation_horizon)

    max_high_3d = float(strong_window["high"].max())
    min_low_2d = float(break_window["low"].min())
    close_3d = float(strong_window.iloc[-1]["close"])
    close_5d = float(eval_window.iloc[-1]["close"])
    ret_high_3d = (max_high_3d / entry_open) - 1.0
    ret_close_3d = (close_3d / entry_open) - 1.0
    ret_close_5d = (close_5d / entry_open) - 1.0
    min_low_2d_ret = (min_low_2d / entry_open) - 1.0
    next_2d_limit_up = int(bool(cont_window["is_limit_up"].astype(bool).any()))
    day1_close_ret = (float(entry["close"]) / entry_open) - 1.0
    break_risk = int(
        min_low_2d_ret <= label_cfg.break_drawdown_threshold
        or day1_close_ret < -0.01
        or float(entry["close"]) < float(current["close"])
    )

    payload: dict[str, Any] = {
        "label_source": LABEL_SOURCE_PROXY,
        "entry_date": str(entry["trade_date"]),
        "entry_open": round(entry_open, 6),
        "label_continuation": next_2d_limit_up,
        "label_strong_3d": int(ret_high_3d >= label_cfg.strong_threshold),
        "label_break_risk": break_risk,
        "future_max_high_3d": round(max_high_3d, 6),
        "future_min_low_2d": round(min_low_2d, 6),
        "future_close_3d": round(close_3d, 6),
        "future_close_5d": round(close_5d, 6),
        "ret_high_3d": round(ret_high_3d, 6),
        "ret_close_3d": round(ret_close_3d, 6),
        "ret_close_5d": round(ret_close_5d, 6),
        "ret_min_low_2d": round(min_low_2d_ret, 6),
        "future_limit_up_count_5d": int(eval_window["is_limit_up"].astype(bool).sum()),
    }
    for offset in range(1, label_cfg.evaluation_horizon + 1):
        row = future.iloc[offset - 1]
        for field_name in ("open", "high", "low", "close"):
            payload[f"d{offset}_{field_name}"] = round(float(row[field_name]), 6)
    return payload
