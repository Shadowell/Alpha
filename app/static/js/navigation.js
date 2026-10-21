/* Alpha frontend module: navigation.js — Tab 切换与首屏加载编排.
 * 无构建、classic script，按 index.html 中的顺序加载，函数挂在全局作用域。
 */

const TAB_TITLES = { market: '大盘', data: '数据中心', funnel: '策略选股', notice: '公告选股', predict: '预测选股', hotai: '热门智能', agent: '智能监控 / 进化', paper: '模拟盘' };

function switchTab(tab) {
  state.activeTab = tab;
  document.querySelectorAll('.sidebar-item[data-tab]').forEach((el) => {
    el.classList.toggle('active', el.dataset.tab === tab);
  });
  document.querySelectorAll('.tab-content').forEach((el) => {
    el.classList.toggle('active', el.id === `tab-${tab}`);
  });
  const noticeCard = document.getElementById('noticeDetailCard');
  if (noticeCard) noticeCard.style.display = tab === 'notice' ? '' : 'none';
  const rightPanel = document.querySelector('.right-panel');
  const layout = document.querySelector('.layout');
  const twoCol = tab === 'funnel' || tab === 'notice';
  if (rightPanel) rightPanel.style.display = twoCol ? '' : 'none';
  if (layout) layout.classList.toggle('two-col', twoCol);
  if (twoCol) {
    renderChartPlaceholder('点击左侧股票查看');
    const stockSummary = document.getElementById('stockSummary');
    if (stockSummary) stockSummary.textContent = '点击左侧股票查看';
  }
  document.getElementById('pageTitle').textContent = TAB_TITLES[tab] || 'Alpha';
  setMeta();
  if (tab === 'data') {
    loadDataCenter();
  }
  if (tab === 'funnel') {
    const chip = document.getElementById('activeConcept');
    if (chip) chip.textContent = state.selectedConcept || '全部';
    renderFunnel();
    loadStrategyCenter();
    _refreshDbEmptyBanners();
  }
  if (tab === 'notice') {
    loadNoticeKeywords();
    if (!state.noticeFunnel) reloadNotice();
  }
  if (tab === 'agent') {
    loadAgentData();
  }
  if (tab === 'paper') {
    loadPaperData();
  }
  if (tab === 'predict') {
    loadPredictFunnel();
  }
  if (tab === 'hotai') {
    loadHotStockAI();
  }
  Polling.onTabChange(tab);
  setTimeout(() => {
    if (state.chart) state.chart.resize();
    if (state.marketChart) state.marketChart.resize();
  }, 50);
}

