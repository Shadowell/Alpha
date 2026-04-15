# Alpha — 自进化量化选股系统

Alpha 是一个面向 A 股市场的量化选股 Web 平台，集成 **[Kronos](https://arxiv.org/abs/2508.02739) 金融 K 线基础模型**进行短期走势预测，将多维度数据筛选、实时评分、AI 价格预测与漏斗管理整合为一体，帮助投资者从数千只股票中高效发现潜力标的。

## 核心功能

| 模块 | 说明 |
|------|------|
| **大盘总览** | 热门概念 Top10 + 热门个股 Top30，每 10 秒自动刷新实时行情 |
| **数据中心** | K 线缓存管理，全量补缺 / 增量同步 / 完整性检查 / 任务历史 |
| **策略选股** | 盘后自动筛选调整期未突破股票，三池漏斗管理（候选池 → 重点池 → 买入池） |
| **公告选股** | 抓取当日公告 → 规则打分（+可选 LLM 打分）→ 关键词标签过滤 |
| **智能监控 / 进化** | Hermes Agent 定时推送市场动态 + 提案管理，系统自进化闭环 |
| **模拟盘** | 从买入池一键模拟买入，实时计算持仓盈亏，支持费用设置 |
| **Kronos 预测** | 金融 K 线基础模型预测未来多日 OHLC，嵌入选股和监控全流程 |
| **MCP 工具集** | 通过 MCP Server 向 Hermes Agent 暴露 Kronos 预测、实时行情等工具 |

---

## 界面预览

### 1. 大盘总览

> 一眼掌握市场情绪和热点方向。

![大盘总览](docs/screenshots/01-market.png)

**功能详解：**

- **市场情绪面板**：展示上涨概念占比、龙头股及涨幅、市场情绪等级（偏强/中性/偏弱）
- **热门概念 Top10**：按概念热度排序，展示热度得分、涨停/上涨/下跌家数、领涨个股及涨幅、漏斗入选数
- **热门个股 Top30**：展示热度排名的个股，点击可查看 K 线图和 Kronos 预测
- **实时轮询**：每 10 秒自动刷新概念和个股行情数据
- **WebSocket 推送**：服务端主动推送漏斗快照和行情更新

### 2. 数据中心

> K 线数据的健康管理中枢。

![数据中心](docs/screenshots/02-data.png)

**功能详解：**

- **数据健康仪表盘**：环形图展示覆盖率百分比，健康状态（完整/轻微缺失/严重缺失）
- **KPI 指标卡**：股票数、K 线总条数、最早/最新日期、数据库大小
- **同步操作**：全量补缺（智能检测缺失日期×股票的交叉补全）、增量同步（仅补当日）、完整性检查
- **任务历史**：分页展示所有同步任务，支持按状态筛选（全部/成功/失败/运行中）
- **数据完整性报告**：按日期展示缺失分布，最差个股排名
- **自动同步**：每日 15:20 自动触发并发同步，完成后飞书群通知

### 3. 策略选股

> 三池漏斗模型，系统化管理从发现到买入的全流程。

![策略选股](docs/screenshots/03-funnel.png)

**功能详解：**

- **三池漏斗结构**：
  - **调整期候选池**：盘后自动筛选处于调整期、尚未突破的股票
  - **重点关注池**：评分 ≥ 65 或手动升级的标的，进入重点关注
  - **买入池**：评分 ≥ 80 或手动确认的最终标的（上限 5 只）
- **概念筛选**：按概念板块过滤候选池，聚焦特定赛道
- **盘后筛选**：一键执行策略引擎，自动评分和入池
- **个股卡片**：展示评分、放量比、突破位、涨跌幅，支持池间迁移操作
- **右侧面板**：点击个股展示 30 日 K 线 + Kronos 预测叠加图
- **自动降级**：买入池个股评分连续 5 分钟 < 65 自动降至重点池

### 4. 公告选股

> 利好公告驱动的事件型选股。

![公告选股](docs/screenshots/04-notice.png)

**功能详解：**

- **公告抓取**：自动获取当日全部上市公司公告
- **双打分引擎**：规则打分（关键词匹配 + 权重加分）+ 可选 LLM 打分（大模型深度理解）
- **7 类关键词标签**：业绩预增、高额分红、股份回购、重大合同、资产重组、融资获批、产品突破
- **三池管理**：公告候选池 → 公告重点池 → 公告买入池
- **公告详情**：点击个股展示公告原文摘要、打分理由、风险提示
- **右侧 K 线**：同样嵌入 30 日 K 线 + Kronos 预测

### 5. 智能监控 / 进化

> Hermes Agent 驱动的自进化智能体系统。

![智能监控](docs/screenshots/05-agent.png)

**功能详解：**

- **双 Tab 页面**：智能监控 + 提案管理
- **智能监控**（详见下文 Hermes Agent 章节）：
  - 可配置定时间隔（5/10/15/30 分钟），LLM 自动分析市场主线
  - 消息流按主线分框展示，每条消息含执行摘要、主线分析、关注个股
  - 个股标签支持悬停 K 线预览（含 Kronos 预测）和点击展开详情
  - 支持手动触发和编辑系统提示词
- **提案管理**：
  - Hermes Agent 产出的策略优化提案（参数调整、规则变更等）
  - 逐条审批（批准/驳回），批准后自动应用参数变更
  - 形成"监控 → 发现问题 → 产出提案 → 审批应用 → 效果反馈"的自进化闭环

### 6. 模拟盘

> 零风险验证选股策略的效果。

![模拟盘](docs/screenshots/06-paper.png)

**功能详解：**

- **账户总览**：总资产、持仓市值、浮动盈亏、已实现盈亏、总费用、最大回撤
- **统计指标**：持仓数、交易数、胜率、胜/负比
- **当前持仓**：成本价、现价、数量、市值、盈亏金额及百分比，一键模拟卖出
- **已平仓历史**：完整的历史交易表格，含买卖价格和实现盈亏
- **成交记录**：所有买卖操作的时间戳和费用明细
- **费用设置**：佣金费率、最低佣金、印花税率、滑点比例均可自定义
- **自动刷新**：盘中 10 秒轮询，盘后 60 秒轮询

---

## Hermes Agent — 自进化智能体

### 架构概览

Hermes Agent 是 Alpha 系统的"大脑"，它以 LLM 为核心决策引擎，通过 MCP（Model Context Protocol）工具协议与 Alpha 系统深度集成，实现从**市场感知 → 分析推理 → 策略建议 → 自动执行**的完整闭环。

```
┌─────────────────────────────────────────────────┐
│                  Hermes Agent                    │
│                                                  │
│  ┌──────────┐   ┌──────────┐   ┌──────────────┐ │
│  │  定时调度  │──▶│ 数据采集  │──▶│  LLM 推理    │ │
│  │ (5~30min) │   │(MCP工具)  │   │(市场分析)    │ │
│  └──────────┘   └──────────┘   └──────┬───────┘ │
│                                       │         │
│                    ┌──────────────────┘         │
│                    ▼                             │
│  ┌──────────┐   ┌──────────────┐                │
│  │ 消息推送  │◀──│  结构化输出   │                │
│  │(WebSocket)│   │(主线/个股)   │                │
│  └──────────┘   └──────┬───────┘                │
│                         │                        │
│                         ▼                        │
│                 ┌──────────────┐                 │
│                 │  提案生成     │                 │
│                 │(策略优化建议) │                 │
│                 └──────┬───────┘                 │
│                         │                        │
│                         ▼                        │
│                 ┌──────────────┐                 │
│                 │  人工审批     │                 │
│                 │ 批准 → 自动  │                 │
│                 │ 应用参数变更  │                 │
│                 └──────────────┘                 │
└─────────────────────────────────────────────────┘
```

### 智能监控工作流

1. **定时触发**：按配置间隔（默认 10 分钟）自动执行，或手动触发
2. **数据采集**：通过 MCP 工具集收集实时数据
   - 热门概念 Top10 + 热门个股（板块动向）
   - 漏斗池中所有个股的实时行情和评分
   - 关注池个股的 **Kronos 预测**（未来 3 日 OHLC 走势）
   - 公告漏斗关键词和入池个股
3. **LLM 分析**：将市场数据 + 系统提示词发送给 LLM，生成结构化分析报告
   - 按"主线"组织（如"医药链共振"、"电力设备链"等），每条主线含确信度等级（高/中/低）
   - 每条主线下列出关注个股及买卖建议
   - 标注风险因素和失效条件
4. **消息推送**：通过 WebSocket 实时推送到前端，按主线卡片式展示
5. **提案产出**：根据市场变化，Agent 可自动产出策略优化提案

### MCP 工具集

Hermes Agent 通过 Alpha MCP Server 调用以下工具，确保基于真实数据分析，禁止 LLM 凭空编造预测数据：

| 工具 | 功能 | 应用场景 |
|------|------|---------|
| `get_funnel` | 获取当前漏斗三池状态 | 了解系统当前关注标的 |
| `get_hot_concepts` | 获取热门概念排行 | 感知市场板块热点 |
| `get_hot_stocks` | 获取热门个股排行 | 发现资金聚集方向 |
| `get_stock_detail` | 获取个股详情 + K 线 | 深入分析具体标的 |
| `get_realtime_quote` | 获取盘中实时行情 | 监控个股价格变动 |
| `get_kronos_prediction` | 调用 Kronos 预测 | **AI 走势预判，辅助决策** |
| `get_notice_funnel` | 获取公告漏斗状态 | 了解事件驱动信号 |
| `get_kline_stats` | K 线缓存统计 | 数据健康感知 |
| `trigger_eod_screen` | 触发盘后筛选 | 主动执行策略更新 |
| `create_proposal` | 创建优化提案 | 输出策略改进建议 |

### 自进化闭环

```
   监控发现市场变化
         │
         ▼
   LLM 分析产出洞察
         │
         ▼
   生成策略优化提案
   (如: "放量阈值 1.5→2.0")
         │
         ▼
   用户审批（批准/驳回）
         │  ✅ 批准
         ▼
   自动应用参数变更
         │
         ▼
   记忆桥接记录反馈
   (效果追踪 → 下轮优化)
```

这使得 Alpha 不仅是一个被动的筛选工具，而是一个能够**观察市场 → 独立思考 → 提出改进 → 持续进化**的智能系统。

---

## Kronos 金融预测模型

### 关于 Kronos

[Kronos](https://huggingface.co/NeoQuasar/Kronos-base) 是首个开源的金融 K 线基础模型（Foundation Model），由清华大学团队发布（[论文](https://arxiv.org/abs/2508.02739)），在全球 45 个交易所超过 120 亿条 K 线数据上预训练。它将连续的 OHLCV 金融数据视为一种"语言"，通过专用 tokenizer 将 K 线量化为离散 token 序列，再用自回归 Transformer 学习时间序列的深层模式。

```
历史 K 线 ──Tokenizer──▶ 离散 token 序列 ──Transformer 自回归──▶ 未来 token ──Decoder──▶ 预测 K 线
  (OHLCV)    (量化编码)      (上下文建模)           (逐步生成)    (反量化)     (OHLC)
```

### Kronos 在 Alpha 中的应用

Kronos 不是一个孤立的预测接口，而是**深度嵌入了 Alpha 系统的每个环节**：

| 应用场景 | 说明 |
|---------|------|
| **策略选股右侧面板** | 点击任意个股卡片，右侧面板展示 30 日历史 K 线 + Kronos 未来 3 日预测 K 线叠加图 |
| **公告选股右侧面板** | 同上，公告驱动型选股也叠加 Kronos 预测辅助判断 |
| **热门个股 K 线弹窗** | 大盘页面点击热门个股，弹窗展示 K 线 + Kronos 预测，实时对比 |
| **Hover K 线浮窗** | 监控消息流中的个股标签，悬停即展示 K 线 + Kronos 3 日预测及涨跌预判 |
| **Hermes Agent 数据源** | Agent 通过 MCP 工具 `get_kronos_prediction` 获取 Kronos 预测数据，融入 LLM 分析 |
| **预测 vs 实际对比** | 预测 K 线区间叠加当日盘中实时 K 线，直观对比预测准确度 |

### 预测可视化

预测 K 线在图表中以**黄色半透明虚线框**展示，与历史 K 线无缝衔接。如果当日已有实际行情数据，会同时叠加实际 K 线进行对比，让用户直观感受预测效果。

### 模型系列

| 模型 | 参数量 | Tokenizer | 上下文长度 | HuggingFace |
|------|--------|-----------|-----------|-------------|
| Kronos-mini | 4.1M | [Kronos-Tokenizer-2k](https://huggingface.co/NeoQuasar/Kronos-Tokenizer-2k) | 2048 | [NeoQuasar/Kronos-mini](https://huggingface.co/NeoQuasar/Kronos-mini) |
| Kronos-small | 24.7M | [Kronos-Tokenizer-base](https://huggingface.co/NeoQuasar/Kronos-Tokenizer-base) | 512 | [NeoQuasar/Kronos-small](https://huggingface.co/NeoQuasar/Kronos-small) |
| **Kronos-base** | **102.3M** | [Kronos-Tokenizer-base](https://huggingface.co/NeoQuasar/Kronos-Tokenizer-base) | **512** | [NeoQuasar/Kronos-base](https://huggingface.co/NeoQuasar/Kronos-base) |
| Kronos-large | 499.2M | Kronos-Tokenizer-base | 512 | 尚未公开 |

Alpha 当前默认使用 **Kronos-base**（102.3M 参数），可在 `app/services/kronos_predict_service.py` 中切换。

### 集成架构

```
用户点击股票卡片 / Hermes Agent 调用 MCP 工具
       │
       ▼
GET /api/predict/{symbol}/kronos?lookback=30&horizon=3
       │
       ▼
KronosPredictService（惰性加载、异步锁、串行推理）
  ├── 从 K 线缓存读取历史 OHLCV
  ├── 交易日历推算未来交易日
  ├── Tokenizer 编码 → Transformer 自回归推理 → Decoder 解码
  └── 返回历史 + 预测 K 线合并序列
       │
       ▼
前端 ECharts 渲染（历史实线 + 预测黄色虚线框 + 实时对比线）
```

集成要点：

- **惰性加载**：模型在首次预测请求时从 HuggingFace Hub 下载并加载，不阻塞服务启动
- **设备自适应**：自动检测 CUDA → MPS（Apple Silicon）→ CPU，优先使用 GPU 加速
- **异步隔离**：推理通过 `asyncio.to_thread` 在线程池执行，不阻塞 FastAPI 事件循环
- **交易日推算**：基于 AkShare 交易日历自动推算预测对应的真实交易日，跳过周末和节假日

### 预测 API

```
GET /api/predict/{symbol}/kronos?lookback=30&horizon=3
```

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `symbol` | path | — | 股票代码（如 `000001`） |
| `lookback` | query | 30 | 历史 K 线天数（10-200） |
| `horizon` | query | 3 | 预测天数（1-10） |

### Benchmark

以下是 Kronos 系列模型在 A 股日 K 预测场景下的性能实测。

#### 硬件环境

| 项目 | 规格 |
|------|------|
| 设备 | MacBook Pro (Mac16,7) |
| 芯片 | Apple M4 Pro, 14 核 (10P + 4E) |
| 内存 | 24 GB 统一内存 |
| 推理设备 | MPS (Metal Performance Shaders) |
| 操作系统 | macOS 15.1 (24B2083) |

#### 推理性能 × sample_count 对比

| 模型 | SC=1 | SC=10 | SC=20 | SC=50 | SC=100 |
|------|------|-------|-------|-------|--------|
| **Kronos-mini** (4.1M) | **0.15s** | 0.87s | 0.80s | 0.65s | 0.65s |
| **Kronos-small** (24.7M) | 0.18s | 0.61s | 0.59s | 0.61s | 0.71s |
| **Kronos-base** (102.3M) | 0.27s | 0.80s | 0.76s | 0.87s | 1.34s |

#### 模型选择建议

- 快速预览 → Kronos-mini + SC=1（0.15s 极速响应）
- 日常使用 → Kronos-base + SC=20（0.76s，质量与速度平衡）
- 高精度场景 → Kronos-base + SC=100（1.34s，方差最低）

```bash
python -m tests.benchmark_kronos  # 复现测试
```

---

## 技术栈

- **后端**：Python 3.11+ / FastAPI / Uvicorn
- **预测模型**：[Kronos](https://huggingface.co/NeoQuasar/Kronos-base)（PyTorch, HuggingFace Hub）
- **智能体**：Hermes Agent（本地 CLI `hermes chat -q`）+ MCP 工具协议
- **数据源**：同花顺（热门概念/个股）、新浪（实时行情 fallback）、AkShare（公告、K线历史）
- **存储**：SQLite（`data/funnel_state.db` 状态/持仓/交易 + `data/market_kline.db` K 线缓存）
- **前端**：原生 HTML/CSS/JS + ECharts（K 线图 + 预测可视化）、Glassmorphism 设计风格
- **通知**：飞书 Webhook（同步完成推送）

## 项目结构

```
Alpha/
├── app/
│   ├── main.py                    # FastAPI 入口、后台调度循环、全部 API 路由
│   ├── config.py                  # StrategyConfig（策略参数配置）
│   ├── models.py                  # Pydantic 数据模型
│   ├── mcp_server.py              # Alpha MCP Server（向 Hermes Agent 暴露工具）
│   ├── routers/
│   │   └── kline.py               # K 线相关路由
│   ├── services/
│   │   ├── kronos_predict_service.py  # Kronos 预测服务（惰性加载、异步推理）
│   │   ├── kronos_model/          # Kronos 模型实现（Tokenizer + Transformer + Predictor）
│   │   ├── funnel_service.py      # 策略选股漏斗核心逻辑
│   │   ├── notice_service.py      # 公告选股 & 规则/LLM 打分
│   │   ├── paper_trading.py       # 模拟盘交易服务（持仓/交易/费用计算）
│   │   ├── hermes_runtime.py      # Hermes Agent 运行时（智能监控 tick + 提案管理）
│   │   ├── hermes_memory.py       # Hermes Agent 记忆持久化
│   │   ├── hermes_memory_bridge.py # Hermes 记忆桥接（审批反馈 → 记忆）
│   │   ├── kline_cache_service.py # K 线并发同步调度
│   │   ├── kline_store.py         # K 线 SQLite 存储
│   │   ├── strategy_engine.py     # 盘后策略评分引擎
│   │   ├── data_provider.py       # 多数据源适配层（同花顺/新浪/AkShare）
│   │   ├── realtime.py            # WebSocket 实时推送
│   │   ├── concept_engine.py      # 概念板块评分引擎
│   │   ├── time_utils.py          # 交易日/时段工具函数
│   │   └── feishu_notify.py       # 飞书 Webhook 通知
│   └── static/
│       ├── index.html             # 主页面（6 Tab 单页应用）
│       ├── app.js                 # 前端核心逻辑（路由/渲染/轮询/ECharts）
│       └── styles.css             # Glassmorphism 风格样式
├── docs/screenshots/              # 界面截图
├── tests/
│   ├── benchmark_kronos.py        # Kronos 模型 benchmark 脚本
│   └── ...
├── start.sh / stop.sh / restart.sh
└── requirements.txt
```

## 快速开始

### 安装依赖

```bash
pip3 install -r requirements.txt
```

### 启动服务

```bash
./start.sh
```

打开浏览器访问 http://127.0.0.1:18888

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PORT` | `18888` | 服务端口 |
| `HOST` | `0.0.0.0` | 监听地址 |
| `RELOAD` | `0` | 热重载（开发模式设为 `1`） |
| `OPENAI_API_KEY` | — | 可选，启用公告 LLM 打分和智能监控 |

### 服务管理

```bash
./start.sh      # 启动（后台运行）
./stop.sh       # 停止
./restart.sh    # 重启（每次代码修改后必须执行）
```

日志文件：`logs/server.log`

---

## API 接口

### Kronos 预测

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/predict/{symbol}/kronos?lookback=30&horizon=3` | Kronos K 线预测 |

### 大盘行情

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/market/hot-concepts?trade_date=YYYY-MM-DD` | 热门概念 Top10 |
| GET | `/api/market/hot-stocks?trade_date=YYYY-MM-DD` | 热门个股 Top30 |

### 策略选股

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/funnel?trade_date=YYYY-MM-DD` | 获取漏斗状态 |
| POST | `/api/jobs/eod-screen` | 执行盘后筛选 |
| POST | `/api/pool/move` | 股票迁移池 |
| GET | `/api/stock/{symbol}/detail` | 个股详情（含 K 线） |

### 公告选股

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/notice/funnel` | 公告漏斗状态 |
| GET | `/api/notice/keywords` | 关键词标签列表 |
| POST | `/api/jobs/notice-screen?keywords=分红,回购` | 执行公告筛选 |
| POST | `/api/notice/pool/move` | 公告股票迁移池 |
| GET | `/api/notice/{symbol}/detail` | 公告个股详情 |

### 智能监控（Hermes Agent）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/agent/status` | Agent 运行状态 |
| POST | `/api/agent/run` | 触发 Agent 执行（复盘/诊断） |
| GET | `/api/agent/proposals` | 提案列表 |
| POST | `/api/agent/proposals/{id}/approve` | 批准提案 |
| POST | `/api/agent/proposals/{id}/reject` | 驳回提案 |
| GET | `/api/agent/monitor/config` | 智能监控配置 |
| POST | `/api/agent/monitor/config` | 更新监控配置 |
| GET | `/api/agent/monitor/messages` | 监控消息流 |
| POST | `/api/agent/monitor/trigger` | 手动触发监控 |

### K 线缓存

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/kline/{symbol}?days=30` | 获取个股 K 线 |
| POST | `/api/jobs/kline-cache/sync` | 全量补缺同步 |
| POST | `/api/jobs/kline-cache/incremental-sync` | 增量同步 |
| POST | `/api/jobs/kline-cache/check` | 完整性检查 |
| GET | `/api/jobs/kline-cache/stats` | 数据库统计 |

### 模拟盘

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/paper/buy` | 模拟买入 |
| POST | `/api/paper/sell` | 模拟卖出 |
| GET | `/api/paper/positions` | 当前持仓 |
| GET | `/api/paper/history` | 历史持仓 |
| GET | `/api/paper/summary` | 盈亏汇总 |
| GET/POST | `/api/paper/settings` | 费用设置 |

### 实时推送

| 方法 | 路径 | 说明 |
|------|------|------|
| WS | `/ws/realtime` | WebSocket 实时数据（漏斗快照 + 监控推送） |

## License

MIT
