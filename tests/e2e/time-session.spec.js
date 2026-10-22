// @ts-check
const { test, expect } = require('@playwright/test');

/**
 * Issue #57：交易时段判定不再依赖浏览器本地时区。
 * - getChinaNowParts() 统一以 Asia/Shanghai 拆解时间；
 * - 市场状态灯 / paperBuy / paperSell 闸门 / _isMarketOpen 共用同一口径；
 * - 服务端提供 market_session / is_trading_day 时优先采用服务端口径。
 *
 * 用例把"当前时刻"固定为几个有代表性的北京时间点，分别在 Asia/Shanghai 与 UTC
 * 两种浏览器时区里渲染，断言两者结果完全一致（真实数据无关，全部走 mock）。
 */

const BASE_URL = process.env.ALPHA_TEST_BASE_URL || 'http://127.0.0.1:18911';

// UTC 瞬时 -> 北京时间 -> 期望状态灯文案
const SESSION_CASES = [
  { utc: '2026-10-22T01:30:00Z', cn: '09:30', text: '上午盘中', session: 'open', open: true },
  { utc: '2026-10-22T03:00:00Z', cn: '11:00', text: '上午盘中', session: 'open', open: true },
  { utc: '2026-10-22T04:00:00Z', cn: '12:00', text: '午间休市', session: 'lunch', open: false },
  { utc: '2026-10-22T06:00:00Z', cn: '14:00', text: '下午盘中', session: 'open', open: true },
  { utc: '2026-10-22T07:30:00Z', cn: '15:30', text: '已收盘', session: 'closed', open: false },
  { utc: '2026-10-22T22:00:00Z', cn: '06:00', text: '盘前', session: 'pre', open: false },
  { utc: '2026-10-24T02:00:00Z', cn: '10:00', text: '休市', session: 'weekend', open: false },
];

async function mockAppRoutes(page, funnelPayload = {}) {
  await page.routeWebSocket(/\/ws\/realtime$/, () => { /* 阻断实时快照覆盖测试数据 */ });
  await page.route('**/api/funnel', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      trade_date: '2026-10-22',
      updated_at: '2026-10-22T10:00:00+08:00',
      pools: { candidate: [], focus: [], buy: [] },
      stats: { candidate: 0, focus: 0, buy: 0 },
      ...funnelPayload,
    }),
  }));
  await page.route('**/api/market/hot-concepts', (route) => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ trade_date: '2026-10-22', updated_at: '', frozen: false, items: [] }),
  }));
  await page.route('**/api/market/hot-stocks', (route) => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ trade_date: '2026-10-22', updated_at: '', frozen: false, items: [] }),
  }));
  await page.route('**/api/strategy/**', (route) => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({ items: [], rules: [] }),
  }));
}

/** 在指定浏览器时区里打开页面，并按固定的北京时刻渲染一次状态灯。 */
async function openInTimezone(browser, timezoneId, funnelPayload) {
  const context = await browser.newContext({ baseURL: BASE_URL, timezoneId });
  const page = await context.newPage();
  await mockAppRoutes(page, funnelPayload);
  await page.goto('/');
  await page.waitForFunction(() => typeof window.getChinaNowParts === 'function');
  return { context, page };
}

function renderAtBeijing(page, isoUtc) {
  return page.evaluate((iso) => {
    const now = new Date(iso);
    window._updateMarketStatus(now);
    const label = document.getElementById('marketLabel');
    const dot = document.getElementById('marketDot');
    return {
      text: label.textContent,
      title: label.title,
      session: label.dataset.session,
      source: label.dataset.source,
      dotClass: dot.className,
      open: window.isChinaMarketOpen(now),
      gate: window._isMarketOpen(now),
      parts: window.getChinaNowParts(now),
    };
  }, isoUtc);
}

test.describe('市场状态与买卖闸门的时区口径（#57）', () => {
  test('Asia/Shanghai 与 UTC 浏览器在每个北京时刻的判定完全一致', async ({ page, browser }) => {
    const cn = await openInTimezone(browser, 'Asia/Shanghai', {});
    const utc = await openInTimezone(browser, 'UTC', {});
    try {
      for (const c of SESSION_CASES) {
        const fromCn = await renderAtBeijing(cn.page, c.utc);
        const fromUtc = await renderAtBeijing(utc.page, c.utc);
        expect(fromCn, `北京时间 ${c.cn} 在 CN 时区浏览器下`).toMatchObject({
          text: c.text, session: c.session, open: c.open, source: 'local',
        });
        expect(fromUtc, `北京时间 ${c.cn} 在 UTC 时区浏览器下`).toMatchObject({
          text: c.text, session: c.session, open: c.open, source: 'local',
        });
        expect(fromUtc.parts.clock, `${c.utc} 应换算为北京 ${c.cn}`).toBe(c.cn);
      }
    } finally {
      await cn.context.close();
      await utc.context.close();
    }
  });

  test('状态灯 title 展示北京时间而非本地时间', async ({ page, browser }) => {
    const cn = await openInTimezone(browser, 'Asia/Shanghai', {});
    const utc = await openInTimezone(browser, 'UTC', {});
    try {
      const a = await renderAtBeijing(cn.page, '2026-10-22T06:00:00Z');
      const b = await renderAtBeijing(utc.page, '2026-10-22T06:00:00Z');
      expect(a.title).toMatch(/北京时间 14:00/);
      expect(b.title).toMatch(/北京时间 14:00/);
      expect(a.dotClass).toContain('open');
      expect(b.dotClass).toContain('open');
    } finally {
      await cn.context.close();
      await utc.context.close();
    }
  });

  test('UTC 浏览器在本地"看起来是白天"时不再误判为盘中', async ({ page, browser }) => {
    // 北京 2026-10-22 22:00（已收盘）= UTC 14:00，本地时钟是下午，旧实现会判成盘中。
    const utc = await openInTimezone(browser, 'UTC', {});
    try {
      const res = await renderAtBeijing(utc.page, '2026-10-22T14:00:00Z');
      expect(res.parts).toMatchObject({ isoDate: '2026-10-22', clock: '22:00', minutesOfDay: 1320 });
      expect(res.text).toBe('已收盘');
      expect(res.gate).toBe(false);
    } finally {
      await utc.context.close();
    }
  });

  test('服务端 market_session 优先于前端本地判定', async ({ page }) => {
    await mockAppRoutes(page, { market_session: 'closed', is_trading_day: true });
    await page.goto('/?tab=funnel');
    await page.waitForFunction(() => document.getElementById('marketLabel').dataset.source === 'server');
    const label = page.locator('#marketLabel');
    await expect(label).toHaveText('已收盘');
    await expect(label).toHaveAttribute('data-session', 'closed');
    expect(await page.evaluate(() => window.isChinaMarketOpen())).toBe(false);
  });

  test('服务端 is_trading_day=false 判为休市（节假日口径留给后端）', async ({ page }) => {
    await mockAppRoutes(page, { market_session: 'open', is_trading_day: false });
    await page.goto('/?tab=funnel');
    await page.waitForFunction(() => document.getElementById('marketLabel').dataset.source === 'server');
    const label = page.locator('#marketLabel');
    await expect(label).toContainText('休市');
    await expect(label).toHaveAttribute('data-session', 'holiday');
    expect(await page.evaluate(() => window.isChinaMarketOpen())).toBe(false);
  });

  test('服务端 open 时闸门放行，即使浏览器本地时钟处于收盘时段', async ({ page, browser }) => {
    const utc = await openInTimezone(browser, 'UTC', { market_session: 'open', is_trading_day: true });
    try {
      await utc.page.waitForFunction(() => document.getElementById('marketLabel').dataset.source === 'server');
      expect(await utc.page.evaluate(() => window._isMarketOpen())).toBe(true);
      expect(await utc.page.evaluate(() => window.isChinaMarketOpen())).toBe(true);
    } finally {
      await utc.context.close();
    }
  });

  test('非交易时段的模拟买入给出可理解提示且不静默发起请求', async ({ page }) => {
    await mockAppRoutes(page, { market_session: 'closed', is_trading_day: true });
    await page.goto('/?tab=funnel');
    await page.waitForFunction(() => document.getElementById('marketLabel').dataset.source === 'server');
    await page.evaluate(() => {
      window.__buyCalls = [];
      window.request = (path) => { window.__buyCalls.push(path); return Promise.resolve({ success: true }); };
    });
    await page.evaluate(() => window.paperBuy('600000', '测试标的', 10));
    const toast = page.locator('.toast').last();
    await expect(toast).toContainText('当前非交易时段');
    await expect(toast).toContainText('北京时间');
    expect(await page.evaluate(() => window.__buyCalls), '闸门未放行时不应发起下单请求').toEqual([]);
  });
});
