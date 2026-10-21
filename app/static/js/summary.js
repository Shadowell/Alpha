/* Alpha frontend module: summary.js — 页面摘要条通用组件.
 * 无构建、classic script，按 index.html 中的顺序加载，函数挂在全局作用域。
 */

/* ==================== Page Summary Bars ==================== */

function _psItem(label, value, cls = '') {
  return `<span class="ps-item">${esc(label)} <span class="ps-value ${esc(cls)}">${esc(value)}</span></span>`;
}
function _psSep() { return '<span class="ps-sep">|</span>'; }

function renderMarketSummary() {
  const el = document.getElementById('marketSummary');
  if (!el) return;
  const concepts = state.hotConcepts?.items || [];
  const stocks = state.hotStocks?.items || [];
  const upCount = concepts.filter(c => Number(c.change_pct || 0) > 0).length;
  const leader = stocks.length ? stocks[0].name : '--';
  const topChg = stocks.length ? Number(stocks[0].change_pct || 0) : 0;
  const mood = upCount >= 7 ? '偏强' : (upCount >= 4 ? '中性' : '偏弱');
  const moodCls = upCount >= 7 ? 'up' : (upCount >= 4 ? '' : 'down');
  el.innerHTML = [
    _psItem('上涨概念', `${upCount}/${concepts.length}`, upCount >= 5 ? 'success' : 'warning'),
    _psSep(),
    _psItem('龙头', `${leader} ${topChg >= 0 ? '+' : ''}${fmtNum(topChg, 2)}%`, topChg >= 0 ? 'up' : 'down'),
    _psSep(),
    _psItem('市场情绪', mood, moodCls),
  ].join('');
}

function renderDataSummary() {}

function renderFunnelSummary() {
  const el = document.getElementById('funnelSummary');
  if (!el) return;
  if (!state.funnel) { el.innerHTML = ''; return; }
  const c = _getCompositeCandidateCount();
  const f = (state.funnel.pools.focus || []).filter(passConceptFilter).length;
  const b = (state.funnel.pools.buy || []).filter(passConceptFilter).length;
  el.innerHTML = [
    _psItem('候选', c + '只'),
    _psSep(),
    _psItem('重点关注', f + '只', 'warning'),
    _psSep(),
    _psItem('买入池', b + '只', 'success'),
  ].join('');
}

function renderNoticeSummaryBar() {
  _renderNoticeSummaryBarFiltered(null);
}

function _renderNoticeSummaryBarFiltered(filteredPools) {
  const el = document.getElementById('noticeSummaryBar');
  if (!el) return;
  if (!state.noticeFunnel) { el.innerHTML = ''; return; }
  const nf = state.noticeFunnel;
  const kwList = state.activeKeywords.size > 0 ? [...state.activeKeywords].join(' + ') : '全部';
  const candCount = filteredPools ? filteredPools.candidate.length : nf.stats.candidate;
  const buyCount = filteredPools ? filteredPools.buy.length : nf.stats.buy;
  const totalOrig = nf.stats.candidate + nf.stats.focus + nf.stats.buy;
  const totalFiltered = filteredPools ? (filteredPools.candidate.length + filteredPools.focus.length + filteredPools.buy.length) : totalOrig;
  const filterHint = (state.activeKeywords.size > 0 && totalFiltered !== totalOrig) ? ` (${totalFiltered}/${totalOrig})` : '';
  el.innerHTML = [
    _psItem('关键词', kwList + filterHint, 'brand'),
    _psSep(),
    _psItem('候选', candCount + '条'),
    _psSep(),
    _psItem('买入池', buyCount + '只', 'success'),
    _psSep(),
    _psItem('打分源', nf.source || '--'),
  ].join('');
}

