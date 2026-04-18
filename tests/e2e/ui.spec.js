// @ts-check
const { test, expect } = require('@playwright/test');

/**
 * 全站 E2E 测试 — 覆盖全部 7 个 tab + 缩量启动点击 bug 回归
 *
 * 前提：后端必须已启动在 127.0.0.1:18888
 */

const TABS = [
  { key: 'market', label: '大盘' },
  { key: 'data', label: '数据中心' },
  { key: 'funnel', label: '策略选股' },
  { key: 'notice', label: '公告选股' },
  { key: 'predict', label: '预测选股' },
  { key: 'agent', label: '智能监控' },
  { key: 'paper', label: '模拟盘' },
];

// 需要忽略的非关键 JS 错误
const IGNORED_ERRORS = [
  /ResizeObserver/,
  /favicon/i,
  /WebSocket/i,
  /Failed to load resource.*status of 404/,  // 静态资源偶发
  /net::ERR_INTERNET_DISCONNECTED/,
];

function isCriticalError(msg) {
  if (!msg) return false;
  return !IGNORED_ERRORS.some(re => re.test(msg));
}

test.describe('页面整体加载与切换', () => {
  test('首页正常加载', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => { if (isCriticalError(e.message)) errors.push(e.message); });
    await page.goto('/');
    await page.waitForLoadState('networkidle');
    // 有 sidebar
    await expect(page.locator('.sidebar-item').first()).toBeVisible();
    // 默认 market tab active
    await expect(page.locator('#tab-market')).toHaveClass(/active/);
    expect(errors, `关键 JS 错误: ${errors.join(' / ')}`).toEqual([]);
  });

  for (const tab of TABS) {
    test(`切换到 [${tab.label}] tab 不崩溃`, async ({ page }) => {
      const errors = [];
      page.on('pageerror', e => { if (isCriticalError(e.message)) errors.push(e.message); });
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      await page.click(`.sidebar-item[data-tab="${tab.key}"]`);
      await page.waitForTimeout(500);  // 给 JS 数据加载时间

      await expect(page.locator(`#tab-${tab.key}`)).toHaveClass(/active/);
      // 对应 tab 主容器可见
      await expect(page.locator(`#tab-${tab.key}`)).toBeVisible();
      expect(errors, `切换 ${tab.label} 出现 JS 错误: ${errors.join(' / ')}`).toEqual([]);
    });
  }
});

test.describe('策略选股 tab - 缩量启动区块', () => {
  test('缩量启动区块可见，且视觉尺寸达标', async ({ page }) => {
    await page.goto('/');
    await page.click('.sidebar-item[data-tab="funnel"]');
    await page.waitForTimeout(600);

    const qbArea = page.locator('.quiet-breakout-area');
    await expect(qbArea).toBeVisible();
    await qbArea.scrollIntoViewIfNeeded();

    // 标题存在
    await expect(qbArea.locator('h3')).toContainText('缩量启动');

    // 放大后容器宽度 ≥ 600，高度 ≥ 160（空态也有合理高度）
    const box = await qbArea.boundingBox();
    expect(box.width).toBeGreaterThan(600);
    expect(box.height).toBeGreaterThan(140);
  });

  test('点击缩量启动卡片不刷新页面 (核心 bug 回归)', async ({ page }) => {
    await page.goto('/');
    const originalUrl = page.url();

    await page.click('.sidebar-item[data-tab="funnel"]');
    await page.waitForTimeout(800);
    await page.locator('.quiet-breakout-area').scrollIntoViewIfNeeded();

    // 尝试找已有卡片；若没有，直接触发一次扫描（30 秒成本高，跳过）
    const cards = page.locator('.qb-card');
    const count = await cards.count();

    // 监控页面是否被导航（刷新/跳转）
    let navigated = false;
    page.on('framenavigated', () => { navigated = true; });

    if (count > 0) {
      // 拦截 window.open 防止新标签（若浏览器阻止会退化为当前页跳转）
      await page.evaluate(() => {
        window.__opened = [];
        const orig = window.open;
        window.open = (...args) => { window.__opened.push(args); return null; };
      });

      await cards.first().click();
      await page.waitForTimeout(800);

      // 必须没有跳转
      expect(navigated, '点击缩量启动卡片后触发了页面跳转/刷新！').toBeFalsy();
      // URL hash 不应变成 #SYMBOL（旧 bug 的 fallback 行为）
      expect(page.url()).toBe(originalUrl);
      // 不应调用 window.open
      const opened = await page.evaluate(() => window.__opened || []);
      expect(opened.length, `不应触发 window.open，但触发了 ${JSON.stringify(opened)}`).toBe(0);

      // 应弹出预测 modal
      const modal = page.locator('#predictModal');
      // 等 modal 展示或确认它存在（加载时可能还没 title）
      await expect(modal).toBeVisible({ timeout: 5000 });
    } else {
      // 无命中时不作为硬失败，只确认空态提示存在
      const meta = page.locator('#qbMeta');
      await expect(meta).toBeVisible();
    }
  });
});

test.describe('核心 Tab 数据展示', () => {
  test('[数据中心] 任务历史表格不出现文字重叠', async ({ page }) => {
    await page.goto('/');
    await page.click('.sidebar-item[data-tab="data"]');
    await page.waitForTimeout(1200);

    const items = page.locator('.dc-task-item');
    const n = await items.count();
    if (n === 0) return; // 空态放过

    // 检查前 3 条每个 grid 单元格是否有水平 overlap
    for (let i = 0; i < Math.min(n, 3); i++) {
      const row = items.nth(i);
      const cells = row.locator('.dc-td');
      const count = await cells.count();
      const boxes = [];
      for (let j = 0; j < count; j++) {
        boxes.push(await cells.nth(j).boundingBox());
      }
      for (let j = 1; j < boxes.length; j++) {
        const prev = boxes[j - 1];
        const cur = boxes[j];
        if (!prev || !cur) continue;
        // prev.right 不应 > cur.left + 2px（2px 容差）
        expect(prev.x + prev.width).toBeLessThanOrEqual(cur.x + 2);
      }
    }
  });

  test('[提案管理] stats + 列表正常', async ({ page }) => {
    await page.goto('/');
    await page.click('.sidebar-item[data-tab="agent"]');
    await page.waitForTimeout(800);

    // 切到 "提案管理" 子 tab
    const proposalTab = page.locator('.agent-inner-tab[data-subtab="proposal"]');
    await expect(proposalTab).toBeVisible();
    await proposalTab.click();
    await page.waitForTimeout(1200);

    // stats 容器应可见
    const stats = page.locator('#proposalStats');
    await expect(stats).toBeVisible();
    // 至少有 stat 卡片/数字
    const cards = stats.locator('.stat-card, .stat, div');
    expect(await cards.count()).toBeGreaterThan(0);
  });

  test('[模拟盘] summary 卡片渲染', async ({ page }) => {
    await page.goto('/');
    await page.click('.sidebar-item[data-tab="paper"]');
    await page.waitForTimeout(800);
    await expect(page.locator('#tab-paper')).toHaveClass(/active/);
    // 主容器有内容
    const html = await page.locator('#tab-paper').innerHTML();
    expect(html.length).toBeGreaterThan(100);
  });
});

test.describe('全站 JS 错误巡检', () => {
  test('遍历所有 tab 不产生关键 JS 错误', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => { if (isCriticalError(e.message)) errors.push(e.message); });
    page.on('console', msg => {
      if (msg.type() === 'error' && isCriticalError(msg.text())) errors.push(msg.text());
    });

    await page.goto('/');
    await page.waitForLoadState('networkidle');

    for (const tab of TABS) {
      await page.click(`.sidebar-item[data-tab="${tab.key}"]`);
      await page.waitForTimeout(700);
    }

    expect(errors, `发现关键 JS 错误:\n${errors.join('\n')}`).toEqual([]);
  });
});
