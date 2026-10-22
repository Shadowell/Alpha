// @ts-check
const { test, expect } = require('@playwright/test');

/**
 * Issue #50：策略中心命中卡片必须提供与其它池一致的操作区，并可真正入池。
 * Issue #53：概念筛选必须同时作用于策略命中分区与候选计数。
 *
 * 两个 issue 都靠 mock 后端契约验证（不依赖真实选股数据）：
 *   POST /api/pool/enroll  {symbol,name,source:"strategy_scan"} -> {success,message,symbol,pool}
 *   POST /api/pool/move    {symbol,target_pool}                 -> {success,message,symbol,pool}
 */

const SYSTEM_CANDIDATE = {
  symbol: '600000',
  name: '系统候选股',
  pool: 'candidate',
  score: 80,
  score_delta: 10,
  recommended_pool: 'focus',
  breakout_level: 11,
  volume_ratio: 2,
  pct_change: 3,
  price: 10.5,
  concept_tags: [{ name: '电机', color: '#3b82f6', heat: 1, change_pct: 2, limit_up_count: 1 }],
};

function hit(symbol, name, extra = {}) {
  return {
    symbol,
    name,
    trade_date: '2026-10-22',
    close: 12.3,
    prev_close: 11.8,
    change_pct: 4.2,
    composite_score: 62.5,
    rule_hits: [{ title: '放量启动', label: '✓', passed: true }],
    ...extra,
  };
}

function tag(name) {
  return { name, color: '#3b82f6', heat: 1, change_pct: 1, limit_up_count: 1 };
}

/**
 * 起一套可控的三池 + 策略扫描 mock。
 * enrollHandler / moveHandler 可改写 pools，用来模拟"入池后再由候选→重点"。
 */
async function setup({ page, hits = [], candidatePool = [SYSTEM_CANDIDATE], enrollHandler = null, moveHandler = null }) {
  const pools = { candidate: [...candidatePool], focus: [], buy: [] };
  const calls = { enroll: [], move: [], funnel: 0 };

  await page.routeWebSocket(/\/ws\/realtime$/, () => { /* 阻断实时快照覆盖 mock 数据 */ });
  await page.route('**/api/funnel', (route) => {
    calls.funnel += 1;
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        trade_date: '2026-10-22',
        updated_at: '2026-10-22T10:00:00+08:00',
        pools,
        stats: { candidate: pools.candidate.length, focus: pools.focus.length, buy: pools.buy.length },
      }),
    });
  });
  await page.route('**/api/market/hot-concepts', (route) => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({
      trade_date: '2026-10-22', updated_at: '', frozen: false,
      items: [
        { name: '电机', change_pct: 3.1, heat: 8.8, limit_up_count: 5, up_count: 20, down_count: 3, leader: '东方电机', selected_count: 2 },
        { name: '光模块', change_pct: -1.2, heat: 5.1, limit_up_count: 1, up_count: 4, down_count: 10, leader: '光库科技', selected_count: 0 },
      ],
    }),
  }));
  await page.route('**/api/market/hot-stocks', (route) => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ trade_date: '2026-10-22', updated_at: '', frozen: false, items: [] }),
  }));
  await page.route('**/api/strategy/rules', (route) => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ rules: [{ code: 'volume_spike_today', title: '放量', category: 'volume', params: [] }] }),
  }));
  await page.route('**/api/strategy/profile', (route) => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({}),
  }));
  await page.route('**/api/strategy/custom', (route) => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({
      items: [{ id: 's1', name: 'QA 策略', description: '', is_builtin: true, is_default: true, rules: [] }],
      default_id: 's1',
    }),
  }));
  await page.route('**/api/strategy/custom/s1/scan', (route) => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({
      generated_at: '2026-10-22T10:00:00',
      total_scanned: 5020,
      total_hits: hits.length,
      elapsed_seconds: 10.72,
      rules_count: 4,
      hits,
    }),
  }));
  await page.route('**/api/stock/**', (route) => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ symbol: '600000', name: '测试标的', kline: [], metrics: {}, concept_tags: [] }),
  }));
  await page.route('**/api/predict/**', (route) => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ predicted_kline: [], merged_kline: [], history_kline: [] }),
  }));

  const defaultEnroll = (route, body) => {
    if (!pools.candidate.some((s) => s.symbol === body.symbol)) {
      pools.candidate.push({
        symbol: body.symbol, name: body.name, pool: 'candidate', score: 60, score_delta: 0,
        concept_tags: body.concept_tags || [], volume_ratio: 1, breakout_level: 12, pct_change: 4, price: 12.3,
      });
    }
    return route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ success: true, message: '已加入候选池', symbol: body.symbol, pool: 'candidate' }),
    });
  };
  const defaultMove = (route, body) => {
    const idx = pools.candidate.findIndex((s) => s.symbol === body.symbol);
    if (idx < 0) {
      return route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ success: false, message: '股票不存在于当前选股池', pool: 'candidate' }),
      });
    }
    const [stock] = pools.candidate.splice(idx, 1);
    pools[body.target_pool].push({ ...stock, pool: body.target_pool });
    return route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ success: true, message: '迁移成功', symbol: body.symbol, pool: body.target_pool }),
    });
  };

  await page.route('**/api/pool/enroll', (route) => {
    const body = JSON.parse(route.request().postData() || '{}');
    calls.enroll.push(body);
    return (enrollHandler || defaultEnroll)(route, body, pools, calls);
  });
  await page.route('**/api/pool/move', (route) => {
    const body = JSON.parse(route.request().postData() || '{}');
    calls.move.push(body);
    return (moveHandler || defaultMove)(route, body, pools, calls);
  });

  await page.addInitScript(() => {
    // 捕获所有 toast 文案，用于断言"不得静默"。core.js 的 setStatus 是 classic script，
    // 在 addInitScript 执行时尚未定义，因此等 DOMContentLoaded 再包一层。
    window.__toasts = [];
    const patch = () => {
      if (typeof window.setStatus === 'function' && !window.__setStatusPatched) {
        window.__setStatusPatched = true;
        const original = window.setStatus;
        window.setStatus = (text, tone) => { window.__toasts.push(`${tone}:${text}`); return original(text, tone); };
      }
    };
    document.addEventListener('DOMContentLoaded', patch);
    patch();
  });

  return { pools, calls };
}

async function openFunnelTab(page) {
  await page.goto('/?tab=funnel');
  await page.waitForSelector('#pool-candidate .composite-section.strategy');
}

function clearToasts(page) {
  return page.evaluate(() => { window.__toasts = []; });
}

test.describe('#50 策略命中入池', () => {
  test('命中卡片有可操作按钮，点击后调用 /api/pool/enroll 再推进重点池', async ({ page }) => {
    const { calls } = await setup({
      page,
      hits: [hit('000065', '北方国际'), hit('600508', '上海能源')],
    });
    await openFunnelTab(page);

    const hitCards = page.locator('#pool-candidate .sc-hit-card');
    await expect(hitCards).toHaveCount(2);
    // 回归"候选 2 只但无任何按钮"：每张命中卡片都必须有操作区
    await expect(hitCards.first().locator('.card-actions button')).toHaveCount(2);

    const countEl = page.locator('#count-candidate');
    await expect(countEl).toHaveText('3');
    await expect(countEl).toHaveAttribute('data-system', '1');
    await expect(countEl).toHaveAttribute('data-strategy', '2');

    await clearToasts(page);
    await hitCards.first().locator('button.btn-enroll-focus').click();

    await expect.poll(() => calls.enroll.length, { message: '必须调用入池接口' }).toBe(1);
    expect(calls.enroll[0]).toEqual({ symbol: '000065', name: '北方国际', source: 'strategy_scan' });
    await expect.poll(() => calls.move.length, { message: '入池后由候选推进重点池' }).toBe(1);
    expect(calls.move[0]).toEqual({ symbol: '000065', target_pool: 'focus' });

    // 重点池出现该股，操作按钮没有顺带打开预测 modal
    await expect(page.locator('#count-focus')).toHaveText('1');
    await expect(page.locator('#pool-focus .stock-card')).toContainText('北方国际');
    const toasts = await page.evaluate(() => window.__toasts);
    expect(toasts.some((t) => t.startsWith('success:') && t.includes('已加入重点池'))).toBe(true);
    await expect(page.locator('#predictModal')).toBeHidden();
  });

  test('推进后重新加载页面仍在重点池（后端为准）', async ({ page }) => {
    const { calls, pools } = await setup({ page, hits: [hit('000065', '北方国际')] });
    await openFunnelTab(page);
    await page.locator('#pool-candidate .sc-hit-card').first().locator('button.btn-enroll-focus').click();
    await expect.poll(() => calls.move.length).toBe(1);
    expect(pools.focus.map((s) => s.symbol)).toContain('000065');

    await page.reload();
    await openFunnelTab(page);
    await expect(page.locator('#pool-focus .stock-card')).toHaveCount(1);
    await expect(page.locator('#count-focus')).toHaveText('1');
  });

  test('后端仍返回 404 时降级调用 /api/pool/move 并给出可理解错误，不静默', async ({ page }) => {
    const { calls } = await setup({
      page,
      hits: [hit('000065', '北方国际'), hit('600508', '上海能源')],
      enrollHandler: (route) => route.fulfill({
        status: 404, contentType: 'application/json', body: JSON.stringify({ detail: 'Not Found' }),
      }),
    });
    await openFunnelTab(page);
    await clearToasts(page);
    await page.locator('#pool-candidate .sc-hit-card').first().locator('button.btn-enroll-focus').click();

    // 降级路径确实打了迁移接口
    await expect.poll(() => calls.move.length).toBe(1);

    const toasts = await page.evaluate(() => window.__toasts);
    const errToast = toasts.find((t) => t.startsWith('error:'));
    expect(errToast, '必须给出可见错误，不得静默').toBeTruthy();
    expect(errToast).toContain('/api/pool/enroll 返回 404');
    expect(errToast).toContain('股票不存在于当前选股池');

    // 之后命中分区声明为不可操作，候选计数不再统计它们
    await expect(page.locator('#pool-candidate .composite-section.strategy')).toHaveAttribute('data-actionable', '0');
    await expect(page.locator('#count-candidate')).toHaveAttribute('data-strategy', '0');
    await expect(page.locator('#count-candidate')).toHaveText('1');
    await expect(page.locator('#pool-candidate .sc-hit-card button.btn-enroll-focus').first()).toBeDisabled();
    await expect(page.locator('#pool-candidate .sc-hit-card').first()).toContainText('入池接口未上线');
  });

  test('入池接口超时也归为可见错误（与 #55 的超时分类联动）', async ({ page }) => {
    await setup({
      page,
      hits: [hit('000065', '北方国际')],
      enrollHandler: () => new Promise(() => {}), // 永不返回，触发前端超时
    });
    await openFunnelTab(page);
    await clearToasts(page);
    await page.evaluate(() => { window.API_TIMEOUT_MS = { read: 1000, write: 1200 }; });
    await page.locator('#pool-candidate .sc-hit-card').first().locator('button.btn-enroll-focus').click();

    await expect.poll(async () => {
      const toasts = await page.evaluate(() => window.__toasts);
      return toasts.some((t) => t.startsWith('error:') && t.includes('入池请求超时'));
    }, { message: '超时必须给出可见错误', timeout: 15000 }).toBe(true);
  });

  test('卡片本体点击仍打开预测 modal', async ({ page }) => {
    await setup({ page, hits: [hit('000065', '北方国际')] });
    await openFunnelTab(page);
    await page.locator('#pool-candidate .sc-hit-card').first().click();
    await expect(page.locator('#predictModal')).toBeVisible({ timeout: 5000 });
    await page.click('#predictModalClose');
    // 「查看预测」按钮同样可用（并阻断冒泡，只开一次）
    await page.locator('#pool-candidate .sc-hit-card').first().locator('button.btn-hit-predict').click();
    await expect(page.locator('#predictModal')).toBeVisible({ timeout: 5000 });
  });
});

test.describe('#53 概念筛选作用域', () => {
  test('命中带 concept_tags 时，筛选同时作用于命中分区与候选计数', async ({ page }) => {
    await setup({
      page,
      hits: [
        hit('000065', '北方国际', { concept_tags: [tag('电机')] }),
        hit('600508', '上海能源', { concept_tags: [tag('煤炭')] }),
      ],
    });
    await openFunnelTab(page);
    await expect(page.locator('#pool-candidate .sc-hit-card')).toHaveCount(2);

    // 走真实 UI：大盘页点热门概念 -> 切回策略选股
    await page.click('.sidebar-item[data-tab="market"]');
    await page.locator('#hotConcepts .hot-item', { hasText: '电机' }).click();
    await expect(page.locator('#conceptFilterHint')).toHaveText('已筛选「电机」');
    await page.click('.sidebar-item[data-tab="funnel"]');

    await expect(page.locator('#pool-candidate .sc-hit-card')).toHaveCount(1);
    await expect(page.locator('#pool-candidate .sc-hit-card')).toContainText('北方国际');
    await expect(page.locator('#pool-candidate .composite-section.strategy')).toHaveAttribute('data-concept-mode', 'filtered');
    await expect(page.locator('#pool-candidate .composite-section.strategy .composite-section-hint'))
      .toContainText('已按概念「电机」过滤策略命中');
    // 系统候选(电机) 1 + 命中 1 = 2
    await expect(page.locator('#count-candidate')).toHaveText('2');
    await expect(page.locator('#count-candidate')).toHaveAttribute('data-strategy', '1');

    await page.click('#clearConcept');
    await expect(page.locator('#pool-candidate .sc-hit-card')).toHaveCount(2);
    await expect(page.locator('#count-candidate')).toHaveText('3');
  });

  test('命中缺少 concept_tags 时明确声明"暂不受筛选影响"，不假装已过滤', async ({ page }) => {
    await setup({
      page,
      candidatePool: [{ ...SYSTEM_CANDIDATE, concept_tags: [tag('煤炭')] }],
      hits: [hit('000065', '北方国际'), hit('600508', '上海能源')],
    });
    await openFunnelTab(page);
    await page.click('.sidebar-item[data-tab="market"]');
    await page.locator('#hotConcepts .hot-item', { hasText: '电机' }).click();
    await page.click('.sidebar-item[data-tab="funnel"]');

    const strategySection = page.locator('#pool-candidate .composite-section.strategy');
    await expect(strategySection).toHaveAttribute('data-concept-coverage', 'none');
    await expect(strategySection.locator('.composite-section-hint'))
      .toContainText('策略命中未带概念标签，暂不受筛选影响');
    // 未过滤时如实铺出全部命中，并把口径写进计数 title
    await expect(page.locator('#pool-candidate .sc-hit-card')).toHaveCount(2);
    const countEl = page.locator('#count-candidate');
    await expect(countEl).toHaveAttribute('data-strategy', '2');
    await expect(countEl).toHaveText('2');
    expect(await countEl.getAttribute('title')).toContain('候选 2 = 系统候选 0 + 策略命中可入池 2');

    // 重点/买入池仍是"过滤后为空"，三池口径不再各说各话
    await expect(page.locator('#pool-focus')).toContainText('当前概念「电机」下无匹配股票');

    await page.click('#clearConcept');
    await expect(page.locator('#count-candidate')).toHaveText('3');
    await expect(page.locator('#pool-candidate .system-stock-card')).toHaveCount(1);
  });

  test('命中带标签但都不含所选概念时显示空态而不是全部铺开', async ({ page }) => {
    await setup({
      page,
      hits: [
        hit('000065', '北方国际', { concept_tags: ['光模块'] }),
        hit('600508', '上海能源', { concept_tags: ['煤炭'] }),
      ],
    });
    await openFunnelTab(page);
    await page.click('.sidebar-item[data-tab="market"]');
    await page.locator('#hotConcepts .hot-item', { hasText: '电机' }).click();
    await page.click('.sidebar-item[data-tab="funnel"]');

    await expect(page.locator('#pool-candidate .sc-hit-card')).toHaveCount(0);
    await expect(page.locator('#pool-candidate .composite-section.strategy')).toContainText('当前概念「电机」下无策略命中');
    await expect(page.locator('#count-candidate')).toHaveText('1');
    await expect(page.locator('#count-candidate')).toHaveAttribute('data-strategy', '0');
  });

  test('部分命中有标签时声明"暂不裁剪该分区"，避免静默丢失无标签个股', async ({ page }) => {
    await setup({
      page,
      hits: [
        hit('000065', '北方国际', { concept_tags: [tag('电机')] }),
        hit('600508', '上海能源'),
      ],
    });
    await openFunnelTab(page);
    await page.click('.sidebar-item[data-tab="market"]');
    await page.locator('#hotConcepts .hot-item', { hasText: '电机' }).click();
    await page.click('.sidebar-item[data-tab="funnel"]');

    const section = page.locator('#pool-candidate .composite-section.strategy');
    await expect(section).toHaveAttribute('data-concept-coverage', 'partial');
    await expect(section.locator('.composite-section-hint')).toContainText('策略命中仅部分带概念标签，暂不裁剪该分区');
    await expect(page.locator('#pool-candidate .sc-hit-card')).toHaveCount(2);
  });
});
