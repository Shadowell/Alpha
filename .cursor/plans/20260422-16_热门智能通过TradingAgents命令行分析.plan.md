# 热门智能通过 TradingAgents 命令行分析

## 背景 / 目标

当前热门智能模块虽然已经接入 `TradingAgents` 适配层，但调用方式仍是 Python 内部导入 `TradingAgentsGraph`。用户要求改为通过命令行方式调用本地项目：

`uv run python -m cli.main analyze --ticker ... --date ... --provider deepseek --quick-model deepseek-chat --deep-model deepseek-reasoner`

并将返回的分析结果放入三池中的第一个池子，同时给出明确的买入/卖出评价。

## 任务分解（checklist）

- [ ] 让 `/Users/jie.feng/work/github/TradingAgents` 支持非交互 `analyze` 命令及参数输入。
- [ ] 增加机器可读输出，方便 Alpha 通过 subprocess 获取分析结果。
- [ ] 修改 Alpha 的 `TradingAgentsAdapter`，改为 shell 调用 `uv run python -m cli.main analyze ...`。
- [ ] 将分析结果映射为候选池展示信息，并补充明确的买入/卖出/观望评价字段。
- [ ] 更新前端展示与 README 文档。
- [ ] 补充回归测试。
- [ ] 执行 `./restart.sh`，完成提交与推送。

## 验收标准

- `TradingAgents` 支持非交互 `analyze` 命令。
- Alpha 不再直接 import `TradingAgentsGraph`，而是通过命令行调用本地 `TradingAgents` 项目。
- 热门智能的候选池能展示 `TradingAgents` 返回的结论，并有明确买入/卖出/观望评价。
- 测试通过，服务重启完成或如实说明阻塞原因。

## 风险与回滚

- `TradingAgents` 外部仓库若持续有未提交脏改动，新增 CLI 可能与其本地改动发生冲突。
- 命令行输出若混入第三方日志，JSON 解析需要额外兼容。
- 回滚方式：恢复 Alpha 适配层为 import 调用，并回退 `TradingAgents` 的 CLI 变更。

## 关键决策记录

- 采用命令行调用是为了满足固定调用方式与进程隔离要求。
- 热门智能仍保留三池评分框架；`TradingAgents` 结果主要作为候选池展示与买卖评价来源。
