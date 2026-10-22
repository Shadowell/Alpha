// @ts-check
const { test, expect } = require('@playwright/test');

/**
 * Issue #56（前端部分）：
 * - 候选池长列表超过阈值时折叠/分页（公告候选池、策略命中分区）；
 * - 公告候选不再有"直接跳买入"的不一致入口，晋升口径与策略池一致；
 * - 公告卡片分数按阈值/相对均值着色，不再恒为上涨色。
 */

const POOL_ENDPOINT = '**/api/notice/funnel';

function noticeStock(i, score, pool = 'candidate') {
  return {
    symbol: String(300000 + i).padStart(6, '0'),
    name: `MOCK公告标的${i}`,
    pool,
    score,
    notice_type: '业绩预告',
    notice_date: '2026-10-22',
    title: `MOCK 标题 ${i}`,
    reason: '业绩预增',
    risk: '无',
    url: 'https://example.com/notice',
  };
}

async function mockNoticeFunnel(page, { candidate = [], focus = [], buy = [] }) {
  await page.routeWebSocket(/\/ws\/realtime$/, () => {});
  await page.route(POOL_ENDPOINT, (route) => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({
      trade_date: '2026-10-22',
      updated_at: '2026-10-22T10:00:00',
      llm_enabled: false,
      source: 'rule',
      stats: { candidate: candidate.length, focus: focus.length, buy: buy.length },
      pools: { candidate, focus, buy },
    }),
  }));
  await page.route('**/api/notice/keywords', (route) => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({ keywords: [] }),
  }));
  await page.route('**/api/funnel', (route) => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ trade_date: '2026-10-22', updated_at: '', pools: { candidate: [], focus: [], buy: [] }, stats: { candidate: 0, focus: 0, buy: 0 } }),
  }));
  await page.route('**/api/market/hot-concepts', (route) => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({ items: [], trade_date: '', updated_at: '', frozen: false }),
  }));
  await page.route('**/api/market/hot-stocks', (route) => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({ items: [], trade_date: '', updated_at: '', frozen: false }),
  }));
  await page.route('**/api/strategy/**', (route) => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({ items: [], rules: [] }),
  }));
  await page.route('**/api/pool/enroll', (route) => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({ success: true, message: '已入池', symbol: '000000', pool: 'candidate' }),
  }));
  await page.route('**/api/pool/move', (route) => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({ success: true, message: '迁移成功', pool: 'focus' }),
  }));
  await page.route('**/api/notice/pool/move', (route) => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({ success: true, message: '迁移成功', pool: 'focus' }),
  }));
}

test.describe('#56 长列表折叠 / 迁移口径 / 分数着色', () => {
  test('公告候选池超过阈值先折叠，展开后显示全部', async ({ page }) => {
    const threshold = await (async () => {
      await mockNoticeFunnel(page, { candidate: [] });
      await page.goto('/?tab=notice');
      return page.evaluate(() => window.POOL_LIST_COLLAPSE_THRESHOLD);
    })();
    expect(threshold, '折叠阈值必须暴露给测试与调试').toBeGreaterThan(0);

    const total = Math.max(49, threshold * 2 + 3);
    const candidate = Array.from({ length: total }, (_, i) => noticeStock(i + 1, 50 + (i % 30)));
    await mockNoticeFunnel(page, { candidate });
    await page.goto('/?tab=notice');

    const list = page.locator('#notice-pool-candidate');
    await expect(list.locator('.stock-card:visible')).toHaveCount(threshold);
    await expect(list).toHaveAttribute('data-collapsed', '1');
    await expect(list).toHaveAttribute('data-total-count', String(total));
    // 计数仍是池内真实数量，不受折叠影响
    await expect(page.locator('#notice-count-candidate')).toHaveText(String(total));

    await list.locator('.list-collapse-toggle').click();
    await expect(list.locator('.stock-card:visible')).toHaveCount(total);
    await expect(list).toHaveAttribute('data-collapsed', '0');
    await expect(list.locator('.list-collapse-toggle')).toContainText('收起列表');

    // 收起后回到阈值
    await list.locator('.list-collapse-toggle').click();
    await expect(list.locator('.stock-card:visible')).toHaveCount(threshold);
  });

  test('公告候选不再有"加入买入"跨级入口，晋升口径与策略池一致', async ({ page }) => {
    const candidate = Array.from({ length: 3 }, (_, i) => noticeStock(i + 1, 70 + i));
    const focus = [noticeStock(10, 80, 'focus')];
    await mockNoticeFunnel(page, { candidate, focus });
    await page.goto('/?tab=notice');

    const firstCard = page.locator('#notice-pool-candidate .stock-card').first();
    await expect(firstCard.locator('button[data-pool="buy"]')).toHaveCount(0);
    await expect(firstCard.locator('button[data-pool="focus"]')).toHaveCount(1);
    // 重点池仍允许升买入 / 退回候选
    const focusCard = page.locator('#notice-pool-focus .stock-card').first();
    await expect(focusCard.locator('button[data-pool="buy"]')).toHaveCount(1);
    await expect(focusCard.locator('button[data-pool="candidate"]')).toHaveCount(1);

    // 页面写明统一口径
    await expect(page.locator('#tab-notice .pool-rule-note')).toContainText('候选 → 重点 → 买入 逐级晋升');

    // 点击"加入重点"仍打公告迁移接口
    const reqPromise = page.waitForRequest('**/api/notice/pool/move');
    await firstCard.locator('button[data-pool="focus"]').click();
    const req = await reqPromise;
    expect(JSON.parse(req.postData() || '{}')).toEqual({ symbol: candidate[0].symbol, target_pool: 'focus' });
  });

  test('策略命中分区同样按阈值折叠', async ({ page }) => {
    const hits = Array.from({ length: 60 }, (_, i) => ({
      symbol: String(600000 + i), name: `MOCK命中${i}`, trade_date: '2026-10-22',
      close: 10, prev_close: 9.8, change_pct: 2, composite_score: 55 + (i % 20),
      rule_hits: [{ title: '放量', label: '✓' }],
    }));
    await mockNoticeFunnel(page, {});
    await page.route('**/api/strategy/rules', (route) => route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ rules: [{ code: 'volume_spike_today', title: '放量', category: 'volume', params: [] }] }),
    }));
    await page.route('**/api/strategy/custom', (route) => route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ items: [{ id: 's1', name: 'QA 策略', rules: [], is_builtin: true, is_default: true }], default_id: 's1' }),
    }));
    await page.route('**/api/strategy/custom/s1/scan', (route) => route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ generated_at: '2026-10-22T10:00:00', total_scanned: 5000, total_hits: hits.length, elapsed_seconds: 3, rules_count: 2, hits }),
    }));
    await page.goto('/?tab=funnel');
    await page.waitForSelector('#pool-candidate .composite-section.strategy .sc-hit-card');

    const body = page.locator('#pool-candidate .composite-section.strategy .composite-section-body');
    const threshold = await page.evaluate(() => window.POOL_LIST_COLLAPSE_THRESHOLD);
    await expect(body.locator('.sc-hit-card:visible')).toHaveCount(threshold);
    await expect(body).toHaveAttribute('data-collapsed', '1');
    await body.locator('.list-collapse-toggle').click();
    await expect(body.locator('.sc-hit-card:visible')).toHaveCount(hits.length);
    // 计数口径与可见卡片不再脱节
    await expect(page.locator('#count-candidate')).toHaveText(String(hits.length));
  });

  test('公告分数按相对均值/阈值着色，不再恒为上涨色', async ({ page }) => {
    // 同池均值 ~60：75 分偏强、45 分偏弱、60 分中性
    const candidate = [
      noticeStock(1, 75), noticeStock(2, 60), noticeStock(3, 45),
      noticeStock(4, 61), noticeStock(5, 59), noticeStock(6, 60),
    ];
    await mockNoticeFunnel(page, { candidate });
    await page.goto('/?tab=notice');

    const classes = await page.evaluate(() => Array.from(
      document.querySelectorAll('#notice-pool-candidate .stock-card .score'),
    ).map((el) => el.className));
    const kinds = new Set(classes.map((c) => c.replace('score', '').trim()));
    expect(kinds.size, `分数颜色应随相对位置分档，实际 ${[...kinds]}`).toBeGreaterThanOrEqual(3);
    expect(classes[0]).toContain('up');
    expect(classes[1]).toContain('neutral');
    expect(classes[2]).toContain('down');
    // 断言不再出现"所有卡片都是 up"
    expect(classes.every((c) => c.includes('up'))).toBe(false);
  });

  test('样本不足时退回绝对阈值着色（>=70 偏强、<=40 偏弱）', async ({ page }) => {
    await mockNoticeFunnel(page, { candidate: [noticeStock(1, 92), noticeStock(2, 30), noticeStock(3, 55)] });
    await page.goto('/?tab=notice');
    const classes = await page.evaluate(() => Array.from(
      document.querySelectorAll('#notice-pool-candidate .stock-card .score'),
    ).map((el) => el.className));
    expect(classes).toEqual(['score up', 'score down', 'score neutral']);
  });
});
