# 接入本地 Ollama 作为 Hermes Agent LLM

- **日期**：2026-04-18 16:00 (Asia/Shanghai)
- **触发场景**：Alpha UI 中 "深度个股研报"、"消息驱动分析" 等 Hermes AI 能力输出 `LLM 未配置`，
  用户反馈本地已部署 hermes agent，希望直接复用本地模型。

## 背景 / 目标

### 现状
- 项目无 `.env`，`OPENAI_API_KEY` 未设置。
- 本地 `:8642`（默认 `HERMES_AGENT_URL`）无服务在跑。
- 本机已启动 **Ollama**（`:11434`），具备模型：`qwen2.5:7b`、`qwen3:latest`、`llama3.2:latest`、`qwen3-vl:8b`。
- Ollama 原生提供 OpenAI 兼容端点 `POST /v1/chat/completions`，且验证过支持 `response_format: json_object`（17s 返回标准 JSON）。

### 目标
1. Alpha 自动接入本地 Ollama，不依赖外部付费 OpenAI API；
2. 研报 / 消息驱动分析 / 监控 LLM 调用全部走 `qwen2.5:7b`；
3. `.env` 只在本地生效，不进 git 仓库；
4. 服务启动时自动加载 `.env`，无需手动 `export`。

## 任务分解（checklist）

- [x] 端口扫描确认本地 LLM 运行在 Ollama `:11434`，并枚举可用模型
- [x] 验证 Ollama `/v1/chat/completions` 对 `response_format=json_object` 的兼容性
- [x] `start.sh` 增加 `.env` 自动加载（`set -o allexport` + grep 过滤注释空行）
- [x] 新建 `.env`：
  - `OPENAI_API_KEY=ollama`
  - `OPENAI_BASE_URL=http://127.0.0.1:11434/v1`
  - `HERMES_MODEL=qwen2.5:7b`
- [x] 新建 `.env.example` 给出三套配置方案（Ollama / OpenAI / hermes-agent server）
- [x] 将 `.env`、`.env.local` 加入 `.gitignore`
- [ ] `./restart.sh` 后，通过 `/api/hermes-ai/news-insight/run` 和 `/api/hermes-ai/research/{symbol}` 验证本地 LLM 被正确调用且返回结构化 JSON
- [ ] README 更新"环境变量"章节：默认示例改为 Ollama，补充 `.env` 自动加载说明
- [ ] 新增 `.cursor/plans/` 规则到 `AGENTS.md`（已完成 —— 文件命名为 `YYYYMMDD-HH_主题.plan.md`）
- [ ] commit + push 到 `origin/main`

## 验收标准

| # | 验收项 | 判定 |
|---|-------|------|
| 1 | `./restart.sh` 输出包含 `已加载 .env` | 必须出现 |
| 2 | 打开"Hermes AI - 深度个股研报"，点"生成研报" | `verdict` 不再固定为"观望" / summary 不再出现"OPENAI_API_KEY 未配置" |
| 3 | 打开"消息驱动分析"，点"生成分析" | `insights` 数组非空、`market_overview` 非"LLM 未配置" |
| 4 | `curl http://127.0.0.1:18888/api/hermes-ai/research/600519 -X POST` 耗时 < 60s 且返回合法 JSON | 必须满足 |
| 5 | API 回归 54/54 + E2E 14/14 仍全绿 | 必须 |

## 风险与回滚

- **模型响应慢**：qwen2.5:7b 单次推理 ~15–30s，批量任务（日间 daily_review、周报）可能阻塞。
  - 缓解：`_call_llm` 已经有 30s 超时；必要时把 `HERMES_MODEL` 换成 `llama3.2:latest`（更快）。
- **JSON 格式偏差**：开源模型偶尔输出非严格 JSON。代码内已有 `find('{')/rfind('}')` 兜底提取。
- **回滚**：删除 `.env` 或注释掉 `OPENAI_API_KEY` → 自动回到"LLM 未配置"降级路径，不影响规则引擎。

## 关键决策记录

- **为什么不启动独立 hermes-agent server（8642）**：当前代码里 hermes-agent server 只是对 MCP 工具的封装，
  本地 Ollama 已能覆盖全部 LLM 能力；启 8642 需要额外的 Node/Python 进程和 API_SERVER_KEY 管理，MVP 阶段收益不大。
- **为什么选 qwen2.5:7b 而非 qwen3:latest**：qwen3 支持 thinking 模式，其输出包含 `<think>` 标签，
  会破坏 Alpha 期望的"纯 JSON"格式；qwen2.5 默认不 thinking，兼容性最好。
- **`.env` 加载方式**：未引入 python-dotenv 依赖，改用 shell 原生 `source <(grep …)`，零成本。
