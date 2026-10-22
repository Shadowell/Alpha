# Tushare 数据接入扩展与 Runner 自动部署

## 背景 / 目标

Alpha 当前只把 Tushare 用于个股日线、交易日历、股票名称和全市场日线快照；估值、涨跌停价、复权因子、资金流、指数基准和分钟线仍缺失。参考 `StockPro` 的 Tushare 数据分层落库方式，在 Alpha 现有 SQLite + 同步框架内扩展四类数据，并补齐数据中心可观测性与查询 API。

同时，Alpha 尚未接入自动部署。目标是在 tokyo（64.83.43.61）注册 `alpha-production` 自托管 Runner，新增 `deploy.yml` + `deploy/deploy.sh`，实现 push main 后自动同步代码、构建镜像、重启容器并做健康检查，再由主 agent 验证线上真实响应。

关联计划：`20261022-00_部署Alpha到tokyo服务器.plan.md`（服务器/Nginx/DNS 基础准备）。

## 任务分解（checklist）

- [x] 1. 扩展 `TushareFirstMarketDataClient`：daily_basic、stk_limit、adj_factor、moneyflow、moneyflow_hsgt、index_daily、rt_min、realtime_quote，统一代码/日期/单位
- [x] 2. 扩展 `KlineSQLiteStore`：新增 `kline_daily_extras`、`moneyflow_daily`、`market_capital_flow`、`index_daily`、`kline_minute`、`enrichment_sync_state` 六张表及 upsert/查询/覆盖概览
- [x] 3. `KlineCacheService`：日线同步封账后按交易日同步增强数据（失败隔离）；新增盘中买入池分钟线同步
- [x] 4. `app/main.py` / `routers/kline.py`：启动分钟线循环，新增查询与增强状态 API，新增手动触发增强同步
- [x] 5. 数据中心页面：新增“数据增强覆盖”卡片（最新日期/行数/状态/分钟线时间）
- [x] 6. 测试：客户端归一化、存储、同步失败隔离、API 回归；运行完整 pytest
- [x] 7. 本地真实验证：用 2026-10-21 跑增强同步，核对行数/抽样值，启动本地服务验证 API/页面
- [x] 8. 部署链路：新增 `.github/workflows/deploy.yml` 与 `deploy/deploy.sh`；tokyo 注册并常驻 `alpha-production` Runner
- [ ] 9. 推送 main，观察 Runner 部署，修复失败直至健康检查通过
- [ ] 10. 线上验证增强数据、API 与页面；按 AGENTS.md 更新 README 并提交推送

## 实施中发现并修复的问题

- **补数日期格式缺陷（阻断性）**：`_date_settled` 与同步入口直接用 `YYYYMMDD` 与 `date().isoformat()` 做字符串比较，
  `"20260901" > "2026-10-22"` 恒成立，导致所有紧凑日期的增量/增强同步被判为"未收盘定型"静默跳过，
  接口仍返回 202 且队列正常执行，日志无任何异常。修复：入口统一 `_normalize_trade_date`，
  并补充紧凑日期回归用例；线上验证 2026-09-01 单日补齐 5015 只。
- **队列不可观测**：`_enqueue_job` / `_run_queue` 原先无日志，`progress` 接口不返回队列状态，
  导致上述跳过问题难以定位。修复：补齐入队/执行/结束日志，`progress` 暴露 `queue_size`、`syncing`、`queue_task`。
- **enrichment-sync 状态码**：该接口同步执行并返回结果，原先标 `202 Accepted` 语义错误，改为 200。
- **WebSocket 冒烟误报**：`curl` 默认 HTTP/2 下 `Upgrade` 头无效，`/ws/realtime` 返回 404；
  加 `--http1.1` 后为 101。已写入 `deploy/README.md` 避免误判。

## 部署前置条件核对（tokyo）

- DNS：`alpha.notenap.com → 64.83.43.61` 已生效（8.8.8.8 与服务器本机一致）
- 证书：`certbot` 已有 `alpha.notenap.com`，有效期至 2027-01-19
- Nginx：`sites-enabled/alpha`（`127.0.0.1:8461` + Basic Auth + `/ws/realtime` 升级）与 SNI map 条目均已存在
- 数据：`alpha_data` 命名卷已含 5,193,787 行 K 线（截至 2026-08-24），本次按 36 个交易日逐日补齐至 2026-10-21
- 磁盘/内存：部署时 `/` 余 22G、可用内存约 4G，容器实测 240MiB

## 验收标准

- `daily_basic`/`stk_limit`/`adj_factor` 按交易日落库，最新交易日行数接近全市场股票数
- `moneyflow` 与 `moneyflow_hsgt`、4 个基准指数日线可查询
- 买入池分钟线在交易时段每分钟入库，非交易时段不拉取；失败不阻塞主 K 线同步
- 新 API 返回结构稳定，`enrichment-status` 能反映每个数据集的最新日期/行数/状态
- 数据中心页面能显示增强数据覆盖，且不影响原有健康度/任务/日志展示
- 完整 pytest 通过；本地服务启动后真实接口返回数据
- push main 后 GitHub Actions 在自托管 Runner 上成功部署，服务器容器 healthy，`http://127.0.0.1:18888/` 返回 200
- 不再新增除 `127.0.0.1:18888` 以外的公网端口

## 风险与回滚

- **Tushare 频率限制**：日频每类 1 次/交易日、指数 4 次、分钟线约 5 次/分钟，余量充足；失败只记录状态不重试风暴
- **SQLite 写入**：所有新表写操作放到 `asyncio.to_thread`，复用现有 WAL + checkpoint 规范
- **失败隔离**：增强同步异常不得影响日线封账；每个数据集独立 try/except 并写 `enrichment_sync_state`
- **数据口径**：沿用 Alpha 现有单位（成交量=手、成交额=元）；Tushare 万元/千元字段统一换算，避免与旧 K 线混用
- **分钟线漏拍**：60 秒轮询 + `(symbol, bar_time)` 唯一键 upsert；非交易时段直接跳过
- **部署回滚**：`docker compose down` 保留 `alpha_data` 卷；Runner 部署失败保留上一镜像；代码回滚用 `git revert` + push 触发重新部署
- **共享服务器**：只新增 Alpha 容器、Runner 和卷，不触碰现有服务与站点

## 关键决策记录

- 不引入 PostgreSQL：沿用 Alpha 的 SQLite 单文件存储和现有同步/封账逻辑
- 估值/涨跌停/复权合并为 `kline_daily_extras`（同为 symbol×trade_date 1:1），资金流、北向、指数、分钟线独立建表
- 日频增强只在“最新交易日增量/自动同步”路径执行，历史初始化不回填，避免一次初始化放大数百次 API 调用
- 分钟线只覆盖买入池（最多 5 只），不做全市场实时；`realtime_quote` 仅作 `rt_min` 失败时的单点兜底
- 部署采用 Runner 在服务器本机执行 rsync + docker compose，不经过 GitHub 托管机 SSH
- 交易日一律在同步入口归一化为 ISO 后再比较与落库，API 继续接受 `YYYYMMDD` 与 `YYYY-MM-DD` 两种入参
- 本地 `./start.sh` 仅用于开发验证；交付以 tokyo 线上为准（并行会话补数期间停本地服务，避免占用 18890 干扰其测试）
