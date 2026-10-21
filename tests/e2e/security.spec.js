// @ts-check
const { test, expect } = require('@playwright/test');

/**
 * Issue #8 存储型 XSS 回归测试。
 *
 * 公告/板块/个股字段来自外部数据源，测试通过 Playwright route 注入恶意
 * 载荷，验证 DOM 中不会产生可执行节点、非法协议不会生成可点击 href。
 */

const MALICIOUS_IMG = '<img src=x onerror="window.__xss=1">';
const MALICIOUS_TITLE = `${MALICIOUS_IMG}业绩预增公告`;
const MALICIOUS_NAME = `${MALICIOUS_IMG}平安银行`;
const MALICIOUS_TYPE = '<svg onload="window.__xss=1">业绩预告';
const MALICIOUS_URL = 'javascript:window.__xss=1';

const SAFE_URL = 'https://example.com/announcement?id=1&from=alpha';

function funnelPayload() {
  return {
    trade_date: '2026-10-22',
    updated_at: '2026-10-22T10:00:00',
    llm_enabled: false,
    source: 'rule',
    stats: { candidate: 0, focus: 0, buy: 0 },
    pools: { candidate: [], focus: [], buy: [] },
  };
}

function noticeFunnelPayload(url = MALICIOUS_URL) {
  return {
    trade_date: '2026-10-22',
    updated_at: '2026-10-22T10:00:00',
    llm_enabled: false,
    source: 'rule',
    stats: { candidate: 1, focus: 0, buy: 0 },
    pools: {
      candidate: [
        {
          symbol: '000001',
          name: MALICIOUS_NAME,
          title: MALICIOUS_TITLE,
          notice_type: MALICIOUS_TYPE,
          notice_date: '<b>2026-10-22</b>',
          url,
          score: 88.5,
          pool: 'candidate',
          reason: `${MALICIOUS_IMG}业绩+合同`,
          risk: '<script>window.__xss=1</script>兑现风险',
        },
      ],
      focus: [],
      buy: [],
    },
  };
}

function noticeDetailPayload(url = MALICIOUS_URL) {
  return {
    symbol: '000001',
    name: MALICIOUS_NAME,
    score: 88.5,
    pool: 'candidate',
    reason: `${MALICIOUS_IMG}业绩+合同`,
    risk: '<script>window.__xss=1</script>兑现风险',
    notices: [
      {
        title: MALICIOUS_TITLE,
        notice_type: MALICIOUS_TYPE,
        notice_date: '<b>2026-10-22</b>',
        url,
      },
    ],
    kline: [],
  };
}

async function installApiMocks(page, { url = MALICIOUS_URL } = {}) {
  // 阻断真实 WebSocket 快照，避免后端实时数据覆盖本测试注入的载荷。
  await page.routeWebSocket(/\/ws\/realtime$/, () => { /* mocked: 不连接真实服务端 */ });
  await page.route('**/api/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    const json = (body) => route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(body),
    });

    if (path === '/api/notice/funnel') return json(noticeFunnelPayload(url));
    if (path === '/api/notice/000001/detail') return json(noticeDetailPayload(url));
    if (path === '/api/notice/keywords') {
      return json({ keywords: [{ tag: '业绩预增', weight: 14 }] });
    }
    if (path === '/api/funnel') return json(funnelPayload());
    if (path === '/api/market/hot-concepts') {
      return json({
        items: [
          {
            name: `${MALICIOUS_IMG}机器人`,
            change_pct: 3.2,
            heat: 1.5,
            limit_up_count: 4,
            up_count: 30,
            down_count: 2,
            leader: MALICIOUS_NAME,
            selected_count: 6,
          },
        ],
      });
    }
    if (path === '/api/market/hot-stocks') {
      return json({
        items: [
          {
            rank: 1,
            symbol: '000001',
            name: MALICIOUS_NAME,
            latest_price: 10.5,
            change_pct: 5.1,
            cumulative_10d_pct: 12.3,
          },
        ],
      });
    }
    if (path === '/api/jobs/kline-cache/stats') {
      return json({ row_count: 100, symbol_count: 1, min_date: '2026-01-01', max_date: '2026-10-22' });
    }
    if (path === '/api/jobs/kline-cache/status') return json({ status: 'idle' });
    if (path === '/api/jobs/kline-cache/report') return json({ status: 'none' });
    if (path === '/api/jobs/kline-cache/logs') return json({ items: [], total: 0 });
    return json({ success: true });
  });
}

async function assertNoExecutedInjection(page) {
  expect(await page.evaluate(() => window.__xss)).toBeUndefined();
  // 恶意字符串本身不应变成 DOM 节点
  expect(await page.locator('.stock-card img, .stock-card script, .stock-card svg[onload]').count()).toBe(0);
  expect(await page.locator('#noticeDetail img, #noticeDetail script, #noticeDetail svg[onload]').count()).toBe(0);
  // 页面不应残留 inline 事件处理器
  expect(await page.evaluate(() => document.querySelectorAll('[onclick],[onerror],[onload]').length)).toBe(0);
}

test('公告 Tab：恶意标题/类型/理由被转义，javascript: 链接降级为纯文本', async ({ page }) => {
  const pageErrors = [];
  page.on('pageerror', (e) => pageErrors.push(e.message));
  await installApiMocks(page);

  await page.goto('/?tab=notice');
  await expect(page.locator('#tab-notice')).toHaveClass(/active/);
  const card = page.locator('#notice-pool-candidate .stock-card').first();
  await expect(card).toBeVisible();

  // 恶意载荷以纯文本展示（浏览器会显示原始标签文本）
  await expect(card).toContainText('<img src=x onerror=');
  await assertNoExecutedInjection(page);

  await card.click();
  const detail = page.locator('#noticeDetail');
  await expect(detail).toContainText('类型:');
  await assertNoExecutedInjection(page);

  // javascript: 链接不能生成可点击的 <a href>
  expect(await detail.locator('a').count()).toBe(0);
  await expect(detail.locator('.link-disabled')).toHaveCount(1);
  expect(pageErrors, `关键 JS 错误: ${pageErrors.join(' / ')}`).toEqual([]);
});

test('公告 Tab：合法 https 链接保留可点击 href 且带 noopener', async ({ page }) => {
  await installApiMocks(page, { url: SAFE_URL });
  await page.goto('/?tab=notice');
  const card = page.locator('#notice-pool-candidate .stock-card').first();
  await expect(card).toBeVisible();
  await card.click();

  const link = page.locator('#noticeDetail a').first();
  await expect(link).toHaveAttribute('href', SAFE_URL);
  await expect(link).toHaveAttribute('rel', /noopener/);
  expect(await page.evaluate(() => window.__xss)).toBeUndefined();
});

test('大盘 Tab：恶意板块名/个股名不产生可执行节点', async ({ page }) => {
  const pageErrors = [];
  page.on('pageerror', (e) => pageErrors.push(e.message));
  await installApiMocks(page);

  await page.goto('/');
  await expect(page.locator('#tab-market')).toHaveClass(/active/);
  await expect(page.locator('#hotConcepts .hot-item').first()).toBeVisible();
  await expect(page.locator('#hotStocks .hot-stock-item').first()).toBeVisible();

  await expect(page.locator('#hotConcepts')).toContainText('<img src=x onerror=');
  expect(await page.locator('#hotConcepts img, #hotStocks img, #marketSummary img').count()).toBe(0);
  expect(await page.evaluate(() => window.__xss)).toBeUndefined();
  expect(pageErrors, `关键 JS 错误: ${pageErrors.join(' / ')}`).toEqual([]);
});

test('两个页面都声明 CSP 且禁止内联脚本', async ({ page, request }) => {
  const indexHtml = await (await request.get('/static/index.html')).text();
  const noticeHtml = await (await request.get('/static/notice.html')).text();
  for (const body of [indexHtml, noticeHtml]) {
    expect(body).toContain('Content-Security-Policy');
    expect(body).toContain("script-src 'self' https://cdn.jsdelivr.net");
    expect(body).toContain("frame-ancestors 'none'");
    expect(body).not.toMatch(/<script(?![^>]*src=)[^>]*>[\s\S]*?<\/script>/i);
    expect(body).not.toMatch(/\son(click|error|load)=/i);
  }

  await page.goto('/');
  const csp = await page.locator('meta[http-equiv="Content-Security-Policy"]').getAttribute('content');
  expect(csp).toContain("default-src 'self'");
  expect(csp).toContain("script-src 'self' https://cdn.jsdelivr.net");
});

test('notice.html 独立页面加载共享安全模块', async ({ page }) => {
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  await page.goto('/static/notice.html');
  await expect(page.locator('#statusBar')).toContainText('就绪');
  expect(await page.evaluate(() => typeof window.esc)).toBe('function');
  expect(await page.evaluate(() => typeof window.safeLinkHtml)).toBe('function');
  expect(await page.locator('meta[http-equiv="Content-Security-Policy"]').count()).toBe(1);
  expect(errors).toEqual([]);
});

test('CSP meta 实际阻断内联脚本执行', async ({ page }) => {
  await page.goto('/');
  const executed = await page.evaluate(() => {
    const script = document.createElement('script');
    script.textContent = 'window.__cspInline = 1';
    document.body.appendChild(script);
    return window.__cspInline;
  });
  expect(executed).toBeUndefined();
});
