# Alpha — 量化选股漏斗系统

Alpha 是一个面向 A 股市场的量化选股 Web 平台，集成 **[Kronos](https://arxiv.org/abs/2508.02739) 金融 K 线基础模型**进行短期走势预测，将多维度数据筛选、实时评分、AI 价格预测与漏斗管理整合为一体，帮助投资者从数千只股票中高效发现潜力标的。

## 核心功能

| 模块 | 说明 |
|------|------|
| **大盘总览** | 热门概念 Top10 + 热门个股 Top10，每 10 秒自动刷新实时行情 |
| **策略选股** | 盘后自动筛选调整期未突破股票，三池漏斗管理（候选池 → 重点池 → 买入池），支持一键执行/停止 |
| **公告选股** | 抓取当日公告 → 规则打分（+可选 LLM 打分）→ 关键词标签过滤（分红/回购/重组等），支持一键执行/停止 |
| **Kronos 预测** | 基于金融 K 线基础模型，历史日 K 自回归生成未来多日 OHLC 预测 K 线；预测 K 线红涨绿跌虚线框展示，叠加盘中实时 K 线对比 |
| **自进化智能体** | 集成 Hermes Agent，支持盘中监控（定时推送市场动态，按主线分框展示消息流，含关注池与 K 线预览）和提案管理（双 Tab 页面） |
| **模拟盘** | 从买入池一键模拟买入，实时计算持仓盈亏；支持滑点、印花税、手续费等费用设置 |
| **K 线缓存** | 每日 15:20 自动同步主板股票日 K（并发调度），同步完成飞书群通知 |
| **实时推送** | WebSocket 实时推送概念行情与个股评分更新 |
| **MCP 工具集** | 通过 Alpha MCP Server 向 Hermes Agent 暴露 Kronos 预测、实时行情等工具，禁止 LLM 编造预测数据 |

---

## Kronos 金融预测模型

### 关于 Kronos

[Kronos](https://huggingface.co/NeoQuasar/Kronos-base) 是首个开源的金融 K 线基础模型（Foundation Model），由清华大学团队发布（[论文](https://arxiv.org/abs/2508.02739)），在全球 45 个交易所超过 120 亿条 K 线数据上预训练。它将连续的 OHLCV 金融数据视为一种"语言"，通过专用 tokenizer 将 K 线量化为离散 token 序列，再用自回归 Transformer 学习时间序列的深层模式。

```
历史 K 线 ──Tokenizer──▶ 离散 token 序列 ──Transformer 自回归──▶ 未来 token ──Decoder──▶ 预测 K 线
  (OHLCV)    (量化编码)      (上下文建模)           (逐步生成)    (反量化)     (OHLC)
```

### 模型系列

Kronos 提供从轻量到大规模的一系列模型，适配不同算力场景：

| 模型 | 参数量 | Tokenizer | 上下文长度 | HuggingFace |
|------|--------|-----------|-----------|-------------|
| Kronos-mini | 4.1M | [Kronos-Tokenizer-2k](https://huggingface.co/NeoQuasar/Kronos-Tokenizer-2k) | 2048 | [NeoQuasar/Kronos-mini](https://huggingface.co/NeoQuasar/Kronos-mini) |
| Kronos-small | 24.7M | [Kronos-Tokenizer-base](https://huggingface.co/NeoQuasar/Kronos-Tokenizer-base) | 512 | [NeoQuasar/Kronos-small](https://huggingface.co/NeoQuasar/Kronos-small) |
| **Kronos-base** | **102.3M** | [Kronos-Tokenizer-base](https://huggingface.co/NeoQuasar/Kronos-Tokenizer-base) | **512** | [NeoQuasar/Kronos-base](https://huggingface.co/NeoQuasar/Kronos-base) |
| Kronos-large | 499.2M | Kronos-Tokenizer-base | 512 | 尚未公开 |

Alpha 当前默认使用 **Kronos-base**（102.3M 参数），可在 `app/services/kronos_predict_service.py` 中切换。

### 集成架构

```
用户点击股票卡片
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
前端 ECharts 渲染弹窗：历史 K 线 + 预测 K 线（黄色半透明区域）
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

返回示例（简化）：

```json
{
  "symbol": "000001",
  "model": "Kronos-base",
  "device": "mps",
  "lookback": 30,
  "horizon": 3,
  "history_kline": [ { "date": "2026-04-11", "open": 11.05, "close": 11.07, "type": "history" } ],
  "predicted_kline": [ { "date": "2026-04-14", "open": 11.06, "close": 11.09, "type": "predicted" } ],
  "merged_kline": [ "...history + predicted..." ],
  "prediction_start_index": 30
}
```

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

#### 软件环境

| 依赖 | 版本 |
|------|------|
| Python | 3.11.14 |
| PyTorch | 2.2.2 |
| NumPy | 1.26.4 |
| Pandas | 2.2.2 |
| 模型来源 | HuggingFace Hub (首次运行自动下载) |

#### 测试数据

| 项目 | 说明 |
|------|------|
| 标的 | 000001 平安银行 |
| 数据源 | AkShare 日 K 缓存 (SQLite) |
| 时间范围 | 2026-03-02 ~ 2026-04-13 (30 根日 K) |
| 输入字段 | date / open / high / low / close / volume / amount |
| 历史窗口 | lookback = 30 |
| 预测天数 | horizon = 5 |
| 最后收盘价 | 11.07 (2026-04-13) |

#### 采样参数

| 参数 | 值 | 说明 |
|------|-----|------|
| T (Temperature) | 1.0 | 采样温度，越高随机性越大 |
| top_p | 0.9 | Nucleus 采样概率阈值 |
| top_k | 0 | 不启用 top-k 截断 |
| sample_count | 1 / 10 / 20 / 50 / 100 | 多路径采样数，取均值输出；数量越多方差越低 |

#### 测试方法

每个模型独立加载，预热推理 1 次后，对每个 `sample_count` 档位执行 3 次有效推理，取耗时均值。模型之间完全释放内存。

#### 模型加载耗时

| 模型 | 参数量 | 加载耗时 |
|------|--------|---------|
| **Kronos-mini** | 4.1M | 1.90s |
| **Kronos-small** | 24.7M | 2.17s |
| **Kronos-base** | 102.3M | 3.55s |
| Kronos-large | 499.2M | — (未公开) |

#### 推理性能 × sample_count 对比

| 模型 | SC=1 | SC=10 | SC=20 | SC=50 | SC=100 |
|------|------|-------|-------|-------|--------|
| **Kronos-mini** (4.1M) | **0.15s** | 0.87s | 0.80s | 0.65s | 0.65s |
| **Kronos-small** (24.7M) | 0.18s | 0.61s | 0.59s | 0.61s | 0.71s |
| **Kronos-base** (102.3M) | 0.27s | 0.80s | 0.76s | 0.87s | 1.34s |

> 推理耗时为 3 次运行均值。首次从 SC=1 切换到较高 SC 时存在 MPS 编译开销（约 1~2s），后续运行大幅降低。

#### 预测质量 × sample_count 对比

**Kronos-mini (4.1M)**

| sample_count | D1 涨跌 | D5 涨跌 | 波动率 |
|-------------|---------|---------|-------|
| 1 | -0.80% | -2.34% | 1.09% |
| 10 | -0.04% | -0.61% | 1.33% |
| 20 | -0.04% | -0.13% | 1.17% |
| 50 | -0.08% | -0.12% | 1.19% |
| **100** | **-0.06%** | **-0.08%** | **1.19%** |

**Kronos-small (24.7M)**

| sample_count | D1 涨跌 | D5 涨跌 | 波动率 |
|-------------|---------|---------|-------|
| 1 | -0.03% | +2.33% | 1.22% |
| 10 | -0.11% | +0.80% | 1.02% |
| 20 | +0.08% | +0.94% | 1.13% |
| 50 | -0.01% | +0.32% | 1.13% |
| **100** | **-0.06%** | **+0.35%** | **1.15%** |

**Kronos-base (102.3M)**

| sample_count | D1 涨跌 | D5 涨跌 | 波动率 |
|-------------|---------|---------|-------|
| 1 | +0.55% | +0.76% | 0.96% |
| 10 | +0.21% | -0.27% | 1.26% |
| 20 | +0.05% | -0.61% | 1.26% |
| 50 | +0.14% | -0.46% | 1.32% |
| **100** | **+0.10%** | **-0.59%** | **1.30%** |

> - Kronos-large (499.2M) 尚未在 HuggingFace 公开发布，暂无法测试
> - D1/D5 预测涨跌 = (预测收盘价 - 历史最后收盘价) / 历史最后收盘价 × 100%
> - 预测波动率 = 预测期内日均 (high - low) / low × 100%
> - 预测结果含随机采样特性，每次运行会有差异，表中数值代表单次运行结果

#### 结论

- **多路径采样收敛**：随着 sample_count 从 1 增加到 50~100，预测值趋于收敛，D1/D5 偏离幅度显著收窄（mini D5: -2.34% → -0.08%），表明多路径取均值有效降低了随机方差
- **推理耗时线性可控**：SC=1 时三个模型均在亚秒级（0.15s ~ 0.27s）；SC=100 时 mini/small 仍在 0.65~0.71s，base 约 1.34s，日 K 预测场景下完全可接受
- **波动率稳定**：各模型在不同 SC 下预测波动率集中在 0.96% ~ 1.33%，说明模型对波幅的预估相对稳健
- **模型选择建议**：
  - 快速预览 → Kronos-mini + SC=1（0.15s 极速响应）
  - 日常使用 → Kronos-base + SC=20（0.76s，质量与速度平衡）
  - 高精度场景 → Kronos-base + SC=100（1.34s，方差最低）

#### 复现

```bash
python -m tests.benchmark_kronos
```

结果输出到 `tests/benchmark_kronos_results.json`。

---

## 界面预览

### 大盘总览

热门概念 Top10 + 热门个股 Top10，每 10 秒自动刷新实时行情。

![大盘总览](docs/screenshots/market.png)

### 策略选股

三池漏斗管理：调整期候选池 → 重点关注池 → 买入池，支持概念筛选、盘后筛选和一键执行。

![策略选股](docs/screenshots/funnel.png)

### 公告选股

抓取当日公告并智能打分，支持 7 类关键词标签筛选（业绩预增、高额分红、股份回购等）。

![公告选股概览](docs/screenshots/notice.png)

![公告候选池](docs/screenshots/notice-list.png)

### 自进化智能体

Hermes Agent 盘中监控，按主线分框展示消息流（Glassmorphism 卡片），含关注池 K 线预览；支持提案管理（双 Tab 页面）。

### 模拟盘

从买入池一键模拟买入，实时计算持仓盈亏，支持滑点/印花税/手续费参数设置。

## 技术栈

- **后端**：Python 3.11+ / FastAPI / Uvicorn
- **预测模型**：[Kronos](https://huggingface.co/NeoQuasar/Kronos-base)（PyTorch, HuggingFace Hub）
- **智能体**：Hermes Agent（本地 CLI `hermes chat -q`）+ MCP 工具协议
- **数据源**：同花顺（热门概念/个股）、新浪（实时行情 fallback）、AkShare（公告、K线历史）
- **存储**：SQLite（`data/funnel_state.db` 状态/持仓/交易 + `data/market_kline.db` K 线缓存）
- **前端**：原生 HTML/CSS/JS + ECharts（K 线图 + 预测可视化）、Glassmorphism 设计风格
- **通知**：飞书 Webhook（同步完成推送）
- **测试**：pytest
- **CI**：GitHub Actions

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
│   │   ├── hermes_runtime.py      # Hermes Agent 运行时（盘中监控 tick）
│   │   ├── hermes_memory.py       # Hermes Agent 记忆持久化
│   │   ├── hermes_memory_bridge.py # Hermes 记忆桥接
│   │   ├── kline_cache_service.py # K 线并发同步调度
│   │   ├── kline_store.py         # K 线 SQLite 存储
│   │   ├── strategy_engine.py     # 盘后策略评分引擎
│   │   ├── data_provider.py       # 多数据源适配层（同花顺/新浪/AkShare）
│   │   ├── realtime.py            # WebSocket 实时推送
│   │   ├── concept_engine.py      # 概念板块评分引擎
│   │   ├── time_utils.py          # 交易日/时段工具函数
│   │   └── feishu_notify.py       # 飞书 Webhook 通知
│   └── static/
│       ├── index.html             # 主页面（6 Tab：大盘/策略/公告/智能体/模拟盘/数据中心）
│       ├── app.js                 # 前端核心逻辑（路由/渲染/轮询/ECharts）
│       └── styles.css             # Glassmorphism 风格样式
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
| `OPENAI_API_KEY` | — | 可选，启用公告 LLM 打分 |

### 服务管理

```bash
./start.sh      # 启动（后台运行）
./stop.sh       # 停止
./restart.sh    # 重启（每次代码修改后必须执行）
```

日志文件：`logs/server.log`

## API 接口

### Kronos 预测

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/predict/{symbol}/kronos?lookback=30&horizon=3` | Kronos K 线预测 |

### 大盘行情

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/market/hot-concepts?trade_date=YYYY-MM-DD` | 热门概念 Top10 |
| GET | `/api/market/hot-stocks?trade_date=YYYY-MM-DD` | 热门个股 Top10 |

### 策略选股

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/funnel?trade_date=YYYY-MM-DD` | 获取漏斗状态 |
| POST | `/api/jobs/eod-screen` | 执行盘后筛选 |
| POST | `/api/pool/move` | 股票迁移池 |
| POST | `/api/score/recompute` | 重新计算评分 |
| GET | `/api/stock/{symbol}/detail` | 个股详情（含K线） |

### 公告选股

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/notice/funnel` | 公告漏斗状态 |
| GET | `/api/notice/keywords` | 获取关键词标签列表 |
| POST | `/api/jobs/notice-screen?keywords=分红,回购` | 执行公告筛选（支持关键词过滤） |
| POST | `/api/notice/pool/move` | 公告股票迁移池 |
| GET | `/api/notice/{symbol}/detail` | 公告个股详情 |

### K 线缓存

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/kline/{symbol}?days=30` | 获取个股 K 线 |
| POST | `/api/jobs/kline-cache/sync` | 手动触发同步 |
| GET | `/api/jobs/kline-cache/progress` | 同步进度 |
| GET | `/api/jobs/kline-cache/logs` | 同步日志 |

### 实时行情

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/stock/{symbol}/realtime` | 个股盘中实时行情（当日 OHLCV） |

### 自进化智能体（Hermes Agent）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/agent/status` | Agent 运行状态 |
| POST | `/api/agent/run` | 触发 Agent 执行 |
| GET | `/api/agent/proposals` | 提案列表 |
| GET | `/api/agent/proposals/{proposal_id}` | 提案详情 |
| POST | `/api/agent/proposals/{proposal_id}/approve` | 批准提案 |
| POST | `/api/agent/proposals/{proposal_id}/reject` | 拒绝提案 |
| POST | `/api/agent/proposals/create` | 创建提案 |
| GET | `/api/agent/tasks` | Agent 任务列表 |
| GET | `/api/agent/monitor/config` | 盘中监控配置 |
| POST | `/api/agent/monitor/config` | 更新监控配置 |
| GET | `/api/agent/monitor/messages` | 监控消息流 |
| POST | `/api/agent/monitor/trigger` | 手动触发监控 tick |
| POST | `/api/agent/monitor/stop` | 停止监控 |

### 模拟盘

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/paper/buy` | 模拟买入 |
| POST | `/api/paper/sell` | 模拟卖出 |
| GET | `/api/paper/positions` | 当前持仓 |
| GET | `/api/paper/history` | 历史持仓 |
| GET | `/api/paper/summary` | 盈亏汇总 |
| GET | `/api/paper/trades` | 交易记录 |
| GET | `/api/paper/settings` | 费用设置（滑点/印花税/手续费） |
| POST | `/api/paper/settings` | 更新费用设置 |

### 其他

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/strategy/profile` | 策略概要信息 |
| WS | `/ws/realtime` | WebSocket 实时数据推送 |

## 选股策略说明

### 漏斗三池模型

```
全市场 ──筛选宇宙──▶ 候选池 ──评分升级──▶ 重点池 ──确认买入──▶ 买入池（≤5只）
                    (调整期)            (高分标的)            (最终标的)
```

- **候选池**：盘后筛选出处于调整期、尚未突破的股票
- **重点池**：评分 ≥ 65 或手动升级的标的
- **买入池**：评分 ≥ 80 或手动确认，上限 5 只
- **自动降级**：买入池个股评分连续 5 分钟 < 65 自动降至重点池

### 公告关键词筛选

支持 7 类利好关键词标签选择性筛选：

| 标签 | 匹配关键词 |
|------|-----------|
| 业绩预增 | 预增、扭亏、同比增长、大幅增长、预盈 |
| 高额分红 | 分红、派息、现金红利、利润分配、送转、转增 |
| 股份回购 | 回购、增持计划、增持股份、回购股份 |
| 重大合同 | 重大合同、中标、签订、定点、订单、采购协议 |
| 资产重组 | 重组、收购、并购、资产注入、购买资产 |
| 融资获批 | 获批、审核通过、注册生效、获得批复 |
| 产品突破 | 量产、商业化、获准上市、新品发布、投产 |

不选择任何标签时全部类别参与筛选，选中部分标签则仅按选中类别过滤。仅筛选主板股票（沪市 6 开头、深市 00 开头），自动排除 ST。

## 测试

```bash
pytest -q
```

## License

MIT
