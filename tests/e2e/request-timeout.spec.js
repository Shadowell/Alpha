// @ts-check
const { test, expect } = require('@playwright/test');

/**
 * Issue #55：request() 必须有超时与中止，单个挂起接口不能把整页钉在加载态。
 * - GET 默认 10s / 写操作默认 20s，可用 window.API_TIMEOUT_MS 或 { timeoutMs } 覆盖；
 * - 错误分三类：timeout / network / http（带 status），提示文案可区分；
 * - Promise.allSettled 的每个区块独立显示错误 + 重试按钮；
 * - requestJob/pollJob 的每一轮请求同样受超时保护，偶发超时不终止任务等待。
 */

/** 让某个接口"挂起不返回"（既不 fulfill 也不 continue）。 */
async function hangEndpoint(page, pattern) {
  await page.route(pattern, () => new Promise(() => {}));
}

test.describe('request() 超时与错误分类', () => {
  test.beforeEach(async ({ page }) => {
    // 关掉实时快照，否则服务端 WS 会把 mock/挂起的三池数据覆盖掉
    await page.routeWebSocket(/\/ws\/realtime$/, () => {});
    await page.addInitScript(() => { window.API_TIMEOUT_MS = { read: 1200, write: 1500 }; });
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
  });

  test('挂起接口在超时预算内被中止，错误码为 timeout', async ({ page }) => {
    await hangEndpoint(page, '**/api/slow-hanging-endpoint');
    await page.goto('/');
    const result = await page.evaluate(async () => {
      const started = Date.now();
      try {
        await window.request('/api/slow-hanging-endpoint');
        return { ok: true };
      } catch (err) {
        return { ok: false, code: err.code, message: err.message, elapsed: Date.now() - started, path: err.path };
      }
    });
    expect(result.ok).toBe(false);
    expect(result.code).toBe('timeout');
    expect(result.elapsed, `超时应在预算内生效：${result.elapsed}ms`).toBeLessThan(6000);
    expect(result.message).toContain('请求超时');
    expect(result.path).toContain('/api/slow-hanging-endpoint');
  });

  test('HTTP 错误与连接失败分类可区分，写操作预算大于读操作', async ({ page }) => {
    await page.route('**/api/http-500', (route) => route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ detail: '内部错误' }) }));
    await page.goto('/');
    const result = await page.evaluate(async () => {
      const out = {};
      try { await window.request('/api/http-500'); } catch (err) { out.http = { code: err.code, status: err.status, message: err.message }; }
      try { await window.request('http://127.0.0.1:1/api-refused'); } catch (err) { out.network = { code: err.code, message: err.message }; }
      out.timeouts = {
        read: window._apiTimeoutMs('GET', {}),
        write: window._apiTimeoutMs('POST', {}),
        explicit: window._apiTimeoutMs('POST', { timeoutMs: 1234 }),
      };
      return out;
    });
    expect(result.http.code).toBe('http');
    expect(result.http.status).toBe(500);
    expect(result.http.message).toContain('内部错误');
    expect(result.network.code).toBe('network');
    expect(result.timeouts.read).toBe(1200);
    expect(result.timeouts.write).toBe(1500);
    expect(result.timeouts.explicit).toBe(1234);
  });

  test('默认预算：GET 10s、写 20s（未覆盖配置时）', async ({ page }) => {
    await page.goto('/');
    const defaults = await page.evaluate(() => {
      delete window.API_TIMEOUT_MS;
      return { read: window._apiTimeoutMs('GET', {}), write: window._apiTimeoutMs('PUT', {}) };
    });
    expect(defaults.read).toBe(10000);
    expect(defaults.write).toBe(20000);
  });

  test('三池接口挂起时其余区块照常渲染并给出独立重试', async ({ page }) => {
    await hangEndpoint(page, '**/api/funnel');
    await page.goto('/?tab=funnel');

    // 热门概念/个股照常渲染（它们在大盘 Tab 上，这里只断言 DOM 已产出而非加载中）
    await expect(page.locator('#hotConcepts .empty-state')).toHaveCount(1);
    await expect(page.locator('#hotStocks .empty-state')).toHaveCount(1);
    // 三池区块就地报错 + 重试
    await expect(page.locator('#pool-candidate .block-error')).toBeVisible({ timeout: 10000 });
    await expect(page.locator('#pool-candidate .block-error-text')).toContainText('三池数据');
    await expect(page.locator('#pool-candidate .block-error-text')).toContainText('请求超时');
    await expect(page.locator('#pool-candidate .block-retry')).toBeVisible();

    // 接口恢复后点重试，页面重新出现三池
    await page.unroute('**/api/funnel');
    await page.route('**/api/funnel', (route) => route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({
        trade_date: '2026-10-22', updated_at: '',
        pools: { candidate: [{ symbol: '600000', name: '恢复标的', pool: 'candidate', score: 66, concept_tags: [] }], focus: [], buy: [] },
        stats: { candidate: 1, focus: 0, buy: 0 },
      }),
    }));
    await page.locator('#pool-candidate .block-retry').click();
    await expect(page.locator('#pool-candidate .system-stock-card')).toContainText('恢复标的', { timeout: 15000 });
    await expect(page.locator('#pool-candidate .block-error')).toHaveCount(0);
  });

  test('挂起区块不会因轮询叠加请求（同一区块上一轮未结束则跳过）', async ({ page }) => {
    let attempts = 0;
    await page.route('**/api/jobs/kline-cache/**', (route) => {
      if (route.request().url().includes('/status')) {
        attempts += 1;
        return new Promise(() => {}); // 挂起
      }
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [], total: 0 }) });
    });
    await page.goto('/?tab=data');
    // 把状态轮询压到 200ms，模拟"旧请求还没回来、新一轮已经到点"
    await page.evaluate(() => { window.DC_POLL.status.idle = 200; window.DC_POLL.status.running = 200; });
    await page.waitForTimeout(3000);
    expect(attempts, `挂起期间不应叠加请求，实际发起 ${attempts} 次`).toBeLessThanOrEqual(2);
  });

  test('pollJob 每轮受超时保护，偶发超时不终止任务等待', async ({ page }) => {
    let polls = 0;
    await page.route('**/api/jobs/job-timeout', (route) => {
      polls += 1;
      if (polls === 1) {
        // 提交任务：返回 202 契约（带 job_id），后续才是轮询
        return route.fulfill({
          status: 202, contentType: 'application/json',
          body: JSON.stringify({ job_id: 'job-timeout', status: 'queued', poll_url: '/api/jobs/job-timeout' }),
        });
      }
      if (polls === 2) return new Promise(() => {}); // 第一轮轮询挂起 -> 前端超时
      return route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ id: 'job-timeout', status: 'succeeded', result: { ok: true } }),
      });
    });
    await page.goto('/');
    const result = await page.evaluate(async () => {
      const seen = [];
      try {
        const payload = await window.requestJob(
          '/api/jobs/job-timeout',
          { method: 'GET' },
          { intervalMs: 120, onProgress: (job) => seen.push(job.status) },
        );
        return { ok: true, payload, seen };
      } catch (err) {
        return { ok: false, message: err.message, code: err.code, seen };
      }
    });
    expect(result.ok, `偶发超时不应让任务失败：${result.message || ''}`).toBe(true);
    expect(result.payload.ok).toBe(true);
    expect(polls).toBeGreaterThanOrEqual(3);
  });
});
