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

test.describe('策略选股 tab - 自定义策略中心', () => {
  test('策略中心区块可见，且视觉尺寸达标', async ({ page }) => {
    await page.goto('/');
    await page.click('.sidebar-item[data-tab="funnel"]');
    await page.waitForTimeout(800);

    const scArea = page.locator('#strategyCenterArea');
    await expect(scArea).toBeVisible();
    await scArea.scrollIntoViewIfNeeded();

    // 标题为"自定义策略中心"
    await expect(scArea.locator('h3')).toContainText('自定义策略中心');

    // 策略下拉 & 核心按钮存在
    await expect(page.locator('#scStrategySelect')).toBeVisible();
    await expect(page.locator('#btnScScan')).toBeVisible();
    await expect(page.locator('#btnScBacktest')).toBeVisible();

    // 规则卡片至少渲染 1 张
    const ruleCards = page.locator('.sc-rule-card');
    await expect(ruleCards.first()).toBeVisible();
    const ruleCount = await ruleCards.count();
    expect(ruleCount).toBeGreaterThanOrEqual(3);

    // 容器视觉尺寸（内容较多，最小高度远超缩量启动旧版）
    const box = await scArea.boundingBox();
    expect(box.width).toBeGreaterThan(600);
    expect(box.height).toBeGreaterThan(200);
  });

  test('策略命中卡片点击不刷新页面 (核心 bug 回归)', async ({ page }) => {
    await page.goto('/');
    const originalUrl = page.url();

    await page.click('.sidebar-item[data-tab="funnel"]');
    await page.waitForTimeout(800);
    await page.locator('#strategyCenterArea').scrollIntoViewIfNeeded();

    // 尝试找已有命中卡片；若没有，只确认空态提示存在
    const cards = page.locator('.sc-hit-card');
    const count = await cards.count();

    let navigated = false;
    page.on('framenavigated', () => { navigated = true; });

    if (count > 0) {
      await page.evaluate(() => {
        window.__opened = [];
        window.open = (...args) => { window.__opened.push(args); return null; };
      });

      await cards.first().click();
      await page.waitForTimeout(800);

      expect(navigated, '点击策略命中卡片后触发了页面跳转/刷新！').toBeFalsy();
      expect(page.url()).toBe(originalUrl);
      const opened = await page.evaluate(() => window.__opened || []);
      expect(opened.length, `不应触发 window.open，但触发了 ${JSON.stringify(opened)}`).toBe(0);

      const modal = page.locator('#predictModal');
      await expect(modal).toBeVisible({ timeout: 5000 });
    } else {
      const meta = page.locator('#scScanMeta');
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

  test('[智能进化] 不再显示提案管理入口', async ({ page }) => {
    await page.goto('/');
    await page.click('.sidebar-item[data-tab="agent"]');
    await page.waitForTimeout(800);
    const proposalTab = page.locator('.agent-inner-tab[data-subtab="proposal"]');
    await expect(proposalTab).toHaveCount(0);
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
