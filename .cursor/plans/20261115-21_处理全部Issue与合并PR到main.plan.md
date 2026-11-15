# 处理全部 Open Issue 与合并全部 PR 到 main

## 背景 / 目标

当前仓库 `Shadowell/Alpha` 存在：
- **2 个 Open PR**：`#59`（Dependabot Python 依赖更新）、`#60`（Dependabot `@playwright/test` 1.65.0 更新）。
- **17 个 Open Issue**：
  - QA 缺陷类（2 个）：`#50`、`#53`（后端 `d9083c8` 已合入 `main`，需核验并闭环）。
  - 打板 Roadmap 类（15 个）：`#22`–`#36`。其中 `#22`/`#23`/`#24`/`#25`/`#26`/`#33`/`#36` 已合入 `main`（部分待数据回填/接线核验）；`#27`、`#28`–`#31`、`#32`、`#34`、`#35` 在本地分支及 worktree 中有已提交与未提交的在途成果。
- **4 个存在未提交改动的 Worktree**：
  - `.qoder/worktrees/agent-issue-51-52-frontend`（`FE-HOT` 前端告警横幅与 E2E）
  - `.qoder/worktrees/issue-32-daban-timing`（`alerts.py`/`rejections.py`/`risk.py` 及测试）
  - `.qoder/worktrees/issue-34-hotmoney`（`factors.py`/`follow.py`/`validation.py` 及测试）
  - `.qoder/worktrees/issue-35-daban-events`（`dedupe.py`/`extraction.py`/`guard.py`）

目标：
1. 核验并合并 PR `#59`、`#60` 到 `main`。
2. 回收、测试并提交各 worktree 中的有效未提交成果，将所有待合入分支（`feature/issue-27-*`、`28-31`、`32`、`34`、`35`、`51-52-frontend-alerts`）安全合入 `main`（或补齐缺失测试后再合入）。
3. 核验 `#50`、`#53` 及 `#22`–`#36` 的完成度与接线/数据状态，按事实更新 Issue 评论或关闭已完成 Issue，同步更新 `README.md`（如有架构/模块/文档变更）。
4. 全量通过 CI 门禁（`ruff check`、`ruff format --check`、`mypy`、`pytest`）并推送 `origin/main`，确认 GitHub Actions（`Python Tests` + `Deploy Alpha`）全部成功及线上 `https://alpha.notenap.com` 冒烟通过。

## 任务分解（checklist）

- [ ] **阶段 1：PR #59 / #60 核验与合并**
  - 检查 PR #59（Python 依赖）与 PR #60（Playwright E2E 依赖）变更与冲突情况，合并到 `main`（或合入后在本地/CI 复验 `pip check` 与测试）。
- [ ] **阶段 2：回收 4 个 Dirty Worktree 并补齐/运行对应测试**
  - 检查并测试 `agent-issue-51-52-frontend`、`issue-32-daban-timing`、`issue-34-hotmoney`、`issue-35-daban-events` 的未提交改动；确认无误后在各自 worktree 提交。
  - 对 `#27`（`feature/issue-27-tiered-promotion-model` 标为无测试）与 `#35`（`dedupe`/`extraction`/`guard`）补齐必要的单元测试并验证通过。
- [ ] **阶段 3：串行合入全部未合并分支到 `main`**
  - 依次将 `feature/issue-51-52-frontend-alerts`、`feature/issue-27-tiered-promotion-model`、`feature/issue-28-31-daban-factors`、`feature/issue-32-daban-timing`、`feature/issue-34-hotmoney-seats`、`feature/issue-35-daban-events` 合并（或 rebase/cherry-pick 无旧基线冲突的提交）到 `main`。
- [ ] **阶段 4：全量本地 CI 门禁复检 & 文档同步**
  - 运行 `ruff check app tests`、`.github/workflows/tests.yml` 中的 `ruff format --check`、`python -m mypy`、以及全量 `pytest -q`。
  - 如新增模块/页面能力未反映在 `README.md`，同步更新 `README.md`。
- [ ] **阶段 5：推送 `origin/main`、确认 CI 与线上部署**
  - `git push origin main`，监控 `Python Tests` 与 `Deploy Alpha` workflow 至全部 success。
  - 对 `https://alpha.notenap.com` 执行线上冒烟检查（页面、`/api/health`、`/api/market/session`、`/api/pool/enroll` 等）。
- [ ] **阶段 6：核验并处理全部 17 个 Open Issue**
  - 核实 `#50`、`#53` 线上与单测均已修复后关闭。
  - 核实 `#22`–`#36` 每个 Issue 的验收标准，已完成部分写明交付证据并关闭或按当前实际状态（如部分因子族/数据回填依赖）精准更新并处理。

## 验收标准

- Open PR 数量为 0（`#59`、`#60` 均已合并到 `main`）。
- 所有待合入的 feature/fix 分支与 worktree 有效代码均已合入 `main` 且工作区干净。
- `origin/main` 上 `Python Tests` 与 `Deploy Alpha` 均绿，线上 `https://alpha.notenap.com` 冒烟正常。
- 所有 17 个 Open Issue 均已得到明确处理（已完结的关闭并附证据，若存在因外部权限/生产数据回填等客观阻塞的如实记录）。

## 风险与回滚

- **旧基线分支合并风险**：`#28-31`、`#32`、`#34`、`#35` 基于旧 `main` 分出，直接 `git merge` 可能正常三方合并，需确认不会回退 `d9083c8` 等新改动。
- **CI 门禁差异**：`ruff check app tests`、`ruff format --check`（指定列表）与 `mypy` 必须在本地提前跑通。
- **回滚**：每个 feature 分支均为独立模块（`strategy/*`），如出现回归可直接 `git revert -m 1 <merge-sha>`。
