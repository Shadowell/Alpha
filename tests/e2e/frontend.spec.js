// @ts-check
const { test, expect } = require('@playwright/test');

/**
 * Issue #12 前端拆分回归：
 * - app.js 仅为入口，逻辑按 classic scripts 模块拆分；
 * - 轮询由 Polling 调度器集中管理，切 Tab 时暂停非当前 Tab 任务；
 * - requestJob() 同时兼容同步响应与 P2a 的 202 + /api/jobs/{id} 轮询契约。
 */

test('app.js 已拆分为模块且入口保持轻量', async ({ page, request }) => {
  const entry = await (await request.get('/static/app.js')).text();
  expect(entry.length).toBeLessThan(10_000);
  const html = await (await request.get('/static/index.html')).text();
  const moduleScripts = [...html.matchAll(/\/static\/js\/([a-z0-9-]+\.js)/g)].map((m) => m[1]);
  expect(moduleScripts.length).toBeGreaterThanOrEqual(15);
  expect(moduleScripts).toContain('polling.js');
  expect(moduleScripts).toContain('api.js');
  expect(moduleScripts).toContain('security.js');

  // 全部模块可加载，无 404
  for (const module of moduleScripts) {
    const resp = await request.get(`/static/js/${module}`);
    expect(resp.status(), `模块 ${module} 应当可访问`).toBe(200);
  }

  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  await page.goto('/');
  await expect(page.locator('#tab-market')).toHaveClass(/active/);
  expect(errors).toEqual([]);
});

test('轮询调度器随 Tab 切换暂停/恢复任务', async ({ page }) => {
  await page.goto('/');
  await expect(page.locator('#tab-market')).toHaveClass(/active/);

  const snapshot = () => page.evaluate(() => {
    const out = {};
    for (const [name, task] of window.Polling.tasks) out[name] = task.active;
    return out;
  });

  const market = await snapshot();
  expect(market['market-hot']).toBe(true);
  expect(market['data-center']).toBe(false);
  expect(market['paper-prices']).toBe(false);

  await page.click('.sidebar-item[data-tab="data"]');
  const data = await snapshot();
  expect(data['data-center']).toBe(true);
  expect(data['market-hot']).toBe(false);

  await page.click('.sidebar-item[data-tab="paper"]');
  const paper = await snapshot();
  expect(paper['paper-prices']).toBe(true);
  expect(paper['data-center']).toBe(false);
});

test('requestJob 兼容 202 + /api/jobs/{id} 轮询并展示进度', async ({ page }) => {
  let polls = 0;
  await page.route('**/api/agent/run', (route) => route.fulfill({
    status: 202,
    contentType: 'application/json',
    body: JSON.stringify({ job_id: 'job-1', status: 'queued', poll_url: '/api/jobs/job-1' }),
  }));
  await page.route('**/api/jobs/job-1', (route) => {
    polls += 1;
    const body = polls === 1
      ? { id: 'job-1', status: 'running', progress: { current: 1, total: 2, message: '分析中' } }
      : { id: 'job-1', status: 'succeeded', result: { summary: { message: '诊断完成' } } };
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
  });

  await page.goto('/');
  const result = await page.evaluate(async () => {
    const progress = [];
    const payload = await window.requestJob(
      '/api/agent/run',
      { method: 'POST', body: JSON.stringify({ task_type: 'full_diagnosis' }) },
      { intervalMs: 30, onProgress: (job) => progress.push(`${job.status}:${job.progress?.message || ''}`) },
    );
    return { payload, progress };
  });

  expect(polls).toBeGreaterThanOrEqual(2);
  expect(result.progress[0]).toBe('running:分析中');
  expect(result.payload.summary.message).toBe('诊断完成');
  expect(result.payload.job_id).toBe('job-1');
});

test('requestJob 兼容旧版同步响应（无 job_id）', async ({ page }) => {
  await page.route('**/api/agent/run', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ success: true, summary: { message: '同步完成' } }),
  }));
  await page.goto('/');
  const payload = await page.evaluate(() => window.requestJob('/api/agent/run', { method: 'POST' }));
  expect(payload.summary.message).toBe('同步完成');
});

test('requestJob 兼容运行中重复提交的 409 + 现有 job_id', async ({ page }) => {
  await page.route('**/api/agent/run', (route) => route.fulfill({
    status: 409,
    contentType: 'application/json',
    body: JSON.stringify({ detail: '任务已在运行中', job_id: 'job-409', status: 'running' }),
  }));
  await page.route('**/api/jobs/job-409', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ id: 'job-409', status: 'succeeded', result: { summary: { message: '复用运行中任务' } } }),
  }));
  await page.goto('/');
  const payload = await page.evaluate(() => window.requestJob('/api/agent/run', { method: 'POST' }, { intervalMs: 20 }));
  expect(payload.summary.message).toBe('复用运行中任务');
  expect(payload.job_id).toBe('job-409');
});
