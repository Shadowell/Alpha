# #26 打板可成交性、容量与真实交易约束回测引擎

> 状态：Batch 1 实现会话交付 —— **纯服务引擎 + 离线测试**，未接 HTTP/API。
> 模块：`app/services/limit_up_backtest.py`；测试：`tests/test_limit_up_backtest.py`。
> 数据契约：只读依赖 #22 的 `limit_board_daily` / `kline_daily` / `kline_daily_extras`（daily_basic 落库表）/ `index_daily`。

## 1. 设计口径

- **无未来函数**：T 日排队成交概率只用 T 日收盘前可知字段（`fd_amount`/`amount`/`first_time`/`open_times`/`limit_flag`）；`as_of` 之后数据一律不可见（PIT，只读 sqlite `mode=ro` 连接，不触发迁移）。
- **不造数**：涨停表无记录且封单质量不可评估 → `queue_fill_unassessable`；基准缺数 → `benchmark_skipped` 显式记录；分层缺 regime → `missing`/`unknown` 桶，不伪造。
- **禁止虚假成交**：
  - T+1 一字涨停买入失败（strict/loose 一致，全天封死买不到）；
  - T+1 停牌买入失败；次日开盘价即涨停价 → strict 不确认、loose 概率模型（固定 seed 可复现）；
  - T 日一字/回封板 strict 一律不计收益；只有炸板（收盘未封）才确定性成交；
  - 卖出：一字跌停/开盘即跌停价（strict）/收盘封跌停/停牌 → 顺延至下一可卖日；
  - 跳空低开穿越止损价：按开盘价成交（不按止损价乐观成交）；开盘封死卖不出 → 顺延。
- **T+1 制度**：买入次一交易日方可卖出；`exit_scene=next_open` 场景不检查入场日盘中高价（避免未来函数）。

## 2. 费用 / 滑点 / 容量（全部可配置，写入报告 `assumptions`）

| 项 | 默认 | 说明 |
|---|---|---|
| 佣金 | 万 2.5，最低 5 元/笔 | `FeeConfig` |
| 印花税 | 卖出万 5 | `FeeConfig.stamp_tax_rate` |
| 过户费 | 双边万 0.1，可关 | `FeeConfig.transfer_fee_enabled` |
| 滑点 | 基准 10bps + 参与率冲击（5% 参与率时 +30bps，上限 ×4） | `SlippageConfig` |
| 容量 | 单票单日成交额参与率 ≤5%，超限 `scale`（缩量）或 `skip`（跳过） | `CapacityConfig` |
| 排队概率 | seal_ratio 阈值 0.30，一字 loose 上限 0.25，炸板确定性成交 | `QueueFillConfig` |
| 除权 | `adj_factor` 比值近似调整持仓股数（分红并入近似） | `use_adj_factor` |
| 其他 | 100 股整手；竞价无 `stk_auction` 数据时以开盘价代理并标注 | — |

## 3. 绩效口径

- 净值曲线来自逐日现金账本 + 持仓 mark-to-market（停牌用最近收盘价），**不再单笔相加**；`trade_stats.sum_single_trade_pnl_pct` 仅作对照并带 note。
- 指标：年化（242 交易日）、最大回撤、Sharpe/Sortino/Calmar、胜率、盈亏比、平均持有天数、费用占比（占买入额 / 占双边成交额）、次日开盘溢价（竞价代理）、3/5 日收益（对全部信号统计，与成交无关）。
- 基准：`index_daily` 指数（如 000300.SH）+ 等权全市场（`kline_daily` 窗口函数日收益均值，含 warm-up）；覆盖率不足 80% → 显式 skip。
- 分层：regime（`regime_by_date` 可选入参，未提供 = skip 并注明；日期缺数 = `missing` 桶）、板型（一字/换手/炸板/上游 `board_type`）、流通市值分桶、连板高度、题材。

## 4. 供主 agent 接入 API 的入口签名

```python
run_limit_up_backtest(
    signals,                      # pandas DataFrame 或 list[dict]，必需列 symbol + signal_date，可选 name
    *,
    db_path: str | Path,          # market_kline.db
    as_of: str | None = None,     # PIT 截止日（YYYY-MM-DD）
    mode: str = "strict",         # "strict" | "loose"
    entry_scene: str = "next_open",   # "queue_t" | "next_auction" | "next_open"
    exit_scene: str = "hold_days",    # "next_auction" | "next_open" | "next_close" | "hold_days"
    hold_days: int = 1, tp_pct: float | None = None, sl_pct: float | None = None,
    initial_capital: float = 1_000_000.0, position_size_pct: float = 0.2,
    fees: FeeConfig | None = None, slippage: SlippageConfig | None = None,
    capacity: CapacityConfig | None = None, queue_fill: QueueFillConfig | None = None,
    regime_by_date: dict[str, str] | None = None,
    benchmark_ts_codes: Iterable[str] = (), include_equal_weight_benchmark: bool = True,
    use_adj_factor: bool = True,
    mcap_buckets: tuple[float, ...] = DEFAULT_MCAP_BUCKETS,
    seed: int = 20261022,
    persist_path: str | Path | None = None,   # 给定时写 JSON（可复现：seed/params/数据水位/信号哈希）
    generated_at: str | None = None,          # 便于测试注入
) -> dict  # JSON 可序列化：assumptions / fill_stats / capacity / layered_metrics /
           # metrics / nav_series / trades / signal_details / benchmarks /
           # benchmark_skipped / open_positions / data_quality_notes / repro
```

- `run_strict_loose_comparison(signals, *, db_path, as_of=None, **kwargs) -> {"strict", "loose", "delta"}` —— delta 列出仅 loose 计入的概率成交及解释。
- `capacity_curve(signals, *, db_path, as_of=None, capitals=(1e5,...,5e7), **kwargs) -> {"curve": [...]}` —— 资金规模 vs 收益/换手/容量命中。
- `legacy_lab_diff_report(legacy_samples, report) -> dict` —— 与旧 `BacktestLab` 同批信号差异（纯函数，未改旧模块）：`legacy_only_signals` 即旧口径系统性高估来源。

## 5. 等待 #23 / #25 的字段

| 依赖 | 需要的字段 | 本引擎的现状降级 |
|---|---|---|
| #23 可执行标签 | 逐日可成交标签（竞价/开盘可成交性、封单质量等级），`limit_board_daily` 增列后可回填 `BoardContext` | 目前用 U/D/Z + seal_ratio + first_time 启发式；标签接入后应替换 `queue_fill_probability` 输入 |
| #25 regime 表 | `regime_by_date: dict[trade_date, regime]`（或等价表读取函数） | 未提供时分层 skip 并注明；缺日期入 `missing` 桶 |
| #1 竞价数据 | `stk_auction` 落库（开盘竞价成交价/量） | 当前以开盘价代理并在 `entry/exit_price_source` 标注 `open_proxy(auction_missing)` |

## 6. 已知限制（报告内同样声明）

- 排队成交概率是**模型假设**（无 L2 逐笔），loose 口径必须标注为"非确认成交"。
- 除权用复权因子近似（分红不单独出现金券流）。
- 等权全市场基准对小市值打板策略风险暴露不同，仅并列展示不作优劣结论。
- `tests/test_api_regression.py::TestKlineCache::test_kline_minute_*` 两个用例在基线 a5c0a67 即失败，与本模块无关。
