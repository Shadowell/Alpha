# 背景 / 目标

用户希望把 `FirstLimit Alpha` 继续整理成适合直接交给 AI 执行的完整设计提示词，放入 `Alpha` 项目中，便于切换到该项目后持续让 AI 分阶段落地实现。

目标：

- 在 `strategy/first_limit_alpha/` 下新增一份提示词文档
- 提示词覆盖从需求澄清、数据与标签、特征工程、baseline、时序模型、回测、接入 Alpha 到实施顺序
- 同步更新 `README.md` 的项目结构说明

## 任务分解（checklist）

- [x] 确认 `Alpha` 项目约定与目标目录
- [ ] 新增 `strategy/first_limit_alpha/AI_PROMPTS.md`
- [ ] 在提示词文档中写入总设计 prompt
- [ ] 在提示词文档中写入分阶段实现 prompt
- [ ] 在提示词文档中写入代码生成约束与验收要求
- [ ] 更新根级 `README.md` 的项目结构
- [ ] 提交并推送到 `origin/main`

## 验收标准

- `strategy/first_limit_alpha/AI_PROMPTS.md` 已创建
- 文档内容可以直接复制给 AI 执行
- 文档至少覆盖：模块定位、数据标签、特征工程、baseline 模型、深度学习升级、回测设计、Alpha 集成路径
- `README.md` 已体现新增文件
- 变更已 commit 并 push 到 `origin/main`

## 风险与回滚

- 风险：提示词写得过宽，AI 后续执行时容易跑偏
- 应对：在文档中加入明确的边界、阶段目标、文件落点、验收标准和“不做事项”
- 回滚：如果提示词结构不理想，可在后续版本继续新增文档迭代，不覆盖历史 plan

## 关键决策记录

- 提示词文档放在 `strategy/first_limit_alpha/`，与设计文档同目录，便于模块化管理
- 采用“总提示词 + 子任务提示词 + 实施约束”结构，方便不同 AI/不同轮次复用
