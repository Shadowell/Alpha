// @ts-check
const { test, expect } = require('@playwright/test');

/**
 * Issue #54：数据中心不再"每 3s 全量拉 5 个接口"。
 * - dc-status 任务：running 5s / 空闲 60s；
 * - stats / enrichment：running 30s / 空闲 60s；
 * - report / logs：只在阶段跃迁或用户操作后刷新，空闲最长 300s 兜底；
 * - 保留 Polling 调度器的 Tab 暂停与页面隐藏暂停。
 * 用例用 Node 侧计数器量化观测窗口内的请求次数（旧实现同窗口约 5 接口 × 每 3s 一轮）。
 */

const DC_ENDPOINTS = {
  status: '/api/jobs/kline-cache/status',
  stats: '/api/jobs/kline-cache/stats',
  report: '/api/jobs/kline-cache/report',
  logs: '/api/jobs/kline-cache/logs',
  enrichment: '/api/jobs/kline-cache/enrichment-status',
};

function freshCounters() {
  const c = { status: 0, stats: 0, report: 0, logs: 0, enrichment: 0 };
  c.total = () => c.status + c.stats + c.report + c.logs + c.enrichment;
  c.snapshot = () => ({ status: c.status, stats: c.stats, report: c.report, logs: c.logs, enrichment: c.enrichment });
  return c;
}

const IDLE_STATUS = {
  status: 'idle', updated_at: '2026-10-22T10:00:00', synced_symbols: 0, total_symbols: 0,
  progress_pct: 0, last_success_trade_date: '2026-10-21', trigger_mode: 'scheduled',
};

/**
 * @param {object} options
 * @param {(callIndex:number)=>object} [options.statusPayload] 可随调用次数变化的状态响应
 */
async function mockDataCenter(page, counters, options = {}) {
  const json = (route, body) => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify(body),
  });
  await page.routeWebSocket(/\/ws\/realtime$/, () => {});
  await page.route('**/api/funnel', (route) => json(route, {
    trade_date: '2026-10-22', updated_at: '', pools: { candidate: [], focus: [], buy: [] },
    stats: { candidate: 0, focus: 0, buy: 0 },
  }));
  await page.route('**/api/market/hot-concepts', (route) => json(route, { items: [], trade_date: '', updated_at: '', frozen: false }));
  await page.route('**/api/market/hot-stocks', (route) => json(route, { items: [], trade_date: '', updated_at: '', frozen: false }));
  await page.route(/\/api\/strategy\//, (route) => json(route, { items: [], rules: [] }));

  const statusPayload = options.statusPayload || (() => IDLE_STATUS);
  await page.route(`**${DC_ENDPOINTS.status}`, (route) => {
    counters.status += 1;
    return json(route, statusPayload(counters.status));
  });
  await page.route(`**${DC_ENDPOINTS.stats}`, (route) => {
    counters.stats += 1;
    return json(route, { symbol_count: 5000, row_count: 1200000, min_date: '2024-01-02', max_date: '2026-10-21', db_size_mb: 812 });
  });
  await page.route(`**${DC_ENDPOINTS.report}`, (route) => {
    counters.report += 1;
    return json(route, {
      status: 'ok', check_time: '2026-10-22T09:00:00', coverage_pct: 99.4, total_expected: 100,
      total_actual: 99, total_missing: 1, trade_days_checked: 20, missing_by_date: [],
    });
  });
  await page.route(`**${DC_ENDPOINTS.logs}*`, (route) => {
    counters.logs += 1;
    return json(route, {
      items: [{
        status: 'success', trade_date: '2026-10-21', trigger_mode: 'scheduled', success_symbols: 10,
        failed_symbols: 0, total_symbols: 10, synced_symbols: 10, started_at: '2026-10-21T15:20:00',
      }],
      total: 1,
    });
  });
  await page.route(`**${DC_ENDPOINTS.enrichment}`, (route) => {
    counters.enrichment += 1;
    return json(route, {
      items: [{ dataset: 'daily_basic', label: '估值/换手', status: 'success', latest_trade_date: '2026-10-21', total_rows: 5000 }],
    });
  });
}

async function gotoDataTab(page) {
  await page.goto('/?tab=data');
  await page.waitForSelector('#dcHealthCard .dc-health-ring');
}

test.describe('#54 数据中心轮询拆分', () => {
  test('轮询预算收敛为单一可配置常量，区间符合验收口径', async ({ page }) => {
    await mockDataCenter(page, freshCounters());
    await gotoDataTab(page);
    const cfg = await page.evaluate(() => window.DC_POLL);
    expect(cfg.status).toEqual({ running: 5000, idle: 60000 });
    expect(cfg.stats.running).toBeGreaterThanOrEqual(30000);
    expect(cfg.stats.idle).toBeLessThanOrEqual(60000);
    expect(cfg.enrichment.running).toBeGreaterThanOrEqual(30000);
    expect(cfg.report.idle).toBeGreaterThan(cfg.report.running);
    expect(cfg.logs.idle).toBeGreaterThan(cfg.logs.running);
    // 空闲态最长 60s 的口径：状态/概览类区块空闲不得超过 60s
    expect(cfg.stats.idle).toBeLessThanOrEqual(60000);
    expect(cfg.enrichment.idle).toBeLessThanOrEqual(60000);
  });

  test('空闲 16 秒：report/logs 各只取 1 次，总量远低于旧实现', async ({ page }) => {
    const counters = freshCounters();
    await mockDataCenter(page, counters);
    await gotoDataTab(page);
    await page.waitForTimeout(16000);
    const snap = counters.snapshot();
    expect(snap.status, '空闲时状态最多 60s 一次').toBeLessThanOrEqual(2);
    expect(snap.report, '未变化的完整性报告不得每轮重传').toBe(1);
    expect(snap.logs, '未变化的任务日志不得每轮重传').toBe(1);
    expect(snap.stats).toBeLessThanOrEqual(2);
    expect(snap.enrichment).toBeLessThanOrEqual(2);
    // 旧实现：3s × 5 接口 => 16s 约 25 次请求
    expect(counters.total(), `16 秒内请求总数应远低于旧实现（实测 ${counters.total()}）`).toBeLessThanOrEqual(8);
  });

  test('任务运行中状态按 5s 级轮询，进度保持实时', async ({ page }) => {
    const counters = freshCounters();
    await mockDataCenter(page, counters, {
      statusPayload: (i) => ({
        status: 'running', updated_at: `2026-10-22T10:0${i % 10}:00`,
        synced_symbols: i * 100, total_symbols: 5000, progress_pct: Math.min(99, i * 6),
        message: '批量同步中', last_success_trade_date: '2026-10-21', trigger_mode: 'manual',
      }),
    });
    await gotoDataTab(page);
    await page.waitForTimeout(16000);
    const snap = counters.snapshot();
    expect(snap.status, 'running 时状态应 5s 级轮询').toBeGreaterThanOrEqual(3);
    await expect(page.locator('#dcProgressSection')).toBeVisible();
    await expect(page.locator('#dcProgressText')).toContainText('批量同步中');
    expect(await page.locator('#dcProgressBar').evaluate((el) => el.style.width)).toMatch(/^\d+(\.\d+)?%$/);
    // 进度每秒在变，但 report/logs 只按阶段签名与 TTL 刷新，不跟着每 5s 整包重传
    expect(snap.report, `report 次数=${snap.report}`).toBeLessThanOrEqual(2);
    expect(snap.logs, `logs 次数=${snap.logs}`).toBeLessThanOrEqual(3);
  });

  test('阶段跃迁（idle -> running）立即触发 report/logs 条件刷新', async ({ page }) => {
    const counters = freshCounters();
    let running = false;
    await mockDataCenter(page, counters, {
      statusPayload: () => (running
        ? { ...IDLE_STATUS, status: 'running', updated_at: '2026-10-22T10:05:00', trigger_mode: 'manual', message: '批量同步中' }
        : IDLE_STATUS),
    });
    await gotoDataTab(page);
    await page.waitForTimeout(800);
    const before = counters.snapshot();
    expect(before.report).toBe(1);
    expect(before.logs).toBe(1);

    running = true;
    // 把空闲状态轮询压缩到 1s 并让调度器重新取间隔，模拟"下一轮 dc-status 拉到阶段已变"
    await page.evaluate(() => {
      window.DC_POLL.status.idle = 1000;
      window.Polling.reschedule('dc-status');
    });
    await page.waitForTimeout(3500);
    const after = counters.snapshot();
    expect(after.status).toBeGreaterThan(before.status);
    expect(after.report, '状态阶段变化后报告应重取一次').toBeGreaterThan(before.report);
    expect(after.logs, '状态阶段变化后任务日志应重取一次').toBeGreaterThan(before.logs);
    expect(after.report - before.report, '变化后也只补取必要次数').toBeLessThanOrEqual(2);
  });

  test('切走 Tab 后轮询暂停（Polling 调度器的 Tab 暂停仍生效）', async ({ page }) => {
    const counters = freshCounters();
    await mockDataCenter(page, counters);
    await gotoDataTab(page);
    await page.waitForTimeout(1500);
    const onTab = counters.total();
    expect(onTab).toBeGreaterThan(0);
    await page.click('.sidebar-item[data-tab="paper"]');
    await page.waitForTimeout(5000);
    expect(counters.total(), '离开数据中心后不应继续请求').toBe(onTab);
  });
});
