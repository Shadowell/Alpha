# FirstLimit 因子目录（FACTOR CATALOG）

> 本目录覆盖 `strategy/first_limit_alpha/` 的因子定义。
> **第 2 章为 issue #24「涨停微观结构 + 个股股性因子族」**，由
> `microstructure_features.py` 实现；因子注册表（与代码同源）可通过
> `microstructure_factor_specs()` 获取，本文件与其逐条对应（46 个因子 +
> `*_is_missing` / `*_is_proxy` 指示列）。

---

## 1. 既有日线特征（#24 之前，46 列）

`features.py / FirstLimitFeatureBuilder` 的 OHLCV 日线特征（收益率、振幅、
均线、量比、影线、`limit_quality` 等）保持原行为不变。注意：旧
`limit_quality = body_pct + close_near_high*0.02 - upper_shadow_pct` 只是
K 线代理，**不能**区分一字封死与尾盘偷袭板；打板场景请优先使用下述 #24 因子。

`features.py` 仅追加了 `build_microstructure_features` 等函数的 re-export
（向后兼容接入），不改变既有输出列与默认行为。

---

## 2. issue #24：涨停微观结构 + 个股股性因子族

### 2.0 总体约定

| 项目 | 约定 |
| --- | --- |
| 入口 | `build_microstructure_features(db_path, symbols, as_of_date, lookback=250)`，离线只读（`file:...?mode=ro`），不注册 API、不触规则引擎 |
| 主键 | 输出 `symbol` + `as_of_date`，供 #23 dataset 按 `(symbol, trade_date)` join |
| 金额单位 | **元**（#22 v3 映射层已统一；空值 NULL） |
| 缺失处理 | 因子值保持 `NaN`，并配 `<name>_is_missing`（1/0）指示列；**禁止填 0**。真实 0（如窗口内确无涨停）与缺失严格区分 |
| 代理口径 | 日线无法精确还原的结构配 `<name>_is_proxy`（1/0）；真实字段优先（`is_proxy=0`） |
| 数据源优先级 | 封板质量字段只取 Tushare `limit_list_d`（`limit_board_daily`，source=`tushare.limit_list_d`）；kpl 回填的 `board_type/theme/limit_reason` 仅透传/标注，**不与 Tushare 口径混用** |
| PIT | 封板强度/板型/交互项使用 T 日盘后数据（合法）；股性（Group C）只用 `trade_date < T` 的记录，事件类要求溢价/晋级观察日也 `< T`（等价严格 `shift(1)`） |
| 交易分钟轴 | `session_minutes()`：09:30=0、11:30=120、13:00=120、15:00=240；集合竞价封板（09:25 等）=0；午休时间钳制为 120 |
| 涨跌停幅度 | 主板 10%、创业板/科创板（30/68）20%、北交所（4/8/92）30%；**新股上市首日不适用该近似**（相关代理因子会误判，样本应结合 `hist_days_observed` 过滤） |

### 2.A 封板强度（13 因子）

| 因子 | 公式 | 单位 | 数据源 | 缺失处理 | PIT | 代理 |
| --- | --- | --- | --- | --- | --- | --- |
| `fd_amount` | `limit_board_daily.fd_amount` 原值（收盘封单额） | 元 | limit_list_d | NULL→缺失 | T 日盘后 | 否 |
| `fd_ratio_float_mv` | `fd_amount / float_mv` | 无量纲 | limit_list_d | 任一空/分母≤0→缺失 | T 日盘后 | 否 |
| `fd_ratio_amount` | `fd_amount / amount`（封成比） | 无量纲 | limit_list_d | 同上 | T 日盘后 | 否 |
| `fd_ratio_volume` | `(fd_amount/close) / 成交量`；成交量优先 `kline_daily.volume`，缺失用 `amount/close` 折算 | 无量纲 | limit_list_d + kline_daily | 同上 | T 日盘后 | 否 |
| `first_seal_minute` | `session_minutes(first_time)` | 分钟 | limit_list_d | 无法解析→缺失 | T 日盘后 | 否 |
| `last_seal_minute` | `session_minutes(last_time)` | 分钟 | limit_list_d | 同上 | T 日盘后 | 否 |
| `seal_duration_min` | U 行：`240 - first_seal_minute`；Z 行：`last - first`（均未扣盘中开板段，**上界近似**） | 分钟 | limit_list_d | 依赖时间缺失→缺失 | T 日盘后 | **is_proxy=1** |
| `open_times` | 开板次数原值 | 次 | limit_list_d | NULL→缺失 | T 日盘后 | 否 |
| `broken_duration_min` | 仅 Z 行：`240 - last_seal_minute`（最后一次封板至收盘的破板时长）。U 行需分钟线才能精确，保持缺失（待 #9） | 分钟 | limit_list_d | U 行/时间缺失→缺失 | T 日盘后 | **is_proxy=1** |
| `reseal_speed_min` | `open_times≥1` 时 `(last_seal - first_seal) / open_times`（均匀节奏假设的平均回封周期） | 分钟 | limit_list_d | 条件不满足→缺失 | T 日盘后 | **is_proxy=1** |
| `on_board_amount` | `limit_amount`（板上成交额）原值 | 元 | limit_list_d | NULL→缺失 | T 日盘后 | 否 |
| `on_board_amount_ratio` | `limit_amount / amount`（板上成交占比） | 无量纲 | limit_list_d | 同上 | T 日盘后 | 否 |
| `seal_quality_proxy` | 日线近似序数：2=一字(O=H=L=C)、1=T字/加速(O=H=C>low)、0=收盘封死(H=C)、-1=盘中触板未封死(H>C 且 high 达板)。`is_proxy` 仅当无 limit 行佐证时为 1 | 序数 | kline_daily | 无 K 线/无前收→缺失 | T 日盘后 | 条件性 |

### 2.B 板型（13 因子）

| 因子 | 公式 | 单位 | 数据源 | 缺失处理 | PIT | 代理 |
| --- | --- | --- | --- | --- | --- | --- |
| `is_one_word` | 真实：有 U 行且首封≤09:30 且 `open_times=0` 且 O=H=L=C；Z 行按定义=0；无 limit 行时降级日线代理（O=H=L=C 且涨幅达板，`is_proxy=1`） | 0/1 | limit_list_d + kline_daily | 两者都缺→缺失 | T 日盘后 | 条件性 |
| `is_t_word` | O=H=C>low 且收盘封板（无分时数据，恒为日线近似 `is_proxy=1`；Z 行=0） | 0/1 | kline_daily | 无 K 线→缺失 | T 日盘后 | **恒 1** |
| `is_turnover_board` | `open_times≥1` 且 `on_board_amount_ratio ≥ 0.5` | 0/1 | limit_list_d | 无板行或关键字段缺失→缺失 | T 日盘后 | 否 |
| `is_tail_board` | `first_seal_minute ≥ 210`（14:30 后首封）。日线无法还原时间，无 `first_time` 时**不做代理**，直接缺失 | 0/1 | limit_list_d | 首封时间缺失→缺失 | T 日盘后 | 否 |
| `is_acceleration_board` | `limit_times≥2` 且 `open_times=0` 且（一字板 或 首封≤第 30 分钟）（近似定义，`is_proxy=1`） | 0/1 | limit_list_d + kline_daily | 关键字段缺失→缺失 | T 日盘后 | **恒 1** |
| `is_reversal_board` | 收盘封死(H=C) 且低开（`open < prev_close`），即低开/负转翻红板（近似，`is_proxy=1`） | 0/1 | kline_daily | 缺前收/当日 K 线→缺失 | T 日盘后 | **恒 1** |
| `limit_times` | 连板数原值 | 板 | limit_list_d | NULL→缺失 | T 日盘后 | 否 |
| `board_position_code` | 按 T 日 `limit_times` 分档：1=首板、2=1进2、3=2进3、4=3进4、5=高位(≥5) | 序数 | limit_list_d | 无板行/limit_times 缺失→缺失 | T 日盘后 | 否 |
| `board_position_label` | 上表中文标签 | 文本 | 同上 | 同上 | 同上 | 否 |
| `prev_limit_times` | T-1 日板行的 `limit_times`（供下游"晋级"交叉校验；T-1 无板行→缺失，不填 0） | 板 | limit_list_d | 见左 | T-1 | 否 |
| `up_stat_days` | `up_stat` 文本 `"X/Y"` 解析：X=自最近首个涨停日起的天数 | 天 | limit_list_d | 解析失败→缺失 | T 日盘后 | 否 |
| `up_stat_limits` / `up_stat_limit_density` | Y=期间涨停次数；密度 = Y/X | 次 / 无量纲 | limit_list_d | 同上 | T 日盘后 | 否 |
| `up_stat_raw` / `board_type_text` | 原文透传；`board_type_text` 为 kpl 回填，仅供人工交叉验证，**不参与任何口径计算** | 文本 | limit_list_d / kpl | NULL→缺失 | T 日 | 标注 |

### 2.C 个股股性（15 因子，全部严格 `shift(1)`）

窗口 = 市场交易日历上 `as_of_date` 之前最近 `lookback`（默认 250）个交易日，
记为 W；所有统计只使用 `trade_date ∈ W` 且 `trade_date < T` 的记录。
新股保护：`hist_days_observed < 5` 时股性因子整体缺失（指示列=1）。
事件类统计中，若次一市场交易日该股无 K 线（停牌），事件从分母剔除，不填 0。

| 因子 | 公式 | 单位 | 缺失处理 | PIT 说明 |
| --- | --- | --- | --- | --- |
| `limit_up_count_250d` | W 内 `limit_flag='U'` 天数 | 次 | 新股保护 | 严格 <T |
| `first_board_count_250d` | W 内 `limit_times=1` 的 U 天数 | 次 | 同上 | 严格 <T |
| `first_board_next_day_premium_avg` | 历史首板日 d：`d<T` 且其次一市场交易日 `n(d)<T` 且两日均可观察时，`mean(close[n]/close[d]-1)` | 比率 | 无事件→缺失 | **d 与 n(d) 均 <T**（shift(1)）；跨除权事件用原始价，存在失真（已知口径限制，见 2.5） |
| `first_board_next_day_win_rate` | 上述事件溢价>0 占比 | 比率 | 同上 | 同上 |
| `continuation_success_rate` | 历史 U 日中 `n(d)<T` 且可观察者里 `n(d)` 仍为 U 的占比（连板晋级率） | 比率 | 样本<1→缺失 | 同上 |
| `broken_board_rate` | W 内 U/Z 板行中 Z（收盘炸板）占比 | 比率 | 无 U/Z 行→缺失 | 严格 <T |
| `max_consecutive_boards_250d` | W 内按市场日历连续 U 日最长串 | 板 | W 内无 U 日→缺失 | 严格 <T；停牌日不打断连板串 |
| `limit_up_amount_avg` | W 内 U 日 `amount` 均值 | 元 | 无 U 日/全空→缺失 | 严格 <T |
| `limit_up_freq_250d` | U 天数 / 该股 W 内有 K 线的天数 | 比率 | 观察<5 天→缺失 | 严格 <T |
| `float_mv_bucket` | T-1 及以前最近板行 `float_mv` 分桶：0=<20亿、1=20-50亿、2=50-100亿、3=100-300亿、4=>300亿（元） | 序数 | 无市值→缺失 | 严格 <T |
| `small_cap_limit_up_rate` | 同市值分桶股票的 `limit_up_freq` 均值。宇宙=窗口内出现过于 `limit_board_daily` 的股票（全市场日频市值表未接入，**板内代理** `is_proxy=1`；待 #1 daily_basic 全量接入后升级为精确值） | 比率 | 桶内无股票→缺失 | 窗口 ≤ T-1 |
| `price_bucket` | T-1 收盘价分桶：0=<5、1=5-10、2=10-30、3=30-60、4=>60 元；停牌股用最近可得收盘 | 序数 | 无价→缺失 | T-1 |
| `price_bucket_limit_up_rate` | T-1 同价格分桶全 K 线股票中，W 内 ≥1 次涨停的股票占比（真实全宇宙） | 比率 | 桶内无股票→缺失 | 窗口 ≤ T-1 |
| `hist_days_observed` | W 内该股有 K 线的天数（诊断列，恒有值：新股=小值） | 天 | 否 | 严格 <T |

### 2.D 交互项（4 因子）

| 因子 | 公式 | 缺失处理 | PIT | 代理 |
| --- | --- | --- | --- | --- |
| `seal_strength_x_float_mv` | `fd_ratio_float_mv × log10(float_mv/1e8 + 1)`（小盘封板的封单杠杆放大） | 任一成分缺失→缺失 | T 日盘后 | 否 |
| `first_seal_time_x_market_regime` | `(1 - first_seal_minute/240) × regime`，`regime = clip(T-1 全市场涨停数 / 其前 20 个交易日均值, 0, 3)` | 任一成分缺失→缺失 | regime 只用 ≤T-1；首封为 T 日 | **is_proxy=1**（regime 用涨停家数而非连板高度，待 #25 情绪引擎升级） |
| `open_times_x_continuation_rate` | `open_times × continuation_success_rate` | 任一成分缺失→缺失 | 股性成分 <T | 否 |
| `fd_ratio_x_theme_heat` | `fd_ratio_amount × min(T 日同 theme 涨停数/10, 1)`；theme 依赖 #8 的 kpl 回填，未回填→缺失 | 见左 | T 日截面（盘后合法） | **is_proxy=1，PARTIAL（依赖 #8）** |

### 2.E 指示列规则

* 对 2.A/2.B/2.C/2.D 每个因子 `X`：`X_is_missing ∈ {0,1}`，当且仅当 `X` 为 NaN/空时为 1。
  测试保证「数值缺失 ⇔ 指示列=1」逐列成立（`test_output_schema_and_indicators`）。
* 代理列（`PROXY_FACTORS`）额外有 `X_is_proxy ∈ {0,1}`。`seal_quality_proxy`、
  `is_one_word` 的 proxy=1 表示"因缺 limit_list_d 行而降级为日线口径"。

### 2.F 验收状态（截至本分支）

| 验收项 | 状态 |
| --- | --- |
| 每个因子有定义/公式/单位/数据源/缺失处理/PIT 说明 | DONE（本文件 + `microstructure_factor_specs()` 代码同源） |
| 最新交易日封板强度非空率 ≥90%、股性覆盖 ≥95% | **PARTIAL**：需服务侧对 `data/market_kline.db` 真实数据跑 `microstructure_quality_report()` 复核（本地 worktree 无生产库，禁止伪造数值） |
| 单因子 RankIC / 分层收益（次日溢价/晋级/断板） | **PARTIAL**：标签由 #23 产出、评估基线在 #27，本报告接口已预留 `ic_report=PARTIAL` 字段，不提前填数 |
| 与旧 `limit_quality` 的增量（AUC/PR-AUC/Precision@TopK） | **PARTIAL**：待 #23/#27/#6 |
| 无未来函数（随机抽查 100 样本人工核对） | DONE（自动化等价实现：`test_pit_random_spot_check`（seed 固定、截断库重算比对）+ `test_pit_future_rows_cannot_change_features`；生产 100 样本抽查建议随 #27 复跑） |
| 单测覆盖一字板/T字板/炸板/无封单/停牌/除权/新股/缺失率 | DONE（`tests/test_daban_microstructure_features.py`，15 项，deterministic fixture，无网络） |

### 2.G #23 / #27 消费接口

```python
from strategy.first_limit_alpha.features import build_microstructure_features  # 或 microstructure_features 模块

feats = build_microstructure_features(
    db_path="data/market_kline.db",
    symbols=sample_df["symbol"].unique(),
    as_of_date="2026-10-23",   # 与 #23 samples 的 trade_date 一致
    lookback=250,
)
joined = sample_df.merge(feats, left_on=["symbol", "trade_date"],
                         right_on=["symbol", "as_of_date"], how="left")
```

* 列清单：`microstructure_output_columns()`（108 列：5 诊断列 + 46 因子 + 46 缺失指示 + 11 代理指示）。
* 建模建议：训练时对 `X_is_missing=1` 的样本保留 NaN（LightGBM 原生缺失分支），
  不要用 `fill_value=0` 二次填充；`X_is_proxy=1` 可作为域漂移特征或分层评估键。
* 逐日重建：`build_dataset` 后对每个 `trade_date` 各调一次（内部只读连接，无副作用）；
  **禁止**对同一 as_of 使用 T 日之后的库增量（因子按 as_of 时点可复现）。

### 2.H 已知口径限制（诚实披露）

1. 炸板时长（U 行）、精确封板/开板区间需要分钟线（#9），当前以上界/缺失表示。
2. 次日溢价、晋级率使用未复权收盘价；除权除息事件的溢价会失真（本目录已标注，
   修复依赖复权因子表接入，测试 `test_exright_premium_is_finite_and_flagged_as_limitation` 固化行为）。
3. `small_cap_limit_up_rate` 是板内宇宙代理；`daily_basic` 全量市值接入后应升级为全市场频率。
4. 新股首日 44%/30% 无板规则不适用 `board_limit_pct` 近似；请结合 `hist_days_observed` 过滤。
5. `up_stat` 解析假设 Tushare「X 天 Y 涨停」口径（如 `"5/3"`=5 天 3 涨停）；解析失败→缺失。
6. Tushare 与 kpl 口径不混用：kpl 回填列只透传展示，不进入任何公式。
