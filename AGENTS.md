# Project Rules

## 服务与代码

- 本地 `./start.sh` / `./restart.sh` 仅用于开发调试，禁止把本地启动当作交付；线上以 tokyo 服务器部署为准。
- 默认通过 `./start.sh` 启动，保持 `RELOAD=0`（后台运行更稳定）。
- 若需开发期热重载，可显式使用 `RELOAD=1 ./start.sh`。
- 每次修改代码、配置、脚本或测试并完成必要验证后，必须执行 `git add`、创建 Git commit，并合并到 `main` 后 push 到 GitHub 的 `origin/main`（`https://github.com/Shadowell/Alpha`）；禁止只保留本地未提交改动、本地 commit 或只推送功能分支。
- 合并到 `main` 后必须触发 CI 自动部署，并按下方「自动部署规则」确认线上部署成功；禁止用本地启动代替线上部署。
- 当系统架构、页面功能、API 接口、项目结构或技术栈发生变化时，必须同步更新 `README.md`（包括但不限于：核心功能表、界面预览、技术栈、项目结构树、API 接口列表等对应章节）。

## 自动部署规则

- 代码改动流程固定为：`git add` → `git commit` → 合并到 `main` → push `origin/main` → GitHub Actions 自动部署。
- 自动部署 workflow：`.github/workflows/deploy.yml`；部署脚本：`deploy/deploy.sh`；部署目标：tokyo 服务器（`https://alpha.notenap.com`）。
- 禁止只保留本地改动、只创建本地 commit 或只在本地启动服务；线上必须运行最新 `main`。
- 自动部署触发后，必须确认 GitHub Actions 运行成功，并完成线上冒烟检查（页面、关键 API、静态资源）；失败需修复后重新触发，直至部署成功。

## 服务重启与代码变更操作规范

**本地 macOS 环境每次重启服务或修改代码前，必须严格按以下顺序执行，防止 SQLite 进程卡死（UEs 内核不可中断状态）；服务器自动部署不适用此本地流程。**

```
1. 关闭 SQLite 连接（触发 WAL checkpoint）
2. 停止服务
3. 修改代码
4. 启动服务
```

### 具体步骤

**步骤 1 — 关闭 SQLite 连接（手动 checkpoint）**

在停服前，先对所有数据库执行 WAL checkpoint，确保没有挂起的 fsync：

```bash
python3 - <<'PY'
import sqlite3
for db in ["data/funnel_state.db", "data/market_kline.db"]:
    try:
        conn = sqlite3.connect(db, timeout=5)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
        print(f"checkpoint OK: {db}")
    except Exception as e:
        print(f"checkpoint skip: {db} ({e})")
PY
```

**步骤 2 — 停止服务**

```bash
./stop.sh
```

确认进程已退出（无 `UEs` 残留）：

```bash
ps aux | grep "uvicorn app.main" | grep -v grep
```

**步骤 3 — 修改代码**

完成所有代码变更。

**步骤 4 — 启动服务**

```bash
./start.sh
```

### 原因说明

SQLite 在 WAL 模式下，若服务被强制中断（`SIGKILL` 或任务被 `CancelledError` 打断）而未完成 `fsync`，macOS 内核会让进程进入 **UEs（Uninterruptible Sleep + Exiting）** 状态，此时连 `kill -9` 也无法清除，只能重启 Mac。

lifespan shutdown 虽已内置自动 checkpoint（`_checkpoint_all_sqlite`），但手动操作时额外执行一次步骤 1 可彻底规避风险。

## 计划（Plan）产出规则

- 每次生成的"实施计划 / 任务方案 / plan" 都必须保存到 `.cursor/plans/` 目录下的 Markdown 文件。
- 文件名格式固定为：`YYYYMMDD-HH_<简短主题>.plan.md`
  - `YYYYMMDD` 为当天日期，`HH` 为 00–23 的 24 小时制小时（本地时区，亚洲/上海）。
  - 示例：`20260418-17_修复缩量启动点击bug.plan.md`、`20260418-22_接入本地Ollama-LLM.plan.md`
- 文件内容要求：
  - 必须包含 "背景 / 目标"、"任务分解（checklist）"、"验收标准" 三个章节。
  - 建议再加 "风险与回滚" 与 "关键决策记录"。
- 计划文件只增不删；同一主题后续有更新，另起新文件并引用旧文件链接，而不是覆盖原文件。
- 生成计划后，先保存到 `.cursor/plans/`，再开始执行具体任务。


- 每次生成的"实施计划 / 任务方案 / plan" 都必须保存到 `.cursor/plans/` 目录下的 Markdown 文件。
- 文件名格式固定为：`YYYYMMDD-HH_<简短主题>.plan.md`
  - `YYYYMMDD` 为当天日期，`HH` 为 00–23 的 24 小时制小时（本地时区，亚洲/上海）。
  - 示例：`20260418-17_修复缩量启动点击bug.plan.md`、`20260418-22_接入本地Ollama-LLM.plan.md`
- 文件内容要求：
  - 必须包含 "背景 / 目标"、"任务分解（checklist）"、"验收标准" 三个章节。
  - 建议再加 "风险与回滚" 与 "关键决策记录"。
- 计划文件只增不删；同一主题后续有更新，另起新文件并引用旧文件链接，而不是覆盖原文件。
- 生成计划后，先保存到 `.cursor/plans/`，再开始执行具体任务。
