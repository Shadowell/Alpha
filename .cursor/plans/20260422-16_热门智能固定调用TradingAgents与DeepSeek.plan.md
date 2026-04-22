# 热门智能固定调用 TradingAgents 与 DeepSeek

## 背景 / 目标

当前热门股票智能分析模块虽然已经具备 `TradingAgents` 适配层，但调用行为仍允许通过配置切换 provider / model，默认值也依赖运行时配置。需要把“热门个股讨论分析”明确固定为调用本地 `TradingAgents` 项目，并使用 `DeepSeek API` 作为 LLM 后端。

本次目标：

- 明确热门智能模块的讨论分析来源就是 `/Users/jie.feng/work/github/TradingAgents`。
- 在适配层中显式使用 `DeepSeek` 官方 API 配置，不再沿用默认 OpenAI 配置。
- 在服务元信息与 README 中体现当前热门智能的分析后端。

## 任务分解（checklist）

- [ ] 梳理 `TradingAgentsAdapter` 与 `HotStockAIService` 的现有调用链。
- [ ] 在适配层中补充 `DeepSeek` provider / backend URL / key 校验逻辑。
- [ ] 调整热门智能服务元信息，使前端能看到当前讨论后端为 `TradingAgents + DeepSeek`。
- [ ] 补充测试，覆盖适配器配置与元信息输出。
- [ ] 更新 `README.md` 对热门智能模块与 TradingAgents 接入方式的描述。
- [ ] 执行测试与静态检查。
- [ ] 执行 `./restart.sh`，完成 `git add / commit / push`。

## 验收标准

- 热门智能模块对热门股的讨论分析明确通过本地 `TradingAgents` 项目触发。
- 适配层默认且显式使用 `DeepSeek` API。
- 接口或页面元信息能看出当前分析后端是 `TradingAgents + DeepSeek`。
- 测试通过，服务可正常重启。

## 风险与回滚

- 若 `DEEPSEEK_API_KEY` 缺失，真实讨论分析会失败，需要在错误信息中清晰暴露。
- 若 `TradingAgents` 本地依赖变更，适配层导入路径可能失效。
- 回滚方式：恢复本次提交前的适配器和热门智能服务逻辑并重启。

## 关键决策记录

- 热门智能基础量化打分继续保留，用于排序和三池；`TradingAgents` 负责单股深度讨论与加减分。
- 将 provider 固定到 `DeepSeek`，减少运行时分叉和排障复杂度。
