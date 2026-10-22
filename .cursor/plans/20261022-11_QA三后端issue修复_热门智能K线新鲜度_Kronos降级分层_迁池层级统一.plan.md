# QA 三后端 issue 修复：热门智能 K 线新鲜度 / Kronos 降级分层 / 迁池层级统一

> 承接 [20261022-10_打板roadmap分批实施.plan.md](./20261022-10_%E6%89%93%E6%9D%BFroadmap%E5%88%86%E6%89%B9%E5%AE%9E%E6%96%BD.plan.md) 与
> [20261022-10_选股池页面功能实测与缺陷分级.plan.md](./20261022-10_%E9%80%89%E8%82%A1%E6%B1%A0%E9%A1%B5%E9%9D%A2%E5%8A%9F%E8%83%BD%E5%AE%9E%E6%B5%8B%E4%B8%8E%E7%BC%BA%E9%99%B7%E5%88%86%E7%BA%A7.plan.md)。
> 分支：`fix/qa-hot-ai-pool-consistency`（基于 origin/main），只修 issue #51 / #52 / #56，不触碰 #50/#53/#54/#55/#57 前端改动。

## 背景 / 目标

- **#51（P1）**：`hot_stock_ai_service` 把「实时价」与「陈旧本地 K 线窗口」混算，产出 `距MA20 +77%` 类跨期指标；快照 `trade_date` 取 K 线最后日期，掩盖缓存落后。
- **#52（P2）**：Kronos 不可用/无预测结果时 `prediction_score` 恒 0，绝对阈值（focus 11.5 / buy 14.5）实际不可达，重点/买入池静默恒空。
- **#56（P3）**：策略池 `/api/pool/move` 与公告池 `/api/notice/pool/move` 迁池校验口径不一致：策略侧只限买入池容量、允许任意层级跳转；公告侧完全无层级/容量校验；失败 HTTP 语义不同（200 vs 400）。

目标：
1. 决策时点统一：实时交易日为分析基准；K 线陈旧超过容忍窗口→跳过并计入 `skipped_stale`；仅差当日且实时 bar 可验证→追加后统一计算；快照暴露 `kline_max_date` / `realtime_trade_date` / `stale_kline_count` / 每只 `stale_kline`。
2. 无预测分量时启用明确的降级分层（分位数），有预测时保持绝对阈值向后兼容；快照暴露 `prediction_enabled` / `prediction_available` / `pred_missing_count`。
3. 抽取共享 `pool_transition` 校验器：候选→重点→买入逐级晋升、降级与同级幂等允许、跨级晋升一致 4xx；`/api/pool/move` 与 `/api/notice/pool/move` 使用同一语义、消息与 trigger reason。

## 任务分解（checklist）

- [ ] #51 新增 `app/services/trading_day_utils.py`：交易日窗口计算（含无日历时的工作日近似回退）。
- [ ] #51 `_analyze_symbol`：新鲜度决策（lag==0 正常 / lag==1 且实时 bar 可验证→追加 / 否则 `StaleKlineError`→`skipped_stale`）。
- [ ] #51 快照/meta：`trade_date` 改为决策交易日；`kline_max_date`、`realtime_trade_date`、`stale_kline_count`、`skipped_stale`、每 entry `stale_kline`/`realtime_bar_appended`/`kline_max_date`。
- [ ] #51 单测：陈旧 K 线+实时价不再输出 +77% 类指标（跳过 & 追加两条路径）。
- [ ] #52 meta：`prediction_enabled` / `prediction_available` / `pred_missing_count`；`_build_pools` 分位数降级模式；`pool_tier_mode`。
- [ ] #52 单测：有/无 Kronos 两种三池结果。
- [ ] #56 新增 `app/services/pool_transition.py`（纯函数校验器 + 统一消息/reason）。
- [ ] #56 `funnel_service.move_pool`、`notice_service.move_pool` 接入校验器；买入池容量口径统一；trigger reason 统一。
- [ ] #56 `market.py /pool/move` 失败返回 400（与 notice 路由一致）。
- [ ] #56 测试：validator 单测 + 两个服务级用例 + TestClient API 级（合法/非法/容量/幂等）。
- [ ] README / CHANGELOG 对应后端说明更新。
- [ ] `python3 -m pytest -q` 全量通过（`test_api_regression` 依赖本机 18890 已运行服务；不重启共享服务）。
- [ ] 小步 commit，push `fix/qa-hot-ai-pool-consistency` 分支（不合并 main，由主 agent 串行接线）。

## 验收标准

- #51：给定 K 线截止 D-30、实时价来自今日的样本，该股不出现在 entries（进 `skipped_stale`）或基于追加 bar 计算且 `dist_ma20` 与卡片价格同基期；断言不存在 `+77%` 类混期指标。
- #52：`use_kronos` 关闭或预测全缺失时，重点/买入池非空（若存在 ≥3 只达标股），meta 可见 `pool_tier_mode="quantile_degraded"` 与原因；有预测时分池结果与旧逻辑一致。
- #56：`candidate→buy` 在两个端点都返回 4xx 且 detail 文案一致；`focus→buy`、降级、同级幂等成功；非法目标池 4xx；买入池满返回一致的"买入池已满"消息。
- API 兼容：只新增字段，不删除既有字段；请求体结构不变。

## 风险与回滚

- 现有 `tests/test_hot_stock_ai_service.py` fixture 的 K 线日期为静态历史区间，新鲜度校验会将其判为陈旧 → 通过给 FakeProvider/服务注入可控"决策交易日"更新 fixture，不放松生产逻辑。
- `/api/pool/move` 从 200+success:false 改为 400：前端 `request()` 已按 4xx throw+alert 处理（funnel.js movePool catch），e2e 只 mock 成功路径；风险低。回滚仅需还原 market.py 一处。
- 公告侧新增买入池容量限制（与策略侧同源 5 只）可能拒绝既有的直接移入操作——属于 issue 明确要求的一致性修复。

## 关键决策记录

1. 陈旧处理采用「跳过 + 可验证追加」双路径（issue 建议 1+2），不做「仅 warning 继续混算」。
2. 追加 bar 的可验证判据：`latest_price/(1+change_pct/100)` 与 K 线最后收盘偏差 ≤ 2%；合成 bar 仅用于价格/均线/振幅类因子，量能类因子保持用最近一条真实 K 线并注明基期，不伪造成交额。
3. 降级分层用分位数（top 10% 买入 / top 30% 重点，候选阈值做质量下限），只在"完全无预测数据"时启用，保持有预测时绝对分数语义完全不变。
4. 迁池校验只作用于手动迁移端点；引擎自动升/降池（`apply_transition_rules`）本身已逐级（candidate→focus→buy），无需改动 strategy_engine。
5. 并发边界：不触碰 `app/main.py`、`app/models.py`、`app/container.py`、`app/dependencies.py`、Tushare/Kline/migration、`limit_board*`、前端与 e2e。
