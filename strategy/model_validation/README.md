# `strategy/model_validation` — 序列 / 多模态模型增量验证脚手架（issue #36）

> **这个包不训练、不部署任何深度模型。** 它只回答一个问题：
> 候选模型（GRU / Transformer / K 线图像 / 多模态）在**严格样本外、扣掉费用之后**，
> 相对表格 baseline 是否有**稳定**增量？没有 ⇒ 输出「不保留」并落盘。

issue #36 的硬门槛是「不要为了酷而酷」，因此判定逻辑写在代码里（`gate.py`），
不靠人读表格。`fixtures.py` 提供两个合成剧本，同一个脚手架一次产出
`keep`（序列真有增量）和 `reject`（序列是纯噪声）两种结论，证明门槛既不是
橡皮章、也不是永远说不。

---

## 1. 模块划分

| 文件 | 职责 |
| --- | --- |
| `contracts.py` | 输入契约：`Panel` / `SequenceBundle` / `ColumnNames` / `ModelInput` / `validate_panel()` |
| `splitting.py` | walk-forward + embargo + purging；`leakage_probe()` 自检；`EmbargoViolation` / `InsufficientDataError` |
| `metrics.py` | IC / rank IC / 逐日横截面 ICIR / ROC-AUC / PR-AUC / 逐日 Precision@TopK / 分层收益+单调性 / Brier+ECE / 最大回撤 / 单边换手（纯 numpy+pandas） |
| `portfolio.py` | 每日 TopK 组合、可成交性消费、成本敏感性网格、覆盖率 |
| `models.py` | 内置确定性模型（表格 baseline / 序列候选 / 排名融合 / 无技能常数）+ 可选依赖适配器 + artifact 存取 |
| `gate.py` | 保留 / 不保留 判定（阈值全部显式配置） |
| `runner.py` | 编排 + 复现信息（seed、`config_hash`、数据指纹）+ 异常降级 |
| `report.py` | `report.md` / `metrics.json` / `decision.json` / `model_cards.json` / `windows.csv` |
| `fixtures.py` | 合成面板生成器（`informative_sequence` / `pure_noise_sequence`） |
| `cli.py` | 运行入口 `python -m strategy.model_validation.cli` |

issue 原文建议的文件名是 `strategy/first_limit_alpha/sequence_eval.py` 与
`multimodal_eval.py`。因为 `strategy/first_limit_alpha/*` 在并行开发中属于 #23/#24/#25/#26/#27
所有（本分支禁止改动），这里用独立包 + 本地契约实现等价职责：
序列分支 = `SequenceTemporalModel` + runner 的 sequence 输入通道；
多模态分支 = `ModelInput` 同时携带 `tabular` + `sequence`（可再挂嵌入通道）+ `RankBlendModel` 融合。

---

## 2. 运行

```bash
# 合成数据演示（不需要上游数据、不联网、纯 CPU）
python -m strategy.model_validation.cli demo --out data/model_validation --windows 3

# 真实面板（#23/#26 落地后）
python -m strategy.model_validation.cli run \
  --input-csv data/model_validation/panel.csv \
  --seq-npy  data/model_validation/panel_seq.npy \
  --features f_alpha,f_volume_z,f_sentiment \
  --seq-len 30 --length-policy error --label-horizon 2 --top-k 5 \
  --out data/model_validation --tag real_run

# 单测（全部 tmp_path，不读写 data/*.db）
python -m pytest tests/test_model_validation_*.py -q
python -m ruff check strategy/model_validation tests/test_model_validation_*.py
python -m ruff format --check strategy/model_validation tests/test_model_validation_*.py
```

`--fail-on-reject` 让脚本在「所有候选都不保留」时返回非零，方便未来接 CI/定时任务；
默认输出目录 `data/model_validation`（`data/` 已在 `.gitignore`），禁止把报告提交进 git。

---

## 3. 防泄漏口径

日历布局：`[ train .. ][ valid .. ][ embargo guard ][ test block ]`

- OOS 窗口互不重叠、严格按时间前进（随机切分在构造上不可达）。
- `guard = max(cfg.embargo, label_horizon)`：标签前瞻天数只是 embargo 的下限。
- **逐行 purge**：训练样本只要标签窗口 `[t+1, t+h]` 触及 OOS 块就单独剔除
  （#23 是多 horizon 标签，按日期一刀切仍会泄漏）。
- `report.md` 第 4 节打印 `leakage_probe()`：落入 test 的训练标签窗口数必须为 0、
  train→test 位置间隔、被 purge 行数、被 embargo 拦住的样本数。

---

## 4. 判定门槛（`GateConfig`）

全部满足才 `keep`：

| 检查 | 默认阈值 |
| --- | --- |
| 样本外窗口数 | ≥ 3（issue 要求） |
| 每个指标规则 | 跨窗口平均增量 ≥ `min_delta` **且** 胜出窗口占比 ≥ `min_beat_ratio`(0.6) |
| 默认指标规则 | 横截面 rank IC 均值、PR-AUC、Precision@TopK、费用后 TopK 平均收益、费用后累计收益 |
| 可成交性 | 必须 `executability_source == "upstream"`（缺 `can_buy/can_sell` ⇒ 直接 reject） |
| 打分完整性 | 每个窗口都成功（`skipped`/`failed` ⇒ reject，不允许只报好看的指标） |
| 风险预算 | 最大回撤劣化 ≤ 0.05、换手上升 ≤ 0.15、覆盖率 ≥ 0.5 |
| 成本预算 | 每样本推理 ≤ 25ms、单窗口训练 ≤ 600s、参数量/基准 ≤ 200 倍 |

结论落盘在 `decision.json`：`overall_decision`、`overall_decision_zh`（不保留 / 保留）、
`kept_candidates`、`rejected_candidates`、逐候选 `checks` + `blockers`。

---

## 5. 显式不负责（写在代码注释里，避免误用）

| 事项 | 归属 | 本包行为 |
| --- | --- | --- |
| 一字板买不到 / 跌停卖不出 / 停牌 | #26 | 只消费 `can_buy` / `can_sell`；被排除样本计入 `excluded_not_buyable`、`excluded_not_sellable` |
| 退市与样本池幸存者偏差 | #22 / #23 | 假定上游样本池已处理；这里只报 `coverage_ratio` 和「无可成交候选的日期数」 |
| 标签与多 horizon 定义 | #23 | 通过 `label` / `label_horizon` 列接入；`label_horizon` 驱动 purging |
| 因子计算 | #4 / #7 / #8 / #9 / #10 | 通过 `features` 列名接入 |
| 生产灰度、异步推理链路 | 未来 issue | 不注册路由、不进 `app/`、不被 `app.main` import |

---

## 6. 主 agent 落地清单（集成步骤）

1. **准备面板**：#23/#26 合并后导出 DataFrame / CSV，最小列集合
   `symbol, event_date, label, outcome_return(#26 可成交毛收益), can_buy, can_sell, label_horizon`；
   可选 `next_day_premium`。列名不同就配 `ColumnNames(...)`。
2. **准备序列张量**：`(n_rows, seq_len, n_channels)`，行序必须与面板一致
   （`validate_panel` 会硬校验并对不齐直接报错）。
3. **挂真实模型**：新增 `ModelSpec(name, role="candidate", factory="<类名>", params={...})`，
   适配器必须**在方法内部**导入 torch（参考 `models.TorchSequenceAdapter` /
   `KronosImageAdapter`），缺失依赖时抛 `OptionalDependencyError`，由 runner 记为
   `skipped`。**禁止**在 `models.py` 顶层 import 重依赖，也禁止改 `requirements*.txt` 来迁就实验。
4. **校准阈值**：`GateConfig` 的 `min_delta` / 延迟预算需按 #6 评估框架和线上 API 超时
   要求复核后固化成一次专门 commit（当前是保守默认值）。
5. **文档同步**：本包被合并后，根 `README.md` 的项目结构树与核心功能表需要加一行
   「模型增量验证脚手架」（本分支按并行开发纪律未改根 README 与 CHANGELOG）。
6. **可选调度**：若要做成周期性研究任务，请在 `app/` 侧新增离线 job 调用
   `run_validation()`，保持实验模型不进生产推理路径。

---

## 7. 复现性

`metrics.json` / `decision.json` 记录：`seed`、`config_hash`（配置 JSON sha256 前 16 位）、
`data_fingerprint`（行数 / 日期跨度 / `(symbol, event_date)` 键集合 sha256 前 16 位）、
`data_version`、`feature_version`、每个模型的 artifact 路径（`--save-artifacts` 时）。
内置 numpy 模型是闭式解 ⇒ 两次运行的指标逐字节一致（`test_identical_inputs_produce_identical_reports`）；
唯一非确定项是墙钟耗时（训练秒数 / 每样本毫秒），只出现在成本/延迟报告里，不参与一致性断言。

报告样例：

- 第 2 节 baseline vs 候选 vs 融合的指标总表（同口径）
- 第 3 节逐窗口明细（含 `status`，失败窗口单列，不允许隐藏）
- 第 4 节泄漏自检
- 第 5 节成本敏感性（0/5/10/20/30/50 bps）
- 第 6 节成本 / 延迟 / 容量（参数量、训练秒、ms/样本、峰值 RSS、设备）
- 第 7 节模型卡（适用场景 / 失效场景 / 数据依赖 / 容量 / 决策 / 阻断原因）
