const state = {
  activeTab: 'market',
  funnel: null,
  hotConcepts: null,
  hotStocks: null,
  syncStatus: null,
  syncLogs: null,
  strategyProfile: null,
  selectedSymbol: null,
  selectedHotSymbol: null,
  selectedConcept: null,
  chart: null,
  noticeFunnel: null,
  noticeSelectedSymbol: null,
  noticeKeywords: [],
  activeKeywords: new Set(),
  predictChart: null,
  dcStats: null,
  dcReport: null,
  dcTaskFilter: 'all',
  dcLogsExpanded: false,
  monitorConfig: null,
  monitorMessages: [],
  monitorKlineChart: null,
  paperUpdatedAt: null,
  predictFunnel: null,
  predictConfig: null,
  predictRunning: false,
  hotStockAI: null,
  hotStockAIConfig: null,
  hotStockAIRunning: false,
  graphicFunnel: null,
  graphicConfig: null,
  graphicRunning: false,
  strategyScanSnapshot: null,
};

function fmtNum(v, digits = 2) {
  const n = Number(v || 0);
  return Number.isFinite(n) ? n.toFixed(digits) : '0.00';
}

function pnlCls(v) { return v > 0 ? 'up' : v < 0 ? 'down' : 'neutral'; }

function fmtShortTime(iso) {
  if (!iso) return '--';
  const m = iso.match(/(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/);
  return m ? `${m[2]}-${m[3]} ${m[4]}:${m[5]}` : iso.slice(0, 16);
}

function fmtDateTime(iso) {
  if (!iso) return '--';
  const m = iso.match(/(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})/);
  return m ? `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}:${m[6]}` : iso.slice(0, 19).replace('T', ' ');
}

function fmtMoney(v) {
  const n = Number(v || 0);
  if (!Number.isFinite(n)) return '¥0.00';
  return '¥' + n.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function _scrollToRightPanel() {
  setTimeout(() => {
    const rp = document.querySelector('.right-panel');
    if (rp) rp.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }, 200);
}

function setMeta() {
  const meta = document.getElementById('meta');
  if (state.activeTab === 'market') {
    if (!state.funnel) { meta.textContent = '加载中...'; return; }
    meta.textContent = `交易日 ${state.funnel.trade_date} · 更新 ${fmtDateTime(state.funnel.updated_at)}`;
  } else if (state.activeTab === 'data') {
    meta.textContent = '数据同步 · 完整性检查 · 任务管理';
  } else if (state.activeTab === 'funnel') {
    if (!state.funnel) { meta.textContent = '暂无数据'; return; }
    meta.textContent = `交易日 ${state.funnel.trade_date} · 更新 ${fmtDateTime(state.funnel.updated_at)}`;
  } else if (state.activeTab === 'notice') {
    if (!state.noticeFunnel) { meta.textContent = '暂无数据'; return; }
    const nf = state.noticeFunnel;
    meta.textContent = `公告日 ${nf.trade_date} · 更新 ${fmtDateTime(nf.updated_at)} · 打分源 ${nf.source}`;
  } else if (state.activeTab === 'agent') {
    meta.textContent = '智能监控 / 进化 — 自动追踪市场主线 · 推送投研情报 · 输出诊断建议';
  } else if (state.activeTab === 'paper') {
    const ts = state.paperUpdatedAt || '--';
    meta.textContent = `模拟账户 · 初始资金 ¥1,000,000 · 更新 ${ts}`;
  } else if (state.activeTab === 'predict') {
    const snap = state.predictFunnel;
    if (!snap) { meta.textContent = '预测选股 · 加载中…'; return; }
    const m = snap.meta || {};
    const extra = m.stocks_scanned ? `扫描 ${m.stocks_scanned} · 命中 ${m.entries_count || 0} · 用时 ${m.elapsed_sec || 0}s` : '尚未执行';
    meta.textContent = `交易日 ${snap.trade_date || '--'} · 更新 ${fmtDateTime(snap.updated_at)} · ${extra}`;
  } else if (state.activeTab === 'hotai') {
    const snap = state.hotStockAI;
    if (!snap) { meta.textContent = '热门智能分析 · 加载中…'; return; }
    const m = snap.meta || {};
    const modeText = m.execution_mode === 'light_auto' ? ' · 自动轻量' : '';
    const taPart = m.runtime_tradingagents_enabled
      ? ` · 讨论 ${m.tradingagents_discussed || 0}`
      : (m.tradingagents_enabled ? ' · 已跳过讨论' : '');
    const extra = m.stocks_scanned ? `分析 ${m.entries_count || 0}/${m.stocks_scanned}${modeText} · 均分 ${fmtNum(m.avg_score || 0, 1)}${taPart} · ${m.elapsed_sec || 0}s` : (m.error || '尚未执行');
    meta.textContent = `交易日 ${snap.trade_date || '--'} · 更新 ${fmtDateTime(snap.updated_at)} · ${extra}`;
  } else if (state.activeTab === 'graphic') {
    const snap = state.graphicFunnel;
    if (!snap) { meta.textContent = '图形选股 · 加载中…'; return; }
    const m = snap.meta || {};
    const extra = m.entries_count ? `命中 ${m.entries_count} · ${m.model_backend || 'baseline'} · ${m.elapsed_sec || 0}s` : (m.error || '尚未执行');
    meta.textContent = `交易日 ${snap.trade_date || '--'} · 更新 ${fmtDateTime(snap.updated_at)} · ${extra}`;
  }
}

function _updateMarketStatus() {
  const dot = document.getElementById('marketDot');
  const label = document.getElementById('marketLabel');
  if (!dot || !label) return;
  const now = new Date();
  const h = now.getHours(), m = now.getMinutes();
  const t = h * 60 + m;
  const day = now.getDay();
  const isWeekend = day === 0 || day === 6;
  let cls, text;
  if (isWeekend) {
    cls = 'close'; text = '休市';
  } else if (t >= 570 && t < 690) {
    cls = 'open'; text = '上午盘中';
  } else if (t >= 690 && t < 780) {
    cls = 'pre'; text = '午间休市';
  } else if (t >= 780 && t < 900) {
    cls = 'open'; text = '下午盘中';
  } else if (t >= 900) {
    cls = 'close'; text = '已收盘';
  } else {
    cls = 'pre'; text = '盘前';
  }
  dot.className = 'market-dot ' + cls;
  label.textContent = text;
  const timeStr = `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}`;
  label.title = timeStr;
}

function setStatus(text, tone = 'info') {
  const toast = document.createElement('div');
  toast.className = `toast toast-${tone}`;
  toast.textContent = text;
  document.body.appendChild(toast);
  requestAnimationFrame(() => toast.classList.add('show'));
  const duration = tone === 'error' ? 5000 : tone === 'success' ? 3500 : 2500;
  setTimeout(() => {
    toast.classList.remove('show');
    setTimeout(() => toast.remove(), 300);
  }, duration);
}

/* ==================== Page Summary Bars ==================== */

function _psItem(label, value, cls = '') {
  return `<span class="ps-item">${label} <span class="ps-value ${cls}">${value}</span></span>`;
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

async function request(path, options = {}) {
  let resp;
  try {
    resp = await fetch(path, {
      headers: { 'Content-Type': 'application/json' },
      ...options,
    });
  } catch (_) {
    throw new Error('后端服务不可用(连接失败)，请确认服务已启动: ./start.sh');
  }
  if (!resp.ok) {
    const raw = await resp.text();
    let message = '';
    try {
      const payload = JSON.parse(raw);
      message = payload.detail || payload.message || raw;
    } catch (_) {
      message = raw;
    }
    throw new Error(message || `HTTP ${resp.status}`);
  }
  return await resp.json();
}

/* ==================== Tab switching ==================== */

const TAB_TITLES = { market: '大盘', data: '数据中心', funnel: '策略选股', notice: '公告选股', predict: '预测选股', hotai: '热门智能', graphic: '图形选股', agent: '智能监控 / 进化', paper: '模拟盘' };

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
    _startPaperPoll();
  } else {
    _stopPaperPoll();
  }
  if (tab === 'predict') {
    loadPredictFunnel();
    _startPredictPoll();
  } else {
    _stopPredictPoll();
  }
  if (tab === 'hotai') {
    loadHotStockAI();
    _startHotAiPoll();
  } else {
    _stopHotAiPoll();
  }
  if (tab === 'graphic') {
    loadGraphicFunnel();
    _startGraphicPoll();
  } else {
    _stopGraphicPoll();
  }
  setTimeout(() => {
    if (state.chart) state.chart.resize();
    if (state.marketChart) state.marketChart.resize();
  }, 50);
}

/* ==================== Funnel tab ==================== */

function passConceptFilter(stock) {
  if (!state.selectedConcept) return true;
  return (stock.concept_tags || []).some((t) => t.name === state.selectedConcept);
}

function _getStrategyScanHits() {
  return state.strategyScanSnapshot?.hits || [];
}

function _getCompositeCandidateCount() {
  const strategyHits = _getStrategyScanHits().length;
  const funnelCandidates = (state.funnel?.pools?.candidate || []).filter(passConceptFilter).length;
  return strategyHits + funnelCandidates;
}

function renderCounts() {
  if (!state.funnel) return;
  const c = _getCompositeCandidateCount();
  const f = (state.funnel.pools.focus || []).filter(passConceptFilter).length;
  const b = (state.funnel.pools.buy || []).filter(passConceptFilter).length;
  document.getElementById('count-candidate').textContent = c;
  document.getElementById('count-focus').textContent = f;
  document.getElementById('count-buy').textContent = b;
}

function cardActions(stock) {
  const actions = [];
  if (stock.pool === 'candidate') actions.push(['加入重点', 'focus']);
  if (stock.pool === 'focus') { actions.push(['移回候选', 'candidate']); actions.push(['加入买入', 'buy']); }
  if (stock.pool === 'buy') actions.push(['降级重点', 'focus']);
  return actions;
}

function _isMarketOpen() {
  const now = new Date();
  const day = now.getDay();
  if (day === 0 || day === 6) return false;
  const t = now.getHours() * 60 + now.getMinutes();
  return (t >= 570 && t < 690) || (t >= 780 && t < 900);
}

async function paperBuy(symbol, name, _price) {
  if (!_isMarketOpen()) {
    setStatus('当前非交易时段，无法模拟买入', 'error');
    return;
  }
  const qty = prompt(`模拟买入 ${name}(${symbol})\n将以当前实时价成交\n请输入买入股数:`, '100');
  if (!qty) return;
  try {
    const res = await request('/api/paper/buy', {
      method: 'POST',
      body: JSON.stringify({ symbol, name, qty: parseInt(qty) || 100 }),
    });
    if (res.success) {
      const p = res.position || {};
      setStatus(`模拟买入 ${name} ${qty}股 @ ${fmtNum(res.realtime_price, 2)} (成本含滑点: ${fmtNum(p.cost_price, 2)})`, 'success');
      loadPaperData();
    }
  } catch (err) {
    setStatus(`模拟买入失败: ${err.message}`, 'error');
  }
}

async function paperSell(positionId, symbol, name) {
  if (!_isMarketOpen()) {
    setStatus('当前非交易时段，无法模拟卖出', 'error');
    return;
  }
  if (!confirm(`确认模拟卖出 ${name}(${symbol})？\n将以当前实时价成交`)) return;
  try {
    const res = await request('/api/paper/sell', {
      method: 'POST',
      body: JSON.stringify({ position_id: positionId }),
    });
    if (res.success) {
      const p = res.position || {};
      setStatus(`模拟卖出 ${name} @ ${fmtNum(res.realtime_price, 2)} · 盈亏 ${fmtNum(p.realized_pnl, 2)}`, 'success');
      loadPaperData();
    }
  } catch (err) {
    setStatus(`模拟卖出失败: ${err.message}`, 'error');
  }
}

async function movePool(symbol, targetPool) {
  try {
    const res = await request('/api/pool/move', {
      method: 'POST',
      body: JSON.stringify({ symbol, target_pool: targetPool }),
    });
    if (!res.success) alert(res.message || '迁移失败');
    await reloadFunnel();
  } catch (err) {
    alert(err.message || '迁移失败');
  }
}

function renderPool(poolName, list) {
  const root = document.getElementById(`pool-${poolName}`);
  if (poolName === 'candidate') {
    renderCompositeCandidatePool(root, list || []);
    return;
  }
  root.innerHTML = '';
  const filtered = list.filter(passConceptFilter);
  filtered.forEach((stock) => {
    const div = document.createElement('div');
    div.className = `stock-card ${state.selectedSymbol === stock.symbol ? 'active' : ''}`;
    div.onclick = () => selectSymbol(stock.symbol);
    const delta = Number(stock.score_delta || 0);
    const deltaCls = _chgCls(delta);
    const deltaTxt = delta > 0 ? `+${fmtNum(delta)}` : fmtNum(delta);
    const conceptTags = stock.concept_tags || [];
    const tags = conceptTags
      .map((tag) => `<span class="tag" style="background:${tag.color}" title="热度:${tag.heat} 涨幅:${fmtNum(tag.change_pct)} 涨停:${tag.limit_up_count}">${tag.name} ${fmtNum(tag.change_pct, 1)}%/${tag.limit_up_count}</span>`)
      .join('');
    const tagsHtml = tags ? `<div class="tags">${tags}</div>` : '';
    const badge = stock.recommended_pool
      ? `<span class="badge ${stock.recommended_pool === 'buy' ? 'buy' : 'focus'}">建议进入${stock.recommended_pool === 'buy' ? '买入池' : '重点池'}</span>`
      : '';
    const btns = cardActions(stock)
      .map(([txt, pool]) => `<button data-pool="${pool}" data-symbol="${stock.symbol}">${txt}</button>`)
      .join('');
    const simBuyBtn = poolName === 'buy'
      ? `<button class="btn-sim-buy" data-symbol="${stock.symbol}" data-name="${stock.name}" data-price="${stock.price || stock.breakout_level || 0}">模拟买入</button>`
      : '';
    const pct = Number(stock.pct_change || 0);
    const pctCls = _chgCls(pct);
    const pctSign = pct > 0 ? '+' : '';
    const price = Number(stock.price || 0);
    const priceHtml = price > 0 ? `<span class="stock-price">${fmtNum(price, 2)}</span>` : '';
    const pctHtml = pct !== 0 ? ` <span class="${pctCls}">${pctSign}${fmtNum(pct, 2)}%</span>` : '';
    div.innerHTML = `
      <div class="stock-top">
        <div class="stock-name">${stock.name} <span class="stock-code">${stock.symbol}</span></div>
        <div class="stock-price-area">${priceHtml}${pctHtml}</div>
      </div>
      ${tagsHtml}
      <div class="metrics">
        <span>评分 <b class="${deltaCls}">${fmtNum(stock.score)}</b> (${deltaTxt})</span>
        <span class="metrics-sep">·</span>
        <span>放量比 ${fmtNum(stock.volume_ratio, 2)}</span>
        <span class="metrics-sep">·</span>
        <span>突破位 ${fmtNum(stock.breakout_level, 2)}</span>
      </div>
      ${badge}
      <div class="card-actions">${btns}${simBuyBtn}</div>
    `;
    div.querySelectorAll('button[data-pool]').forEach((btn) => {
      btn.onclick = (e) => { e.stopPropagation(); movePool(btn.dataset.symbol, btn.dataset.pool); };
    });
    const sbBtn = div.querySelector('.btn-sim-buy');
    if (sbBtn) {
      sbBtn.onclick = (e) => { e.stopPropagation(); paperBuy(sbBtn.dataset.symbol, sbBtn.dataset.name, Number(sbBtn.dataset.price)); };
    }
    root.appendChild(div);
  });
  if (!filtered.length) {
    const hint = state.selectedConcept
      ? `当前概念「${state.selectedConcept}」下无匹配股票`
      : '尚未运行筛选，点击上方「盘后筛选」开始';
    root.innerHTML = `<div class="empty-state"><div class="empty-state-text">${hint}</div></div>`;
  }
}

function _renderCompositeSection(title, hint, bodyHtml, count, kind = '') {
  const countHtml = `<span class="composite-section-count">${count}</span>`;
  return `
    <section class="composite-section ${kind}">
      <div class="composite-section-head">
        <div class="composite-section-title">${title}${countHtml}</div>
        <div class="composite-section-hint">${hint}</div>
      </div>
      <div class="composite-section-body">${bodyHtml}</div>
    </section>
  `;
}

function _strategyHitCardHtml(hit) {
  const cls = Number(hit.change_pct || 0) >= 0 ? 'up' : 'down';
  const sign = Number(hit.change_pct || 0) >= 0 ? '+' : '';
  const badges = (hit.rule_hits || []).slice(0, 4).map((rh) => (
    `<span class="sc-hit-tag">${esc(rh.title)}: ${esc(String(rh.label || '✓'))}</span>`
  )).join('');
  return `
    <div class="qb-card sc-hit-card sc-hit-card-inline" data-symbol="${hit.symbol}" data-name="${esc(hit.name || '')}">
      <div class="qb-card-top">
        <div class="qb-card-name">${esc(hit.name || '--')}<small>${hit.symbol}</small></div>
        <div class="qb-card-pct ${cls}">${sign}${fmtNum(hit.change_pct, 2)}%</div>
      </div>
      <div class="qb-card-meta">
        <span class="qb-tag">收 ${fmtNum(hit.close, 2)}</span>
        <span class="qb-tag spike">综合分 ${fmtNum(hit.composite_score, 1)}</span>
      </div>
      ${badges ? `<div class="sc-hit-tags">${badges}</div>` : ''}
    </div>
  `;
}

function renderCompositeCandidatePool(root, systemCandidates) {
  if (!root) return;
  const strategyHits = _getStrategyScanHits();
  const filteredSystem = (systemCandidates || []).filter(passConceptFilter);
  const parts = [];

  if (strategyHits.length) {
    parts.push(_renderCompositeSection(
      '策略中心命中',
      '来自当前策略的最新扫描结果',
      strategyHits.slice(0, 200).map(_strategyHitCardHtml).join(''),
      strategyHits.length,
      'strategy',
    ));
  } else {
    parts.push(_renderCompositeSection(
      '策略中心命中',
      '暂无策略命中，先运行上方扫描',
      '<div class="empty-state compact-empty"><div class="empty-state-text">当前策略还没有扫描命中结果</div></div>',
      0,
      'strategy',
    ));
  }

  if (filteredSystem.length) {
    const host = document.createElement('div');
    filteredSystem.forEach((stock) => {
      const div = document.createElement('div');
      div.className = `stock-card ${state.selectedSymbol === stock.symbol ? 'active' : ''}`;
      div.onclick = () => selectSymbol(stock.symbol);
      const delta = Number(stock.score_delta || 0);
      const deltaCls = _chgCls(delta);
      const deltaTxt = delta > 0 ? `+${fmtNum(delta)}` : fmtNum(delta);
      const conceptTags = stock.concept_tags || [];
      const tags = conceptTags
        .map((tag) => `<span class="tag" style="background:${tag.color}" title="热度:${tag.heat} 涨幅:${fmtNum(tag.change_pct)} 涨停:${tag.limit_up_count}">${tag.name} ${fmtNum(tag.change_pct, 1)}%/${tag.limit_up_count}</span>`)
        .join('');
      const tagsHtml = tags ? `<div class="tags">${tags}</div>` : '';
      const badge = stock.recommended_pool
        ? `<span class="badge ${stock.recommended_pool === 'buy' ? 'buy' : 'focus'}">建议进入${stock.recommended_pool === 'buy' ? '买入池' : '重点池'}</span>`
        : '';
      const btns = cardActions(stock)
        .map(([txt, pool]) => `<button data-pool="${pool}" data-symbol="${stock.symbol}">${txt}</button>`)
        .join('');
      const pct = Number(stock.pct_change || 0);
      const pctCls = _chgCls(pct);
      const pctSign = pct > 0 ? '+' : '';
      const price = Number(stock.price || 0);
      const priceHtml = price > 0 ? `<span class="stock-price">${fmtNum(price, 2)}</span>` : '';
      const pctHtml = pct !== 0 ? ` <span class="${pctCls}">${pctSign}${fmtNum(pct, 2)}%</span>` : '';
      div.innerHTML = `
        <div class="stock-top">
          <div class="stock-name">${stock.name} <span class="stock-code">${stock.symbol}</span></div>
          <div class="stock-price-area">${priceHtml}${pctHtml}</div>
        </div>
        ${tagsHtml}
        <div class="metrics">
          <span>评分 <b class="${deltaCls}">${fmtNum(stock.score)}</b> (${deltaTxt})</span>
          <span class="metrics-sep">·</span>
          <span>放量比 ${fmtNum(stock.volume_ratio, 2)}</span>
          <span class="metrics-sep">·</span>
          <span>突破位 ${fmtNum(stock.breakout_level, 2)}</span>
        </div>
        ${badge}
        <div class="card-actions">${btns}</div>
      `;
      div.querySelectorAll('button[data-pool]').forEach((btn) => {
        btn.onclick = (e) => { e.stopPropagation(); movePool(btn.dataset.symbol, btn.dataset.pool); };
      });
      host.appendChild(div);
    });
    parts.push(_renderCompositeSection(
      '系统候选',
      state.selectedConcept ? `当前概念：${state.selectedConcept}` : '来自系统漏斗的调整期候选',
      host.innerHTML,
      filteredSystem.length,
      'system',
    ));
  } else {
    const hint = state.selectedConcept
      ? `当前概念「${state.selectedConcept}」下无系统候选`
      : '尚未运行筛选，点击上方「盘后筛选」开始';
    parts.push(_renderCompositeSection(
      '系统候选',
      '来自系统漏斗的调整期候选',
      `<div class="empty-state compact-empty"><div class="empty-state-text">${hint}</div></div>`,
      0,
      'system',
    ));
  }

  root.innerHTML = parts.join('');
  root.querySelectorAll('.sc-hit-card').forEach((el) => {
    el.onclick = (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      openPredictDetail(el.getAttribute('data-symbol'), el.getAttribute('data-name'));
      return false;
    };
  });
}

function renderFunnel() {
  if (!state.funnel) return;
  renderCounts();
  renderPool('candidate', state.funnel.pools.candidate || []);
  renderPool('focus', state.funnel.pools.focus || []);
  renderPool('buy', state.funnel.pools.buy || []);
  renderFunnelSummary();
  if (state.activeTab === 'funnel') setMeta();
}

function renderHotConcepts() {
  const root = document.getElementById('hotConcepts');
  root.innerHTML = '';
  const activeChip = document.getElementById('activeConcept');
  if (activeChip) {
    activeChip.textContent = state.selectedConcept || '全部';
    activeChip.classList.toggle('muted', !state.selectedConcept);
    activeChip.classList.toggle('active', !!state.selectedConcept);
  }
  const filterHint = document.getElementById('conceptFilterHint');
  if (filterHint) {
    filterHint.textContent = state.selectedConcept ? `已筛选「${state.selectedConcept}」` : '';
  }
  const items = (state.hotConcepts?.items || []).slice(0, 10);
  items.forEach((item) => {
    const rise = Number(item.change_pct || 0) >= 0;
    const cls = rise ? 'up' : 'down';
    const sign = rise ? '+' : '';
    const div = document.createElement('div');
    div.className = `hot-item ${state.selectedConcept === item.name ? 'active' : ''}`;
    div.onclick = () => {
      state.selectedConcept = state.selectedConcept === item.name ? null : item.name;
      renderHotConcepts();
      if (state.activeTab === 'funnel') renderFunnel();
    };
    div.innerHTML = `
      <div class="hot-title"><span>${item.name}</span><span class="${cls}">${sign}${fmtNum(item.change_pct, 2)}%</span></div>
      <div class="hot-meta">热度 ${fmtNum(item.heat, 3)} · 涨停 ${item.limit_up_count} · 上涨 ${item.up_count} / 下跌 ${item.down_count}</div>
      <div class="hot-meta">领涨 ${item.leader || '-'} · 入选 ${item.selected_count}</div>
    `;
    root.appendChild(div);
  });
  if (!items.length) root.innerHTML = '<div class="empty-state"><div class="empty-state-text">暂无概念数据</div></div>';
  renderMarketSummary();
}

function renderHotStocks() {
  const root = document.getElementById('hotStocks');
  root.innerHTML = '';
  const items = state.hotStocks?.items || [];
  items.forEach((item) => {
    const dayPct = Number(item.change_pct || 0);
    const tenPct = Number(item.cumulative_10d_pct || 0);
    const dayCls = dayPct >= 0 ? 'up' : 'down';
    const tenCls = tenPct >= 0 ? 'up' : 'down';
    const daySign = dayPct >= 0 ? '+' : '';
    const tenSign = tenPct >= 0 ? '+' : '';
    const card = document.createElement('div');
    card.className = `hot-stock-item ${state.selectedHotSymbol === item.symbol ? 'active' : ''}`;
    card.onclick = () => selectHotStock(item);
    card.innerHTML = `
      <div class="hot-stock-main"><div class="hot-stock-rank">#${item.rank}</div><div class="hot-stock-name">${item.name} (${item.symbol})</div></div>
      <div class="hot-stock-side">
        <span class="hot-stock-price">${fmtNum(item.latest_price, 2)}</span>
        <span class="${dayCls}">今日 ${daySign}${fmtNum(dayPct, 2)}%</span>
        <span class="${tenCls}">10日 ${tenSign}${fmtNum(tenPct, 2)}%</span>
      </div>
    `;
    root.appendChild(card);
  });
  if (!items.length) root.innerHTML = '<div class="empty-state"><div class="empty-state-text">暂无热门个股数据</div></div>';
}

/* ==================== 数据中心 ==================== */

let _dcTaskPage = 1;

async function loadDataCenter() {
  const [statsRes, statusRes, reportRes, logsRes] = await Promise.allSettled([
    request('/api/jobs/kline-cache/stats'),
    request('/api/jobs/kline-cache/status'),
    request('/api/jobs/kline-cache/report'),
    request('/api/jobs/kline-cache/logs?page=' + _dcTaskPage + '&page_size=15'),
  ]);
  const stats = statsRes.status === 'fulfilled' ? statsRes.value : {};
  const syncStatus = statusRes.status === 'fulfilled' ? statusRes.value : {};
  const report = reportRes.status === 'fulfilled' ? reportRes.value : null;
  const logs = logsRes.status === 'fulfilled' ? logsRes.value : { items: [], total: 0 };

  state.syncStatus = syncStatus;
  state.syncLogs = logs;
  state.dcStats = stats;
  state.dcReport = report;

  renderDcStats(stats, syncStatus, report);
  renderDcProgress(syncStatus);
  renderDcReport(report);
  renderDcTaskList(logs);
  renderDcLogStream(logs);
  renderDataSummary();
}

function _dcHealthLevel(coverage) {
  if (coverage == null) return { label: '未检查', cls: 'brand', icon: '○' };
  if (coverage >= 99) return { label: '健康', cls: 'success', icon: '●' };
  if (coverage >= 90) return { label: '警告', cls: 'warning', icon: '▲' };
  return { label: '严重缺失', cls: 'error', icon: '✖' };
}

function renderDcStats(stats, syncStatus, report) {
  const cov = report?.coverage_pct;
  const health = _dcHealthLevel(cov);
  const syncLabel = { idle: '空闲', running: '同步中', success: '已完成', failed: '失败' }[syncStatus.status] || '--';
  const syncCls = syncStatus.status === 'running' ? 'warning' : (syncStatus.status === 'success' ? 'success' : (syncStatus.status === 'failed' ? 'error' : 'brand'));
  const missing = report?.total_missing || 0;

  const alertBar = document.getElementById('dcAlertBar');
  if (cov != null && cov < 90) {
    alertBar.style.display = '';
    alertBar.className = `dc-alert-bar dc-alert-${health.cls}`;
    alertBar.innerHTML = `<span class="dc-alert-icon">${health.icon}</span>数据覆盖率仅 ${cov.toFixed(1)}%，缺失 ${missing.toLocaleString()} 条，建议执行 <strong>全量补缺</strong>`;
  } else {
    alertBar.style.display = 'none';
  }

  const healthCard = document.getElementById('dcHealthCard');
  const circumference = 2 * Math.PI * 26;
  const offset = cov != null ? circumference * (1 - cov / 100) : circumference;
  const ringColor = cov == null ? 'var(--muted)' : (cov >= 99 ? '#22c55e' : (cov >= 90 ? '#f59e0b' : '#ef4444'));
  healthCard.innerHTML = `
    <div class="dc-health-ring">
      <svg viewBox="0 0 64 64"><circle class="ring-bg" cx="32" cy="32" r="26"/><circle class="ring-fg" cx="32" cy="32" r="26" stroke="${ringColor}" stroke-dasharray="${circumference}" stroke-dashoffset="${offset}"/></svg>
      <div class="dc-health-pct">${cov != null ? cov.toFixed(1) + '%' : '--'}</div>
    </div>
    <div class="dc-health-info">
      <div class="dc-health-title"><span class="dc-health-dot ${health.cls}"></span>数据健康: <strong class="${health.cls}">${health.label}</strong></div>
      <div class="dc-health-sub">任务状态: <span class="${syncCls}">${syncLabel}</span>${syncStatus.updated_at ? ' · ' + fmtDateTime(syncStatus.updated_at) : ''}</div>
      <div class="dc-health-sub">最近同步: ${syncStatus.last_success_trade_date || '--'} (${syncStatus.trigger_mode || '--'})${missing > 0 ? ' · 缺失 <strong style="color:#f59e0b">' + missing.toLocaleString() + '</strong> 条' : ''}</div>
    </div>
  `;

  const kpiRow = document.getElementById('dcKpiRow');
  kpiRow.innerHTML = `
    <div class="dc-kpi"><span class="dc-kpi-val">${(stats.symbol_count || 0).toLocaleString()}</span><span class="dc-kpi-label">股票数</span></div>
    <div class="dc-kpi"><span class="dc-kpi-val">${(stats.row_count || 0).toLocaleString()}</span><span class="dc-kpi-label">K线总条数</span></div>
    <div class="dc-kpi"><span class="dc-kpi-val">${stats.min_date || '--'}</span><span class="dc-kpi-label">最早日期</span></div>
    <div class="dc-kpi"><span class="dc-kpi-val">${stats.max_date || '--'}</span><span class="dc-kpi-label">最新日期</span></div>
    <div class="dc-kpi"><span class="dc-kpi-val">${stats.db_size_mb ?? '--'} MB</span><span class="dc-kpi-label">数据库</span></div>
  `;

  const hint = document.getElementById('dcSyncHint');
  if (hint) {
    const lastDate = syncStatus.last_success_trade_date;
    hint.textContent = lastDate ? `最近同步: ${lastDate}` : '';
  }
}

function renderDcProgress(syncStatus) {
  const section = document.getElementById('dcProgressSection');
  const textEl = document.getElementById('dcProgressText');
  const barEl = document.getElementById('dcProgressBar');
  if (!syncStatus || syncStatus.status !== 'running') {
    section.style.display = 'none';
    return;
  }
  section.style.display = '';
  const synced = Number(syncStatus.synced_symbols || 0);
  const total = Number(syncStatus.total_symbols || 0);
  const pct = Number(syncStatus.progress_pct || 0);
  textEl.textContent = `${syncStatus.message || '同步中'} · ${synced}/${total} (${pct.toFixed(1)}%)`;
  barEl.style.width = Math.max(0, Math.min(100, pct)) + '%';
}

let _dcReportSortBy = 'date';

function renderDcReport(report) {
  const container = document.getElementById('dcReportContent');
  const sortEl = document.getElementById('dcReportSort');

  if (!report || report.status === 'none' || report.status === 'error') {
    container.innerHTML = `<div class="detail-empty">${report?.message || '暂无检查报告，点击上方按钮执行检查'}</div>`;
    if (sortEl) sortEl.innerHTML = '';
    return;
  }

  const pct = report.coverage_pct || 0;
  const health = _dcHealthLevel(pct);
  const checkTime = (report.check_time || '').slice(0, 19).replace('T', ' ');

  if (sortEl) {
    sortEl.innerHTML = `<button class="text-btn text-btn-sm dc-sort-btn" id="btnDcReportSort">${_dcReportSortBy === 'date' ? '按日期' : '按缺失量'} ▾</button>`;
    document.getElementById('btnDcReportSort').onclick = () => {
      _dcReportSortBy = _dcReportSortBy === 'date' ? 'missing' : 'date';
      renderDcReport(state.dcReport);
    };
  }

  let html = `<div class="dc-report-conclusion ${health.cls}">最近 ${report.trade_days_checked || 0} 个交易日覆盖率 <strong>${pct.toFixed(1)}%</strong>，${report.total_missing ? '缺失 <strong>' + (report.total_missing).toLocaleString() + '</strong> 条' : '数据完整'}${pct < 90 ? '，建议执行全量补缺' : ''}</div>`;

  html += `<div class="dc-report-metrics">
    <div class="dc-rm"><span class="dc-rm-val">${(report.total_expected || 0).toLocaleString()}</span><span class="dc-rm-label">期望条数</span></div>
    <div class="dc-rm"><span class="dc-rm-val">${(report.total_actual || 0).toLocaleString()}</span><span class="dc-rm-label">实际条数</span></div>
    <div class="dc-rm"><span class="dc-rm-val ${report.total_missing ? 'warning' : 'success'}">${(report.total_missing || 0).toLocaleString()}</span><span class="dc-rm-label">缺失条数</span></div>
    <div class="dc-rm"><span class="dc-rm-val">${checkTime}</span><span class="dc-rm-label">检查时间</span></div>
  </div>`;

  let missingDates = [...(report.missing_by_date || [])];
  if (missingDates.length > 0) {
    if (_dcReportSortBy === 'missing') {
      missingDates.sort((a, b) => (b.missing_count || 0) - (a.missing_count || 0));
    }
    html += `<div class="dc-missing-header">缺失日期 (${missingDates.length} 天)</div>`;
    html += '<div class="dc-missing-list">';
    for (const d of missingDates) {
      const barPct = d.coverage_pct || 0;
      const barColor = barPct >= 99 ? '#22c55e' : (barPct >= 90 ? '#f59e0b' : '#ef4444');
      html += `<div class="dc-missing-row">
          <span class="dc-missing-date">${d.date}</span>
          <span class="dc-missing-count">缺 ${(d.missing_count || 0).toLocaleString()}</span>
          <div class="dc-missing-bar-wrap"><div class="dc-missing-bar-fill" style="width:${barPct}%;background:${barColor}"></div></div>
          <span class="dc-missing-pct">覆盖 ${barPct.toFixed(1)}%</span>
        </div>`;
    }
    html += '</div>';
  } else {
    html += '<div style="color:#22c55e;font-size:13px;font-weight:600;padding:8px 0">数据完整，无缺失</div>';
  }

  container.innerHTML = html;
}

function renderDcTaskFilters() {
  const container = document.getElementById('dcTaskFilters');
  if (!container) return;
  const filters = [
    { key: 'all', label: '全部' },
    { key: 'success', label: '成功' },
    { key: 'failed', label: '失败' },
    { key: 'running', label: '运行中' },
  ];
  container.innerHTML = filters.map(f => {
    const active = state.dcTaskFilter === f.key ? ' btn-secondary active' : ' btn-secondary';
    return `<button class="${active}" data-filter="${f.key}">${f.label}</button>`;
  }).join('');
  container.querySelectorAll('button').forEach(btn => {
    btn.onclick = () => {
      state.dcTaskFilter = btn.dataset.filter;
      renderDcTaskFilters();
      renderDcTaskList(state.syncLogs || { items: [], total: 0 });
    };
  });
}

function renderDcTaskList(logs) {
  const container = document.getElementById('dcTaskList');
  const pager = document.getElementById('dcTaskPager');
  let items = logs.items || [];

  renderDcTaskFilters();

  if (state.dcTaskFilter !== 'all') {
    items = items.filter(t => t.status === state.dcTaskFilter);
  }

  if (!items.length) {
    container.innerHTML = '<div class="empty-state"><div class="empty-state-text">暂无历史任务，点击上方按钮发起同步</div></div>';
    pager.innerHTML = '';
    return;
  }

  let html = `<div class="dc-task-header">
    <span class="dc-th dc-th-status"></span>
    <span class="dc-th dc-th-date">交易日</span>
    <span class="dc-th dc-th-mode">触发</span>
    <span class="dc-th dc-th-ok">成功</span>
    <span class="dc-th dc-th-fail">失败</span>
    <span class="dc-th dc-th-total">总数</span>
    <span class="dc-th dc-th-time">时间</span>
  </div>`;

  html += items.map(t => {
    const time = (t.started_at || '').slice(11, 16);
    const dotCls = t.status === 'success' ? 'success' : (t.status === 'running' ? 'warning' : (t.status === 'failed' ? 'error' : 'progress'));
    const failRow = t.status === 'failed' && t.message ? `<div class="dc-task-error">${esc(t.message)}</div>` : '';
    const rawMode = t.trigger_mode || '';
    const shortMode = rawMode.split('_')[0] || rawMode;
    return `<div class="dc-task-item">
        <span class="dc-td dc-th-status"><span class="status-dot status-dot--${dotCls}"></span></span>
        <span class="dc-td dc-th-date">${t.trade_date}</span>
        <span class="dc-td dc-th-mode" title="${esc(rawMode)}">${esc(shortMode)}</span>
        <span class="dc-td dc-th-ok success">${t.success_symbols || 0}</span>
        <span class="dc-td dc-th-fail ${(t.failed_symbols || 0) > 0 ? 'error' : ''}">${t.failed_symbols || 0}</span>
        <span class="dc-td dc-th-total">${t.total_symbols || 0}</span>
        <span class="dc-td dc-th-time">${time}</span>
        ${failRow}
      </div>`;
  }).join('');

  container.innerHTML = html;

  const totalPages = Math.ceil((logs.total || 0) / 15);
  if (totalPages > 1) {
    pager.innerHTML = `
      <button ${_dcTaskPage <= 1 ? 'disabled' : ''} onclick="dcTaskPageNav(-1)">上一页</button>
      <span>${_dcTaskPage}/${totalPages}</span>
      <button ${_dcTaskPage >= totalPages ? 'disabled' : ''} onclick="dcTaskPageNav(1)">下一页</button>
    `;
  } else {
    pager.innerHTML = '';
  }
}

function dcTaskPageNav(delta) {
  _dcTaskPage = Math.max(1, _dcTaskPage + delta);
  loadDataCenter();
}

function renderDcLogStream(logs) {
  const container = document.getElementById('dcLogStream');
  const toggleBtn = document.getElementById('btnDcLogsToggle');
  const items = logs.items || [];
  if (!items.length) {
    container.innerHTML = '<div class="empty-state"><div class="empty-state-text">暂无同步日志，发起同步后将实时显示</div></div>';
    if (toggleBtn) toggleBtn.style.display = 'none';
    return;
  }
  const showCount = state.dcLogsExpanded ? items.length : Math.min(5, items.length);
  container.innerHTML = items.slice(0, showCount).map(t => `
    <div class="sync-log-item">[${t.status}] ${t.trade_date} ${t.synced_symbols}/${t.total_symbols} (${t.trigger_mode}) ${t.message || ''}</div>
  `).join('');
  if (toggleBtn) {
    toggleBtn.style.display = items.length > 5 ? '' : 'none';
    toggleBtn.textContent = state.dcLogsExpanded ? '收起日志' : `展开全部 (${items.length})`;
    toggleBtn.className = 'collapse-toggle' + (state.dcLogsExpanded ? ' expanded' : '');
  }
}

/* ==================== Chart ==================== */

function _initChart(domId, stateKey) {
  if (state[stateKey]) return state[stateKey];
  const dom = document.getElementById(domId);
  if (!dom || !window.echarts) return null;
  state[stateKey] = window.echarts.init(dom);
  return state[stateKey];
}

function ensureChart() { return _initChart('klineChart', 'chart'); }
function ensureMarketChart() { return _initChart('marketKlineChart', 'marketChart'); }

window.addEventListener('resize', () => {
  if (state.chart) state.chart.resize();
  if (state.marketChart) state.marketChart.resize();
});

function _calcMA(closes, period) {
  const result = [];
  for (let i = 0; i < closes.length; i++) {
    if (i < period - 1) { result.push(null); continue; }
    let sum = 0;
    for (let j = 0; j < period; j++) sum += closes[i - j];
    result.push(+(sum / period).toFixed(2));
  }
  return result;
}

function _klineOption(rows) {
  const categoryData = rows.map((x) => x.date);
  const candleData = rows.map((x) => [x.open, x.close, x.low, x.high]);
  const volumeData = rows.map((x, idx) => [idx, x.volume, x.close >= x.open ? 1 : -1]);
  const closes = rows.map((x) => x.close);
  const ma5 = _calcMA(closes, 5);
  const ma10 = _calcMA(closes, 10);
  const ma30 = _calcMA(closes, 30);
  return {
    animation: false, backgroundColor: 'transparent',
    legend: {
      show: true, top: 0, right: 20,
      textStyle: { color: '#8da2bb', fontSize: 11 },
      itemWidth: 14, itemHeight: 2, itemGap: 12,
      data: ['MA5', 'MA10', 'MA30'],
    },
    tooltip: { trigger: 'axis', axisPointer: { type: 'cross' } },
    axisPointer: { link: [{ xAxisIndex: 'all' }], label: { backgroundColor: '#334155' } },
    grid: [{ left: 52, right: 18, top: 28, height: '58%' }, { left: 52, right: 18, top: '77%', height: '16%' }],
    xAxis: [
      { type: 'category', data: categoryData, boundaryGap: true, axisLine: { lineStyle: { color: '#64748b' } }, axisLabel: { color: '#94a3b8', fontSize: 11 }, splitLine: { show: false }, min: 'dataMin', max: 'dataMax' },
      { type: 'category', gridIndex: 1, data: categoryData, boundaryGap: true, axisLine: { lineStyle: { color: '#64748b' } }, axisLabel: { show: false }, splitLine: { show: false }, min: 'dataMin', max: 'dataMax' },
    ],
    yAxis: [
      { scale: true, splitArea: { show: false }, axisLine: { lineStyle: { color: '#64748b' } }, axisLabel: { color: '#94a3b8', fontSize: 11 }, splitLine: { lineStyle: { color: '#1e293b' } } },
      { scale: true, gridIndex: 1, splitNumber: 2, axisLine: { lineStyle: { color: '#64748b' } }, axisLabel: { color: '#94a3b8', fontSize: 11 }, splitLine: { lineStyle: { color: '#1e293b' } } },
    ],
    dataZoom: [
      { type: 'inside', xAxisIndex: [0, 1], start: 0, end: 100 },
      { show: true, xAxisIndex: [0, 1], type: 'slider', bottom: '3%', borderColor: '#475569' },
    ],
    series: [
      { name: '日K', type: 'candlestick', data: candleData, itemStyle: { color: '#ef4444', color0: '#16a34a', borderColor: '#ef4444', borderColor0: '#16a34a' } },
      { name: 'MA5', type: 'line', data: ma5, smooth: true, symbol: 'none', lineStyle: { width: 1, color: '#fbbf24' }, connectNulls: false },
      { name: 'MA10', type: 'line', data: ma10, smooth: true, symbol: 'none', lineStyle: { width: 1, color: '#60a5fa' }, connectNulls: false },
      { name: 'MA30', type: 'line', data: ma30, smooth: true, symbol: 'none', lineStyle: { width: 1, color: '#a78bfa' }, connectNulls: false },
      { name: '成交量', type: 'bar', xAxisIndex: 1, yAxisIndex: 1, data: volumeData, itemStyle: { color: (p) => (p.data[2] > 0 ? '#ef4444' : '#16a34a') } },
    ],
  };
}

/**
 * 将 Kronos 返回的 merged_kline (默认 180 历史 + N 预测) 截取为"最近 30 根历史 + 全部预测"，
 * 以统一前端 K 线展示窗口为 30 天。
 * 返回 [slicedMerged, newPredStartIdx]
 */
function _sliceMergedForDisplay(merged, predStartIdx, historyDays = 30) {
  if (!Array.isArray(merged) || merged.length === 0) return [merged, predStartIdx];
  const total = merged.length;
  const predIdx = Number.isFinite(predStartIdx) ? predStartIdx : total;
  const histStart = Math.max(0, predIdx - historyDays);
  if (histStart === 0) return [merged, predIdx];
  return [merged.slice(histStart), predIdx - histStart];
}

function _klinePredictOption(merged, predStartIdx, realtimeMap) {
  [merged, predStartIdx] = _sliceMergedForDisplay(merged, predStartIdx);
  realtimeMap = realtimeMap || {};
  const dates = merged.map(x => x.date);

  const candles = merged.map((x, i) => {
    const val = [x.open, x.close, x.low, x.high];
    if (i >= predStartIdx) {
      const isUp = x.close >= x.open;
      return {
        value: val,
        itemStyle: {
          color: 'transparent',
          borderColor: isUp ? 'rgba(239,68,68,0.7)' : 'rgba(22,163,106,0.7)',
          borderWidth: 1.5,
          borderType: 'dashed',
        },
      };
    }
    return val;
  });

  const realCandles = dates.map((d, i) => {
    const rt = realtimeMap[d];
    if (!rt || i < predStartIdx) return '-';
    return { value: [rt.open, rt.close, rt.low, rt.high] };
  });

  const volumes = merged.map((x, i) => {
    if (i >= predStartIdx) {
      const rt = realtimeMap[dates[i]];
      if (rt && rt.volume) return { value: [i, rt.volume, 1], itemStyle: { color: rt.close >= rt.open ? '#ef4444' : '#16a34a' } };
      const isUp = x.close >= x.open;
      const bc = isUp ? 'rgba(239,68,68,0.7)' : 'rgba(22,163,106,0.7)';
      return { value: [i, x.volume || 0, 5], itemStyle: { color: 'transparent', borderColor: bc, borderWidth: 1, borderType: 'dashed' } };
    }
    return [i, x.volume, x.close >= x.open ? 1 : -1];
  });

  const closes = merged.map(x => x.close);
  const ma5 = _calcMA(closes, 5);
  const ma10 = _calcMA(closes, 10);
  const ma30 = _calcMA(closes, 30);

  const predBoundary = predStartIdx > 0 ? dates[predStartIdx] : null;
  const lastDate = dates[dates.length - 1];
  const hasRealtime = Object.keys(realtimeMap).length > 0;

  const legendData = ['MA5', 'MA10', 'MA30'];
  if (hasRealtime) legendData.push('实际');

  return {
    animation: false, backgroundColor: 'transparent',
    legend: {
      show: true, top: 0, right: 20,
      textStyle: { color: '#8da2bb', fontSize: 11 },
      itemWidth: 14, itemHeight: 2, itemGap: 12,
      data: legendData,
    },
    tooltip: {
      trigger: 'axis', axisPointer: { type: 'cross' },
      backgroundColor: 'rgba(15,23,42,0.92)', borderColor: '#334155',
      textStyle: { color: '#e2e8f0', fontSize: 12 },
      formatter: function(params) {
        if (!params || !params.length) return '';
        const idx = params[0].dataIndex;
        const isPred = idx >= predStartIdx;
        const k = merged[idx];
        const rt = realtimeMap[k.date];
        let html = `<div style="font-weight:600;margin-bottom:4px">${k.date}</div>`;
        if (isPred) {
          html += `<div style="color:#facc15;margin-bottom:3px">── 预测 ──</div>`;
          html += `<div>收: ${fmtNum(k.close,2)} &nbsp; 高: ${fmtNum(k.high,2)}</div>`;
          html += `<div>开: ${fmtNum(k.open,2)} &nbsp; 低: ${fmtNum(k.low,2)}</div>`;
          if (k.volume) html += `<div style="color:#94a3b8">量: ${(k.volume / 10000).toFixed(0)}万 (预)</div>`;
          if (rt) {
            html += `<div style="color:#38bdf8;margin:3px 0">── 实际 ──</div>`;
            html += `<div>收: ${fmtNum(rt.close,2)} &nbsp; 高: ${fmtNum(rt.high,2)}</div>`;
            html += `<div>开: ${fmtNum(rt.open,2)} &nbsp; 低: ${fmtNum(rt.low,2)}</div>`;
            if (rt.volume) html += `<div>量: ${(rt.volume / 10000).toFixed(0)}万</div>`;
            const diff = ((rt.close - k.close) / k.close * 100).toFixed(2);
            const diffColor = diff >= 0 ? '#ef4444' : '#16a34a';
            html += `<div style="color:${diffColor};margin-top:2px">偏差: ${diff >= 0 ? '+' : ''}${diff}%</div>`;
          }
        } else {
          html += `<div>收: ${fmtNum(k.close,2)} &nbsp; 高: ${fmtNum(k.high,2)}</div>`;
          html += `<div>开: ${fmtNum(k.open,2)} &nbsp; 低: ${fmtNum(k.low,2)}</div>`;
          if (k.volume) html += `<div>量: ${(k.volume / 10000).toFixed(0)}万</div>`;
        }
        return html;
      }
    },
    axisPointer: { link: [{ xAxisIndex: 'all' }], label: { backgroundColor: '#334155' } },
    grid: [{ left: 52, right: 18, top: 28, height: '58%' }, { left: 52, right: 18, top: '77%', height: '16%' }],
    xAxis: [
      { type: 'category', data: dates, boundaryGap: true, axisLine: { lineStyle: { color: '#64748b' } }, axisLabel: { color: '#94a3b8', fontSize: 11 }, splitLine: { show: false } },
      { type: 'category', gridIndex: 1, data: dates, boundaryGap: true, axisLine: { lineStyle: { color: '#64748b' } }, axisLabel: { show: false }, splitLine: { show: false } },
    ],
    yAxis: [
      { scale: true, splitArea: { show: false }, axisLine: { lineStyle: { color: '#64748b' } }, axisLabel: { color: '#94a3b8', fontSize: 11 }, splitLine: { lineStyle: { color: '#1e293b' } } },
      { scale: true, gridIndex: 1, splitNumber: 2, axisLine: { lineStyle: { color: '#64748b' } }, axisLabel: { color: '#94a3b8', fontSize: 11 }, splitLine: { lineStyle: { color: '#1e293b' } } },
    ],
    dataZoom: [
      { type: 'inside', xAxisIndex: [0, 1], start: 0, end: 100 },
      { show: true, xAxisIndex: [0, 1], type: 'slider', bottom: '3%', borderColor: '#475569' },
    ],
    series: [
      {
        name: '日K', type: 'candlestick', data: candles,
        itemStyle: { color: '#ef4444', color0: '#16a34a', borderColor: '#ef4444', borderColor0: '#16a34a' },
        markArea: predBoundary ? {
          silent: true,
          itemStyle: { color: 'rgba(250, 204, 21, 0.14)', borderColor: 'rgba(250, 204, 21, 0.75)', borderWidth: 1.5, borderType: 'dashed' },
          label: {
            show: true,
            position: 'top',
            distance: 4,
            formatter: '预 测',
            color: '#0b0f19',
            fontSize: 13,
            fontWeight: 800,
            backgroundColor: 'rgba(250, 204, 21, 0.98)',
            borderRadius: 4,
            padding: [4, 12, 4, 12],
            shadowColor: 'rgba(250, 204, 21, 0.5)',
            shadowBlur: 6,
          },
          data: [[{ xAxis: predBoundary }, { xAxis: lastDate }]],
        } : undefined,
        markLine: predBoundary ? {
          silent: true, symbol: 'none',
          data: [{
            xAxis: predBoundary,
            lineStyle: { type: 'dashed', color: 'rgba(250, 204, 21, 0.85)', width: 1.5 },
            label: { show: false },
          }],
        } : undefined,
      },
      {
        name: '实际', type: 'candlestick', data: realCandles,
        itemStyle: { color: '#ef4444', color0: '#16a34a', borderColor: '#ef4444', borderColor0: '#16a34a' },
        barWidth: '40%',
      },
      { name: 'MA5', type: 'line', data: ma5, smooth: true, symbol: 'none', lineStyle: { width: 1, color: '#fbbf24' }, connectNulls: false },
      { name: 'MA10', type: 'line', data: ma10, smooth: true, symbol: 'none', lineStyle: { width: 1, color: '#60a5fa' }, connectNulls: false },
      { name: 'MA30', type: 'line', data: ma30, smooth: true, symbol: 'none', lineStyle: { width: 1, color: '#a78bfa' }, connectNulls: false },
      { name: '成交量', type: 'bar', xAxisIndex: 1, yAxisIndex: 1, data: volumes, itemStyle: { color: (p) => { const d = Array.isArray(p.data) ? p.data : (p.data?.value || p.data); const flag = d[2]; return flag > 0 ? '#ef4444' : '#16a34a'; } } },
    ],
  };
}

function _placeholderOption(text) {
  return {
    animation: false, xAxis: { show: false }, yAxis: { show: false }, series: [],
    graphic: { type: 'text', left: 'center', top: 'middle', style: { text, fill: '#94a3b8', font: '14px sans-serif' } },
  };
}

function renderChartPlaceholder(text) {
  const chart = ensureChart();
  if (!chart) return;
  chart.clear();
  chart.setOption(_placeholderOption(text));
}

function renderKlineChart(rows, predStartIdx, realtimeMap) {
  const chart = ensureChart();
  if (!chart) return;
  if (!rows.length) { renderChartPlaceholder('暂无K线数据'); return; }
  if (predStartIdx != null && predStartIdx > 0) {
    chart.setOption(_klinePredictOption(rows, predStartIdx, realtimeMap), true);
  } else {
    chart.setOption(_klineOption(rows), true);
  }
}

function renderMarketChartPlaceholder(text) {
  const chart = ensureMarketChart();
  if (!chart) return;
  chart.clear();
  chart.setOption(_placeholderOption(text));
}

function renderMarketKlineChart(rows) {
  const chart = ensureMarketChart();
  if (!chart) return;
  if (!rows.length) { renderMarketChartPlaceholder('暂无K线数据'); return; }
  chart.setOption(_klineOption(rows), true);
}

function _chgCls(v) {
  const n = Number(v || 0);
  return n > 0 ? 'up' : (n < 0 ? 'down' : 'neutral');
}

function renderStockSummary(detail) {
  const root = document.getElementById('stockSummary');
  if (!detail) { root.innerHTML = '点击左侧股票查看'; return; }
  const m = detail.metrics || {};
  const pct = Number(m.pct_change || 0);
  const pctCls = _chgCls(pct);
  const pctSign = pct > 0 ? '+' : '';
  root.innerHTML = `<span class="summary-stock">${detail.name}(${detail.symbol})</span>`
    + `<span class="summary-sep">·</span>`
    + `<span>现价 <b>${fmtNum(m.price, 2)}</b></span>`
    + `<span class="summary-sep">·</span>`
    + `<span class="${pctCls}">涨跌 ${pctSign}${fmtNum(pct, 2)}%</span>`
    + `<span class="summary-sep">·</span>`
    + `<span>放量比 ${fmtNum(m.volume_ratio, 2)}</span>`
    + `<span class="summary-sep">·</span>`
    + `<span>突破位 ${fmtNum(m.breakout_level, 2)}</span>`;
}

function renderStockSummaryLite(item, klinePayload) {
  const root = document.getElementById('marketStockSummary');
  const pct = Number(item.change_pct || 0);
  const pctCls = _chgCls(pct);
  const pctSign = pct > 0 ? '+' : '';
  root.innerHTML = `<span style="font-weight:600">${item.name}(${item.symbol})</span>`
    + `<span class="summary-sep" style="margin:0 6px;color:var(--text-dim)">·</span>`
    + `<span>现价 <b>${fmtNum(item.latest_price, 2)}</b></span>`
    + `<span class="summary-sep" style="margin:0 6px;color:var(--text-dim)">·</span>`
    + `<span class="${pctCls}">涨跌 ${pctSign}${fmtNum(pct, 2)}%</span>`
    + `<span class="summary-sep" style="margin:0 6px;color:var(--text-dim)">·</span>`
    + `<span>K线 ${Number(klinePayload?.count || 0)}日</span>`;
}

/* ==================== Funnel symbol select ==================== */

async function selectSymbol(symbol) {
  state.selectedSymbol = symbol;
  state.selectedHotSymbol = null;
  renderFunnel();
  renderHotStocks();
  renderStockSummary(null);
  renderChartPlaceholder('加载中...');
  try {
    const detail = await request(`/api/stock/${symbol}/detail?kline_days=30`);
    renderStockSummary(detail);
    renderKlineChart(detail.kline || []);
    _scrollToRightPanel();
    _fetchAndRenderFunnelPredict(symbol, detail.name);
  } catch (err) {
    renderStockSummary(null);
    renderChartPlaceholder(`加载失败: ${err.message}`);
  }
}

async function _fetchRealtimeMap(symbol, predictedDates) {
  if (!predictedDates || !predictedDates.length) return {};
  try {
    const rt = await request(`/api/stock/${symbol}/realtime`);
    if (!rt || !rt.found) return {};
    const map = {};
    if (predictedDates.includes(rt.date)) {
      map[rt.date] = { open: rt.open, high: rt.high, low: rt.low, close: rt.close, volume: rt.volume, amount: rt.amount };
    }
    return map;
  } catch { return {}; }
}

function _appendPredictSummary(el, pk, hk, rtMap) {
  const lastClose = hk.length ? hk[hk.length - 1].close : 0;
  if (pk.length && lastClose) {
    const day3Close = pk[pk.length - 1].close;
    const chg = ((day3Close - lastClose) / lastClose * 100).toFixed(2);
    const cls = Number(chg) > 0 ? 'up' : (Number(chg) < 0 ? 'down' : 'neutral');
    const sign = Number(chg) > 0 ? '+' : '';
    el.innerHTML += `<span class="summary-sep">·</span><span class="${cls}">预测${pk.length}日 ${sign}${chg}%</span>`;
  }
  const rtToday = Object.values(rtMap || {})[0];
  if (rtToday && pk.length && lastClose) {
    const realChg = ((rtToday.close - lastClose) / lastClose * 100).toFixed(2);
    const cls = Number(realChg) > 0 ? 'up' : (Number(realChg) < 0 ? 'down' : 'neutral');
    const sign = Number(realChg) > 0 ? '+' : '';
    el.innerHTML += `<span class="summary-sep">·</span><span class="${cls}">实际 ${sign}${realChg}%</span>`;
  }
}

async function _fetchAndRenderFunnelPredict(symbol, name) {
  const summaryEl = document.getElementById('stockSummary');
  try {
    const pred = await request(`/api/predict/${symbol}/kronos?lookback=180&horizon=3`);
    if (pred.merged_kline && pred.merged_kline.length) {
      const pk = pred.predicted_kline || [];
      const predDates = pk.map(x => x.date);
      const rtMap = await _fetchRealtimeMap(symbol, predDates);
      renderKlineChart(pred.merged_kline, pred.prediction_start_index, rtMap);
      _appendPredictSummary(summaryEl, pk, pred.history_kline || [], rtMap);
    }
  } catch (_) {}
}

async function selectHotStock(item) {
  state.selectedHotSymbol = item.symbol;
  renderHotStocks();
  const klineSection = document.getElementById('marketKlineSection');
  if (klineSection) klineSection.style.display = '';
  renderMarketChartPlaceholder('加载中...');
  try {
    let payload = await request(`/api/kline/${item.symbol}?days=30`);
    if (!payload?.items?.length) {
      try {
        const detail = await request(`/api/stock/${item.symbol}/detail?kline_days=30`);
        payload = { items: detail?.kline || [], count: Number(detail?.kline?.length || 0) };
      } catch (_) {}
    }
    renderStockSummaryLite(item, payload || {});
    renderMarketKlineChart(payload?.items || []);
    _fetchAndRenderMarketPredict(item.symbol, item.name);
  } catch (err) {
    renderMarketChartPlaceholder(`加载失败: ${err.message}`);
    setStatus(`热门个股加载失败: ${err.message}`, 'error');
  }
}

async function _fetchAndRenderMarketPredict(symbol, name) {
  const summaryEl = document.getElementById('marketStockSummary');
  try {
    const pred = await request(`/api/predict/${symbol}/kronos?lookback=180&horizon=3`);
    if (pred.merged_kline && pred.merged_kline.length) {
      const pk = pred.predicted_kline || [];
      const predDates = pk.map(x => x.date);
      const rtMap = await _fetchRealtimeMap(symbol, predDates);
      const chart = ensureMarketChart();
      if (chart) chart.setOption(_klinePredictOption(pred.merged_kline, pred.prediction_start_index, rtMap), true);
      _appendPredictSummary(summaryEl, pk, pred.history_kline || [], rtMap);
    }
  } catch (_) {}
}

/* ==================== Notice tab ==================== */

async function loadNoticeKeywords() {
  if (state.noticeKeywords.length) return;
  try {
    const data = await request('/api/notice/keywords');
    state.noticeKeywords = data.keywords || [];
  } catch { state.noticeKeywords = []; }
  renderKeywordTags();
}

function _passNoticeKeywordFilter(stock) {
  if (!state.activeKeywords.size) return true;
  const reason = (stock.reason || '').toLowerCase();
  const title = (stock.title || '').toLowerCase();
  for (const kw of state.activeKeywords) {
    if (reason.includes(kw.toLowerCase()) || title.includes(kw.toLowerCase())) return true;
  }
  return false;
}

function renderKeywordTags() {
  const container = document.getElementById('keywordTags');
  if (!container) return;
  container.innerHTML = '';
  state.noticeKeywords.forEach(kw => {
    const tag = document.createElement('span');
    tag.className = 'keyword-tag' + (state.activeKeywords.has(kw.tag) ? ' active' : '');
    tag.textContent = kw.tag;
    tag.onclick = () => {
      if (state.activeKeywords.has(kw.tag)) {
        state.activeKeywords.delete(kw.tag);
      } else {
        state.activeKeywords.add(kw.tag);
      }
      renderKeywordTags();
      renderNoticeFunnel();
    };
    container.appendChild(tag);
  });
}

function renderNoticeMeta() {
  const el = document.getElementById('noticeMeta');
  if (!state.noticeFunnel) { el.textContent = '暂无数据'; return; }
  const nf = state.noticeFunnel;
  el.textContent = `打分源 ${nf.source} · LLM ${nf.llm_enabled ? '开启' : '关闭'} · 候选 ${nf.stats.candidate} / 重点 ${nf.stats.focus} / 买入 ${nf.stats.buy}`;
}

function renderNoticeCounts() {
  if (!state.noticeFunnel) return;
  document.getElementById('notice-count-candidate').textContent = state.noticeFunnel.stats.candidate;
  document.getElementById('notice-count-focus').textContent = state.noticeFunnel.stats.focus;
  document.getElementById('notice-count-buy').textContent = state.noticeFunnel.stats.buy;
}

function noticeCardActions(stock) {
  if (stock.pool === 'candidate') return [['加入重点', 'focus'], ['加入买入', 'buy']];
  if (stock.pool === 'focus') return [['移回候选', 'candidate'], ['加入买入', 'buy']];
  return [['降级重点', 'focus']];
}

async function moveNoticePool(symbol, targetPool) {
  try {
    await request('/api/notice/pool/move', {
      method: 'POST',
      body: JSON.stringify({ symbol, target_pool: targetPool }),
    });
    setStatus(`已迁移 ${symbol} -> ${targetPool}`, 'success');
    await reloadNotice();
  } catch (err) {
    setStatus(`迁移失败: ${err.message}`, 'error');
  }
}

function renderNoticePool(poolName, list) {
  const root = document.getElementById(`notice-pool-${poolName}`);
  root.innerHTML = '';
  list.forEach((stock) => {
    const card = document.createElement('div');
    card.className = `stock-card ${state.noticeSelectedSymbol === stock.symbol ? 'active' : ''}`;
    card.onclick = () => selectNoticeSymbol(stock.symbol);
    const actions = noticeCardActions(stock)
      .map(([label, pool]) => `<button data-symbol="${stock.symbol}" data-pool="${pool}">${label}</button>`)
      .join('');
    const simBuyBtn = poolName === 'buy'
      ? `<button class="btn-sim-buy" data-symbol="${stock.symbol}" data-name="${stock.name}">模拟买入</button>`
      : '';
    card.innerHTML = `
      <div class="stock-top">
        <div class="stock-name">${stock.name} (${stock.symbol})</div>
        <div class="score up">${fmtNum(stock.score)}</div>
      </div>
      <div class="metrics">${stock.notice_type} · ${stock.notice_date}</div>
      <div class="metrics">${stock.title}</div>
      <div class="metrics">理由: ${stock.reason || '-'}</div>
      <div class="metrics">风险: ${stock.risk || '-'}</div>
      <div class="card-actions">${actions}${simBuyBtn}</div>
    `;
    card.querySelectorAll('button[data-pool]').forEach((btn) => {
      btn.onclick = (e) => { e.stopPropagation(); moveNoticePool(btn.dataset.symbol, btn.dataset.pool); };
    });
    const sbBtn = card.querySelector('.btn-sim-buy');
    if (sbBtn) {
      sbBtn.onclick = (e) => { e.stopPropagation(); paperBuy(sbBtn.dataset.symbol, sbBtn.dataset.name, 0); };
    }
    root.appendChild(card);
  });
  if (!list.length) {
    const hint = state.activeKeywords.size > 0 ? '当前关键词下无匹配公告' : '尚未运行公告筛选';
    root.innerHTML = `<div class="empty-state"><div class="empty-state-text">${hint}</div></div>`;
  }
}

function renderNoticeFunnel() {
  if (!state.noticeFunnel) return;
  const filteredPools = {};
  for (const poolName of ['candidate', 'focus', 'buy']) {
    filteredPools[poolName] = (state.noticeFunnel.pools[poolName] || []).filter(_passNoticeKeywordFilter);
  }
  document.getElementById('notice-count-candidate').textContent = filteredPools.candidate.length;
  document.getElementById('notice-count-focus').textContent = filteredPools.focus.length;
  document.getElementById('notice-count-buy').textContent = filteredPools.buy.length;
  renderNoticePool('candidate', filteredPools.candidate);
  renderNoticePool('focus', filteredPools.focus);
  renderNoticePool('buy', filteredPools.buy);
  renderNoticeMeta();
  _renderNoticeSummaryBarFiltered(filteredPools);
  if (state.activeTab === 'notice') setMeta();
}

async function selectNoticeSymbol(symbol) {
  state.noticeSelectedSymbol = symbol;
  renderNoticeFunnel();
  renderChartPlaceholder('加载中...');
  document.getElementById('noticeSummary').textContent = '加载中...';
  document.getElementById('noticeDetail').textContent = '加载中...';
  try {
    const detail = await request(`/api/notice/${symbol}/detail?days=30`);
    const kline = detail.kline || [];
    document.getElementById('noticeSummary').textContent = `${detail.name}(${detail.symbol}) 分数:${fmtNum(detail.score)} 池:${detail.pool}`;
    const first = (detail.notices || [])[0] || {};
    const kwHighlight = (text) => {
      if (!text || state.activeKeywords.size === 0) return esc(text || '-');
      let result = esc(text);
      for (const kw of state.activeKeywords) {
        const regex = new RegExp(kw.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi');
        result = result.replace(regex, '<span class="notice-keyword-hit">$&</span>');
      }
      return result;
    };
    document.getElementById('noticeDetail').innerHTML = `
      <div class="metrics"><b>${kwHighlight(first.title)}</b></div>
      <div class="metrics">类型: ${first.notice_type || '-'}</div>
      <div class="metrics">理由: ${kwHighlight(detail.reason)}</div>
      <div class="metrics">风险: ${kwHighlight(detail.risk)}</div>
      <div class="metrics"><a href="${first.url || '#'}" target="_blank" style="color:var(--brand)">公告链接</a></div>
    `;
    document.getElementById('stockSummary').textContent = `${detail.name}(${detail.symbol}) 30日日K`;
    if (kline.length) {
      renderKlineChart(kline);
    } else {
      renderChartPlaceholder('K线数据未同步，请先执行数据同步');
    }
    _fetchAndRenderNoticePredict(symbol, detail.name);
    _scrollToRightPanel();
  } catch (err) {
    document.getElementById('noticeDetail').textContent = `加载失败: ${err.message}`;
    renderChartPlaceholder(`加载失败: ${err.message}`);
  }
}

async function _fetchAndRenderNoticePredict(symbol, name) {
  const summaryEl = document.getElementById('stockSummary');
  try {
    const pred = await request(`/api/predict/${symbol}/kronos?lookback=180&horizon=3`);
    if (pred.merged_kline && pred.merged_kline.length) {
      const pk = pred.predicted_kline || [];
      const predDates = pk.map(x => x.date);
      const rtMap = await _fetchRealtimeMap(symbol, predDates);
      renderKlineChart(pred.merged_kline, pred.prediction_start_index, rtMap);
      _appendPredictSummary(summaryEl, pk, pred.history_kline || [], rtMap);
    }
  } catch (_) {}
}

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

/* ==================== 模拟盘 ==================== */

let _paperPollTimer = null;
let _paperLastRefresh = 0;

async function loadPaperData() {
  const [posRes, histRes, sumRes, tradeRes] = await Promise.allSettled([
    request('/api/paper/positions'),
    request('/api/paper/history?limit=50'),
    request('/api/paper/summary'),
    request('/api/paper/trades?limit=100'),
  ]);
  if (posRes.status === 'fulfilled') renderPaperPositions(posRes.value.positions || []);
  if (histRes.status === 'fulfilled') renderPaperHistory(histRes.value.positions || []);
  if (sumRes.status === 'fulfilled') renderPaperSummary(sumRes.value);
  if (tradeRes.status === 'fulfilled') renderPaperTrades(tradeRes.value.trades || []);
  _paperLastRefresh = Date.now();
  _updatePaperRefreshHint();
}

async function refreshPaperPrices() {
  const hint = document.getElementById('paperRefreshHint');
  if (hint) hint.textContent = '刷新中...';
  const [posRes, sumRes] = await Promise.allSettled([
    request('/api/paper/positions'),
    request('/api/paper/summary'),
  ]);
  if (posRes.status === 'fulfilled') renderPaperPositions(posRes.value.positions || []);
  if (sumRes.status === 'fulfilled') renderPaperSummary(sumRes.value);
  _paperLastRefresh = Date.now();
  _updatePaperRefreshHint();
}

function _paperPollInterval() {
  return _isMarketOpen() ? 30000 : 600000;
}

function _startPaperPoll({ immediate = true } = {}) {
  _stopPaperPoll();
  const tick = () => {
    if (state.activeTab !== 'paper') { _stopPaperPoll(); return; }
    if (document.hidden) {
      _paperPollTimer = setTimeout(tick, 5000);
      return;
    }
    refreshPaperPrices();
    _paperPollTimer = setTimeout(tick, _paperPollInterval());
  };
  if (immediate && !document.hidden && state.activeTab === 'paper') {
    refreshPaperPrices();
    _paperPollTimer = setTimeout(tick, _paperPollInterval());
  } else {
    _paperPollTimer = setTimeout(tick, _paperPollInterval());
  }
}

function _stopPaperPoll() {
  if (_paperPollTimer) { clearTimeout(_paperPollTimer); _paperPollTimer = null; }
}

function _updatePaperRefreshHint() {
  const hint = document.getElementById('paperRefreshHint');
  if (!hint) return;
  if (!_paperLastRefresh) { hint.textContent = ''; return; }
  const ago = Math.round((Date.now() - _paperLastRefresh) / 1000);
  const open = _isMarketOpen();
  const freq = open ? '30s' : '10min';
  const stage = open ? '盘中' : '盘后';
  hint.textContent = (ago < 5 ? '刚刚更新' : `${ago}s 前`) + ` · ${stage} ${freq}`;
}

function renderPaperSummary(s) {
  if (!s) return;
  state.paperUpdatedAt = fmtShortTime(s.updated_at);
  if (state.activeTab === 'paper') setMeta();

  const _set = (id, text, cls) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = text;
    if (cls !== undefined) el.className = el.className.replace(/\b(up|down|neutral)\b/g, '').trim() + ' ' + cls;
  };

  _set('paperTotalAsset', fmtMoney(s.total_asset), pnlCls((s.total_asset || 0) - (s.initial_capital || 1e6)));
  _set('paperMarketValue', fmtMoney(s.total_market_value));
  _set('paperFloatPnl', `${s.total_float_pnl >= 0 ? '+' : ''}${fmtMoney(s.total_float_pnl)}`, pnlCls(s.total_float_pnl));
  _set('paperRealizedPnl', `${s.total_realized_pnl >= 0 ? '+' : ''}${fmtMoney(s.total_realized_pnl)}`, pnlCls(s.total_realized_pnl));
  _set('paperTotalFee', fmtMoney(s.total_fee || 0));
  _set('paperMaxDrawdown', fmtMoney(s.max_drawdown || 0));

  document.getElementById('paperOpenCount').textContent = s.open_count || 0;
  document.getElementById('paperTotalTrades').textContent = s.total_trades || 0;

  const totalClosed = (s.win_count || 0) + (s.lose_count || 0);
  if (totalClosed === 0) {
    document.getElementById('paperWinRate').textContent = '--';
  } else if (totalClosed < 5) {
    document.getElementById('paperWinRate').textContent = `${fmtNum(s.win_rate, 1)}%`;
    document.getElementById('paperWinRate').title = `样本 ${totalClosed}，仅供参考`;
  } else {
    document.getElementById('paperWinRate').textContent = `${fmtNum(s.win_rate, 1)}%`;
    document.getElementById('paperWinRate').title = `样本 ${totalClosed}`;
  }
  document.getElementById('paperWinLose').textContent = `${s.win_count || 0}/${s.lose_count || 0}`;

  if (s.settings) {
    document.getElementById('psCommission').value = s.settings.commission_rate;
    document.getElementById('psMinComm').value = s.settings.min_commission;
    document.getElementById('psStampTax').value = s.settings.stamp_tax_rate;
    document.getElementById('psSlippage').value = s.settings.slippage_rate;
  }
}

function renderPaperPositions(positions) {
  const root = document.getElementById('paperPositions');
  document.getElementById('paperHoldCount').textContent = positions.length;
  root.innerHTML = '';
  if (!positions.length) {
    root.innerHTML = `<div class="empty-state paper-empty">
      <div class="empty-state-icon">📭</div>
      <div class="empty-state-text">当前无持仓</div>
      <div class="empty-state-hint">在买入池点击「模拟买入」开始模拟交易</div>
      <div class="empty-state-actions">
        <button class="btn-primary btn-sm" onclick="switchTab('funnel')">前往策略选股</button>
        <button class="btn-secondary btn-sm" onclick="switchTab('notice')">前往公告选股</button>
      </div>
    </div>`;
    return;
  }
  positions.forEach(p => {
    const cls = pnlCls(p.pnl);
    const sign = p.pnl > 0 ? '+' : '';
    const div = document.createElement('div');
    div.className = 'paper-card';
    div.innerHTML = `
      <div class="paper-card-top">
        <div class="paper-card-name">${p.name} (${p.symbol})</div>
        <div class="paper-card-pnl ${cls}">${sign}¥${fmtNum(p.pnl, 2)} (${sign}${fmtNum(p.pnl_pct, 2)}%)</div>
      </div>
      <div class="paper-card-info">
        <span>成本 ${fmtNum(p.cost_price, 2)}</span>
        <span>现价 ${fmtNum(p.current_price, 2)}</span>
        <span>数量 ${p.qty}股</span>
        <span>市值 ¥${fmtNum(p.current_price * p.qty, 2)}</span>
        <span>开仓 ${fmtShortTime(p.opened_at)}</span>
      </div>
      <div class="paper-card-actions">
        <button class="btn-sell" data-id="${p.id}" data-symbol="${p.symbol}" data-name="${p.name}">模拟卖出</button>
      </div>
    `;
    div.querySelector('.btn-sell').onclick = (e) => {
      e.stopPropagation();
      const btn = e.target.closest('.btn-sell');
      paperSell(btn.dataset.id, btn.dataset.symbol, btn.dataset.name);
    };
    root.appendChild(div);
  });
}

function renderPaperHistory(positions) {
  const root = document.getElementById('paperHistory');
  document.getElementById('paperClosedCount').textContent = positions.length;
  root.innerHTML = '';
  if (!positions.length) {
    root.innerHTML = '<div class="empty-state"><div class="empty-state-text">暂无平仓记录</div></div>';
    return;
  }
  let html = `<table class="paper-history-table">
    <thead><tr>
      <th>股票</th><th>数量</th><th>买入</th><th>卖出</th><th>盈亏</th><th>开仓</th><th>平仓</th>
    </tr></thead><tbody>`;
  positions.forEach(p => {
    const rpnl = p.realized_pnl || 0;
    const cls = pnlCls(rpnl);
    const sign = rpnl > 0 ? '+' : '';
    const pctStr = p.realized_pnl_pct != null ? ` (${sign}${fmtNum(p.realized_pnl_pct, 2)}%)` : '';
    html += `<tr>
      <td class="paper-hist-name">${p.name}<br><code>${p.symbol}</code></td>
      <td>${p.qty}</td>
      <td>${fmtNum(p.cost_price, 2)}</td>
      <td>${fmtNum(p.close_price, 2)}</td>
      <td class="${cls}">${sign}¥${fmtNum(rpnl, 2)}${pctStr}</td>
      <td title="${p.opened_at || ''}">${fmtShortTime(p.opened_at)}</td>
      <td title="${p.closed_at || ''}">${fmtShortTime(p.closed_at)}</td>
    </tr>`;
  });
  html += '</tbody></table>';
  root.innerHTML = html;
}

function renderPaperTrades(trades) {
  const root = document.getElementById('paperTrades');
  if (!root) return;
  document.getElementById('paperTradeCount').textContent = trades.length;
  if (!trades.length) {
    root.innerHTML = '<div class="empty-state"><div class="empty-state-text">暂无成交记录</div></div>';
    return;
  }
  let html = `<table class="paper-history-table">
    <thead><tr>
      <th>时间</th><th>股票</th><th>操作</th><th>数量</th><th>成交价</th><th>费用</th>
    </tr></thead><tbody>`;
  trades.forEach(t => {
    const actionCls = t.action === 'buy' ? 'paper-action-buy' : 'paper-action-sell';
    const actionText = t.action === 'buy' ? '买入' : '卖出';
    const time = (t.created_at || '').slice(0, 19).replace('T', ' ');
    html += `<tr>
      <td class="paper-trade-time">${time}</td>
      <td class="paper-hist-name">${t.name}<br><code>${t.symbol}</code></td>
      <td><span class="${actionCls}">${actionText}</span></td>
      <td>${t.qty}</td>
      <td>${fmtNum(t.price, 4)}</td>
      <td>¥${fmtNum(t.fee, 2)}</td>
    </tr>`;
  });
  html += '</tbody></table>';
  root.innerHTML = html;
}

/* ==================== Hermes Agent ==================== */

async function loadAgentData() {
  await Promise.all([loadAgentStatus(), loadAgentTasks(), loadMonitorConfig(), loadMonitorMessages()]);
}

async function loadAgentStatus() {
  try {
    const data = await request('/api/agent/status');
    const dot = document.getElementById('agentStatusDot');
    const txt = document.getElementById('agentStatusText');
    if (dot) dot.className = 'agent-status-dot ' + (data.running ? 'running' : (data.llm_available ? 'ok' : 'error'));

    let mode = '规则模式';
    if (data.hermes_agent_available) mode = 'Hermes Agent';
    else if (data.llm_available) mode = 'LLM 模式';
    if (txt) txt.textContent = data.running ? `运行中 · ${mode}` : `就绪 · ${mode}`;

    const lastEl = document.getElementById('agentLastRun');
    const nextEl = document.getElementById('agentNextRun');
    if (lastEl) {
      if (data.last_run) {
        const t = fmtShortTime(data.last_run.finished_at);
        lastEl.textContent = `上次: ${t} · ${data.last_run.status}`;
      } else {
        lastEl.textContent = '上次: 尚未执行';
      }
    }

    if (nextEl) {
      if (state.monitorConfig?.enabled) {
        const interval = state.monitorConfig.interval_minutes || 10;
        nextEl.textContent = `自动: 每${interval}分钟`;
      } else {
        nextEl.textContent = '自动: 未启用';
      }
    }

  } catch { /* ignore */ }
}

function esc(s) { const d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; }

async function loadAgentTasks() {
  const container = document.getElementById('agentTasks');
  try {
    const data = await request('/api/agent/tasks?limit=10');
    if (!data.items || data.items.length === 0) {
      container.innerHTML = '<div class="empty-state"><div class="empty-state-text">尚无运行记录，点击上方「手动诊断」启动</div></div>';
      return;
    }
    container.innerHTML = data.items.map(t => {
      const typeLabel = { daily_review: '盘后复盘', notice_review: '公告复盘', full_diagnosis: '全面诊断' }[t.task_type] || t.task_type;
      const statusLabel = { success: '成功', failed: '失败', timeout: '超时', running: '运行中' }[t.status] || t.status;
      const statusDotCls = { success: 'success', failed: 'error', timeout: 'warning', running: 'progress' }[t.status] || 'progress';
      const time = (t.finished_at || t.started_at || '').slice(11, 16);
      const elapsed = t.elapsed_ms ? `${(t.elapsed_ms / 1000).toFixed(1)}s` : '--';
      const failDetail = t.status === 'failed' && t.error
        ? `<div style="grid-column:1/-1;font-size:11px;color:var(--status-error);padding-left:20px;margin-top:-4px">${esc(t.error)}</div>`
        : '';
      return `
        <div class="agent-task-row" style="flex-wrap:wrap">
          <span class="status-dot status-dot--${statusDotCls}"></span>
          <span class="agent-task-type">${typeLabel}</span>
          <span class="agent-task-status ${t.status}">${statusLabel}</span>
          <span class="agent-task-time">${time}</span>
          <span class="agent-task-elapsed">${elapsed}</span>
          <span class="agent-task-time">${t.trigger || ''}</span>
          ${failDetail}
        </div>`;
    }).join('');
  } catch {
    container.innerHTML = '<div class="empty-state"><div class="empty-state-text">运行记录加载失败</div></div>';
  }
}

/* ==================== 智能监控 ==================== */

async function loadMonitorConfig() {
  try {
    const config = await request('/api/agent/monitor/config');
    state.monitorConfig = config;
    const intervalSel = document.getElementById('monitorInterval');
    if (intervalSel) intervalSel.value = String(config.interval_minutes || 10);
    const promptEl = document.getElementById('monitorPrompt');
    if (promptEl) promptEl.value = config.system_prompt || '';
    _updateMonitorStatusUI(!!config.enabled);
  } catch (_) {}
}

async function loadMonitorMessages() {
  try {
    const data = await request('/api/agent/monitor/messages?limit=50&today_only=true');
    state.monitorMessages = data.items || [];
    renderMonitorFeed();
  } catch (_) {}
}

function _updateMonitorStatusUI(enabled) {
  const badge = document.getElementById('monitorStatus');
  const btn = document.getElementById('btnMonitorToggle');
  if (badge) {
    badge.textContent = enabled ? '运行中' : '已停止';
    badge.className = 'monitor-status-badge ' + (enabled ? 'on' : 'off');
  }
  if (btn) btn.textContent = enabled ? '停止监控' : '启动监控';
}

function renderMonitorFeed() {
  const container = document.getElementById('monitorFeed');
  if (!container) return;
  const msgs = state.monitorMessages;
  const countEl = document.getElementById('monitorMsgCount');
  if (countEl) countEl.textContent = msgs.length || '';
  if (!msgs.length) {
    container.innerHTML = '<div class="empty-state"><div class="empty-state-text">暂无推送消息，启动监控或手动触发</div></div>';
    return;
  }
  container.innerHTML = msgs.map(m => _renderMonitorCard(m)).join('');
  bindHoverKline(container);
}

async function _loadMonitorKline(symbol, name) {
  const section = document.getElementById('monitorKlineSection');
  const titleEl = document.getElementById('monitorKlineTitle');
  const chartEl = document.getElementById('monitorKlineChart');
  section.style.display = 'block';
  titleEl.textContent = `${name}(${symbol}) K线 + 预测`;

  if (state.monitorKlineChart) state.monitorKlineChart.dispose();
  state.monitorKlineChart = echarts.init(chartEl);
  state.monitorKlineChart.setOption(_placeholderOption('加载中...'));

  try {
    let kline = [];
    try {
      const payload = await request(`/api/kline/${symbol}?days=30`);
      kline = payload?.items || [];
    } catch {}
    if (!kline.length) {
      try {
        const detail = await request(`/api/stock/${symbol}/detail?kline_days=30`);
        kline = detail?.kline || [];
      } catch {}
    }

    if (!kline.length) {
      state.monitorKlineChart.setOption(_placeholderOption('暂无K线数据'));
      return;
    }

    state.monitorKlineChart.setOption(_klineOption(kline), true);

    try {
      const pred = await request(`/api/predict/${symbol}/kronos?lookback=180&horizon=3`);
      if (pred.merged_kline && pred.merged_kline.length) {
        const pk = pred.predicted_kline || [];
        const predDates = pk.map(x => x.date);
        const rtMap = await _fetchRealtimeMap(symbol, predDates);
        state.monitorKlineChart.setOption(_klinePredictOption(pred.merged_kline, pred.prediction_start_index, rtMap), true);
      }
    } catch {}
  } catch (err) {
    state.monitorKlineChart.setOption(_placeholderOption(`加载失败: ${err.message}`));
  }
  section.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

/* ==================== Hover K线预览浮窗 ==================== */

const _hoverKline = {
  chart: null,
  timer: null,
  activeSymbol: null,
  abortCtrl: null,
};

function _positionHoverPopup(popup, anchorEl) {
  const rect = anchorEl.getBoundingClientRect();
  const pw = 480, ph = 310;
  let left = rect.right + 4;
  let top = rect.top;
  if (left + pw > window.innerWidth) left = rect.left - pw - 4;
  if (left < 4) left = 4;
  if (top + ph > window.innerHeight) top = window.innerHeight - ph - 8;
  if (top < 4) top = 4;
  popup.style.left = left + 'px';
  popup.style.top = top + 'px';
}

function _showHoverKline(symbol, name, anchorEl) {
  if (_hoverKline.activeSymbol === symbol) {
    clearTimeout(_hoverKline.timer);
    return;
  }
  _hideHoverKlineNow();
  _hoverKline.activeSymbol = symbol;

  const popup = document.getElementById('hoverKlinePopup');
  const titleEl = document.getElementById('hoverKlineTitle');
  const chartEl = document.getElementById('hoverKlineChartEl');

  titleEl.textContent = `${name}(${symbol}) · 60日K线 + 预测`;
  _positionHoverPopup(popup, anchorEl);
  popup.style.display = '';
  requestAnimationFrame(() => popup.classList.add('visible'));

  if (_hoverKline.chart) _hoverKline.chart.dispose();
  _hoverKline.chart = echarts.init(chartEl);
  _hoverKline.chart.setOption(_placeholderOption('加载中...'));

  if (_hoverKline.abortCtrl) _hoverKline.abortCtrl.abort();
  _hoverKline.abortCtrl = new AbortController();
  const signal = _hoverKline.abortCtrl.signal;

  (async () => {
    try {
      let kline = [];
      try {
        const payload = await request(`/api/kline/${symbol}?days=30`);
        kline = payload?.items || [];
      } catch {}
      if (!kline.length) {
        try {
          const detail = await request(`/api/stock/${symbol}/detail?kline_days=30`);
          kline = detail?.kline || [];
        } catch {}
      }
      if (signal.aborted) return;
      if (!kline.length) {
        _hoverKline.chart?.setOption(_placeholderOption('暂无K线数据'));
        return;
      }
      _hoverKline.chart?.setOption(_klineOption(kline), true);

      try {
        const pred = await request(`/api/predict/${symbol}/kronos?lookback=180&horizon=3`);
        if (signal.aborted) return;
        if (pred.merged_kline && pred.merged_kline.length) {
          const pk = pred.predicted_kline || [];
          const predDates = pk.map(x => x.date);
          const rtMap = await _fetchRealtimeMap(symbol, predDates);
          if (signal.aborted) return;
          _hoverKline.chart?.setOption(_klinePredictOption(pred.merged_kline, pred.prediction_start_index, rtMap), true);
          const hk = pred.history_kline || [];
          const lastClose = hk.length ? hk[hk.length - 1].close : 0;
          if (pk.length && lastClose) {
            const day3Close = pk[pk.length - 1].close;
            const chg = ((day3Close - lastClose) / lastClose * 100).toFixed(2);
            const cls = Number(chg) > 0 ? 'up' : (Number(chg) < 0 ? 'down' : 'neutral');
            const sign = Number(chg) > 0 ? '+' : '';
            titleEl.innerHTML = `${esc(name)}(${symbol}) · <span class="${cls}">预测3日 ${sign}${chg}%</span>`;
          }
        }
      } catch {}
    } catch {}
  })();
}

function _hideHoverKlineNow() {
  const popup = document.getElementById('hoverKlinePopup');
  if (!popup) return;
  popup.classList.remove('visible');
  popup.style.display = 'none';
  if (_hoverKline.abortCtrl) { _hoverKline.abortCtrl.abort(); _hoverKline.abortCtrl = null; }
  if (_hoverKline.chart) { _hoverKline.chart.dispose(); _hoverKline.chart = null; }
  _hoverKline.activeSymbol = null;
  clearTimeout(_hoverKline.timer);
}

function _scheduleHideHoverKline() {
  clearTimeout(_hoverKline.timer);
  _hoverKline.timer = setTimeout(() => {
    const popup = document.getElementById('hoverKlinePopup');
    if (!popup) return;
    popup.classList.remove('visible');
    setTimeout(() => {
      if (!popup.classList.contains('visible')) {
        popup.style.display = 'none';
        if (_hoverKline.abortCtrl) { _hoverKline.abortCtrl.abort(); _hoverKline.abortCtrl = null; }
        if (_hoverKline.chart) { _hoverKline.chart.dispose(); _hoverKline.chart = null; }
        _hoverKline.activeSymbol = null;
      }
    }, 200);
  }, 400);
}

function _cancelHideHoverKline() {
  clearTimeout(_hoverKline.timer);
}

function bindHoverKline(container) {
  container.querySelectorAll('.mtheme-stock').forEach(el => {
    el.addEventListener('mouseenter', () => {
      _cancelHideHoverKline();
      _hoverKline.timer = setTimeout(() => {
        _showHoverKline(el.dataset.symbol, el.dataset.name, el);
      }, 150);
    });
    el.addEventListener('mouseleave', () => {
      _scheduleHideHoverKline();
    });
    el.addEventListener('click', () => {
      if (el.dataset.symbol && state.activeTab === 'agent') {
        _loadMonitorKline(el.dataset.symbol, el.dataset.name || el.dataset.symbol);
      }
    });
  });
}

function _initHoverKlinePopupEvents() {
  const popup = document.getElementById('hoverKlinePopup');
  if (!popup) return;
  popup.addEventListener('mouseenter', () => _cancelHideHoverKline());
  popup.addEventListener('mouseleave', () => _scheduleHideHoverKline());
}

function _parseMonitorThemes(text) {
  const themes = [];
  const cnIdx = '一二三四五六七八九十';

  const splitRe = new RegExp(
    `(?=(?:^|\\n)\\s*(?:【[高中低]+[高]?】\\s*主线[${cnIdx}\\d]|[${cnIdx}]+[、\\.]\\s*【[高中低]+[高]?】))`
  );
  const blocks = text.split(splitRe);
  const seenTitles = new Set();

  const headRe = new RegExp(
    `(?:【([高中低]+[高]?)】\\s*主线([${cnIdx}\\d]+)[：:]\\s*(.+?)(?:\\n|$))` +
    `|(?:([${cnIdx}\\d]+)[、\\.]\\s*【([高中低]+[高]?)】\\s*([^\\n]+?)(?:\\n|$))`
  );

  for (const rawBlock of blocks) {
    const tailIdx = rawBlock.search(/(?:={6,}|-{10,}|\n\s*(?:10\s*分钟内)?执行摘要)/);
    const block = tailIdx >= 0 ? rawBlock.slice(0, tailIdx) : rawBlock;
    const m = block.match(headRe);
    if (!m) continue;
    let level, idx, title;
    if (m[1]) { level = m[1]; idx = m[2]; title = m[3]; }
    else { idx = m[4]; level = m[5]; title = m[6]; }
    title = (title || '').trim().replace(/[（(][^）)]*[）)]\s*$/, '').trim();
    if (!title) continue;
    const dedupeKey = `${level}|${title}`;
    if (seenTitles.has(dedupeKey)) continue;
    seenTitles.add(dedupeKey);

    const stocks = [];
    const stockRe = /[-·]\s*(\d{6})\s+([^\s：:]+)[：:]\s*(.+)/g;
    const stockSection = block.match(/关注个股[\s\S]*?(?=\n\s*\d\)\s|失效条件|【|$)/i);
    const stockText = stockSection ? stockSection[0] : block;
    let sm;
    while ((sm = stockRe.exec(stockText)) !== null) {
      stocks.push({ symbol: sm[1], name: sm[2], reason: sm[3].trim() });
    }

    let analysis = '';
    const logicMatch = block.match(/逻辑链[\s\S]*?(?=\n\s*\d\)\s*关注|$)/i);
    if (logicMatch) {
      analysis = logicMatch[0].replace(/^.*逻辑链[（(].*?[)）]\s*/i, '').replace(/^.*逻辑链[：:\s]*/i, '').trim();
      analysis = analysis.split('\n').filter(l => l.trim().startsWith('-')).map(l => l.trim().replace(/^-\s*/, '')).join(' ');
    }

    let risk = '';
    const riskMatch = block.match(/失效条件[\s\S]*?(?=\n\s*\d\)\s|【稳健|【激进|$)/i);
    if (riskMatch) {
      risk = riskMatch[0].replace(/^.*失效条件[^\n]*\n/i, '').trim();
      risk = risk.split('\n').filter(l => l.trim().startsWith('-')).map(l => l.trim().replace(/^-\s*/, '')).slice(0, 2).join('；');
    }

    themes.push({ level, idx, title, analysis, risk, stocks });
  }
  return themes;
}

function _renderMonitorCard(msg) {
  const fullTime = fmtShortTime(msg.created_at);
  const triggerLabel = msg.trigger === 'manual' ? '手动触发' : '自动轮询';
  const triggerCls = msg.trigger === 'manual' ? 'trigger-manual' : 'trigger-auto';
  const raw = msg.content || '';
  const themes = _parseMonitorThemes(raw);

  if (!themes.length) {
    return `<div class="monitor-msg-card">
      <div class="monitor-msg-header">
        <span class="monitor-msg-time">${fullTime}</span>
        <span class="monitor-msg-trigger ${triggerCls}">${triggerLabel}</span>
      </div>
      <div class="monitor-msg-body">${_formatPlainContent(raw)}</div>
    </div>`;
  }

  let summaryMatch = raw.match(/10分钟内执行摘要([\s\S]*?)$/i) || raw.match(/执行摘要([\s\S]*?)$/i);
  let summaryHtml = '';
  if (summaryMatch) {
    const allLines = summaryMatch[1].trim().split('\n').filter(l => l.trim());
    const keyLines = allLines.filter(l => l.trim().startsWith('-')).map(l => l.trim().replace(/^-\s*/, ''));
    if (keyLines.length) {
      const preview = keyLines.slice(0, 2).map(l => esc(l)).join(' | ');
      const rest = keyLines.slice(2);
      const restHtml = rest.length
        ? `<div class="monitor-summary-detail" style="display:none">${rest.map(l => `<div>· ${esc(l)}</div>`).join('')}</div>
           <span class="monitor-summary-toggle" onclick="this.previousElementSibling.style.display=this.previousElementSibling.style.display==='none'?'block':'none';this.textContent=this.previousElementSibling.style.display==='none'?'展开 ${rest.length} 条':'收起'">展开 ${rest.length} 条</span>`
        : '';
      summaryHtml = `<div class="monitor-summary"><b>执行摘要</b> ${preview}${restHtml}</div>`;
    }
  }

  const themesHtml = themes.map(t => {
    const levelCls = t.level === '高' ? 'high' : t.level.includes('高') ? 'mid-high' : t.level === '中' ? 'mid' : 'low';
    const stocksHtml = t.stocks.map(s =>
      `<div class="mtheme-stock" data-symbol="${s.symbol}" data-name="${s.name}" title="点击查看 ${esc(s.name)} K线预测">` +
      `<code>${s.symbol}</code> <b>${esc(s.name)}</b><svg viewBox="0 0 20 20" fill="currentColor" class="mtheme-stock-arrow"><path fill-rule="evenodd" d="M7.21 14.77a.75.75 0 01.02-1.06L11.168 10 7.23 6.29a.75.75 0 111.04-1.08l4.5 4.25a.75.75 0 010 1.08l-4.5 4.25a.75.75 0 01-1.06-.02z" clip-rule="evenodd"/></svg></div>`
    ).join('');
    const analysisHtml = t.analysis ? `<div class="mtheme-analysis">${esc(t.analysis)}</div>` : '';
    const riskHtml = t.risk ? `<div class="mtheme-risk">风险: ${esc(t.risk)}</div>` : '';
    return `<div class="mtheme-card ${levelCls}">
      <div class="mtheme-header">
        <span class="mtheme-level ${levelCls}">${esc(t.level)}</span>
        <span class="mtheme-title">${esc(t.title)}</span>
      </div>
      ${analysisHtml}
      ${riskHtml}
      <div class="mtheme-pool-section">
        <div class="mtheme-pool-label">关注池 · ${t.stocks.length}只</div>
        <div class="mtheme-pool">${stocksHtml || '<span class="muted">暂无</span>'}</div>
      </div>
    </div>`;
  }).join('');

  return `<div class="monitor-msg-card">
    <div class="monitor-msg-header">
      <span class="monitor-msg-time">${fullTime}</span>
      <span class="monitor-msg-trigger ${triggerCls}">${triggerLabel}</span>
    </div>
    ${summaryHtml}
    <div class="mtheme-grid">${themesHtml}</div>
  </div>`;
}

function _formatPlainContent(text) {
  let html = esc(text);
  html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  html = html.replace(/【(.+?)】/g, '<span class="monitor-tag">$1</span>');
  html = html.replace(/\n/g, '<br>');
  return html;
}

function _appendMonitorMessage(msg) {
  state.monitorMessages.unshift(msg);
  renderMonitorFeed();
  const feed = document.getElementById('monitorFeed');
  if (feed) feed.scrollTop = 0;
}

/* ==================== Kronos 预测弹窗 ==================== */

function closePredictModal() {
  document.getElementById('predictModal').style.display = 'none';
  if (state.predictChart) { state.predictChart.dispose(); state.predictChart = null; }
}

function ensurePredictChart() {
  const dom = document.getElementById('predictChart');
  if (!dom || !window.echarts) return null;
  if (state.predictChart) state.predictChart.dispose();
  state.predictChart = window.echarts.init(dom);
  return state.predictChart;
}

async function openPredictModal(symbol, detail, options = {}) {
  const modal = document.getElementById('predictModal');
  modal.style.display = 'flex';

  const name = detail?.name || symbol;
  const titleSuffix = options.titleSuffix || 'Kronos 三日预测';
  document.getElementById('predictModalTitle').textContent = `${name} (${symbol}) — ${titleSuffix}`;

  const m = detail?.metrics || {};
  const pct = Number(m.pct_change || 0);
  const pctCls = pct >= 0 ? 'up' : 'down';
  const sign = pct >= 0 ? '+' : '';
  const badge = options.badge ? `<span class="${options.badgeClass || 'predict-modal-badge'}">${options.badge}</span>` : '';
  document.getElementById('predictModalSubtitle').innerHTML = `
    <span>现价: ${fmtNum(m.price, 2)}</span>
    <span class="${pctCls}">涨跌: ${sign}${fmtNum(pct, 2)}%</span>
    <span>放量比: ${fmtNum(m.volume_ratio, 2)}</span>
    <span>突破位: ${fmtNum(m.breakout_level, 2)}</span>
    ${badge}
  `;

  document.getElementById('predictSummary').innerHTML = '';
  document.getElementById('predictStatus').innerHTML = '<span class="loading">加载历史K线...</span>';

  const kline = detail?.kline || [];
  if (kline.length) {
    renderPredictChartHistory(kline);
    document.getElementById('predictStatus').innerHTML = '<span class="loading">历史K线已加载，正在请求 Kronos 预测（首次可能需加载模型）...</span>';
  } else {
    document.getElementById('predictStatus').innerHTML = '<span class="loading">正在请求预测数据...</span>';
  }

  try {
    const pred = await request(`/api/predict/${symbol}/kronos?lookback=180&horizon=3`);
    renderPredictChartFull(pred.merged_kline, pred.prediction_start_index);
    renderPredictSummary(pred);
    document.getElementById('predictStatus').innerHTML = '';
  } catch (err) {
    document.getElementById('predictStatus').innerHTML = `<span class="error">预测失败: ${err.message}</span>`;
    if (!kline.length) {
      const chart = ensurePredictChart();
      if (chart) {
        chart.setOption({
          animation: false, xAxis: { show: false }, yAxis: { show: false }, series: [],
          graphic: { type: 'text', left: 'center', top: 'middle', style: { text: '预测失败', fill: '#ef4444', font: '14px sans-serif' } },
        });
      }
    }
  }
}

function renderPredictChartHistory(rows) {
  const chart = ensurePredictChart();
  if (!chart || !rows.length) return;
  const dates = rows.map(x => x.date);
  const candles = rows.map(x => [x.open, x.close, x.low, x.high]);
  const volumes = rows.map((x, i) => [i, x.volume, x.close >= x.open ? 1 : -1]);
  chart.setOption({
    animation: false, backgroundColor: 'transparent', legend: { show: false },
    tooltip: { trigger: 'axis', axisPointer: { type: 'cross' }, backgroundColor: 'rgba(15,23,42,0.92)', borderColor: '#334155', textStyle: { color: '#e2e8f0', fontSize: 12 } },
    axisPointer: { link: [{ xAxisIndex: 'all' }], label: { backgroundColor: '#334155' } },
    grid: [{ left: 56, right: 20, top: 16, height: '60%' }, { left: 56, right: 20, top: '78%', height: '15%' }],
    xAxis: [
      { type: 'category', data: dates, boundaryGap: true, axisLine: { lineStyle: { color: '#475569' } }, axisLabel: { color: '#64748b', fontSize: 10 }, splitLine: { show: false } },
      { type: 'category', gridIndex: 1, data: dates, boundaryGap: true, axisLine: { lineStyle: { color: '#475569' } }, axisLabel: { show: false }, splitLine: { show: false } },
    ],
    yAxis: [
      { scale: true, splitArea: { show: false }, axisLine: { lineStyle: { color: '#475569' } }, axisLabel: { color: '#64748b' }, splitLine: { lineStyle: { color: '#1f2937' } } },
      { scale: true, gridIndex: 1, splitNumber: 2, axisLine: { lineStyle: { color: '#475569' } }, axisLabel: { color: '#64748b' }, splitLine: { lineStyle: { color: '#1f2937' } } },
    ],
    dataZoom: [{ type: 'inside', xAxisIndex: [0, 1], start: 0, end: 100 }],
    series: [
      { name: '日K', type: 'candlestick', data: candles, itemStyle: { color: '#ef4444', color0: '#16a34a', borderColor: '#ef4444', borderColor0: '#16a34a' } },
      { name: '成交量', type: 'bar', xAxisIndex: 1, yAxisIndex: 1, data: volumes, itemStyle: { color: (p) => (p.data[2] > 0 ? '#ef4444' : '#16a34a') } },
    ],
  }, true);
}

function renderPredictChartFull(merged, predStartIdx) {
  const chart = ensurePredictChart();
  if (!chart || !merged.length) return;

  [merged, predStartIdx] = _sliceMergedForDisplay(merged, predStartIdx);

  const dates = merged.map(x => x.date);
  const candles = merged.map((x, i) => {
    const val = [x.open, x.close, x.low, x.high];
    if (i >= predStartIdx) {
      return { value: val, itemStyle: { opacity: 0.55, borderWidth: 0.8, borderColor: x.close >= x.open ? 'rgba(239,68,68,0.6)' : 'rgba(22,163,106,0.6)', color: x.close >= x.open ? 'rgba(239,68,68,0.4)' : 'rgba(22,163,106,0.4)' } };
    }
    return val;
  });
  const volumes = merged.map((x, i) => [i, i < predStartIdx ? x.volume : 0, x.close >= x.open ? 1 : -1]);

  const predBoundary = predStartIdx > 0 ? dates[predStartIdx] : null;
  const lastDate = dates[dates.length - 1];

  chart.setOption({
    animation: false, backgroundColor: 'transparent', legend: { show: false },
    tooltip: {
      trigger: 'axis', axisPointer: { type: 'cross' },
      backgroundColor: 'rgba(15,23,42,0.92)', borderColor: '#334155',
      textStyle: { color: '#e2e8f0', fontSize: 12 },
      formatter: function(params) {
        if (!params || !params.length) return '';
        const idx = params[0].dataIndex;
        const isPred = idx >= predStartIdx;
        const k = merged[idx];
        const tag = isPred ? '<span style="color:#facc15">[预测]</span>' : '[历史]';
        let html = `<div style="font-weight:600;margin-bottom:4px">${k.date} ${tag}</div>`;
        html += `<div>收: ${fmtNum(k.close, 2)} &nbsp; 高: ${fmtNum(k.high, 2)}</div>`;
        html += `<div>开: ${fmtNum(k.open, 2)} &nbsp; 低: ${fmtNum(k.low, 2)}</div>`;
        if (!isPred && k.volume) html += `<div>量: ${(k.volume / 10000).toFixed(0)}万</div>`;
        return html;
      }
    },
    axisPointer: { link: [{ xAxisIndex: 'all' }], label: { backgroundColor: '#334155' } },
    grid: [{ left: 56, right: 20, top: 16, height: '60%' }, { left: 56, right: 20, top: '78%', height: '15%' }],
    xAxis: [
      { type: 'category', data: dates, boundaryGap: true, axisLine: { lineStyle: { color: '#475569' } }, axisLabel: { color: '#64748b', fontSize: 10 }, splitLine: { show: false } },
      { type: 'category', gridIndex: 1, data: dates, boundaryGap: true, axisLine: { lineStyle: { color: '#475569' } }, axisLabel: { show: false }, splitLine: { show: false } },
    ],
    yAxis: [
      { scale: true, splitArea: { show: false }, axisLine: { lineStyle: { color: '#475569' } }, axisLabel: { color: '#64748b' }, splitLine: { lineStyle: { color: '#1f2937' } } },
      { scale: true, gridIndex: 1, splitNumber: 2, axisLine: { lineStyle: { color: '#475569' } }, axisLabel: { color: '#64748b' }, splitLine: { lineStyle: { color: '#1f2937' } } },
    ],
    dataZoom: [{ type: 'inside', xAxisIndex: [0, 1], start: 0, end: 100 }],
    series: [
      {
        name: '日K', type: 'candlestick', data: candles,
        itemStyle: { color: '#ef4444', color0: '#16a34a', borderColor: '#ef4444', borderColor0: '#16a34a' },
        markArea: predBoundary ? {
          silent: true,
          itemStyle: { color: 'rgba(250, 204, 21, 0.16)', borderColor: 'rgba(250, 204, 21, 0.8)', borderWidth: 1.5, borderType: 'dashed' },
          label: {
            show: true,
            position: 'top',
            distance: 4,
            formatter: '预 测',
            color: '#0b0f19',
            fontSize: 14,
            fontWeight: 800,
            backgroundColor: 'rgba(250, 204, 21, 0.98)',
            borderRadius: 4,
            padding: [4, 14, 4, 14],
            shadowColor: 'rgba(250, 204, 21, 0.5)',
            shadowBlur: 6,
          },
          data: [[{ xAxis: predBoundary }, { xAxis: lastDate }]],
        } : undefined,
        markLine: predBoundary ? {
          silent: true, symbol: 'none',
          data: [{
            xAxis: predBoundary,
            lineStyle: { type: 'dashed', color: 'rgba(250, 204, 21, 0.85)', width: 1.5 },
            label: { show: false },
          }],
        } : undefined,
      },
      { name: '成交量', type: 'bar', xAxisIndex: 1, yAxisIndex: 1, data: volumes, itemStyle: { color: (p) => (p.data[2] > 0 ? '#ef4444' : '#16a34a') } },
    ],
  }, true);
}

function renderPredictSummary(pred) {
  const container = document.getElementById('predictSummary');
  if (!pred || !pred.predicted_kline || !pred.predicted_kline.length) {
    container.innerHTML = '';
    return;
  }
  const pk = pred.predicted_kline;
  const hk = pred.history_kline;
  const lastClose = hk.length ? hk[hk.length - 1].close : 0;

  const day3Close = pk[pk.length - 1].close;
  const chgPct = lastClose ? ((day3Close - lastClose) / lastClose * 100) : 0;
  const chgCls = chgPct >= 0 ? 'up' : 'down';
  const chgSign = chgPct >= 0 ? '+' : '';

  const maxHigh = Math.max(...pk.map(k => k.high));
  const minLow = Math.min(...pk.map(k => k.low));

  const items = pk.map((k, i) => {
    const d = chgSign2(k.close, lastClose);
    return `<div class="predict-summary-item">
      <span class="predict-summary-label">第${i + 1}日预测收盘 (${k.date})</span>
      <span class="predict-summary-value ${d.cls}">${fmtNum(k.close, 2)} ${d.txt}</span>
    </div>`;
  });

  items.push(`<div class="predict-summary-item">
    <span class="predict-summary-label">3日最高预测价</span>
    <span class="predict-summary-value">${fmtNum(maxHigh, 2)}</span>
  </div>`);
  items.push(`<div class="predict-summary-item">
    <span class="predict-summary-label">3日最低预测价</span>
    <span class="predict-summary-value">${fmtNum(minLow, 2)}</span>
  </div>`);
  items.push(`<div class="predict-summary-item">
    <span class="predict-summary-label">第3日预测涨跌幅</span>
    <span class="predict-summary-value ${chgCls}">${chgSign}${fmtNum(chgPct, 2)}%</span>
  </div>`);

  items.push(`<div class="predict-summary-meta">
    模型: ${esc(pred.model)} · 设备: ${esc(pred.device)} · 窗口: ${pred.lookback}→${pred.horizon} · 生成: ${(pred.generated_at || '').replace('T', ' ')}
  </div>`);

  container.innerHTML = items.join('');
}

function chgSign2(val, base) {
  if (!base) return { cls: '', txt: '' };
  const pct = (val - base) / base * 100;
  const sign = pct >= 0 ? '+' : '';
  return { cls: pct >= 0 ? 'up' : 'down', txt: `(${sign}${fmtNum(pct, 2)}%)` };
}

/* ==================== WebSocket ==================== */

let _wsRetryDelay = 2000;
function connectWs() {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(`${protocol}//${window.location.host}/ws/realtime`);
  ws.onopen = () => { _wsRetryDelay = 2000; };
  ws.onmessage = (evt) => {
    try {
      const msg = JSON.parse(evt.data);
      if (!msg || !msg.data) return;
      if (msg.event === 'snapshot') {
        state.funnel = msg.data.funnel;
        state.hotConcepts = msg.data.hot_concepts;
        state.hotStocks = msg.data.hot_stocks || state.hotStocks;
        renderHotConcepts();
        renderHotStocks();
        renderFunnel();
        setMeta();
      } else if (msg.event === 'monitor_update') {
        _appendMonitorMessage({
          id: msg.data.message_id || '',
          content: msg.data.content || '',
          created_at: msg.data.created_at || '',
          trigger: msg.data.trigger || 'scheduled',
        });
        if (state.activeTab === 'agent') {
          setStatus('收到智能监控推送', 'success');
        }
      }
    } catch (err) {
      console.error('ws parse error', err);
    }
  };
  ws.onerror = () => {};
  ws.onclose = () => {
    setTimeout(connectWs, _wsRetryDelay);
    _wsRetryDelay = Math.min(_wsRetryDelay * 1.5, 30000);
  };
}

/* ==================== Button Loading Helper ==================== */

function _btnStart(btn, loadingText) {
  btn.disabled = true;
  btn._origText = btn.textContent;
  btn.textContent = loadingText || '处理中...';
  btn.classList.add('btn-loading');
}

function _btnEnd(btn) {
  btn.textContent = btn._origText || '操作';
  btn.disabled = false;
  btn.classList.remove('btn-loading');
}

/* ==================== Init ==================== */

async function init() {
  document.querySelectorAll('.sidebar-item[data-tab]').forEach((el) => {
    el.onclick = (e) => { e.preventDefault(); switchTab(el.dataset.tab); };
  });

  document.querySelectorAll('.agent-inner-tab').forEach(btn => {
    btn.onclick = () => {
      const sub = btn.dataset.subtab;
      document.querySelectorAll('.agent-inner-tab').forEach(b => b.classList.toggle('active', b === btn));
      document.getElementById('agentSubMonitor').classList.toggle('active', sub === 'monitor');
      const aiEl = document.getElementById('agentSubAI');
      if (aiEl) aiEl.classList.toggle('active', sub === 'ai');
      if (sub === 'ai') loadAiPanel();
    };
  });

  document.getElementById('predictModalClose').onclick = closePredictModal;
  document.getElementById('predictModal').onclick = (e) => {
    if (e.target === e.currentTarget) closePredictModal();
  };

  _updateMarketStatus();
  setInterval(_updateMarketStatus, 30000);

  document.getElementById('btnPaperRefresh').onclick = () => {
    refreshPaperPrices();
    _startPaperPoll();
  };
  setInterval(_updatePaperRefreshHint, 5000);
  document.getElementById('btnPaperSettings').onclick = () => {
    const panel = document.getElementById('paperSettingsPanel');
    panel.style.display = panel.style.display === 'none' ? '' : 'none';
  };
  document.getElementById('btnPaperSettingsSave').onclick = async () => {
    try {
      await request('/api/paper/settings', {
        method: 'POST',
        body: JSON.stringify({
          commission_rate: parseFloat(document.getElementById('psCommission').value),
          min_commission: parseFloat(document.getElementById('psMinComm').value),
          stamp_tax_rate: parseFloat(document.getElementById('psStampTax').value),
          slippage_rate: parseFloat(document.getElementById('psSlippage').value),
        }),
      });
      setStatus('费用设置已保存', 'success');
      document.getElementById('paperSettingsPanel').style.display = 'none';
      loadPaperData();
    } catch (err) {
      setStatus(`保存失败: ${err.message}`, 'error');
    }
  };
  document.getElementById('btnMonitorKlineClose').onclick = () => {
    document.getElementById('monitorKlineSection').style.display = 'none';
    if (state.monitorKlineChart) { state.monitorKlineChart.dispose(); state.monitorKlineChart = null; }
  };

  _initHoverKlinePopupEvents();

  const dcStart = document.getElementById('dcSyncStart');
  const dcEnd = document.getElementById('dcSyncEnd');
  const _fmtDate = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  const today = new Date();
  dcEnd.value = _fmtDate(today);
  const weekAgo = new Date(today); weekAgo.setDate(weekAgo.getDate() - 7);
  dcStart.value = _fmtDate(weekAgo);

  document.getElementById('btnDcFullSync').onclick = async () => {
    const btn = document.getElementById('btnDcFullSync');
    _btnStart(btn, '同步中...');
    setStatus('全量同步（智能补缺）执行中...', 'info');
    try {
      const payload = await request('/api/jobs/kline-cache/sync?trigger_mode=manual&force=true', { method: 'POST' });
      const filled = payload.missing_filled ?? 0;
      const mTotal = payload.missing_total ?? 0;
      const unfillable = payload.missing_unfillable ?? 0;
      let extra = '';
      if (mTotal > 0) {
        if (unfillable > 0 && filled === 0) {
          extra = ` · ${mTotal}条缺失均为停牌/未上市`;
        } else if (unfillable > 0) {
          extra = ` · 实补${filled}条，${unfillable}条停牌无数据`;
        } else {
          extra = ` · 实补${filled}/${mTotal}条`;
        }
      }
      setStatus(`同步完成: ${payload.success_symbols || 0}/${payload.total_symbols || 0}股${extra}`, 'success');
      await loadDataCenter();
    } catch (err) {
      setStatus(`同步失败: ${err.message}`, 'error');
      await loadDataCenter();
    } finally {
      _btnEnd(btn);
    }
  };

  function _tradeDatesBetween(start, end) {
    const dates = [];
    const cur = new Date(start + 'T00:00:00');
    const last = new Date(end + 'T00:00:00');
    while (cur <= last) {
      const day = cur.getDay();
      if (day !== 0 && day !== 6) dates.push(_fmtDate(cur));
      cur.setDate(cur.getDate() + 1);
    }
    return dates;
  }

  document.getElementById('btnDcIncrSync').onclick = async () => {
    const btn = document.getElementById('btnDcIncrSync');
    const startVal = dcStart.value;
    const endVal = dcEnd.value;
    if (!startVal || !endVal) { setStatus('请选择起止日期', 'error'); return; }
    if (startVal > endVal) { setStatus('开始日期不能晚于结束日期', 'error'); return; }
    const dates = _tradeDatesBetween(startVal, endVal);
    if (!dates.length) { setStatus('所选范围内无交易日', 'error'); return; }
    _btnStart(btn, `同步 0/${dates.length}`);
    setStatus(`增量同步 ${startVal} ~ ${endVal} (${dates.length}天) 执行中...`, 'info');
    let ok = 0, fail = 0;
    try {
      for (let i = 0; i < dates.length; i++) {
        try {
          await request(`/api/jobs/kline-cache/incremental-sync?trade_date=${dates[i]}&trigger_mode=manual`, { method: 'POST' });
          ok++;
        } catch { fail++; }
        btn.textContent = `同步 ${i + 1}/${dates.length}`;
      }
      setStatus(`增量同步完成: ${startVal}~${endVal} · 成功${ok}天 / 失败${fail}天`, ok > 0 ? 'success' : 'error');
      await loadDataCenter();
    } catch (err) {
      setStatus(`增量同步失败: ${err.message}`, 'error');
      await loadDataCenter();
    } finally {
      _btnEnd(btn);
    }
  };

  document.getElementById('btnDcLogsToggle').onclick = () => {
    state.dcLogsExpanded = !state.dcLogsExpanded;
    renderDcLogStream(state.syncLogs || { items: [], total: 0 });
  };

  document.getElementById('btnDcCheck').onclick = async () => {
    const btn = document.getElementById('btnDcCheck');
    _btnStart(btn, '检查中...');
    setStatus('数据完整性检查中...', 'info');
    try {
      const report = await request('/api/jobs/kline-cache/check', { method: 'POST' });
      setStatus(`检查完成: 覆盖率 ${report.coverage_pct || 0}% 缺失 ${report.total_missing || 0} 条`, 'success');
      await loadDataCenter();
      document.getElementById('dcReportContent')?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    } catch (err) {
      setStatus(`检查失败: ${err.message}`, 'error');
    } finally {
      _btnEnd(btn);
    }
  };

  document.getElementById('clearConcept').onclick = () => {
    state.selectedConcept = null;
    renderHotConcepts();
    renderFunnel();
  };

  document.getElementById('btnEodScreen').onclick = async () => {
    const btn = document.getElementById('btnEodScreen');
    _btnStart(btn, '执行中...');
    setStatus('盘后筛选执行中...', 'info');
    try {
      const payload = await request('/api/jobs/eod-screen', { method: 'POST' });
      await reloadFunnel();
      setStatus(`盘后筛选完成: 候选${payload.candidate_count || 0}只`, 'success');
    } catch (err) {
      setStatus(`盘后筛选失败: ${err.message}`, 'error');
    } finally {
      _btnEnd(btn);
    }
  };

  document.getElementById('btnNoticeScreen').onclick = async () => {
    const btn = document.getElementById('btnNoticeScreen');
    _btnStart(btn, '执行中...');
    setStatus('公告筛选执行中...', 'info');
    try {
      const today = new Date();
      const y = today.getFullYear();
      const m = String(today.getMonth() + 1).padStart(2, '0');
      const d = String(today.getDate()).padStart(2, '0');
      let url = `/api/jobs/notice-screen?notice_date=${y}${m}${d}&limit=50`;
      if (state.activeKeywords.size > 0) {
        url += `&keywords=${encodeURIComponent([...state.activeKeywords].join(','))}`;
      }
      const payload = await request(url, { method: 'POST' });
      await reloadNotice();
      setStatus(`公告筛选完成: ${payload.candidate_count || 0}只`, 'success');
    } catch (err) {
      setStatus(`公告筛选失败: ${err.message}`, 'error');
    } finally {
      _btnEnd(btn);
    }
  };

  document.getElementById('btnAgentRun').onclick = async () => {
    const btn = document.getElementById('btnAgentRun');
    _btnStart(btn, '诊断执行中...');
    setStatus('Hermes 正在采集数据并执行诊断分析...', 'info');
    try {
      const payload = await request('/api/agent/run', { method: 'POST', body: JSON.stringify({ task_type: 'full_diagnosis' }) });
      const msg = payload.summary?.message || '全面诊断完成';
      const dailyUsedLLM = payload.summary?.daily?.llm_used;
      const noticeUsedLLM = payload.summary?.notice?.llm_used;
      const llmHint = (dailyUsedLLM || noticeUsedLLM) ? '（LLM 分析）' : '（规则诊断）';
      setStatus(`✅ ${msg} ${llmHint}`, 'success');
      await loadAgentData();
    } catch (err) {
      setStatus(`诊断失败: ${err.message}`, 'error');
    } finally {
      _btnEnd(btn);
    }
  };

  // ── 智能监控按钮绑定 ──
  document.getElementById('btnMonitorToggle').onclick = async () => {
    const btn = document.getElementById('btnMonitorToggle');
    const isOn = state.monitorConfig && state.monitorConfig.enabled;
    if (isOn) {
      _btnStart(btn, '停止中...');
      try {
        await request('/api/agent/monitor/stop', { method: 'POST' });
        state.monitorConfig.enabled = false;
        _updateMonitorStatusUI(false);
        setStatus('智能监控已停止', 'success');
      } catch (err) { setStatus(`停止失败: ${err.message}`, 'error'); }
      finally { _btnEnd(btn); }
    } else {
      _btnStart(btn, '启动中...');
      try {
        const interval = parseInt(document.getElementById('monitorInterval').value) || 10;
        const result = await request('/api/agent/monitor/config', {
          method: 'POST',
          body: JSON.stringify({ enabled: true, interval_minutes: interval }),
        });
        state.monitorConfig = result;
        _updateMonitorStatusUI(true);
        setStatus(`智能监控已启动，每 ${interval} 分钟推送`, 'success');
      } catch (err) { setStatus(`启动失败: ${err.message}`, 'error'); }
      finally { _btnEnd(btn); }
    }
  };

  document.getElementById('btnMonitorTrigger').onclick = async () => {
    const btn = document.getElementById('btnMonitorTrigger');
    _btnStart(btn, '分析中...');
    setStatus('智能监控手动触发中，请稍候...', 'info');
    try {
      const result = await request('/api/agent/monitor/trigger', { method: 'POST' });
      if (result.success) {
        setStatus(`智能监控完成 (${result.elapsed_ms || 0}ms)`, 'success');
      } else {
        setStatus(`监控执行失败: ${result.message || ''}`, 'error');
      }
    } catch (err) {
      setStatus(`手动触发失败: ${err.message}`, 'error');
    } finally { _btnEnd(btn); }
  };

  document.getElementById('btnMonitorPromptToggle').onclick = () => {
    const wrap = document.getElementById('monitorPromptWrap');
    wrap.style.display = wrap.style.display === 'none' ? 'block' : 'none';
  };

  document.getElementById('btnMonitorPromptSave').onclick = async () => {
    const btn = document.getElementById('btnMonitorPromptSave');
    const prompt = document.getElementById('monitorPrompt').value.trim();
    if (!prompt) { setStatus('提示词不能为空', 'error'); return; }
    _btnStart(btn, '保存中...');
    try {
      const result = await request('/api/agent/monitor/config', {
        method: 'POST', body: JSON.stringify({ system_prompt: prompt }),
      });
      state.monitorConfig = result;
      setStatus('提示词已保存', 'success');
    } catch (err) { setStatus(`保存失败: ${err.message}`, 'error'); }
    finally { _btnEnd(btn); }
  };

  document.getElementById('btnMonitorPromptReset').onclick = async () => {
    try {
      const result = await request('/api/agent/monitor/config', {
        method: 'POST', body: JSON.stringify({ system_prompt: null }),
      });
      state.monitorConfig = result;
      document.getElementById('monitorPrompt').value = result.system_prompt || '';
      setStatus('提示词已恢复默认', 'success');
    } catch (err) { setStatus(`恢复失败: ${err.message}`, 'error'); }
  };

  document.getElementById('monitorInterval').onchange = async () => {
    const interval = parseInt(document.getElementById('monitorInterval').value) || 10;
    try {
      const result = await request('/api/agent/monitor/config', {
        method: 'POST', body: JSON.stringify({ interval_minutes: interval }),
      });
      state.monitorConfig = result;
    } catch (_) {}
  };

  const urlTab = new URLSearchParams(window.location.search).get('tab');
  switchTab((urlTab && TAB_TITLES[urlTab]) ? urlTab : (state.activeTab || 'market'));

  await reload();
  connectWs();

  let _dcPollTimer = null;
  function startDcPolling() {
    if (_dcPollTimer) return;
    _dcPollTimer = setInterval(async () => {
      if (state.activeTab !== 'data') { clearInterval(_dcPollTimer); _dcPollTimer = null; return; }
      try { await loadDataCenter(); } catch (_) {}
    }, 3000);
  }

  let _marketPollTimer = null;
  function startMarketPolling() {
    if (_marketPollTimer) return;
    _marketPollTimer = setInterval(async () => {
      if (state.activeTab !== 'market') { clearInterval(_marketPollTimer); _marketPollTimer = null; return; }
      try {
        const [hc, hs] = await Promise.allSettled([
          request('/api/market/hot-concepts'),
          request('/api/market/hot-stocks'),
        ]);
        if (hc.status === 'fulfilled') state.hotConcepts = hc.value;
        if (hs.status === 'fulfilled') state.hotStocks = hs.value;
        renderHotConcepts();
        renderHotStocks();
      } catch (_) {}
    }, 10000);
  }

  const _origSwitchTab = switchTab;
  switchTab = function(tab) {
    _origSwitchTab(tab);
    if (tab === 'data') startDcPolling();
    if (tab === 'market') startMarketPolling();
  };
  if (state.activeTab === 'market') startMarketPolling();
  if (state.activeTab === 'data') startDcPolling();
  if (state.activeTab === 'paper') _startPaperPoll();
  if (state.activeTab === 'predict') { loadPredictFunnel(); _startPredictPoll(); }
  if (state.activeTab === 'hotai') { loadHotStockAI(); _startHotAiPoll(); }
  if (state.activeTab === 'graphic') { loadGraphicFunnel(); _startGraphicPoll(); }

  document.addEventListener('visibilitychange', () => {
    if (state.activeTab === 'paper') {
      if (document.hidden) _stopPaperPoll(); else _startPaperPoll();
    }
    if (state.activeTab === 'predict') {
      if (document.hidden) _stopPredictPoll(); else _startPredictPoll();
    }
    if (state.activeTab === 'hotai') {
      if (document.hidden) _stopHotAiPoll(); else _startHotAiPoll();
    }
    if (state.activeTab === 'graphic') {
      if (document.hidden) _stopGraphicPoll(); else _startGraphicPoll();
    }
  });

  _bindPredictActions();
  _bindHotAiActions();
  _bindGraphicActions();
  _bindStrategyCenterActions();
}

/* ==================== 预测选股 ==================== */

let _predictPollTimer = null;
let _hotAiPollTimer = null;
let _graphicPollTimer = null;

function _startPredictPoll() {
  _stopPredictPoll();
  const tick = async () => {
    if (state.activeTab !== 'predict') { _stopPredictPoll(); return; }
    try { await loadPredictFunnel(); } catch (_) {}
    const interval = state.predictRunning ? 2500 : 15000;
    _predictPollTimer = setTimeout(tick, interval);
  };
  _predictPollTimer = setTimeout(tick, 1000);
}

function _stopPredictPoll() {
  if (_predictPollTimer) { clearTimeout(_predictPollTimer); _predictPollTimer = null; }
}

function _startHotAiPoll() {
  _stopHotAiPoll();
  const tick = async () => {
    if (state.activeTab !== 'hotai') { _stopHotAiPoll(); return; }
    try { await loadHotStockAI(); } catch (_) {}
    const interval = state.hotStockAIRunning ? 2500 : 15000;
    _hotAiPollTimer = setTimeout(tick, interval);
  };
  _hotAiPollTimer = setTimeout(tick, 1000);
}

function _stopHotAiPoll() {
  if (_hotAiPollTimer) { clearTimeout(_hotAiPollTimer); _hotAiPollTimer = null; }
}

async function loadPredictFunnel() {
  try {
    const snap = await request('/api/predict-funnel');
    state.predictFunnel = snap;
    state.predictRunning = !!snap.running;
    renderPredictFunnel();
    if (state.activeTab === 'predict') setMeta();
  } catch (err) {
    const el = document.getElementById('predictMeta');
    if (el) el.textContent = `加载失败: ${err.message}`;
  }
}

function _predictPctCls(pct) {
  if (pct >= 8) return 'up';
  if (pct >= 4) return 'up';
  if (pct >= 2) return 'up';
  if (pct <= -2) return 'down';
  return 'neutral';
}

function _predictCardHtml(e) {
  const pct = Number(e.pred_max_high_pct || 0);
  const cls = _predictPctCls(pct);
  const boards = (e.boards || []).slice(0, 3)
    .map(b => `<span class="predict-card-board-tag">${b.name}</span>`).join('');
  const sign = pct >= 0 ? '+' : '';
  return `
    <div class="pool-card" data-symbol="${e.symbol}" onclick="openPredictDetail('${e.symbol}')">
      <div class="card-head">
        <div class="card-name">
          <span class="stock-name">${e.name || e.symbol}</span>
          <span class="stock-code">${e.symbol}</span>
        </div>
        <div class="card-score ${cls}">${sign}${pct.toFixed(1)}%</div>
      </div>
      <div class="predict-card-meta">
        <span class="predict-card-metric">今收 <b>${fmtNum(e.today_close)}</b></span>
        <span class="predict-card-metric">3日高 <b>${fmtNum(e.pred_max_high)}</b></span>
        <span class="predict-card-metric">末日 ${(e.pred_last_close_pct >= 0 ? '+' : '')}${fmtNum(e.pred_last_close_pct, 1)}%</span>
        <span class="predict-card-metric">均价 ${(e.pred_avg_close_pct >= 0 ? '+' : '')}${fmtNum(e.pred_avg_close_pct, 1)}%</span>
      </div>
      ${boards ? `<div class="predict-card-boards">${boards}</div>` : ''}
    </div>`;
}

function renderPredictFunnel() {
  const snap = state.predictFunnel;
  const metaEl = document.getElementById('predictMeta');
  const progEl = document.getElementById('predictProgress');
  const btn = document.getElementById('btnPredictRun');
  if (!snap) {
    if (metaEl) metaEl.textContent = '无数据';
    return;
  }
  const cfg = snap.config || {};
  const thC = document.getElementById('predict-th-candidate');
  const thF = document.getElementById('predict-th-focus');
  const thB = document.getElementById('predict-th-buy');
  if (thC) thC.textContent = `≥${cfg.threshold_candidate || 2}%`;
  if (thF) thF.textContent = `≥${cfg.threshold_focus || 4}%`;
  if (thB) thB.textContent = `≥${cfg.threshold_buy || 8}%`;

  const tog = document.getElementById('predictFeishuToggle');
  if (tog && typeof cfg.feishu_enabled === 'boolean') tog.checked = cfg.feishu_enabled;

  const pools = snap.pools || { candidate: [], focus: [], buy: [] };
  const renderPool = (id, list) => {
    const el = document.getElementById(id);
    const cnt = document.getElementById(`predict-count-${id.split('-').pop()}`);
    if (cnt) cnt.textContent = (list || []).length;
    if (!el) return;
    if (!list || list.length === 0) {
      el.innerHTML = `<div class="empty-pool">暂无</div>`;
      return;
    }
    el.innerHTML = list.map(_predictCardHtml).join('');
  };
  renderPool('predict-pool-candidate', pools.candidate);
  renderPool('predict-pool-focus', pools.focus);
  renderPool('predict-pool-buy', pools.buy);

  const prog = snap.progress || {};
  const running = !!snap.running;
  if (btn) {
    btn.disabled = running;
    btn.textContent = running ? '预测中…' : '立即预测';
  }
  if (progEl) {
    if (running) {
      progEl.textContent = `${prog.phase || '...'} ${prog.current || 0}/${prog.total || 0} ${prog.detail || ''}`;
    } else if (prog.error) {
      progEl.textContent = `失败：${prog.error}`;
    } else if (snap.meta && snap.meta.elapsed_sec) {
      progEl.textContent = `上次 ${snap.meta.trigger || '手动'} · ${snap.meta.elapsed_sec}s · 扫描${snap.meta.stocks_scanned || 0}股`;
    } else {
      progEl.textContent = '';
    }
  }
  if (metaEl) {
    const m = snap.meta || {};
    metaEl.textContent = `交易日 ${snap.trade_date || '--'} · 命中 ${m.entries_count || 0} · 板块${m.boards_used || 0}`;
  }
  const summary = document.getElementById('predictPageSummary');
  if (summary) {
    summary.innerHTML = [
      _psItem('候选', (pools.candidate || []).length),
      _psSep(),
      _psItem('关注', (pools.focus || []).length),
      _psSep(),
      _psItem('买入', (pools.buy || []).length, 'warning'),
      _psSep(),
      _psItem('板块', (snap.meta?.boards_used || 0)),
    ].join('');
  }
}

function _hotAiScoreCls(score) {
  const cfg = state.hotStockAI?.config || {};
  if (score >= Number(cfg.threshold_buy || 14.5)) return 'up';
  if (score >= Number(cfg.threshold_focus || 11.5)) return 'up';
  return 'neutral';
}

function _hotAiDecisionCls(decision) {
  const value = String(decision || '').toUpperCase();
  if (value === 'BUY') return 'buy';
  if (value === 'OVERWEIGHT') return 'watch';
  if (value === 'UNDERWEIGHT' || value === 'SELL') return 'risk';
  return 'hold';
}

function _hotAiDecisionText(decision) {
  const value = String(decision || '').toUpperCase();
  return {
    BUY: '买入',
    OVERWEIGHT: '增配',
    HOLD: '观望',
    UNDERWEIGHT: '减配',
    SELL: '回避',
  }[value] || '待讨论';
}

function _hotAiActions(poolName) {
  const actions = [];
  if (poolName === 'candidate') actions.push(['加入重点', 'focus']);
  if (poolName === 'focus') {
    actions.push(['移回候选', 'candidate']);
    actions.push(['加入买入', 'buy']);
  }
  if (poolName === 'buy') actions.push(['降级重点', 'focus']);
  return actions;
}

async function moveHotAiPool(symbol, targetPool) {
  try {
    const res = await request('/api/strategy/hot-stock-ai/pool/move', {
      method: 'POST',
      body: JSON.stringify({ symbol, target_pool: targetPool }),
    });
    if (!res.success) {
      alert(res.message || '迁移失败');
      return;
    }
    state.hotStockAI = res.snapshot;
    renderHotStockAI();
    setStatus(res.message || '迁移成功', 'success');
  } catch (err) {
    alert(err.message || '迁移失败');
  }
}

function _hotAiCardHtml(e, poolName) {
  const score = Number(e.score || 0);
  const cls = _hotAiScoreCls(score);
  const ta = e.tradingagents || {};
  const decision = String(ta.decision || '').toUpperCase();
  const evaluationText = e.evaluation_text || _hotAiDecisionText(decision);
  const baseScore = Number(e.base_score ?? e.score ?? 0);
  const bonus = Number(e.tradingagents_bonus || 0);
  const bonusSign = bonus > 0 ? '+' : '';
  const decisionCls = _hotAiDecisionCls(decision);
  const sourceText = ta.source === 'cache' ? '缓存讨论' : ta.source === 'fresh' ? '实时讨论' : '';
  const taSummary = ta.summary || ta.discussion || '';
  const pct = Number(e.change_pct || 0);
  const pctCls = _chgCls(pct);
  const pctSign = pct > 0 ? '+' : '';
  const price = Number(e.latest_price || 0);
  const priceHtml = price > 0 ? `<span class="stock-price">${fmtNum(price, 2)}</span>` : '';
  const pctHtml = pct !== 0 ? ` <span class="${pctCls}">${pctSign}${fmtNum(pct, 2)}%</span>` : '';
  const tags = (e.tags || []).slice(0, 4)
    .map(tag => `<span class="tag hot-ai-tag">${tag}</span>`)
    .join('');
  const btns = _hotAiActions(poolName)
    .map(([txt, pool]) => `<button data-hotai-pool="${pool}" data-symbol="${e.symbol}">${txt}</button>`)
    .join('');
  const decisionHtml = ta.status === 'ok'
    ? `<div class="hot-ai-ta-row">
        <span class="hot-ai-ta-pill ${decisionCls}">TradingAgents ${_hotAiDecisionText(decision)}</span>
        <span class="hot-ai-ta-source">${sourceText}</span>
        <span class="hot-ai-ta-bonus ${bonus >= 0 ? 'up' : 'down'}">${bonusSign}${fmtNum(bonus, 1)}分</span>
      </div>`
    : ta.status === 'failed'
      ? `<div class="hot-ai-ta-row"><span class="hot-ai-ta-pill risk">讨论失败</span><span class="hot-ai-ta-source">${ta.error || '调用异常'}</span></div>`
      : ta.status === 'skipped'
        ? `<div class="hot-ai-ta-row"><span class="hot-ai-ta-pill hold">未讨论</span><span class="hot-ai-ta-source">${ta.reason || '未进入讨论名单'}</span></div>`
        : ta.status === 'disabled'
          ? `<div class="hot-ai-ta-row"><span class="hot-ai-ta-pill hold">讨论关闭</span></div>`
          : `<div class="hot-ai-ta-row"><span class="hot-ai-ta-pill hold">待讨论</span></div>`;
  return `
    <div class="stock-card hot-ai-card ${state.selectedSymbol === e.symbol ? 'active' : ''}" data-symbol="${e.symbol}" onclick="openHotAiDetail('${e.symbol}')">
      <div class="stock-top">
        <div class="stock-name">${e.name || e.symbol} <span class="stock-code">${e.symbol}</span></div>
        <div class="stock-price-area">${priceHtml}${pctHtml}</div>
      </div>
      ${tags ? `<div class="tags">${tags}</div>` : ''}
      <div class="metrics">
        <span>评分 <b class="${cls}">${fmtNum(score, 1)}</b></span>
        <span class="metrics-sep">·</span>
        <span>基础分 <b>${fmtNum(baseScore, 1)}</b></span>
        <span class="metrics-sep">·</span>
        <span>热度 #<b>${e.rank || '--'}</b></span>
        <span class="metrics-sep">·</span>
        <span>3日高 <b>${(Number(e.pred_max_high_pct || 0) >= 0 ? '+' : '')}${fmtNum(e.pred_max_high_pct, 2)}%</b></span>
      </div>
      <div class="metrics">
        <span>距MA20 <b>${(Number(e.dist_ma20_pct || 0) >= 0 ? '+' : '')}${fmtNum(e.dist_ma20_pct, 2)}%</b></span>
        <span class="metrics-sep">·</span>
        <span>距20日高 <b>${(Number(e.dist_high20_pct || 0) >= 0 ? '+' : '')}${fmtNum(e.dist_high20_pct, 2)}%</b></span>
        <span class="metrics-sep">·</span>
        <span>量额比20日 <b>${fmtNum(e.amount_ratio_20d, 2)}</b></span>
      </div>
      <div class="metrics hot-ai-breakdown">
        <span>热度分 <b>${fmtNum(e.score_breakdown?.popularity, 1)}</b></span>
        <span class="metrics-sep">·</span>
        <span>趋势分 <b>${fmtNum(e.score_breakdown?.trend, 1)}</b></span>
        <span class="metrics-sep">·</span>
        <span>预测分 <b>${fmtNum(e.score_breakdown?.prediction, 1)}</b></span>
        <span class="metrics-sep">·</span>
        <span>风险扣分 <b>${fmtNum(e.score_breakdown?.risk_penalty, 1)}</b></span>
      </div>
      <div class="metrics"><span>评价 <b>${evaluationText}</b></span></div>
      ${decisionHtml}
      ${taSummary ? `<div class="hot-ai-ta-summary">${taSummary}</div>` : ''}
      <div class="hot-ai-analysis">${e.analysis || '暂无分析摘要'}</div>
      <div class="card-actions">${btns}</div>
    </div>`;
}

function renderHotStockAI() {
  const snap = state.hotStockAI;
  const metaEl = document.getElementById('hotAiMeta');
  const progEl = document.getElementById('hotAiProgress');
  const btn = document.getElementById('btnHotAiRun');
  const summary = document.getElementById('hotAiPageSummary');
  const toggle = document.getElementById('hotAiAutoToggle');
  if (!snap) {
    if (metaEl) metaEl.textContent = '无数据';
    if (summary) summary.innerHTML = '';
    return;
  }
  const cfg = snap.config || {};
  state.hotStockAIConfig = cfg;
  if (toggle) toggle.checked = !!cfg.auto_refresh_enabled;
  const thC = document.getElementById('hotai-th-candidate');
  const thF = document.getElementById('hotai-th-focus');
  const thB = document.getElementById('hotai-th-buy');
  if (thC) thC.textContent = `≥${cfg.threshold_candidate || 8}分`;
  if (thF) thF.textContent = `≥${cfg.threshold_focus || 11.5}分`;
  if (thB) thB.textContent = `≥${cfg.threshold_buy || 14.5}分`;

  const pools = snap.pools || { candidate: [], focus: [], buy: [] };
  const renderPool = (id, list) => {
    const el = document.getElementById(id);
    const cnt = document.getElementById(`hotai-count-${id.split('-').pop()}`);
    if (cnt) cnt.textContent = (list || []).length;
    if (!el) return;
    if (!list || list.length === 0) {
      el.innerHTML = '<div class="empty-pool">暂无</div>';
      return;
    }
    el.innerHTML = list.map((item) => _hotAiCardHtml(item, id.split('-').pop())).join('');
    el.querySelectorAll('button[data-hotai-pool]').forEach((btn) => {
      btn.onclick = (e) => {
        e.stopPropagation();
        moveHotAiPool(btn.dataset.symbol, btn.dataset.hotaiPool);
      };
    });
  };
  renderPool('hotai-pool-candidate', pools.candidate);
  renderPool('hotai-pool-focus', pools.focus);
  renderPool('hotai-pool-buy', pools.buy);

  const prog = snap.progress || {};
  const running = !!snap.running;
  if (btn) {
    btn.disabled = running;
    btn.textContent = running ? '分析中…' : '立即分析';
  }
  if (progEl) {
    if (running) {
      progEl.textContent = `${prog.phase || '...'} ${prog.current || 0}/${prog.total || 0} ${prog.detail || ''}`;
    } else if (prog.error) {
      progEl.textContent = `失败：${prog.error}`;
    } else if (snap.meta?.elapsed_sec) {
      progEl.textContent = `上次${snap.meta.trigger || '手动'} · ${snap.meta.elapsed_sec}s · 失败${snap.meta.failed_count || 0}`;
    } else {
      progEl.textContent = '';
    }
  }
  if (metaEl) {
    const m = snap.meta || {};
    const modeText = m.execution_mode === 'light_auto' ? ' · 自动轻量模式' : '';
    const taText = m.runtime_tradingagents_enabled
      ? ` · 讨论 ${m.tradingagents_discussed || 0} · 缓存 ${m.tradingagents_cache_hits || 0}`
      : (m.tradingagents_enabled ? ' · 自动任务已跳过讨论' : '');
    const backendText = m.tradingagents_backend ? ` · ${m.tradingagents_backend}` : '';
    metaEl.textContent = `交易日 ${snap.trade_date || '--'} · 分析 ${m.entries_count || 0}/${m.stocks_scanned || cfg.top_n || 20}${modeText} · Kronos ${m.kronos_device || '--'}${taText}${backendText} · 均分 ${fmtNum(m.avg_score || 0, 1)}`;
  }
  if (summary) {
    summary.innerHTML = [
      _psItem('候选', (pools.candidate || []).length),
      _psSep(),
      _psItem('关注', (pools.focus || []).length),
      _psSep(),
      _psItem('买入', (pools.buy || []).length, 'warning'),
      _psSep(),
      _psItem('均分', fmtNum(snap.meta?.avg_score || 0, 1)),
      _psSep(),
      _psItem('扫描', `${snap.meta?.stocks_scanned || 0}只`),
      _psSep(),
      _psItem('讨论', `${snap.meta?.tradingagents_discussed || 0}只`, snap.meta?.tradingagents_discussed ? 'brand' : ''),
    ].join('');
  }
}

async function loadHotStockAI() {
  try {
    const snap = await request('/api/strategy/hot-stock-ai');
    state.hotStockAI = snap;
    state.hotStockAIRunning = !!snap.running;
    renderHotStockAI();
    if (state.activeTab === 'hotai') setMeta();
  } catch (err) {
    const el = document.getElementById('hotAiMeta');
    if (el) el.textContent = `加载失败: ${err.message}`;
  }
}

function _graphicScoreCls(score) {
  const cfg = state.graphicFunnel?.config || {};
  if (score >= Number(cfg.threshold_buy || 14)) return 'up';
  if (score >= Number(cfg.threshold_focus || 10)) return 'up';
  return 'neutral';
}

function _graphicCardHtml(e) {
  const score = Number(e.first_limit_score || 0);
  const cls = _graphicScoreCls(score);
  const risk = Number(e.proba_break_risk || 0) * 100;
  const cont = Number(e.proba_continuation || 0) * 100;
  const strong = Number(e.proba_strong_3d || 0) * 100;
  const tags = [];
  if (risk <= 35) tags.push('<span class="predict-card-board-tag graphic-tag-lowrisk">低破板风险</span>');
  if (cont >= 55) tags.push('<span class="predict-card-board-tag graphic-tag-watch">次日承接强</span>');
  if (strong >= 45) tags.push('<span class="predict-card-board-tag">3日强势</span>');
  if (risk >= 55) tags.push('<span class="predict-card-board-tag graphic-tag-break">断板风险高</span>');
  return `
    <div class="pool-card graphic-card" data-symbol="${e.symbol}" onclick="openGraphicDetail('${e.symbol}')">
      <div class="card-head">
        <div class="card-name">
          <span class="stock-name">${e.name || e.symbol}</span>
          <span class="stock-code">${e.symbol}</span>
        </div>
        <div class="card-score ${cls}">${fmtNum(score, 1)}分</div>
      </div>
      <div class="predict-card-meta">
        <span class="predict-card-metric">今收 <b>${fmtNum(e.close)}</b></span>
        <span class="predict-card-metric">当日 ${(e.pct_change_today >= 0 ? '+' : '')}${fmtNum(e.pct_change_today, 2)}%</span>
        <span class="predict-card-metric">承接 <b>${fmtNum(cont, 1)}%</b></span>
        <span class="predict-card-metric">3日强势 <b>${fmtNum(strong, 1)}%</b></span>
        <span class="predict-card-metric">断板风险 <b>${fmtNum(risk, 1)}%</b></span>
      </div>
      <div class="predict-card-meta">
        <span class="predict-card-metric">开盘缺口 <b>${fmtNum(e.open_gap_pct, 2)}%</b></span>
        <span class="predict-card-metric">距20日高 <b>${fmtNum(e.distance_to_20d_high, 2)}%</b></span>
        <span class="predict-card-metric">量比20日 <b>${fmtNum(e.volume_ratio_20d, 2)}</b></span>
        <span class="predict-card-metric">涨停质量 <b>${fmtNum(e.limit_quality, 2)}</b></span>
      </div>
      ${tags.length ? `<div class="predict-card-boards">${tags.join('')}</div>` : ''}
    </div>`;
}

function renderGraphicFunnel() {
  const snap = state.graphicFunnel;
  const metaEl = document.getElementById('graphicMeta');
  const progEl = document.getElementById('graphicProgress');
  const btn = document.getElementById('btnGraphicRun');
  const summary = document.getElementById('graphicPageSummary');
  if (!snap) {
    if (metaEl) metaEl.textContent = '无数据';
    if (summary) summary.innerHTML = '';
    return;
  }
  const cfg = snap.config || {};
  state.graphicConfig = cfg;
  const thC = document.getElementById('graphic-th-candidate');
  const thF = document.getElementById('graphic-th-focus');
  const thB = document.getElementById('graphic-th-buy');
  if (thC) thC.textContent = `≥${cfg.threshold_candidate || 6}分`;
  if (thF) thF.textContent = `≥${cfg.threshold_focus || 10}分`;
  if (thB) thB.textContent = `≥${cfg.threshold_buy || 14}分`;

  const pools = snap.pools || { candidate: [], focus: [], buy: [] };
  const renderPool = (id, list) => {
    const el = document.getElementById(id);
    const cnt = document.getElementById(`graphic-count-${id.split('-').pop()}`);
    if (cnt) cnt.textContent = (list || []).length;
    if (!el) return;
    if (!list || list.length === 0) {
      el.innerHTML = `<div class="empty-pool">暂无</div>`;
      return;
    }
    el.innerHTML = list.map(_graphicCardHtml).join('');
  };
  renderPool('graphic-pool-candidate', pools.candidate);
  renderPool('graphic-pool-focus', pools.focus);
  renderPool('graphic-pool-buy', pools.buy);

  const prog = snap.progress || {};
  const running = !!snap.running;
  if (btn) {
    btn.disabled = running;
    btn.textContent = running ? '扫描中…' : '立即扫描';
  }
  if (progEl) {
    if (running) {
      progEl.textContent = `${prog.phase || '...'} ${prog.current || 0}/${prog.total || 0} ${prog.detail || ''}`;
    } else if (prog.error) {
      progEl.textContent = `失败：${prog.error}`;
    } else if (snap.meta && snap.meta.elapsed_sec) {
      progEl.textContent = `上次扫描 ${snap.meta.elapsed_sec}s · 模型 ${snap.meta.model_backend || 'baseline'}`;
    } else {
      progEl.textContent = '';
    }
  }
  if (metaEl) {
    const m = snap.meta || {};
    metaEl.textContent = `交易日 ${snap.trade_date || '--'} · 命中 ${m.entries_count || 0} · ${m.model_backend || 'baseline'} · 特征 ${m.feature_count || 0}`;
  }
  if (summary) {
    summary.innerHTML = [
      _psItem('候选', (pools.candidate || []).length),
      _psSep(),
      _psItem('关注', (pools.focus || []).length),
      _psSep(),
      _psItem('买入', (pools.buy || []).length, 'warning'),
      _psSep(),
      _psItem('模型', snap.meta?.model_backend || 'baseline'),
      _psSep(),
      _psItem('耗时', `${snap.meta?.elapsed_sec || 0}s`),
    ].join('');
  }
}

async function loadGraphicFunnel() {
  try {
    const snap = await request('/api/strategy/first-limit-alpha/graphic');
    state.graphicFunnel = snap;
    state.graphicRunning = !!snap.running;
    renderGraphicFunnel();
    if (state.activeTab === 'graphic') setMeta();
  } catch (err) {
    const el = document.getElementById('graphicMeta');
    if (el) el.textContent = `加载失败: ${err.message}`;
  }
}

function _startGraphicPoll() {
  _stopGraphicPoll();
  const tick = async () => {
    if (state.activeTab !== 'graphic') { _stopGraphicPoll(); return; }
    try { await loadGraphicFunnel(); } catch (_) {}
    const interval = state.graphicRunning ? 2500 : 15000;
    _graphicPollTimer = setTimeout(tick, interval);
  };
  _graphicPollTimer = setTimeout(tick, 1000);
}

function _stopGraphicPoll() {
  if (_graphicPollTimer) { clearTimeout(_graphicPollTimer); _graphicPollTimer = null; }
}

async function openGraphicDetail(symbol) {
  if (!symbol) return;
  state.selectedSymbol = symbol;
  let detail = { name: symbol, kline: [], metrics: {} };
  try {
    const d = await request(`/api/stock/${symbol}/detail?kline_days=30`);
    detail = {
      name: d?.name || symbol,
      kline: d?.kline || [],
      metrics: d?.metrics || {},
    };
  } catch (_) {}
  try {
    await openPredictModal(symbol, detail, {
      titleSuffix: '图形选股详情',
      badge: '附带 Kronos 三日预测',
      badgeClass: 'predict-modal-badge',
    });
  } catch (err) {
    setStatus(`详情加载失败: ${err?.message || err}`, 'error');
  }
}

async function openHotAiDetail(symbol) {
  if (!symbol) return;
  state.selectedSymbol = symbol;
  let detail = { name: symbol, kline: [], metrics: {} };
  try {
    const d = await request(`/api/stock/${symbol}/detail?kline_days=30`);
    detail = {
      name: d?.name || symbol,
      kline: d?.kline || [],
      metrics: d?.metrics || {},
    };
  } catch (_) {}
  try {
    await openPredictModal(symbol, detail, {
      titleSuffix: '热门股票智能分析',
      badge: 'Kronos 预测 + TradingAgents 讨论',
      badgeClass: 'predict-modal-badge',
    });
  } catch (err) {
    setStatus(`详情加载失败: ${err?.message || err}`, 'error');
  }
}

async function openPredictDetail(symbol, name) {
  if (!symbol) return;
  state.selectedSymbol = symbol;
  let detail = { name: name || symbol, kline: [], metrics: {} };
  try {
    const d = await request(`/api/stock/${symbol}/detail?kline_days=30`);
    detail = {
      name: d?.name || name || symbol,
      kline: d?.kline || [],
      metrics: d?.metrics || {},
    };
  } catch (_) { /* fallback 到基础信息 */ }
  try {
    if (typeof openPredictModal === 'function') {
      await openPredictModal(symbol, detail);
    }
  } catch (err) {
    setStatus(`预测加载失败: ${err?.message || err}`, 'error');
  }
}

function _bindPredictActions() {
  const btn = document.getElementById('btnPredictRun');
  if (btn) {
    btn.onclick = async () => {
      if (state.predictRunning) return;
      try {
        setStatus('预测任务已启动，预计 3-5 分钟完成…', 'info');
        await request('/api/predict-funnel/trigger', { method: 'POST' });
        state.predictRunning = true;
        _startPredictPoll();
      } catch (err) {
        setStatus(`启动失败: ${err.message}`, 'error');
      }
    };
  }
  const tog = document.getElementById('predictFeishuToggle');
  if (tog) {
    tog.onchange = async () => {
      try {
        await request('/api/predict-funnel/config', {
          method: 'POST',
          body: JSON.stringify({ feishu_enabled: tog.checked }),
          headers: { 'Content-Type': 'application/json' },
        });
        setStatus(`飞书推送已${tog.checked ? '开启' : '关闭'}`, 'success');
      } catch (err) {
        setStatus(`设置失败: ${err.message}`, 'error');
        tog.checked = !tog.checked;
      }
    };
  }
}

function _bindHotAiActions() {
  const btn = document.getElementById('btnHotAiRun');
  if (btn) {
    btn.onclick = async () => {
      if (state.hotStockAIRunning) return;
      try {
        setStatus('热门股票智能分析已启动，正在逐股打分…', 'info');
        state.hotStockAIRunning = true;
        renderHotStockAI();
        await request('/api/strategy/hot-stock-ai/run', { method: 'POST' });
        _startHotAiPoll();
      } catch (err) {
        state.hotStockAIRunning = false;
        renderHotStockAI();
        setStatus(`启动失败: ${err.message}`, 'error');
      }
    };
  }
  const toggle = document.getElementById('hotAiAutoToggle');
  if (toggle) {
    toggle.onchange = async () => {
      try {
        await request('/api/strategy/hot-stock-ai/config', {
          method: 'POST',
          body: JSON.stringify({ auto_refresh_enabled: toggle.checked }),
          headers: { 'Content-Type': 'application/json' },
        });
        if (state.hotStockAI?.config) state.hotStockAI.config.auto_refresh_enabled = toggle.checked;
        setStatus(`热门智能自动刷新已${toggle.checked ? '开启' : '关闭'}`, 'success');
      } catch (err) {
        toggle.checked = !toggle.checked;
        setStatus(`设置失败: ${err.message}`, 'error');
      }
    };
  }
}

function _bindGraphicActions() {
  const btn = document.getElementById('btnGraphicRun');
  if (!btn) return;
  btn.onclick = async () => {
    if (state.graphicRunning) return;
    try {
      setStatus('图形选股扫描已启动，正在读取首板候选并打分…', 'info');
      state.graphicRunning = true;
      renderGraphicFunnel();
      await request('/api/strategy/first-limit-alpha/graphic/run', { method: 'POST' });
      _startGraphicPoll();
    } catch (err) {
      state.graphicRunning = false;
      renderGraphicFunnel();
      setStatus(`启动失败: ${err.message}`, 'error');
    }
  };
}

/* ==================== 自定义策略中心 ==================== */

const strategyCenterState = {
  rules: [],          // [{code,title,category,description,params:[...]}]
  strategies: [],     // [{id,name,...,rules:[{rule_code,enabled,params}]}]
  currentId: null,
  editing: null,      // 当前正在编辑的策略（本地 draft）
  scActionsBound: false,
  scanSnapshot: null,
};

async function loadStrategyCenter() {
  try {
    if (!strategyCenterState.rules.length) {
      const r = await request('/api/strategy/rules');
      strategyCenterState.rules = r.rules || [];
    }
    const lst = await request('/api/strategy/custom');
    strategyCenterState.strategies = lst.items || [];
    if (!strategyCenterState.currentId) {
      strategyCenterState.currentId = lst.default_id
        || (strategyCenterState.strategies[0] && strategyCenterState.strategies[0].id)
        || null;
    }
    _renderStrategySelect();
    _loadEditingStrategy();
    _renderRulesList();
    _renderStrategyMeta();
    await _loadScanSnapshot();
  } catch (err) {
    setStatus(`策略中心加载失败: ${err.message}`, 'error');
  }
}

function _bindStrategyCenterActions() {
  if (strategyCenterState.scActionsBound) return;
  strategyCenterState.scActionsBound = true;

  const $ = (id) => document.getElementById(id);

  $('scStrategySelect').onchange = async (ev) => {
    strategyCenterState.currentId = ev.target.value;
    _loadEditingStrategy();
    _renderRulesList();
    _renderStrategyMeta();
    await _loadScanSnapshot();
  };

  $('btnScNew').onclick = () => {
    strategyCenterState.currentId = null;
    strategyCenterState.editing = {
      id: null,
      name: '新策略',
      description: '',
      is_builtin: false,
      is_default: false,
      rules: strategyCenterState.rules.slice(0, 3).map((r) => ({
        rule_code: r.code,
        enabled: false,
        params: _defaultParams(r),
      })),
    };
    _renderStrategySelect();
    _renderRulesList();
    _renderStrategyMeta();
  };

  $('btnScClone').onclick = () => {
    const src = strategyCenterState.editing;
    if (!src) return;
    strategyCenterState.currentId = null;
    strategyCenterState.editing = JSON.parse(JSON.stringify({
      ...src,
      id: null,
      name: `${src.name} · 副本`,
      is_builtin: false,
      is_default: false,
    }));
    _renderStrategySelect();
    _renderRulesList();
    _renderStrategyMeta();
    setStatus('已克隆策略，可继续编辑后保存', 'success');
  };

  $('btnScSave').onclick = async () => {
    const draft = strategyCenterState.editing;
    if (!draft) return;
    draft.name = $('scStrategyName').value.trim() || draft.name;
    draft.description = $('scStrategyDesc').value.trim();
    if (!draft.name) { setStatus('请填写策略名称', 'error'); return; }
    const body = {
      id: draft.id || undefined,
      name: draft.name,
      description: draft.description,
      rules: draft.rules.filter((r) => r.rule_code),
    };
    try {
      const r = await request('/api/strategy/custom', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      setStatus('策略已保存', 'success');
      strategyCenterState.currentId = r.strategy.id;
      await loadStrategyCenter();
    } catch (err) {
      setStatus(`保存失败: ${err.message}`, 'error');
    }
  };

  $('btnScDelete').onclick = async () => {
    const draft = strategyCenterState.editing;
    if (!draft || !draft.id) { setStatus('未保存的新策略，无需删除', 'error'); return; }
    if (draft.is_builtin) { setStatus('内置策略不可删除', 'error'); return; }
    if (!confirm(`确定删除策略「${draft.name}」？`)) return;
    try {
      await request(`/api/strategy/custom/${draft.id}`, { method: 'DELETE' });
      strategyCenterState.currentId = null;
      setStatus('已删除', 'success');
      await loadStrategyCenter();
    } catch (err) {
      setStatus(`删除失败: ${err.message}`, 'error');
    }
  };

  $('btnScSetDefault').onclick = async () => {
    const draft = strategyCenterState.editing;
    if (!draft || !draft.id) { setStatus('请先保存策略', 'error'); return; }
    try {
      await request(`/api/strategy/custom/${draft.id}/default`, { method: 'POST' });
      setStatus(`已设为默认策略`, 'success');
      await loadStrategyCenter();
    } catch (err) {
      setStatus(`设置失败: ${err.message}`, 'error');
    }
  };

  $('btnScScan').onclick = async () => {
    const draft = strategyCenterState.editing;
    if (!draft || !draft.id) { setStatus('请先保存策略后再扫描', 'error'); return; }
    const btn = $('btnScScan');
    const meta = $('scScanMeta');
    btn.disabled = true;
    btn.textContent = '扫描中…';
    if (meta) meta.textContent = '扫描中，约 30~60 秒（全 A 股）…';
    try {
      const snap = await request(`/api/strategy/custom/${draft.id}/scan`, { method: 'POST' });
      _renderScanSnapshot(snap);
      setStatus(`扫描完成：命中 ${snap.total_hits} 只`, 'success');
    } catch (err) {
      setStatus(`扫描失败: ${err.message}`, 'error');
      if (meta) meta.textContent = `扫描失败: ${err.message}`;
    } finally {
      btn.disabled = false;
      btn.textContent = '立即扫描';
    }
  };

  $('btnScBacktest').onclick = async () => {
    const draft = strategyCenterState.editing;
    if (!draft || !draft.id) { setStatus('请先保存策略后再回测', 'error'); return; }
    const btn = $('btnScBacktest');
    btn.disabled = true;
    btn.textContent = '回测中…';
    try {
      const r = await request(`/api/strategy/custom/${draft.id}/backtest?history_days=180&hold_days=3&tp_pct=8&sl_pct=-5`, { method: 'POST' });
      _renderBacktestResult(r);
      setStatus(`回测完成：信号 ${r.total_signals} · 胜率 ${r.hit_rate}%`, 'success');
    } catch (err) {
      setStatus(`回测失败: ${err.message}`, 'error');
    } finally {
      btn.disabled = false;
      btn.textContent = '回测 180 天';
    }
  };
}

function _defaultParams(ruleSpec) {
  const out = {};
  (ruleSpec.params || []).forEach((p) => { out[p.key] = p.default; });
  return out;
}

function _renderStrategySelect() {
  const sel = document.getElementById('scStrategySelect');
  if (!sel) return;
  const items = strategyCenterState.strategies;
  const cur = strategyCenterState.currentId;
  sel.innerHTML = '';
  items.forEach((s) => {
    const opt = document.createElement('option');
    opt.value = s.id;
    const badges = [];
    if (s.is_builtin) badges.push('内置');
    if (s.is_default) badges.push('默认');
    opt.textContent = badges.length ? `${s.name} · ${badges.join('/')}` : s.name;
    if (s.id === cur) opt.selected = true;
    sel.appendChild(opt);
  });
  if (!cur) {
    const opt = document.createElement('option');
    opt.value = '';
    opt.textContent = '-- 未保存的新策略 --';
    opt.selected = true;
    sel.appendChild(opt);
  }
}

function _loadEditingStrategy() {
  const cur = strategyCenterState.currentId;
  if (!cur) {
    if (!strategyCenterState.editing) return;
    return;
  }
  const src = strategyCenterState.strategies.find((x) => x.id === cur);
  if (!src) return;
  strategyCenterState.editing = JSON.parse(JSON.stringify(src));
  const existingCodes = new Set((strategyCenterState.editing.rules || []).map((r) => r.rule_code));
  strategyCenterState.rules.forEach((spec) => {
    if (!existingCodes.has(spec.code)) {
      strategyCenterState.editing.rules.push({
        rule_code: spec.code,
        enabled: false,
        params: _defaultParams(spec),
      });
    }
  });
}

function _renderStrategyMeta() {
  const draft = strategyCenterState.editing;
  const $ = (id) => document.getElementById(id);
  const badges = $('scStrategyBadges');
  const nameInput = $('scStrategyName');
  const descInput = $('scStrategyDesc');
  const btnDelete = $('btnScDelete');
  const btnSetDefault = $('btnScSetDefault');
  if (!badges || !nameInput) return;
  if (!draft) {
    nameInput.value = '';
    descInput.value = '';
    badges.innerHTML = '';
    return;
  }
  nameInput.value = draft.name || '';
  descInput.value = draft.description || '';
  const parts = [];
  if (draft.is_builtin) parts.push('<span class="sc-chip builtin">内置</span>');
  if (draft.is_default) parts.push('<span class="sc-chip default">默认</span>');
  if (!draft.id) parts.push('<span class="sc-chip new">未保存</span>');
  badges.innerHTML = parts.join('');
  if (btnDelete) btnDelete.disabled = !!(draft.is_builtin || !draft.id);
  if (btnSetDefault) btnSetDefault.disabled = !draft.id || draft.is_default;
}

function _renderRulesList() {
  const root = document.getElementById('scRulesList');
  if (!root) return;
  const draft = strategyCenterState.editing;
  if (!draft) { root.innerHTML = '<div class="sc-empty">请先选择或新建策略</div>'; return; }
  const specsMap = {};
  strategyCenterState.rules.forEach((s) => { specsMap[s.code] = s; });

  const byCat = {};
  strategyCenterState.rules.forEach((spec) => {
    if (!byCat[spec.category]) byCat[spec.category] = [];
    byCat[spec.category].push(spec);
  });

  const catOrder = ['filter', 'price', 'volume', 'pattern', 'trend'];
  const catTitles = { filter: '过滤条件', price: '价格', volume: '量能', pattern: '形态', trend: '趋势' };

  const html = catOrder.filter((c) => byCat[c]).map((cat) => {
    const cards = byCat[cat].map((spec) => {
      const ref = draft.rules.find((r) => r.rule_code === spec.code) || {
        rule_code: spec.code, enabled: false, params: _defaultParams(spec),
      };
      const paramsHtml = (spec.params || []).map((p) => {
        const val = ref.params[p.key] ?? p.default;
        if (p.type === 'bool') {
          return `<label class="sc-param sc-param-bool">
            <input type="checkbox" data-code="${spec.code}" data-key="${p.key}" data-type="bool" ${val ? 'checked' : ''} />
            ${esc(p.label)}
          </label>`;
        }
        if (p.type === 'text') {
          return `<label class="sc-param">
            <span class="sc-param-label">${esc(p.label)}</span>
            <input type="text" class="sc-input sc-param-input" data-code="${spec.code}" data-key="${p.key}" data-type="text" value="${esc(String(val))}" />
          </label>`;
        }
        return `<label class="sc-param">
          <span class="sc-param-label">${esc(p.label)}</span>
          <input type="number" class="sc-input sc-param-input" data-code="${spec.code}" data-key="${p.key}" data-type="${p.type}"
            ${p.min !== null && p.min !== undefined ? `min="${p.min}"` : ''}
            ${p.max !== null && p.max !== undefined ? `max="${p.max}"` : ''}
            ${p.step ? `step="${p.step}"` : 'step="any"'}
            value="${Number(val)}" />
        </label>`;
      }).join('');
      return `
        <div class="sc-rule-card ${ref.enabled ? 'enabled' : ''}" data-code="${spec.code}">
          <div class="sc-rule-head">
            <label class="sc-rule-toggle">
              <input type="checkbox" class="sc-rule-enabled" data-code="${spec.code}" ${ref.enabled ? 'checked' : ''} />
              <span class="sc-rule-title">${esc(spec.title)}</span>
            </label>
            <span class="sc-rule-cat">${esc(catTitles[cat] || cat)}</span>
          </div>
          <div class="sc-rule-desc">${esc(spec.description || '')}</div>
          <div class="sc-rule-params">${paramsHtml || '<span class="muted">此规则无参数</span>'}</div>
        </div>
      `;
    }).join('');
    return `
      <div class="sc-rule-group">
        <div class="sc-rule-group-title">${esc(catTitles[cat] || cat)}</div>
        <div class="sc-rule-group-body">${cards}</div>
      </div>`;
  }).join('');

  root.innerHTML = html;

  root.querySelectorAll('.sc-rule-enabled').forEach((cb) => {
    cb.onchange = () => {
      const code = cb.getAttribute('data-code');
      const ref = _ensureRuleRef(code);
      if (!ref) return;
      ref.enabled = cb.checked;
      const card = root.querySelector(`.sc-rule-card[data-code="${code}"]`);
      if (card) card.classList.toggle('enabled', cb.checked);
    };
  });
  root.querySelectorAll('.sc-param-input, .sc-param-bool input').forEach((el) => {
    el.onchange = () => {
      const code = el.getAttribute('data-code');
      const key = el.getAttribute('data-key');
      const type = el.getAttribute('data-type');
      const ref = _ensureRuleRef(code);
      if (!ref) return;
      let val = el.value;
      if (type === 'bool') val = el.checked;
      else if (type === 'int') val = parseInt(el.value, 10) || 0;
      else if (type === 'float' || type === 'pct') val = parseFloat(el.value) || 0;
      ref.params[key] = val;
    };
  });
}

function _ensureRuleRef(code) {
  const draft = strategyCenterState.editing;
  if (!draft) return null;
  let ref = draft.rules.find((r) => r.rule_code === code);
  if (!ref) {
    const spec = strategyCenterState.rules.find((s) => s.code === code);
    if (!spec) return null;
    ref = { rule_code: code, enabled: false, params: _defaultParams(spec) };
    draft.rules.push(ref);
  }
  return ref;
}

async function _loadScanSnapshot() {
  const draft = strategyCenterState.editing;
  if (!draft || !draft.id) { _renderScanSnapshot(null); return; }
  try {
    const snap = await request(`/api/strategy/custom/${draft.id}/scan`);
    _renderScanSnapshot(snap);
  } catch {
    _renderScanSnapshot(null);
  }
}

function _renderScanSnapshot(snap) {
  const meta = document.getElementById('scScanMeta');
  if (!meta) return;
  strategyCenterState.scanSnapshot = snap && snap.generated_at ? snap : null;
  state.strategyScanSnapshot = strategyCenterState.scanSnapshot;
  if (state.activeTab === 'funnel') renderFunnel();
  if (!snap || !snap.generated_at) {
    meta.textContent = '尚未扫描，点击"立即扫描"开始（覆盖全 A 股，约 30~60 秒）';
    return;
  }
  const hits = snap.hits || [];
  meta.textContent = `扫描时间 ${(snap.generated_at || '').replace('T', ' ')} · 扫描 ${snap.total_scanned} 只 · 命中 ${snap.total_hits} 只 · 耗时 ${snap.elapsed_seconds}s · 启用规则 ${snap.rules_count}`;
  if (!hits.length) meta.textContent += ' · 当前无命中';
}

function _renderBacktestResult(r) {
  const root = document.getElementById('scBacktestCard');
  if (!root) return;
  if (!r || r.total_signals === undefined) {
    root.style.display = 'none';
    return;
  }
  root.style.display = 'block';
  const pnlCls = Number(r.total_pnl_pct || 0) >= 0 ? 'up' : 'down';
  const mddCls = Number(r.max_drawdown_pct || 0) < 0 ? 'down' : 'muted';
  const params = r.params || {};
  const sample = (r.samples || []).slice(0, 10).map((s) => {
    const cls = Number(s.pnl_pct || 0) >= 0 ? 'up' : 'down';
    return `<tr><td>${esc(s.symbol)}</td><td>${esc(s.name || '')}</td><td>${esc(s.signal_date)}</td><td>${esc(String(s.hold_days))}</td><td class="${cls}">${s.pnl_pct >= 0 ? '+' : ''}${fmtNum(s.pnl_pct, 2)}%</td><td>${esc(s.reason)}</td></tr>`;
  }).join('');
  root.innerHTML = `
    <div class="sc-bt-header">
      <h4>回测结果 · ${esc(params.strategy_name || '')}</h4>
      <span class="sc-bt-meta">${esc(r.generated_at || '')} · ${r.elapsed_seconds}s · 规则 ${params.rules_count || '-'} 条 · 持有 ${params.hold_days}d / tp ${params.tp_pct}% / sl ${params.sl_pct}%</span>
    </div>
    <div class="sc-bt-stats">
      <div class="sc-bt-stat"><span class="sc-bt-label">总信号</span><span class="sc-bt-value">${r.total_signals}</span></div>
      <div class="sc-bt-stat"><span class="sc-bt-label">胜 / 负</span><span class="sc-bt-value">${r.wins} / ${r.losses}</span></div>
      <div class="sc-bt-stat"><span class="sc-bt-label">胜率</span><span class="sc-bt-value">${fmtNum(r.hit_rate, 2)}%</span></div>
      <div class="sc-bt-stat"><span class="sc-bt-label">累计收益</span><span class="sc-bt-value ${pnlCls}">${r.total_pnl_pct >= 0 ? '+' : ''}${fmtNum(r.total_pnl_pct, 2)}%</span></div>
      <div class="sc-bt-stat"><span class="sc-bt-label">最大回撤</span><span class="sc-bt-value ${mddCls}">${fmtNum(r.max_drawdown_pct, 2)}%</span></div>
      <div class="sc-bt-stat"><span class="sc-bt-label">平均持有</span><span class="sc-bt-value">${fmtNum(r.avg_hold_days, 1)}d</span></div>
    </div>
    ${sample ? `<details class="sc-bt-samples"><summary>展开样本 Top 10</summary>
      <table class="sc-bt-table">
        <thead><tr><th>代码</th><th>名称</th><th>信号日</th><th>持有</th><th>收益</th><th>出场原因</th></tr></thead>
        <tbody>${sample}</tbody>
      </table>
    </details>` : ''}
  `;
}

/* ==================== Hermes AI 能力面板 ==================== */

async function loadAiPanel() {
  _bindAiPanelActions();
  try {
    const risk = await request('/api/hermes-ai/risk');
    _renderRiskSnapshot(risk);
  } catch {}
  try {
    const auto = await request('/api/hermes-ai/auto-trade');
    _renderAutoSnapshot(auto);
  } catch {}
  try {
    const bt = await request('/api/hermes-ai/backtest');
    if (bt && bt.total_signals !== undefined) _renderBacktest(bt);
  } catch {}
}

let _aiActionsBound = false;
function _bindAiPanelActions() {
  if (_aiActionsBound) return;
  _aiActionsBound = true;

  const bySym = (id) => document.getElementById(id);

  // Risk
  bySym('btnRiskSave').onclick = async () => {
    const body = {
      enabled: bySym('riskEnabled').checked,
      auto_close: bySym('riskAutoClose').checked,
      tp_pct: Number(bySym('riskTp').value),
      sl_pct: Number(bySym('riskSl').value),
    };
    try {
      const r = await request('/api/hermes-ai/risk/config', { method: 'POST', body: JSON.stringify(body), headers: { 'Content-Type': 'application/json' } });
      _renderRiskSnapshot(r.snapshot);
      setStatus('风险守门人配置已保存', 'success');
    } catch (e) { setStatus(`保存失败: ${e.message}`, 'error'); }
  };
  bySym('btnRiskTick').onclick = async () => {
    try {
      const r = await request('/api/hermes-ai/risk/tick', { method: 'POST' });
      _renderRiskSnapshot(r.snapshot);
      setStatus(`风险扫描完成：检查 ${r.result.checked || 0} 只持仓，触发 ${r.result.triggered || 0} 次告警`, 'success');
    } catch (e) { setStatus(`扫描失败: ${e.message}`, 'error'); }
  };

  // Auto-trade
  bySym('btnAutoSave').onclick = async () => {
    const body = {
      enabled: bySym('autoEnabled').checked,
      dry_run: bySym('autoDry').checked,
      max_positions: Number(bySym('autoMaxPos').value),
      position_size: Number(bySym('autoSize').value),
    };
    try {
      const r = await request('/api/hermes-ai/auto-trade/config', { method: 'POST', body: JSON.stringify(body), headers: { 'Content-Type': 'application/json' } });
      _renderAutoSnapshot(r.snapshot);
      setStatus('自动交易配置已保存', 'success');
    } catch (e) { setStatus(`保存失败: ${e.message}`, 'error'); }
  };
  bySym('btnAutoTick').onclick = async () => {
    try {
      const r = await request('/api/hermes-ai/auto-trade/tick', { method: 'POST' });
      _renderAutoSnapshot(r.snapshot);
      setStatus(`自动交易执行：买 ${r.result.buys || 0} 卖 ${r.result.sells || 0} 跳过 ${r.result.skipped || 0}`, 'success');
    } catch (e) { setStatus(`执行失败: ${e.message}`, 'error'); }
  };

  // Backtest
  bySym('btnBacktestRun').onclick = async () => {
    const params = new URLSearchParams({
      hold_days: String(bySym('btHold').value || 3),
      tp_pct: String(bySym('btTp').value || 8),
      sl_pct: String(bySym('btSl').value || -5),
      require_limit_up: String(bySym('btLimitUp').checked),
    });
    bySym('btnBacktestRun').disabled = true;
    bySym('btnBacktestRun').textContent = '回测中…（~90 秒）';
    try {
      const r = await request(`/api/hermes-ai/backtest/run?${params}`, { method: 'POST' });
      _renderBacktest(r);
      setStatus(`回测完成：${r.total_signals} 次信号 / 胜率 ${r.hit_rate}% / 累计 ${r.total_pnl_pct}%`, 'success');
    } catch (e) { setStatus(`回测失败: ${e.message}`, 'error'); }
    finally {
      bySym('btnBacktestRun').disabled = false;
      bySym('btnBacktestRun').textContent = '开始回测（~90秒）';
    }
  };

  // News insight
  bySym('btnNewsRun').onclick = async () => {
    bySym('newsInsightBox').textContent = '分析中…';
    try {
      const r = await request('/api/hermes-ai/news-insight/run', { method: 'POST' });
      _renderNewsInsight(r);
    } catch (e) {
      bySym('newsInsightBox').textContent = `失败: ${e.message}`;
    }
  };

  // Research
  bySym('btnResearchRun').onclick = async () => {
    const sym = (bySym('researchSym').value || '').trim();
    const nm = (bySym('researchName').value || '').trim();
    if (!sym) { setStatus('请填入股票代码', 'error'); return; }
    bySym('researchCardBox').textContent = '生成中…';
    try {
      const r = await request(`/api/hermes-ai/research/${sym}?name=${encodeURIComponent(nm)}`, { method: 'POST' });
      _renderResearch(r);
    } catch (e) {
      bySym('researchCardBox').textContent = `失败: ${e.message}`;
    }
  };

  // Weekly
  bySym('btnWeeklyRun').onclick = async () => {
    bySym('weeklyReportBox').textContent = '生成中…';
    try {
      const r = await request('/api/hermes-ai/weekly-report/run', { method: 'POST' });
      _renderWeekly(r);
    } catch (e) {
      bySym('weeklyReportBox').textContent = `失败: ${e.message}`;
    }
  };
}

function _renderRiskSnapshot(snap) {
  if (!snap) return;
  const cfg = snap.rule || {};
  const el = document.getElementById('riskEnabled'); if (el) el.checked = !!snap.enabled;
  const ac = document.getElementById('riskAutoClose'); if (ac) ac.checked = !!snap.auto_close;
  const tp = document.getElementById('riskTp'); if (tp) tp.value = cfg.tp_pct ?? 8;
  const sl = document.getElementById('riskSl'); if (sl) sl.value = cfg.sl_pct ?? -5;
  const box = document.getElementById('riskAlerts');
  if (!box) return;
  const alerts = (snap.alerts || []).slice().reverse();
  if (!alerts.length) {
    box.innerHTML = '<div class="ai-empty">暂无告警</div>';
    return;
  }
  box.innerHTML = alerts.map(a => {
    const cls = a.kind === 'take_profit' ? 'tp' : (a.kind === 'stop_loss' ? 'sl' : 'surge_pullback');
    const closed = a.auto_closed ? `<b style="color:#10b981"> · 已自动平仓</b>` : (a.close_note ? ` · ${esc(a.close_note)}` : '');
    return `<div class="ai-alert-item ${cls}"><b>${esc(a.symbol)} ${esc(a.name || '')}</b> · ${a.kind} · ${a.pnl_pct}% · ${esc(a.details)}${closed}<br><small>${esc(a.at)}</small></div>`;
  }).join('');
}

function _renderAutoSnapshot(snap) {
  if (!snap) return;
  const cfg = snap.config || {};
  const set = (id, v) => { const el = document.getElementById(id); if (el) { if (el.type === 'checkbox') el.checked = !!v; else el.value = v; } };
  set('autoEnabled', cfg.enabled);
  set('autoDry', cfg.dry_run);
  set('autoMaxPos', cfg.max_positions);
  set('autoSize', cfg.position_size);

  const box = document.getElementById('autoActions');
  if (!box) return;
  const actions = (snap.recent_actions || []).slice().reverse();
  if (!actions.length) {
    box.innerHTML = '<div class="ai-empty">暂无动作</div>';
    return;
  }
  box.innerHTML = actions.map(a => {
    const tag = a.type === 'buy' ? '买入' : '卖出';
    const dry = a.dry_run ? ' [DRY]' : '';
    return `<div class="ai-alert-item ${a.type === 'buy' ? 'tp' : 'sl'}"><b>${tag}${dry} ${esc(a.symbol)} ${esc(a.name || '')}</b> @ ${a.price} · ${esc(a.reason || '')}${a.pnl_pct !== undefined ? ` · ${a.pnl_pct}%` : ''}<br><small>${esc(a.at)} · ${esc(a.note || '')}</small></div>`;
  }).join('');
}

function _renderBacktest(r) {
  const statsEl = document.getElementById('backtestStats');
  const samplesEl = document.getElementById('backtestSamples');
  if (!statsEl || !samplesEl) return;
  if (!r || r.total_signals === undefined) {
    statsEl.innerHTML = '<div class="ai-empty">尚未执行回测</div>';
    samplesEl.innerHTML = '';
    return;
  }
  const pnlCls = r.total_pnl_pct >= 0 ? 'pos' : 'neg';
  const hrCls = r.hit_rate >= 50 ? 'pos' : 'neg';
  statsEl.innerHTML = `
    <div class="bt-stat"><div class="bt-stat-label">信号总数</div><div class="bt-stat-value">${r.total_signals}</div></div>
    <div class="bt-stat"><div class="bt-stat-label">胜 / 负</div><div class="bt-stat-value">${r.wins}/${r.losses}</div></div>
    <div class="bt-stat"><div class="bt-stat-label">胜率</div><div class="bt-stat-value ${hrCls}">${r.hit_rate}%</div></div>
    <div class="bt-stat"><div class="bt-stat-label">累计收益</div><div class="bt-stat-value ${pnlCls}">${r.total_pnl_pct}%</div></div>
    <div class="bt-stat"><div class="bt-stat-label">最大回撤</div><div class="bt-stat-value neg">${r.max_drawdown_pct}%</div></div>
    <div class="bt-stat"><div class="bt-stat-label">平均持仓</div><div class="bt-stat-value">${r.avg_hold_days}天</div></div>
  `;
  const rows = (r.samples || []).slice(0, 30).map(s => {
    const cls = s.pnl_pct >= 0 ? 'pos' : 'neg';
    return `<tr><td>${esc(s.symbol)}</td><td>${esc(s.name || '')}</td><td>${esc(s.signal_date)}</td><td>→ ${esc(s.exit_date)}</td><td>${s.hold_days}天</td><td class="${cls}">${s.pnl_pct >= 0 ? '+' : ''}${s.pnl_pct}%</td><td>${esc(s.reason)}</td></tr>`;
  }).join('');
  samplesEl.innerHTML = `<table><thead><tr><th>代码</th><th>名称</th><th>信号日</th><th>退出日</th><th>持仓</th><th>收益</th><th>原因</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function _renderNewsInsight(r) {
  const box = document.getElementById('newsInsightBox');
  if (!box) return;
  const llm = r.llm || {};
  const insights = llm.insights || [];
  let s = `【市场情绪】${llm.overall_mood || '-'}\n【概要】${llm.summary || '-'}\n\n`;
  if (insights.length) {
    s += insights.map((i, idx) => {
      const syms = (i.symbols || []).join(', ') || '-';
      return `${idx + 1}. ${i.message}\n   标的: ${syms} | 操作: ${i.operation} | 置信度: ${i.confidence}\n   理由: ${i.reason}`;
    }).join('\n\n');
  } else {
    s += '（LLM 未返回 insights）';
  }
  box.textContent = s;
}

function _renderResearch(r) {
  const box = document.getElementById('researchCardBox');
  if (!box) return;
  const llm = r.llm || {};
  const kr = r.kronos || {};
  const kln = r.kline || {};
  let s = `【${r.name || ''}(${r.symbol})】\n`;
  s += `建议: ${llm.verdict || '-'} | 置信度: ${llm.confidence || '-'}\n`;
  s += `操作: ${llm.action || '-'}\n\n`;
  s += `评述: ${llm.summary || '-'}\n\n`;
  if (llm.bullish_points?.length) s += `利好:\n${llm.bullish_points.map(x => `  • ${x}`).join('\n')}\n`;
  if (llm.bearish_points?.length) s += `\n风险:\n${llm.bearish_points.map(x => `  • ${x}`).join('\n')}\n`;
  s += `\n【数据层】\n`;
  s += `K线: 现价 ${kln.latest_close || '-'} · 期间涨幅 ${kln.period_return_pct !== undefined ? kln.period_return_pct.toFixed(2) + '%' : '-'}\n`;
  s += `Kronos 预测 3 日最高: ${kr.max_high || '-'} · 均收盘: ${kr.avg_close !== undefined ? kr.avg_close.toFixed(2) : '-'}\n`;
  s += `概念: ${(r.concepts || []).join(', ') || '-'}\n`;
  s += `近期公告: ${(r.notices || []).length} 条\n`;
  box.textContent = s;
}

function _renderWeekly(r) {
  const box = document.getElementById('weeklyReportBox');
  if (!box) return;
  const llm = r.llm || {};
  let s = `《${llm.headline || 'Alpha 周报'}》\n`;
  s += `${r.week_start} ~ ${r.week_end}\n\n`;
  s += `【市场】${llm.market_overview || '-'}\n\n`;
  s += `【系统】${llm.system_performance || '-'}\n\n`;
  if (llm.next_week_focus?.length) s += `【下周关注】\n${llm.next_week_focus.map(x => `  • ${x}`).join('\n')}\n\n`;
  if (llm.risk_alerts?.length) s += `【风险】\n${llm.risk_alerts.map(x => `  ⚠ ${x}`).join('\n')}\n`;
  s += `\n热门概念: ${(r.hot_concepts || []).slice(0,5).map(c => `${c.name}(${c.change_pct}%)`).join(', ')}`;
  if (r.feishu_push_error) s += `\n\n⚠ 飞书推送失败: ${r.feishu_push_error}`;
  box.textContent = s;
}

init().catch((err) => {
  const meta = document.getElementById('meta');
  if (meta) meta.textContent = `初始化失败: ${err.message}`;
  renderChartPlaceholder(`初始化失败: ${err.message}`);
});
