# Alpha x Hermes Agent：受控自治研究与策略进化架构设计

> **版本**: v2.0  
> **日期**: 2026-04-15  
> **状态**: 已实现 Phase 1-3  
> **核心原则**: 自动发现问题 → 自动形成优化提案 → 自动排序 → **人工审批执行** → 效果追踪 → 经验沉淀

---

## 目录

1. [设计理念与边界](#1-设计理念与边界)
2. [Hermes 三层职责模型](#2-hermes-三层职责模型)
3. [内嵌式架构设计](#3-内嵌式架构设计)
4. [工具面定义](#4-工具面定义)
5. [自主进化闭环](#5-自主进化闭环)
6. [数据模型与持久化](#6-数据模型与持久化)
7. [Prompt 工程](#7-prompt-工程)
8. [API 接口设计](#8-api-接口设计)
9. [前端交互设计](#9-前端交互设计)
10. [稳定性与隔离](#10-稳定性与隔离)
11. [分阶段实施计划](#11-分阶段实施计划)
12. [验收场景](#12-验收场景)
13. [假设与约束](#13-假设与约束)

---

## 1. 设计理念与边界

### 1.1 Hermes 是什么

Hermes 不是聊天入口，不是通用 AI 助手。它是 **Alpha 内嵌的投研代理层**，围绕现有模块建立受控的自学习闭环：

```
┌──────────────────────────────────────────────────────────┐
│                       Alpha 主系统                        │
│                                                          │
│  策略漏斗  ·  公告选股  ·  K线缓存  ·  定时任务              │
│                                                          │
│  ┌──────────────────────────────────────────────────┐    │
│  │              Hermes 研究代理层                      │    │
│  │                                                    │    │
│  │  观察系统表现 → 产出参数建议 → 策略候选建议          │    │
│  │  公告理解增强 → 异常诊断 → 实验提案                  │    │
│  │                                                    │    │
│  │  ⚠️ 所有高风险动作必须经过人工确认                    │    │
│  └──────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────┘
```

### 1.2 不可逾越的边界

| 允许 | 禁止 |
|------|------|
| 读取所有业务数据 | 直接修改策略配置 |
| 生成参数调整建议 | 自动应用参数变更 |
| 产出研究报告与提案 | 自动改代码、自动提交 |
| 创建实验定义 | 自动发布、自动下单 |
| 诊断异常并告警 | 绕过人工审批执行变更 |

### 1.3 v1 的"自主进化"定义

**自动发现问题** + **自动形成优化提案** + **自动排序** + **人工审批执行**

不是全自动改代码，不是全自动调参。Hermes 是一个永远在线的投研助理，它只建议，不决策。

---

## 2. Hermes 三层职责模型

Hermes 不是单一 agent，而是三类能力的统一运行时：

```
            ┌─────────────┐
            │  Evolver    │  产出提案，不直接执行
            │  进化器      │  参数建议 / 实验提案 / 代码建议
            └──────┬──────┘
                   │ 基于分析结论
            ┌──────┴──────┐
            │  Analyst    │  产出结构化结论
            │  分析者      │  理由增强 / 参数诊断 / 事件归因
            └──────┬──────┘
                   │ 基于观察数据
            ┌──────┴──────┐
            │  Observer   │  只读，采集系统状态
            │  观察者      │  漏斗 / 公告 / K线 / 热点
            └─────────────┘
```

### 2.1 Observer（观察者）

只读权限，采集以下数据面：

| 数据源 | 对应现有模块 | 采集内容 |
|--------|------------|---------|
| 漏斗状态 | `FunnelService` | 各池股票数、分数分布、迁移记录 |
| 公告池状态 | `NoticeService` | 候选数、评分分布、命中关键词分布 |
| 策略配置 | `StrategyConfig` | 当前策略参数值 |
| K 线缓存 | `KlineCacheService` | 同步状态、成功/失败率、耗时 |
| 热门概念 | `concept_engine` | Top10 概念热度、涨跌分布 |
| 热门个股 | `data_provider` | Top10 个股涨跌幅 |
| 人工操作 | `SQLiteStateStore` | 手动迁移记录 |

### 2.2 Analyst（分析者）

基于观察数据调用 LLM 产出结构化结论：

- **候选股票理由增强**：为公告选股的候选补充深度分析（为什么推荐/不推荐）
- **规则参数诊断**：检测参数是否导致过度追高或过度保守
- **公告事件归因**：分析公告类型命中率与次日表现的关联
- **失败筛选诊断**：分析候选池为空或转化率低的根因
- **热点演化解读**：概念板块轮动趋势与选股策略的关系

### 2.3 Evolver（进化器）

只生成提案（Proposal），不直接执行高风险变更：

| 提案类型 | 描述 | 风险等级 |
|---------|------|---------|
| `rule_patch` | 策略参数改动建议（含理由、预期影响） | 中 |
| `notice_rule_patch` | 公告规则关键词/权重调整建议 | 中 |
| `experiment` | 新实验建议（A/B 对比窗口定义） | 低 |
| `code_suggestion` | 代码优化建议草案 | 高（仅建议，不执行） |
| `backtest_task` | 回测/复盘任务建议 | 低 |
| `alert` | 异常告警（数据源故障、同步失败等） | 即时 |

---

## 3. 内嵌式架构设计

### 3.1 模块拆分

```
app/
├── mcp_server.py                # MCP 工具服务器（供 Hermes Agent 通过 MCP 调用）
└── services/
    ├── hermes_runtime.py        # 运行时 + 调度 + Agent/降级双模式
    ├── hermes_memory.py         # 记忆持久化 + 提案 + outcome tracking
    └── hermes_memory_bridge.py  # Hermes MEMORY.md 同步桥接

~/.hermes/
├── config.yaml                  # 已添加 alpha MCP server 配置
├── memories/
│   ├── MEMORY.md                # Alpha 项目上下文 + 历史调参经验
│   └── USER.md                  # 用户投资画像
└── skills/alpha/
    ├── SKILL.md                 # Alpha 投研代理技能定义
    └── references/
        ├── alpha-daily-review.md    # 盘后复盘最佳实践
        ├── alpha-notice-diagnosis.md # 公告选股诊断流程
        └── alpha-param-tuning.md    # 参数调优经验库
```

### 3.2 hermes_runtime.py 职责

```python
class HermesRuntime:
    """Hermes 统一运行时。"""

    def __init__(self, memory: HermesMemory, tools: dict):
        self.memory = memory
        self.tools = tools       # 受控工具集
        self._semaphore = asyncio.Semaphore(2)  # 最多 2 个并发任务
        self._running: dict[str, HermesTask] = {}

    async def run_task(self, task_type: str, params: dict) -> AgentRun:
        """执行一次 Hermes 任务，带超时和熔断保护。"""
        ...

    async def schedule_daily_review(self):
        """盘后复盘：15:30 自动触发。"""
        ...

    async def schedule_notice_review(self):
        """公告复盘：21:00 自动触发。"""
        ...

    async def schedule_weekly_evolution(self):
        """周度进化报告：每周日 20:00。"""
        ...

    async def on_anomaly(self, anomaly_type: str, detail: dict):
        """异常即时诊断：接口连续失败 / 同步失败时触发。"""
        ...
```

### 3.3 hermes_memory.py 职责

```python
class HermesMemory:
    """Hermes 记忆层，底层 SQLite。"""

    def __init__(self, db_path: str = "data/funnel_state.db"):
        ...

    def record_run(self, run: AgentRun) -> int: ...
    def record_proposal(self, proposal: AgentProposal) -> int: ...
    def record_feedback(self, proposal_id: int, action: str, note: str) -> int: ...
    def record_observation(self, snapshot: dict) -> int: ...

    def list_proposals(self, status: str = None, limit: int = 20) -> list[dict]: ...
    def get_proposal(self, proposal_id: int) -> dict | None: ...
    def approve_proposal(self, proposal_id: int, note: str = "") -> bool: ...
    def reject_proposal(self, proposal_id: int, note: str = "") -> bool: ...

    def get_observations(self, days: int = 7) -> list[dict]: ...
    def get_recent_runs(self, limit: int = 10) -> list[dict]: ...
```

### 3.4 与 FastAPI 主进程的集成

在 `app/main.py` 的 `lifespan` 中注册 Hermes 后台任务：

```python
# main.py lifespan 扩展
async def lifespan(app: FastAPI):
    app.state.ticker_task = asyncio.create_task(_ticker_loop())
    app.state.kline_cache_task = asyncio.create_task(_kline_cache_loop())
    app.state.hermes_task = asyncio.create_task(_hermes_scheduler_loop())  # 新增
    yield
    for key in ["ticker_task", "kline_cache_task", "hermes_task"]:
        task = getattr(app.state, key, None)
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
```

### 3.5 隔离约束

| 约束 | 实现方式 |
|------|---------|
| Hermes 不阻塞主 API | `asyncio.Semaphore(2)` 限并发；LLM 调用走 `asyncio.to_thread` |
| 单任务超时 | 每个 `run_task` 包裹 `asyncio.wait_for(timeout=120)` |
| 失败重试 | 最多重试 2 次，间隔 30s |
| 熔断保护 | 连续 3 次失败后跳过该类型任务 1 小时 |
| 资源隔离 | Hermes 独立 SQLite 表，不直接写 `funnel_state` / `notice_state` |

---

## 4. 工具面定义

Hermes 通过受控工具集访问系统能力，不允许直接操作服务对象。

### 4.1 工具优先级

#### P0 — v1 必须实现

| 工具 | 权限 | 对应现有模块 |
|------|------|------------|
| `get_funnel_snapshot(trade_date)` | 只读 | `FunnelService.get_funnel()` |
| `get_notice_snapshot(trade_date)` | 只读 | `NoticeService.get_notice_funnel()` |
| `get_strategy_profile()` | 只读 | `FunnelService.get_strategy_profile()` |
| `get_kline_sync_status()` | 只读 | `KlineCacheService.get_sync_state()` |

#### P1 — v1 尽量实现

| 工具 | 权限 | 对应现有模块 |
|------|------|------------|
| `get_hot_concepts()` | 只读 | `FunnelService.get_hot_concepts()` |
| `get_hot_stocks()` | 只读 | `FunnelService.get_hot_stocks()` |
| `get_stock_detail(symbol, days)` | 只读 | `FunnelService.get_stock_detail()` |
| `get_notice_detail(symbol, days)` | 只读 | `NoticeService.get_notice_detail()` |
| `propose_notice_rule_patch(diff)` | 写提案 | → `agent_proposals` 表 |
| `create_experiment(spec)` | 写实验 | → `agent_proposals` 表（类型=experiment） |

#### P2 — v2 实现

| 工具 | 权限 | 说明 |
|------|------|------|
| `list_kline_sync_logs(page, size)` | 只读 | 分析同步质量趋势 |
| `query_manual_pool_moves(days)` | 只读 | 分析人工决策模式 |
| `evaluate_experiment(id)` | 写评估 | 对比实验前后指标 |
| `publish_agent_report(id)` | 写报告 | 生成周度/月度进化报告 |

### 4.2 工具调用规则

1. **Hermes 只能通过工具集读写**，不直接 `import` 业务服务
2. **"写"动作仅限生成提案**（写入 `agent_proposals`），不直接改主配置
3. **正式改规则仍走人工审批接口**：`POST /api/agent/proposals/{id}/approve`
4. 每个工具调用记录到 `agent_runs` 的 `tool_calls` 字段，可审计

---

## 5. 自主进化闭环

### 5.1 四阶段闭环

```
  ┌───────────┐      ┌───────────┐      ┌───────────┐      ┌───────────┐
  │  Observe  │ ───▶ │ Diagnose  │ ───▶ │  Propose  │ ───▶ │  Review   │
  │  观察采集  │      │  问题诊断  │      │  生成提案  │      │  人工审批  │
  └───────────┘      └───────────┘      └───────────┘      └─────┬─────┘
       ▲                                                         │
       │                    反馈写入 agent_feedback                │
       └─────────────────────────────────────────────────────────┘
```

### 5.2 Observe — 观察采集

定时抓取系统快照，写入 `agent_observations`：

```json
{
  "timestamp": "2026-04-14T15:30:00+08:00",
  "metrics": {
    "funnel_candidate_count": 12,
    "funnel_focus_count": 3,
    "funnel_buy_count": 1,
    "notice_candidate_count": 49,
    "notice_focus_count": 1,
    "notice_buy_count": 0,
    "kline_sync_success_rate": 0.998,
    "kline_sync_elapsed_sec": 45.2,
    "hot_concepts_top3": ["AI PC", "宁德时代概念", "BC电池"],
    "strategy_config_hash": "a3f7c2..."
  }
}
```

### 5.3 Diagnose — 问题诊断

Hermes 运行诊断逻辑判断问题类型：

| 问题类型 | 触发条件 | 诊断方向 |
|---------|---------|---------|
| 候选池过空 | `candidate_count == 0` 连续 3 个交易日 | 参数过严？数据源异常？ |
| 候选池过满 | `candidate_count > 50` | 参数过松？需要提高门槛 |
| 转化率过低 | `focus→buy` 转化率 < 5% 持续一周 | 买入条件过严？评分权重偏差？ |
| 公告高分低效 | 高分公告股次日跌幅 > 3% 超过 30% | 关键词权重偏差？利好已兑现？ |
| 追高信号过多 | `penalty_gap_up` 触发率 > 40% | 高开惩罚或买入阈值是否需要复核？ |
| 同步异常 | 失败率 > 5% 或耗时 > 300s | 网络问题？接口限流？ |

### 5.4 Propose — 生成提案

产出结构化提案写入 `agent_proposals`：

```json
{
  "type": "rule_patch",
  "title": "建议调整买入评分阈值",
  "risk_level": "medium",
  "reasoning": "过去 5 个交易日候选池为空，当前 buy_score_threshold=78 可能过严。同期市场波动率上升，建议适度下调后观察。",
  "diff": {
    "buy_score_threshold": { "from": 78, "to": 74 }
  },
  "expected_impact": "候选池预计增加 5-10 只",
  "confidence": 0.72,
  "evidence": [
    "连续 5 日候选池为空",
    "市场 20 日波动率从 12% 上升到 18%",
    "放宽至 0.22 后回测命中率下降 < 3%"
  ]
}
```

### 5.5 Review — 人工审批

前端展示提案，人工操作：

| 操作 | 效果 |
|------|------|
| **批准** | 将 `diff` 应用到 `StrategyConfig`，记录 `agent_feedback` |
| **驳回** | 标记驳回原因，记录 `agent_feedback`，Hermes 学习避免重复建议 |
| **暂缓** | 标记为 `deferred`，下次复盘时重新评估 |
| **转研发** | 标记为 `dev_task`，提醒开发者关注 |

### 5.6 触发频率

| 任务 | 触发时间 | 说明 |
|------|---------|------|
| 盘后复盘 | 每日 15:30 | 当日策略表现回顾、参数效果评估 |
| 公告复盘 | 每日 21:00 | 公告选股效果回顾、关键词命中分析 |
| 周度进化报告 | 每周日 20:00 | 本周累积观察、趋势、综合建议 |
| 异常诊断 | 即时触发 | 接口连续失败、同步失败、数据源异常 |
| 手动触发 | API 调用 | `POST /api/agent/run` |

---

## 6. 数据模型与持久化

### 6.1 v1 精简表结构

v1 新增 3 张表（复用现有 `data/funnel_state.db`），不引入新数据库：

#### agent_tasks

合并"运行记录"与"观察快照"，每次 Hermes 运行即一次观察：

```sql
CREATE TABLE IF NOT EXISTS agent_tasks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type     TEXT NOT NULL,        -- daily_review / notice_review / weekly_evolution / anomaly / manual
    trigger       TEXT NOT NULL,        -- scheduled / manual / anomaly
    status        TEXT NOT NULL,        -- running / success / failed / timeout
    input_summary TEXT,                 -- 输入摘要（JSON）
    output_summary TEXT,                -- 输出摘要（JSON）
    observations  TEXT,                 -- 本次采集的指标快照（JSON）
    tool_calls    TEXT,                 -- 工具调用记录（JSON array）
    error_message TEXT,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    elapsed_ms    INTEGER,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_agent_tasks_type ON agent_tasks(task_type);
CREATE INDEX IF NOT EXISTS idx_agent_tasks_created ON agent_tasks(created_at);
```

#### agent_proposals

核心表，存储所有提案：

```sql
CREATE TABLE IF NOT EXISTS agent_proposals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id       INTEGER REFERENCES agent_tasks(id),
    type          TEXT NOT NULL,        -- rule_patch / notice_rule_patch / experiment / code_suggestion / alert
    title         TEXT NOT NULL,
    risk_level    TEXT NOT NULL,        -- low / medium / high / critical
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending / approved / rejected / deferred / dev_task
    reasoning     TEXT,                 -- LLM 推理过程
    diff_payload  TEXT,                 -- 建议变更（JSON）
    expected_impact TEXT,
    confidence    REAL,
    evidence      TEXT,                 -- 证据列表（JSON array）
    approved_by   TEXT,
    approved_at   TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_agent_proposals_status ON agent_proposals(status);
CREATE INDEX IF NOT EXISTS idx_agent_proposals_type ON agent_proposals(type);
```

#### agent_feedback

闭环必须 — 人工采纳/驳回记录，用于 Hermes 后续学习：

```sql
CREATE TABLE IF NOT EXISTS agent_feedback (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id   INTEGER NOT NULL REFERENCES agent_proposals(id),
    action        TEXT NOT NULL,        -- approve / reject / defer / dev_task
    note          TEXT,                 -- 人工备注
    outcome       TEXT,                 -- 应用后的实际效果（后续填写）
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_agent_feedback_proposal ON agent_feedback(proposal_id);
```

### 6.2 存储原则

| 原则 | 说明 |
|------|------|
| 结果主存 JSON | `observations`、`diff_payload`、`evidence` 等用 JSON 字符串，便于快速落地 |
| 核心索引字段结构化 | `task_type`、`status`、`risk_level`、`type` 等为独立列，支持高效筛选 |
| 不引入向量库 | v1 只用 SQLite 记忆；v2 若需 RAG 再升级 |
| 与业务表隔离 | `agent_*` 表独立，不混入 `funnel_state` / `notice_state` |

### 6.3 v2 扩展表（预留设计，v1 不实现）

- `agent_experiments`：实验定义、实验窗口、对照组、结果摘要
- `agent_reports`：独立报告表（v1 中报告作为 proposal 的特殊类型处理）

---

## 7. Prompt 工程

### 7.1 System Prompt 模板

```
你是 Hermes，Alpha 量化选股系统的内嵌投研代理。

你的职责是：
1. 观察系统运行数据（漏斗状态、公告筛选结果、策略参数、K线同步质量）
2. 诊断问题（候选池过空/过满、转化率异常、参数偏差、数据质量问题）
3. 产出结构化优化提案（参数建议、关键词调整、实验设计）

你的约束是：
- 你只能建议，不能直接修改系统配置
- 所有建议必须包含理由、证据、预期影响和置信度
- 你的输出必须是严格的 JSON 格式

当前系统状态会通过工具调用提供给你，不要凭空推测数据。
```

### 7.2 任务 Prompt 结构

每种任务类型有固定的 prompt 模板：

#### 盘后复盘 (daily_review)

```
## 任务：盘后复盘

### 今日系统快照
{funnel_snapshot}

### 当前策略参数
{strategy_profile}

### K线同步状态
{kline_sync_status}

### 热门概念
{hot_concepts}

### 请你完成：
1. 评估今日策略表现（候选池质量、转化率、筛选效率）
2. 识别参数可能存在的偏差
3. 如果发现问题，产出参数调整建议

### 输出格式
{output_schema}
```

#### 公告复盘 (notice_review)

```
## 任务：公告复盘

### 今日公告筛选结果
{notice_snapshot}

### 公告关键词规则
{notice_rules}

### 请你完成：
1. 评估公告筛选的命中质量
2. 分析关键词类型的命中分布
3. 如果发现权重偏差，产出调整建议

### 输出格式
{output_schema}
```

### 7.3 输出 Schema 示例

所有 Hermes 输出统一为 JSON：

```json
{
  "summary": "今日候选池正常，但追高信号偏多",
  "diagnosis": [
    {
      "issue": "追高信号触发率偏高",
      "severity": "medium",
      "detail": "penalty_gap_up 在 42% 的候选中触发，高于 30% 的健康阈值",
      "metric_value": 0.42,
      "healthy_range": [0.1, 0.3]
    }
  ],
  "proposals": [
    {
      "type": "rule_patch",
      "title": "建议提高高开惩罚权重",
      "risk_level": "medium",
      "diff": {
        "penalty_gap_up": { "from": 8, "to": 10 }
      },
      "reasoning": "当前高开惩罚不足以过滤追高候选...",
      "expected_impact": "减少 ~15% 的惩罚触发",
      "confidence": 0.68,
      "evidence": ["连续 3 日惩罚率 > 40%", "同期大盘涨幅温和"]
    }
  ],
  "observations": {
    "candidate_count": 12,
    "focus_count": 3,
    "buy_count": 1,
    "penalty_trigger_rate": 0.42
  }
}
```

### 7.4 LLM 配置

| 配置项 | v1 方案 |
|--------|--------|
| 模型 | 通过 `HERMES_MODEL` 环境变量配置，默认 `gpt-4o-mini`；支持 `gpt-4o`、`deepseek-chat` 等 |
| API Key | 复用 `OPENAI_API_KEY`；若未设置则 Hermes 整体降级为离线模式 |
| Token 预算 | 盘后复盘 ~2000 tokens / 公告复盘 ~1500 tokens / 周度报告 ~4000 tokens |
| 降级策略 | 无 API Key → 仅执行 Observer（纯指标采集），不执行 Analyst/Evolver |
| 请求超时 | 单次 LLM 调用 30s；超时记录失败，不重试 LLM 本身 |

---

## 8. API 接口设计

所有 Hermes API 以 `/api/agent/` 为前缀，与现有 API 风格保持一致。

### 8.1 接口总览

| 方法 | 路径 | 说明 | 优先级 |
|------|------|------|--------|
| GET | `/api/agent/status` | Hermes 运行状态、最近运行摘要 | P0 |
| POST | `/api/agent/run` | 手动触发任务 | P0 |
| GET | `/api/agent/proposals` | 提案列表（支持按 status/type 筛选） | P0 |
| GET | `/api/agent/proposals/{id}` | 提案详情 | P0 |
| POST | `/api/agent/proposals/{id}/approve` | 批准提案 | P0 |
| POST | `/api/agent/proposals/{id}/reject` | 驳回提案 | P0 |
| GET | `/api/agent/tasks` | 运行记录列表 | P1 |
| GET | `/api/agent/observations` | 观察指标时间序列 | P1 |
| WS | `/ws/agent` | Hermes 状态变化推送 | P2 |

### 8.2 关键接口详细设计

#### GET /api/agent/status

```json
{
  "running": false,
  "last_run": {
    "task_type": "daily_review",
    "status": "success",
    "finished_at": "2026-04-14T15:32:18+08:00",
    "elapsed_ms": 8234,
    "proposals_generated": 2
  },
  "stats": {
    "total_proposals": 15,
    "pending_proposals": 3,
    "approved_proposals": 8,
    "rejected_proposals": 4
  },
  "llm_available": true,
  "next_scheduled": {
    "task_type": "notice_review",
    "scheduled_at": "2026-04-14T21:00:00+08:00"
  }
}
```

#### POST /api/agent/run

请求体：

```json
{
  "task_type": "daily_review",
  "params": {}
}
```

`task_type` 可选值：`daily_review` / `notice_review` / `weekly_evolution` / `anomaly` / `full_diagnosis`

#### GET /api/agent/proposals

查询参数：`status=pending&type=rule_patch&limit=20&offset=0`

```json
{
  "items": [
    {
      "id": 7,
      "task_id": 12,
      "type": "rule_patch",
      "title": "建议放宽箱体振幅上限",
      "risk_level": "medium",
      "status": "pending",
      "confidence": 0.72,
      "created_at": "2026-04-14T15:32:18+08:00"
    }
  ],
  "total": 3
}
```

#### POST /api/agent/proposals/{id}/approve

请求体：

```json
{
  "note": "市场波动加大，同意放宽"
}
```

效果：
1. 将 `diff_payload` 应用到 `StrategyConfig`
2. 更新 `agent_proposals.status = 'approved'`
3. 写入 `agent_feedback`
4. 通过 WebSocket 推送变更事件

### 8.3 WebSocket 事件格式（P2）

```json
{
  "event": "agent_proposal_created",
  "data": {
    "proposal_id": 7,
    "type": "rule_patch",
    "title": "建议放宽箱体振幅上限",
    "risk_level": "medium"
  }
}
```

事件类型：`agent_task_started` / `agent_task_completed` / `agent_proposal_created` / `agent_proposal_approved` / `agent_proposal_rejected`

---

## 9. 前端交互设计

### 9.1 v1 最小化方案

新增左侧导航 **Agent** 页签（第 5 个 tab），包含：

```
┌─────────────────────────────────────────────────┐
│  Agent                                           │
│  Hermes 运行状态: ✅ 正常 · 上次运行: 15:32      │
├─────────────────────────────────────────────────┤
│                                                  │
│  📋 待审批提案 (3)                                │
│  ┌────────────────────────────────────────────┐  │
│  │ 🟡 建议放宽箱体振幅上限                      │  │
│  │   类型: rule_patch · 风险: 中等              │  │
│  │   置信度: 72% · 2026-04-14 15:32            │  │
│  │   [✅ 批准]  [❌ 驳回]  [📖 详情]            │  │
│  └────────────────────────────────────────────┘  │
│  ┌────────────────────────────────────────────┐  │
│  │ 🟢 建议调高公告分红权重                      │  │
│  │   类型: notice_rule_patch · 风险: 低         │  │
│  │   置信度: 81% · 2026-04-14 21:05            │  │
│  │   [✅ 批准]  [❌ 驳回]  [📖 详情]            │  │
│  └────────────────────────────────────────────┘  │
│                                                  │
│  📊 最近运行记录                                  │
│  ┌────────────────────────────────────────────┐  │
│  │ 盘后复盘  ✅ 成功  15:32  8.2s  2个建议      │  │
│  │ 公告复盘  ✅ 成功  21:05  5.1s  1个建议      │  │
│  │ 异常诊断  ⚠️ 跳过  --     --    无异常       │  │
│  └────────────────────────────────────────────┘  │
│                                                  │
│  [🔄 手动触发复盘]                                │
└─────────────────────────────────────────────────┘
```

### 9.2 与现有页面联动（v2）

| 页面 | 联动内容 |
|------|---------|
| 公告选股 | 高分股票旁显示 `🤖 Hermes 分析: 业绩预增叠加行业景气` |
| 策略选股 | 漏斗顶部显示 `ℹ️ Hermes 发现: 连续 3 日候选池为空` |

---

## 10. 稳定性与隔离

### 10.1 资源隔离矩阵

| 维度 | Hermes | 主系统 |
|------|--------|--------|
| 并发 | 最多 2 个任务 | 无限制 |
| CPU | LLM 调用走 thread pool | 事件循环主线程 |
| 数据库 | `agent_*` 表 | `funnel_state` / `notice_state` 等 |
| 失败影响 | 仅 agent 功能降级 | 不受影响 |

### 10.2 异常处理流程

```
Hermes 任务异常
  ├── 超时 (>120s)
  │     → 取消任务，记录 status=timeout
  │     → 不影响主系统
  │
  ├── LLM 调用失败
  │     → 降级为纯指标采集（Observer only）
  │     → 记录 error_message，不生成提案
  │
  ├── 工具调用失败
  │     → 跳过该工具，用缺省值
  │     → 降低本次提案置信度
  │
  └── 连续 3 次失败
        → 熔断：该类型任务暂停 1 小时
        → 生成 alert 类型提案通知人工
```

### 10.3 监控指标

| 指标 | 健康阈值 | 告警 |
|------|---------|------|
| 任务成功率 | > 90% | 连续 3 次失败 |
| 平均耗时 | < 30s | 单次 > 120s |
| 提案采纳率 | > 30%（v2 关注） | < 10% 持续两周 |
| LLM 可用性 | > 95% | 连续 5 次 API 失败 |

---

## 11. 分阶段实施计划

### Phase 1 — 基础骨架（1-2 周）

- [ ] 创建 `hermes_memory.py`：3 张 SQLite 表（`agent_tasks` / `agent_proposals` / `agent_feedback`）
- [ ] 创建 `hermes_runtime.py`：任务调度框架、超时/熔断、P0 工具集
- [ ] API：`/api/agent/status`、`/api/agent/proposals`、`/api/agent/proposals/{id}/approve|reject`
- [ ] 前端：Agent 页签、提案列表、审批按钮
- [ ] 集成到 `main.py` lifespan

### Phase 2 — 复盘与提案（1-2 周）

- [ ] 实现 `daily_review` 任务：盘后自动复盘
- [ ] 实现 `notice_review` 任务：公告复盘
- [ ] Prompt 模板调优
- [ ] 提案批准自动应用到 `StrategyConfig`
- [ ] Toast 通知 + 提案数 badge

### Phase 3 — 进化闭环（1-2 周）

- [ ] 实现 `weekly_evolution` 任务
- [ ] 异常即时诊断
- [ ] `agent_feedback` 反馈回路（驳回原因影响后续建议）
- [ ] 观察指标趋势图
- [ ] P1 工具集完善

### Phase 4 — 增强联动（v2）

- [ ] 公告/漏斗页 Hermes 解释联动
- [ ] 实验中心
- [ ] WebSocket 推送
- [ ] P2 工具集

---

## 12. 验收场景

### 12.1 观察链路

- [ ] Hermes 能读取漏斗、公告、同步日志等现有模块数据
- [ ] 观察数据写入 `agent_tasks.observations`
- [ ] Hermes 任务执行失败不影响主页面 `/api/funnel`、`/api/notice/*` 响应

### 12.2 提案链路

- [ ] Hermes 能生成参数建议与公告关键词建议
- [ ] 提案持久化到 `agent_proposals`，可通过 API 查询
- [ ] 人工批准后自动应用参数变更到 `StrategyConfig`
- [ ] 驳回/采纳记录写入 `agent_feedback`

### 12.3 自主进化闭环

- [ ] 每日 15:30 自动生成盘后复盘报告
- [ ] 当候选池连续 3 日为空时，能自动产出规则优化建议
- [ ] Hermes 不会直接改动策略配置，所有变更必须经过审批

### 12.4 前端联动

- [ ] Agent 页签可查看提案列表、状态、详情
- [ ] 提案可一键批准/驳回
- [ ] 运行记录可查看历史任务

### 12.5 稳定性与隔离

- [ ] Hermes 长任务运行时，`/api/funnel`、`/api/notice/*`、`/api/kline/*` 正常响应
- [ ] LLM 超时时 Hermes 降级为 Observer only，不影响主系统
- [ ] 连续失败触发熔断，自动暂停并告警

---

## 13. 假设与约束

| 假设 | 说明 |
|------|------|
| Hermes 运行时 | 使用 Hermes agent/runtime 体系，非从零自建 |
| v1 不自动改代码 | 只做"自动研究 + 自动提案 + 人工审批" |
| v1 不引入新依赖 | 继续基于 FastAPI + SQLite，不加消息队列或新数据库 |
| 非通用聊天入口 | Hermes 是面向投研和策略进化的专用 agent |
| LLM 可选 | 无 API Key 时降级为纯指标采集模式 |
| 文档先行 | 本文档落仓库后，按 Phase 分步实现 |
