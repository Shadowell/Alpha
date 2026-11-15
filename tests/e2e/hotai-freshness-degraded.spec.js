// @ts-check
const fs = require('fs');
const path = require('path');
const { test, expect } = require('@playwright/test');

/**
 * Issue #51 / #52：热门智能面板的「数据新鲜度 / 降级分层」提示。
 *
 * 后端 (app/services/hot_stock_ai_service.py) 已在快照 meta 层产出：
 *   realtime_trade_date / kline_max_date / stale_kline_count / skipped_stale /
 *   prediction_enabled / prediction_available / pred_missing_count /
 *   pool_tier_mode / pool_tier_reason
 * 前端此前完全没消费。本 spec 用 page.route 全量拦截（无需本地起服务，避免与并行会话抢端口/SQLite WAL），
 * 直接以磁盘上的 app/static 提供页面，并 mock /api/strategy/hot-stock-ai 快照，
 * 分别构造「正常 / stale_kline_count>0 / quantile_degraded」三态，断言横幅文案与不误报。
 *
 * /ws/realtime 的 WebSocket 快照会覆盖 mock 的 HTTP 数据，沿用既有 spec 的 addInitScript 打桩。
 */

const STATIC_ROOT = path.resolve(__dirname, '../../app/static');
const BASE_ORIGIN = new URL(process.env.ALPHA_TEST_BASE_URL || 'http://127.0.0.1:18890').origin;
const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'application/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.ico': 'image/x-icon',
  '.json': 'application/json; charset=utf-8',
};

function readStatic(relPath) {
  const clean = decodeURIComponent(relPath).replace(/^\/+/, '').split('?')[0].split('#')[0];
  const abs = path.join(STATIC_ROOT, clean);
  if (!abs.startsWith(STATIC_ROOT)) return null;
  if (!fs.existsSync(abs) || !fs.statSync(abs).isFile()) return null;
  return { body: fs.readFileSync(abs), type: MIME[path.extname(abs).toLowerCase()] || 'application/octet-stream' };
}

function baseSnapshot({
  staleCount = 0,
  skipped = [],
  realtimeTradeDate = '2026-10-22',
  klineMaxDate = '2026-10-22',
  poolTierMode = 'absolute',
  poolTierReason = '',
  predictionEnabled = true,
  predictionAvailable = true,
  predMissingCount = 0,
  updatedAt = '2026-10-22T10:00:00',
} = {}) {
  return {
    trade_date: realtimeTradeDate,
    updated_at: updatedAt,
    running: false,
    progress: {},
    config: {
      threshold_candidate: 8, threshold_focus: 11.5, threshold_buy: 14.5,
      top_n: 20, use_kronos: predictionEnabled, auto_refresh_enabled: true,
      tradingagents_enabled: false,
    },
    pools: { candidate: [], focus: [], buy: [] },
    entries: [],
    meta: {
      trigger: 'manual',
      entries_count: 0,
      stocks_scanned: 20,
      failed_count: 0,
      failed_symbols: [],
      stale_kline_count: staleCount,
      skipped_stale: skipped,
      realtime_trade_date: realtimeTradeDate,
      kline_max_date: klineMaxDate,
      prediction_enabled: predictionEnabled,
      prediction_available: predictionAvailable,
      pred_missing_count: predMissingCount,
      pool_tier_mode: poolTierMode,
      pool_tier_reason: poolTierReason,
      elapsed_sec: 3.2,
      avg_score: 0,
      execution_mode: 'full_manual',
      runtime_top_n: 20,
      kronos_enabled: predictionEnabled,
      runtime_kronos_enabled: predictionEnabled,
      kronos_loaded: true,
      kronos_device: 'cpu',
      tradingagents_enabled: false,
      runtime_tradingagents_enabled: false,
      tradingagents_discussed: 0,
      tradingagents_cache_hits: 0,
      tradingagents_failures: 0,
      tradingagents_backend: 'disabled',
      thresholds: { candidate: 8, focus: 11.5, buy: 14.5 },
    },
  };
}

async function installMocks(page, hotAISnapshot) {
  // 隔离首屏 WS snapshot，确保测试用下方 mock 的 HTTP 快照。
  await page.addInitScript(() => {
    const OriginalWebSocket = window.WebSocket;
    function FakeRealtimeWebSocket(url) {
      if (String(url).includes('/ws/realtime')) {
        this.onopen = null;
        this.onmessage = null;
        this.onerror = null;
        this.onclose = null;
        this.readyState = 1;
        this.close = () => {};
        this.send = () => {};
        this.addEventListener = () => {};
        this.removeEventListener = () => {};
        return;
      }
      return new OriginalWebSocket(url);
    }
    FakeRealtimeWebSocket.prototype = Object.create(OriginalWebSocket.prototype);
    Object.assign(FakeRealtimeWebSocket, { CONNECTING: 0, OPEN: 1, CLOSING: 2, CLOSED: 3 });
    window.WebSocket = FakeRealtimeWebSocket;
  });

  await page.route('**/*', async (route) => {
    const req = route.request();
    const url = new URL(req.url());

    const json = (body) => route.fulfill({
      status: 200, contentType: 'application/json; charset=utf-8', body: JSON.stringify(body),
    });

    // 外部 CDN/字体在离线测试里降级为空 stub（charts.js 已守卫 window.echarts）。
    if (url.origin !== BASE_ORIGIN) {
      if (/cdn\.jsdelivr\.net/.test(url.href)) {
        return route.fulfill({ status: 200, contentType: 'application/javascript; charset=utf-8', body: '/* stubbed echarts */' });
      }
      if (/fonts\.(googleapis|gstatic)\.com/.test(url.href)) {
        return route.fulfill({ status: 200, contentType: 'text/css; charset=utf-8', body: '/* stubbed fonts */' });
      }
      return route.fulfill({ status: 404, body: '' });
    }

    if (url.pathname.startsWith('/api/')) {
      if (url.pathname === '/api/strategy/hot-stock-ai') return json(hotAISnapshot);
      if (url.pathname === '/api/funnel') {
        return json({
          trade_date: '2026-10-22', updated_at: '2026-10-22T10:00:00', llm_enabled: false, source: 'rule',
          stats: { candidate: 0, focus: 0, buy: 0 }, pools: { candidate: [], focus: [], buy: [] },
        });
      }
      if (url.pathname === '/api/market/hot-concepts' || url.pathname === '/api/market/hot-stocks') {
        return json({ trade_date: '2026-10-22', updated_at: '', frozen: false, items: [] });
      }
      if (url.pathname === '/api/strategy/profile') return json({});
      if (url.pathname === '/api/jobs/kline-cache/stats') {
        return json({ row_count: 100, symbol_count: 1, min_date: '2026-01-01', max_date: '2026-10-22' });
      }
      if (url.pathname === '/api/predict-funnel') {
        return json({
          trade_date: '2026-10-22', updated_at: '2026-10-22T10:00:00', running: false,
          config: {}, pools: { candidate: [], focus: [], buy: [] }, progress: {}, meta: {},
        });
      }
      if (url.pathname === '/api/predict/kronos/status') return json({ loaded: false, loading: false });
      return json({});
    }

    if (url.pathname === '/' || url.pathname === '/index.html') {
      const s = readStatic('index.html');
      return s
        ? route.fulfill({ status: 200, contentType: s.type, body: s.body })
        : route.fulfill({ status: 500, body: 'index.html missing' });
    }

    if (url.pathname.startsWith('/static/')) {
      const s = readStatic(url.pathname.slice('/static/'.length));
      return s ? route.fulfill({ status: 200, contentType: s.type, body: s.body }) : route.fulfill({ status: 404, body: '' });
    }

    return route.fulfill({ status: 404, body: '' });
  });
}

async function openHotAITab(page, hotAISnapshot) {
  await installMocks(page, hotAISnapshot);
  await page.goto('/?tab=hotai');
  await expect(page.locator('#tab-hotai')).toHaveClass(/active/);
}

test('正常态：无陈旧/降级横幅，绝不出现误导文案', async ({ page }) => {
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  await openHotAITab(page, baseSnapshot());

  const alerts = page.locator('#hotAiAlerts .hot-ai-alert');
  await expect(alerts).toHaveCount(0);
  // 健康快照下不得出现任何降级 / 陈旧字样
  const box = page.locator('#hotAiAlerts');
  await expect(box).not.toContainText('分位数降级');
  await expect(box).not.toContainText('K 线落后被跳过');
  await expect(box).not.toContainText('混期');
  expect(errors, `关键 JS 错误: ${errors.join(' / ')}`).toEqual([]);
});

test('陈旧/混期态：横幅写明被跳过标的并可展开列出 symbol/落后天数/reason', async ({ page }) => {
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  await openHotAITab(page, baseSnapshot({
    staleCount: 3,
    // 混期证据：K 线缓存 (2026-10-16) 落后实时交易日 (2026-10-22)
    klineMaxDate: '2026-10-16',
    skipped: [
      { symbol: '688001', name: '样例甲', kline_max_date: '2026-10-16', realtime_trade_date: '2026-10-22', lag_trading_days: 4, reason: 'beyond_tolerance' },
      { symbol: '688002', name: '样例乙', kline_max_date: '2026-10-17', realtime_trade_date: '2026-10-22', lag_trading_days: 3, reason: 'realtime_prev_close_mismatch' },
      { symbol: '688003', name: '样例丙', kline_max_date: '2026-10-19', realtime_trade_date: '2026-10-22', lag_trading_days: 1, reason: 'realtime_price_missing' },
    ],
  }));

  const stale = page.locator('#hotAiAlerts .hot-ai-alert--stale');
  await expect(stale).toHaveCount(1);
  await expect(stale).toContainText('3 只因 K 线落后被跳过，未参与评分');
  await expect(stale).toContainText('混期风险');
  await expect(stale).toContainText('实时交易日 2026-10-22');

  // 展开被跳过标的
  const toggle = stale.locator('.hot-ai-alert-details summary');
  await expect(toggle).toContainText('展开被跳过标的（3）');
  await toggle.click();
  const rows = stale.locator('.hot-ai-alert-list li');
  await expect(rows).toHaveCount(3);
  await expect(rows.nth(0)).toContainText('688001');
  await expect(rows.nth(0)).toContainText('落后 4 个交易日');
  await expect(rows.nth(0)).toContainText('beyond_tolerance');
  await expect(rows.nth(2)).toContainText('688003');

  // 正常阈值未触发降级提示（三态隔离，不误报）
  await expect(page.locator('#hotAiAlerts .hot-ai-alert--tier')).toHaveCount(0);
  expect(errors, `关键 JS 错误: ${errors.join(' / ')}`).toEqual([]);
});

test('降级分层态：明示 Kronos 不可用并带原因与缺失数量', async ({ page }) => {
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  await openHotAITab(page, baseSnapshot({
    poolTierMode: 'quantile_degraded',
    poolTierReason: 'prediction_unavailable：Kronos 未启用或全部标的无预测结果，改用分位数降级分层',
    predictionEnabled: false,
    predictionAvailable: false,
    predMissingCount: 5,
  }));

  const tier = page.locator('#hotAiAlerts .hot-ai-alert--tier');
  await expect(tier).toHaveCount(1);
  await expect(tier).toContainText('Kronos 预测不可用，三池已切换为分位数降级分层');
  await expect(tier).toContainText('原因：prediction_unavailable');
  await expect(tier).toContainText('预测分量缺失 5 只');
  await expect(tier).toContainText('Kronos 当前未启用');

  // 该快照无陈旧标的，不应出现陈旧横幅
  await expect(page.locator('#hotAiAlerts .hot-ai-alert--stale')).toHaveCount(0);
  expect(errors, `关键 JS 错误: ${errors.join(' / ')}`).toEqual([]);
});

test('降级态但绝对阈值不误报：absolute 模式绝不出现降级文案', async ({ page }) => {
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  // prediction_available=true（部分缺失也不触发降级横幅），pool_tier_mode=absolute
  await openHotAITab(page, baseSnapshot({ predMissingCount: 4 }));
  await expect(page.locator('#hotAiAlerts .hot-ai-alert--tier')).toHaveCount(0);
  await expect(page.locator('#hotAiAlerts')).not.toContainText('分位数降级');
  expect(errors, `关键 JS 错误: ${errors.join(' / ')}`).toEqual([]);
});
