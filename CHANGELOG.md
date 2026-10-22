# Changelog

本文件记录 Alpha 的重要版本变化。版本号遵循 [Semantic Versioning](https://semver.org/)。

## [Unreleased]

### Added

- Bilingual README overview (English + 中文) with contribution / security entry points.
- CONTRIBUTING.md, SECURITY.md, GitHub Issue templates, and PR template for OSS maintainership.
- `app/services/paths.py`：以项目根目录解析数据/日志绝对路径，支持 `ALPHA_DATA_DIR` / `ALPHA_LOG_DIR` 覆盖，供 DB、备份与 shutdown checkpoint 复用（#21）。
- `app/services/blocking_io.py`：外部 AkShare/Tushare 调用统一超时、有界独立线程池、熔断与降级计数（#9）。
- `app/services/job_store.py` / `jobs.py`：通用任务持久化、进度、去重、超时和重启恢复；Agent、策略扫描、回测默认返回 `202 + job_id`（#15）。
- `/api/health`、`/api/metrics`、结构化日志与 request ID；compose 健康检查从首页改为真实依赖检查（#18）。
- `app/services/deployment_guard.py`：显式区分 loopback/public 暴露面，public + TRUST_EDGE + 无写令牌时启动 fail-fast（#10）。
- `app/services/sqlite_migrations.py`：`PRAGMA user_version` 启动迁移；funnel v2 持久化 `hot_stocks_updated_at`，market v2 覆盖 Tushare 增强表（#16 #21）。
- `app/services/limit_up_backtest.py`：打板可成交性回测引擎（纯服务，未接 HTTP/API）——T 日涨停排队/次日竞价开盘买入、一字与停牌买不到建模、跌停封死/停牌卖出顺延、T+1、佣金万2.5最低5/印花税万5/过户费/动态滑点、单票参与率容量与容量曲线、净值口径绩效（年化/回撤/Sharpe/Sortino/Calmar/胜率/盈亏比/费用占比/溢价指标）、指数与等权全市场基准（缺数显式 skip）、regime/板型/市值/连板高度分层、strict/loose 双口径对照、与旧 BacktestLab 差异纯函数报告、seed 可复现与 JSON 持久化（#26）。详见 `docs/issue-26-limit-up-backtest.md`。
- `app/services/error_response.py`：5xx 仅返回通用消息与 error id，异常细节写服务端日志（#19）。
- CI：ruff lint/format、scoped mypy、pytest coverage 门禁、Playwright E2E workflow、Dependabot 和精确依赖 pin（#13）。
- 前端安全模块、22 个 classic script 模块与统一轮询调度器；两个页面增加 CSP（#8 #12）。
- `app/services/trading_day_utils.py`：交易日窗口计算（日历缺失时按工作日近似回退），用于热门智能分析的 K 线新鲜度判定（#51）。
- `app/services/pool_transition.py`：策略池 / 公告池共享的手动迁池层级校验器（逐级晋升、跨级非法、统一文案与 trigger reason）（#56）。
- `app/services/sentiment_factors.py` + `app/services/market_regime.py`：**市场情绪周期与连板梯队日频因子族（#25，纯计算层）**。计算口径：
  - 因子表按 `trade_date` 一行为粒度，覆盖广度（涨停/跌停/涨跌家数/中位收益/强弱势/成交额比）、连板梯队（最高连板、2/3/4+ 家数、首板/续板数、1→2/2→3/3→4 晋级率、梯队完整度与集中度）、赚钱/亏钱效应（炸板率、大面率、昨日涨停今日开/收/高溢价均值与中位数）、指数与资金（指数 1/5/20d 收益、20d 波动、北向透传）；`main_theme_count`/`theme_diffusion`/`margin_balance_change` 为 #8/#1 预留占位（缺失即 None）。
  - **严格 PIT**：`D` 行只使用 `<= D` 的数据；"昨日涨停今日溢价"按观察日记账（cohort `D-1` 的溢价写入 `D` 行），行在当日收盘后即可一次写死，历史不随后续数据改写；回测窗口与增量跑批逐行一致（幂等）。
  - **盘中隔离**：`compute_intraday_estimates`（kline_minute/rt_min）只输出 `intraday_` 前缀键；收盘 regime 的规则输入由 `market_regime.RULE_INPUT_FIELDS` 白名单锁死，盘中估算在结构上无法污染收盘标签。
  - **口径参数化**：`market_scope`（all/sh_sz/main/sh_main/sz_main/chinext/star/bse）、`include_st`（默认剔除，按涨停池当日名称优先）、`exclude_new_days`（默认剔除上市不足 5 个交易日的新股）；停牌/退市尾部以 `halted_or_missing_count` 报表列呈现；分母为 0 或上游缺数一律 None + `missing_flags` 标注，绝不填 0。
  - **regime**：连续 `sentiment_score`（0–100，绝对锚点分段线性，桶内缺失重归一）+ 六阶段规则链（冰点/修复/发酵/高潮/分歧/退潮，可注入 `regime_rules`/`regime_params`，默认阈值为全周期稳定值并含确定性兜底）。
  - 持久化（`market_sentiment_daily` 建表）、`/api/market/sentiment/*` 与前端卡片属后续接线范围，不在本次计算层提交内。

### Removed

- 移除未被调用的全表扫描快照入口 `FunnelService._pick_screen_snapshot` / `KlineCacheService.build_snapshot_for_screen`，避免误接入时对 500 万行级 K 线全表扫描（#20）。
- 旧版打板脚本 `strategy/daban/` 归档到 `legacy/daban/`（`git mv` 保留历史），运行时不再引用（#20）。

### Changed

- `app/main.py` 从 972 行缩到约 189 行，只保留装配、中间件和 lifespan；业务路由按 market/predict/strategy/notice/agent/paper/system/jobs/ws 拆分（#11）。
- 所有写接口增加 Pydantic 请求模型与参数边界；非法数量、费率、规则参数、扫描/回测参数返回 422 而非 500（#14）。
- Kronos 增加 TTL/LRU 缓存、可配置 `sample_count`、同 key single-flight 与专用推理线程池；状态接口暴露缓存命中率（#17）。
- 前端公告/板块/个股/LLM 文本统一转义，外链仅允许 http(s)，恶意标题与 `javascript:` URL 有 Playwright 回归（#8 #12）。
- 时间戳统一使用 aware Asia/Shanghai；`hot_stocks_updated_at` 可持久化并在重启后恢复；checkpoint/备份路径不再依赖启动 cwd（#21）。
- Docker 运行用户降为 uid/gid 10001，root entrypoint 仅修复挂载权限后通过 gosu 启动应用；compose 注释与安全边界同步（#19 #10）。
- 写令牌使用 `hmac.compare_digest` 常量时间比较（#19）。
- 候选→重点、重点→买入按 README 规则自动迁移，手动迁池仍可覆盖；自动进入买入池受容量限制且日志区分 manual/auto（#48）。
- 热门股票智能分析（#51 #52）：快照 `trade_date` 改为「决策交易日」（`meta.realtime_trade_date`），逐股暴露 `kline_max_date` / `stale_kline` / `realtime_bar_appended` / `amount_base_date`；Kronos 无预测分量时三池改用「排名分位数（买入 top 10%、重点 top 30%）+ 候选阈值下限」降级分层，`meta.prediction_enabled` / `prediction_available` / `pred_missing_count` / `pool_tier_mode` 可见；有预测时保持原绝对阈值（8 / 11.5 / 14.5）语义不变。
- 手动迁池层级统一（#56）：新增 `app/services/pool_transition.py` 共享校验器，`POST /api/pool/move` 与 `POST /api/notice/pool/move` 同语义——晋升必须逐级（候选→重点→买入）、跨级晋升与非法池名返回一致 400（detail 文案相同）、降级与同级迁移幂等成功、买入池容量口径同源（5 只）；`/api/pool/move` 失败从「200 + success:false」改为 400，两侧 trigger 日志统一记录 `手动迁移: <from> -> <to>`。

### Fixed

- GET `/api/funnel`、hot panels、stock detail 不再因历史 `trade_date` 重置当前三池；历史读取仅返回空快照/404（#46）。
- 策略选股复合候选池的卡片和迁池按钮在 DOM 重渲染后恢复事件绑定，新增 Playwright 回归（#47）。
- 自定义策略扫描快照持久化到 `funnel_state.db`，重启后恢复，并用 `stale` / `latest_data_date` 标识旧快照（#49）。
- 外部数据源无响应时占满 asyncio 默认线程池、拖垮 API/Kronos 的可靠性问题（#9）。
- enrichment 路由依赖全局 service、容器化后返回 500 的集成缺口（#11）。
- Tushare 客户端补回 `asyncio` 导入，避免增强数据调用被异常吞掉后返回空结果（#9 #11）。
- 热门股票智能分析不再把实时价与陈旧本地 K 线混算评分（#51）：K 线恰好落后一个交易日且实时价昨收口径可对账（偏差 ≤2%）时追加可验证的当日实时 bar 后统一计算均线/振幅；落后超过容忍窗口或对账失败时整只跳过并计入 `meta.skipped_stale`（含 `stale_kline` 标记与 `stale_kline_count`），杜绝「今日 +10% 与 距MA20 +77%」同卡的跨期指标。
- Kronos 未加载/无预测结果时重点池、买入池静默恒空的问题（#52）：无预测分量时启用可解释的分位数降级分层，不再用含 prediction 上限贡献的绝对阈值判层。

## [0.1.0] - 2026-07-22

### Added

- 首次正式发布 Alpha 自进化量化选股系统。
- 提供 A 股三池漏斗选股、Kronos K 线预测、公告分析、热门股智能分析、Hermes Agent 监控和模拟交易能力。
- 增加标准 MIT 开源许可证与 GitHub Release 版本入口。

### Changed

- 将 README 中 TradingAgents 的本机文件链接替换为可公开访问的 GitHub 仓库链接。

[0.1.0]: https://github.com/Shadowell/Alpha/releases/tag/v0.1.0
