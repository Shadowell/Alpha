// @ts-check
const { test, expect } = require('@playwright/test');

/** Issue #47：候选池卡片和迁池按钮必须在 composite 渲染后仍真实可用。 */
test('候选卡片点击详情与加入重点均调用 API 并更新池计数', async ({ page }) => {
  // 隔离服务端首屏 WS snapshot，确保测试使用下方 mock 的三池状态。
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
    Object.assign(FakeRealtimeWebSocket, {
      CONNECTING: 0,
      OPEN: 1,
      CLOSING: 2,
      CLOSED: 3,
    });
    window.WebSocket = FakeRealtimeWebSocket;
  });

  const card = {
    symbol: '600000',
    name: '测试候选',
    pool: 'candidate',
    score: 80,
    score_delta: 10,
    recommended_pool: 'focus',
    breakout_level: 11,
    volume_ratio: 2,
    pct_change: 3,
    price: 10.5,
    concept_tags: [],
    reasons: [],
    warnings: [],
    updated_at: '2026-10-22T10:00:00+08:00',
  };
  const pools = { candidate: [card], focus: [], buy: [] };

  await page.route('**/api/funnel', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      trade_date: '2026-10-22',
      updated_at: '2026-10-22T10:00:00+08:00',
      pools,
      stats: {
        candidate: pools.candidate.length,
        focus: pools.focus.length,
        buy: pools.buy.length,
      },
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
  await page.route('**/api/stock/600000/detail*', (route) => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({
      symbol: '600000', name: '测试候选', pool: 'candidate', score: 80,
      score_breakdown: {}, metrics: {}, concept_tags: [], concept_candidates: [],
      trigger_log: [], kline: [],
    }),
  }));
  await page.route('**/api/pool/move', async (route) => {
    const body = JSON.parse(route.request().postData() || '{}');
    expect(body).toEqual({ symbol: '600000', target_pool: 'focus' });
    pools.candidate = [];
    pools.focus = [{ ...card, pool: 'focus' }];
    await route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ success: true, message: '迁移成功', symbol: '600000', pool: 'focus' }),
    });
  });

  await page.goto('/');
  await page.click('.sidebar-item[data-tab="funnel"]');
  const candidateCard = page.locator('#pool-candidate .system-stock-card');
  await expect(candidateCard).toHaveCount(1);
  await expect(candidateCard).toContainText('测试候选');

  const detailRequest = page.waitForRequest('**/api/stock/600000/detail*');
  await candidateCard.click();
  await detailRequest;
  await expect(page.locator('#stockSummary')).toContainText('600000');

  const moveRequest = page.waitForRequest('**/api/pool/move');
  await candidateCard.locator('button', { hasText: '加入重点' }).click();
  await moveRequest;
  await expect(page.locator('#count-candidate')).toHaveText('0');
  await expect(page.locator('#count-focus')).toHaveText('1');
  await expect(page.locator('#pool-focus .stock-card')).toHaveCount(1);
});
