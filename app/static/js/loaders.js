/* Alpha frontend module: loaders.js — 首屏/刷新数据加载.
 * 无构建、classic script，按 index.html 中的顺序加载，函数挂在全局作用域。
 */

/* ==================== Data loading ==================== */

async function reloadFunnel() {
  const [funnelRes, hotConceptsRes, hotStocksRes, strategyRes] = await Promise.allSettled([
    request('/api/funnel'),
    request('/api/market/hot-concepts'),
    request('/api/market/hot-stocks'),
    request('/api/strategy/profile'),
  ]);
  if (funnelRes.status === 'fulfilled') state.funnel = funnelRes.value;
  if (hotConceptsRes.status === 'fulfilled') state.hotConcepts = hotConceptsRes.value;
  if (hotStocksRes.status === 'fulfilled') state.hotStocks = hotStocksRes.value;
  if (strategyRes.status === 'fulfilled') state.strategyProfile = strategyRes.value;

  renderHotConcepts();
  renderHotStocks();
  renderFunnel();
  setMeta();
  if (state.selectedHotSymbol) return;
  if (state.selectedSymbol) {
    const found = ['candidate', 'focus', 'buy']
      .flatMap((x) => state.funnel?.pools?.[x] || [])
      .some((x) => x.symbol === state.selectedSymbol);
    if (!found) {
      state.selectedSymbol = null;
      renderStockSummary(null);
      renderChartPlaceholder('点击左侧股票查看');
    } else {
      await selectSymbol(state.selectedSymbol);
    }
  } else {
    renderChartPlaceholder('点击左侧股票查看');
  }
}

async function reloadNotice() {
  try {
    state.noticeFunnel = await request('/api/notice/funnel');
  } catch (_) {}
  renderNoticeFunnel();
  setMeta();
  if (!state.noticeSelectedSymbol) {
    document.getElementById('noticeSummary').textContent = '点击左侧股票查看';
    document.getElementById('noticeDetail').innerHTML = '<div class="empty-state"><div class="empty-state-text">请点击左侧股票查看公告详情</div></div>';
    if (state.activeTab === 'notice') renderChartPlaceholder('点击左侧股票查看');
  }
}

async function reload() {
  await reloadFunnel();
  if (state.noticeFunnel) await reloadNotice();
}

