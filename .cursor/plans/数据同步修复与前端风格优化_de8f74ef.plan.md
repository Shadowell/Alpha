---
name: 数据同步修复与前端风格优化
overview: 优先修复三个紧急 bug（股票名称丢失、模拟盘不刷新、下单拿不到实时价），根因统一在 get_realtime_snapshot 语义错位；其次修复 K 线同步卡死与覆盖率上不去；最后做数据中心 UI 与前端整体风格优化。
todos:
  - id: realtime_snapshot_fix
    content: 修复 get_realtime_snapshot 语义错位 — 真正调用东财 spot API（根因，影响下单价/模拟盘/股票名称）
    status: completed
  - id: symbol_name_backfill
    content: 修复筛选股票名称丢失 — get_symbol_name_map 冷启动时从 AkShare 拉取并回写 cache
    status: completed
  - id: paper_price_source
    content: 下单走独立实时价通道（主 em_spot，fallback sina_spot，最后 DB）+ cache_ttl 真正生效
    status: completed
  - id: paper_poll_30s
    content: 模拟盘轮询改为盘中 30s / 盘后 10min + init 时若当前 tab=paper 主动启动 + visibilitychange 暂停
    status: completed
  - id: sync_lock_fix
    content: 修复 _syncing 死锁 — 改用 asyncio.Lock + 单个 symbol wait_for 超时
    status: in_progress
  - id: coverage_fix
    content: 修复覆盖率卡 97.55% — 引入 unfillable 集合 + success_trade_date 仅在真正完整时升级
    status: pending
  - id: writer_queue
    content: 解除写库串行锁 — asyncio.Queue + 单 writer 协程批量 upsert
    status: pending
  - id: get_hist_to_thread
    content: data_provider.get_hist DB 读包 asyncio.to_thread 避免阻塞事件循环
    status: pending
  - id: loop_guard_timeout
    content: kline_cache_loop 加重入保护 + check_data_integrity 超时控制
    status: pending
  - id: new_reset_api
    content: 新增 POST /api/jobs/kline-cache/reset 强制重置同步状态
    status: pending
  - id: dc_ui_enhance
    content: 数据中心：缺失构成明细 + 失败重试按钮 + 卡死告警 + 强制重置
    status: pending
  - id: css_tokens
    content: 设计 token 清理（panel 分 3 级、新增 spacing/typography 层级）
    status: pending
  - id: responsive_three_tier
    content: 响应式三档断点（1440/1280/900）+ 大屏两栏布局
    status: pending
  - id: density_optimize
    content: 各页信息密度优化：大盘热门网格、策略紧凑卡、表格统一行高
    status: pending
  - id: echarts_theme
    content: ECharts 色值读取 CSS 变量，主题一致
    status: pending
  - id: verify_restart
    content: 每阶段 ./restart.sh 验证 + git commit/push + 更新 README
    status: pending
isProject: false
---

# 数据同步修复与前端风格优化

## 背景分析

### 三个紧急 bug 的共同根因（新增，最高优先级）

用户新提的三个 bug 看起来独立，实则指向**同一处代码漏洞**：`get_realtime_snapshot` 名字是"实时快照"，实现却完全是"DB 历史快照"。

**根因代码** [app/services/data_provider.py:22-65](app/services/data_provider.py):

```22:65:app/services/data_provider.py
async def get_realtime_snapshot(self, retries=2, retry_wait_seconds=1.0, cache_ttl_seconds=300):
    if self.kline_store is not None:
        try:
            rows = self.kline_store.get_latest_snapshot()   # ← 从 DB 读昨日收盘
            if rows:
                name_map = {}
                if self.symbol_name_cache is not None:
                    _, name_map = self.symbol_name_cache    # ← 冷启动时是空的
                ...
                "名称": name_map.get(s, s),                 # ← fallback 到 symbol 代码
                "最新价": close,                             # ← 实际是昨日 close
```

**连锁后果**：

**Bug 1 — 筛选出股票无名称**
- 冷启动 `symbol_name_cache = None` → `_warmup_name_cache`([funnel_service.py:166](app/services/funnel_service.py)) 从 entries 里找名字，但 entries 是空的 → cache 还是 None
- `get_symbol_name_map`([data_provider.py:353-363](app/services/data_provider.py)) 发现 cache 空直接 `return {}`，**从来不去拉 AkShare**
- 最终所有快照的"名称"列 = 代码本身

**Bug 2 — 模拟盘不刷新**
- `_startPaperPoll`([app.js:1415](app/static/app.js)) 只在 `switchTab('paper')` 时启动
- 如果用户 init 进入时直接就在 paper tab（刷新/书签），`reload()` 走的是 `reloadFunnel()`，paper tab 的 init 路径**不主动启动轮询**
- 频率也不是用户期望的 30s

**Bug 3 — 下单拿不到实时价**
- `_get_realtime_price`([main.py:480-489](app/main.py)) 调 `provider.get_realtime_snapshot(cache_ttl_seconds=5)`
- 如上所示，`get_realtime_snapshot` 根本不请求实时 API，`cache_ttl_seconds` 参数被丢弃
- 盘中下单的"实时价"实际 = 昨日收盘价

### K 线同步问题（原计划保留）

日志反复出现 `[kline-cache] daily sync completed: 同步任务正在执行中` + 覆盖率卡 97.55%。三个叠加 bug：
- `_syncing` 普通 bool 无锁，单 symbol 无超时 → 卡死
- `success_trade_date` 只要有 1 只成功就升级 → 短路了后续补缺
- `asyncio.Lock` 下单写 → 并发 8 实际串行

### 前端风格问题（原计划保留）

设计 token 重复（`--panel`/`--panel-2`/`--card-bg` 三个同色）、响应式仅两档、大屏利用率低、ECharts 色板与 CSS 脱节。

---

## 修复方案

### 第 1 阶段：三个紧急 bug（后端 + 前端协同，最高优先级）

#### 1.1 `get_realtime_snapshot` 语义归位

[app/services/data_provider.py](app/services/data_provider.py)

重写 `get_realtime_snapshot`：根据 `cache_ttl_seconds` 决定策略：
- 盘中（9:30-15:00）→ **优先东财 spot**（`ak.stock_zh_a_spot_em`），TTL 尊重参数
- 盘后 → 允许走 DB 快照 + 长 TTL
- 两者都失败 → 返回 stale cache

伪代码：
```python
async def get_realtime_snapshot(self, *, cache_ttl_seconds=300, prefer_live=None):
    if prefer_live is None:
        prefer_live = _is_trading_hours()
    # 缓存命中判断（尊重 ttl）
    if self.realtime_snapshot_cache:
        ts, cached = self.realtime_snapshot_cache
        if (now - ts).total_seconds() <= cache_ttl_seconds:
            return cached.copy()
    # 拉取 live
    if prefer_live:
        df = await self._fetch_spot_em()
        if not df.empty:
            self.realtime_snapshot_cache = (now, df)
            return df
    # DB fallback（盘后/live 失败）
    df = self._snapshot_from_db()
    ...
```

原"DB 快照"逻辑拆到 `_snapshot_from_db()` 私有方法。

#### 1.2 股票名称冷启动回填

[app/services/data_provider.py:353](app/services/data_provider.py) `get_symbol_name_map`：
- 缓存空或过期时，调 `ak.stock_info_a_code_name()` 拉全市场名称映射
- 写入 `symbol_name_cache` 并持久化到 `kline_store`（新增 `symbol_names` 表或复用现有 meta 表）
- 启动时 `lifespan` 里触发一次预热

同时修改 `_snapshot_from_db`：如果 `name_map` 空，先触发一次 `get_symbol_name_map()`（带超时 5s）。

#### 1.3 下单实时价独立通道

[app/main.py:480](app/main.py) `_get_realtime_price` 改为：
```python
async def _get_realtime_price(symbol: str) -> float:
    df = await provider.get_realtime_snapshot(cache_ttl_seconds=5, prefer_live=True)
    ...
```

并在下单返回体增加 `price_source: "em_live" | "db_fallback" | "stale_cache"` 字段，前端提示用户当前成交价来源。

#### 1.4 模拟盘轮询修复

[app/static/app.js](app/static/app.js)

- `_paperPollInterval()` 改为 **盘中 30000 / 盘后 600000**（10min）
- `init()` 末尾判断 `if (state.activeTab === 'paper') _startPaperPoll()`
- 加 `document.addEventListener('visibilitychange', ...)`：隐藏时 `_stopPaperPoll`，显示时若 tab=paper 重启
- 刷新 hint 显示"上次刷新 Xs 前 · 下次 Ys 后"

### 第 2 阶段：K 线同步修复（原计划 1.1-1.5）

（内容同前版计划，不重复）

- `_syncing` → `asyncio.Lock` + 单 symbol `wait_for` 15s 超时
- unfillable 集合 + `success_trade_date` 严格升级
- `asyncio.Queue` 单 writer 批量 upsert
- `get_hist` DB 读包 `to_thread`
- `kline_cache_loop` 加重入保护
- 新增 `POST /api/jobs/kline-cache/reset`

### 第 3 阶段：数据中心 UI 增强

- 缺失构成明细（停牌/失败/未覆盖分类）
- 失败重试按钮
- 卡死告警 + 强制重置按钮（调 `/reset`）

### 第 4 阶段：前端整体风格

- 设计 token 清理（panel 3 级、spacing/typography token）
- 响应式三档断点（1440/1280/900）
- 信息密度优化（热门网格、紧凑卡、统一行高）
- ECharts 色值读 CSS 变量

---

## 交付顺序

1. **提交 1**：三个紧急 bug 修复（第 1 阶段）— 立即生效
2. **提交 2**：K 线同步修复（第 2 阶段）
3. **提交 3**：数据中心 UI 增强（第 3 阶段）
4. **提交 4**：前端整体风格（第 4 阶段）

每次修改后 `./restart.sh`，验证：
- 盘中打开模拟盘，下单显示 `price_source=em_live` 且与交易软件一致
- 策略筛选卡片显示名称而非纯代码
- 模拟盘价格每 30s 自动刷新（盘中）
- 日志不再刷"同步任务正在执行中"
- 覆盖率能突破 97.55%

每次提交后 `git push origin main` + 按需更新 `README.md`（技术栈不变，主要更新数据中心功能表、API 接口列表加 `/reset`、模拟盘章节修正刷新频率描述）。

## 不做的事

- 不拆 app.js 单文件（改动范围大，独立议题）
- 不加 CI / lint / 测试覆盖（独立议题）
- 不动 Hermes Agent / Kronos 推理逻辑
- 不改数据库 schema（只加表不改表）
- 本轮不做 XSS/CSP 加固
