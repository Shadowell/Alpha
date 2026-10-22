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
  // #57：/api/funnel 若带 market_session / is_trading_day，状态灯切到服务端口径。
  _updateMarketStatus();
  // #55：区块之间互不拖累——失败的那块单独显示错误与重试，其余照常渲染。
  if (funnelRes.status === 'rejected') {
    reportBlockFailure('pool-candidate', funnelRes.reason, () => reloadFunnel(),
      { title: '三池数据', hasData: !!state.funnel });
  }
  if (hotConceptsRes.status === 'rejected') {
    reportBlockFailure('hotConcepts', hotConceptsRes.reason, () => reloadFunnel(),
      { title: '热门概念', hasData: !!state.hotConcepts });
  }
  if (hotStocksRes.status === 'rejected') {
    reportBlockFailure('hotStocks', hotStocksRes.reason, () => reloadFunnel(),
      { title: '热门个股', hasData: !!state.hotStocks });
  }
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
  let failure = null;
  try {
    state.noticeFunnel = await request('/api/notice/funnel');
  } catch (err) {
    failure = err;
  }
  renderNoticeFunnel();
  setMeta();
  // #55：公告三池取数失败只标记该区块并给重试入口，不再静默停在空池。
  if (failure) {
    reportBlockFailure('notice-pool-candidate', failure, () => reloadNotice(),
      { title: '公告三池数据', hasData: !!state.noticeFunnel });
  }
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

