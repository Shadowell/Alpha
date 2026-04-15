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
};

function fmtNum(v, digits = 2) {
  const n = Number(v || 0);
  return Number.isFinite(n) ? n.toFixed(digits) : '0.00';
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
    meta.textContent = `交易日 ${state.funnel.trade_date} · 更新 ${state.funnel.updated_at}`;
  } else if (state.activeTab === 'data') {
    meta.textContent = '数据同步 · 完整性检查 · 任务管理';
  } else if (state.activeTab === 'funnel') {
    if (!state.funnel) { meta.textContent = '暂无数据'; return; }
    meta.textContent = `交易日 ${state.funnel.trade_date} · 更新 ${state.funnel.updated_at}`;
  } else if (state.activeTab === 'notice') {
    if (!state.noticeFunnel) { meta.textContent = '暂无数据'; return; }
    const nf = state.noticeFunnel;
    meta.textContent = `公告日 ${nf.trade_date} · 更新 ${nf.updated_at} · 打分源 ${nf.source}`;
  } else if (state.activeTab === 'agent') {
    meta.textContent = 'Hermes 投研代理 — 观察系统表现、产出优化提案、人工审批执行';
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

function renderDataSummary() {
  const el = document.getElementById('dataSummary');
  if (!el) return;
  const stats = state.dcStats || {};
  const report = state.dcReport;
  const syncSt = state.syncStatus || {};
  const cov = report?.coverage_pct;
  const covCls = cov != null ? (cov >= 99 ? 'success' : (cov >= 90 ? 'warning' : 'error')) : '';
  el.innerHTML = [
    _psItem('股票数', (stats.symbol_count || 0).toLocaleString(), 'brand'),
    _psSep(),
    _psItem('最近同步', syncSt.last_success_trade_date || '--', 'brand'),
    _psSep(),
    _psItem('数据覆盖率', cov != null ? cov.toFixed(1) + '%' : '--', covCls),
    _psSep(),
    _psItem('状态', { idle: '空闲', running: '同步中', success: '已完成', failed: '失败' }[syncSt.status] || '--',
      syncSt.status === 'running' ? 'warning' : (syncSt.status === 'success' ? 'success' : '')),
  ].join('');
}

function renderFunnelSummary() {
  const el = document.getElementById('funnelSummary');
  if (!el) return;
  if (!state.funnel) { el.innerHTML = ''; return; }
  const c = (state.funnel.pools.candidate || []).filter(passConceptFilter).length;
  const f = (state.funnel.pools.focus || []).filter(passConceptFilter).length;
  const b = (state.funnel.pools.buy || []).filter(passConceptFilter).length;
  el.innerHTML = [
    _psItem('候选', c + '只'),
    _psSep(),
    _psItem('重点关注', f + '只', 'warning'),
    _psSep(),
    _psItem('买入池', b + '只', 'success'),
    _psSep(),
    _psItem('更新', state.funnel.updated_at || '--'),
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

function renderAgentSummary() {
  const el = document.getElementById('agentSummary');
  if (!el) return;
  const dot = document.getElementById('agentStatusDot');
  const isRunning = dot && dot.classList.contains('running');
  const pendingCount = document.getElementById('agentPendingCount')?.textContent || '0';
  el.innerHTML = [
    _psItem('状态', isRunning ? '运行中' : '空闲', isRunning ? 'warning' : 'success'),
    _psSep(),
    _psItem('待审批', pendingCount + '条', Number(pendingCount) > 0 ? 'warning' : ''),
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
    let message = '';
    try {
      const payload = await resp.json();
      message = payload.detail || payload.message || JSON.stringify(payload);
    } catch (_) {
      message = await resp.text();
    }
    throw new Error(message || `HTTP ${resp.status}`);
  }
  return await resp.json();
}

/* ==================== Tab switching ==================== */

const TAB_TITLES = { market: '大盘', data: '数据中心', funnel: '策略选股', notice: '公告选股', agent: '自进化智能体', paper: '模拟盘' };

function switchTab(tab) {
  state.activeTab = tab;
  document.querySelectorAll('.sidebar-item[data-tab]').forEach((el) => {
    el.classList.toggle('active', el.dataset.tab === tab);
  });
  document.querySelectorAll('.tab-content').forEach((el) => {
    el.classList.toggle('active', el.id === `tab-${tab}`);
  });
  const noticeCard = document.getElementById('noticeDetailCard');
  noticeCard.style.display = tab === 'notice' ? '' : 'none';
  const rightPanel = document.querySelector('.right-panel');
  const layout = document.querySelector('.layout');
  const twoCol = tab === 'funnel' || tab === 'notice';
  if (rightPanel) rightPanel.style.display = twoCol ? '' : 'none';
  if (layout) layout.classList.toggle('two-col', twoCol);
  if (twoCol) {
    renderChartPlaceholder('点击左侧股票查看');
    document.getElementById('stockSummary').textContent = '点击左侧股票查看';
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

function renderCounts() {
  if (!state.funnel) return;
  const c = (state.funnel.pools.candidate || []).filter(passConceptFilter).length;
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

async function paperBuy(symbol, name, price) {
  if (!price || price <= 0) {
    try {
      const rt = await request(`/api/stock/${symbol}/realtime`);
      if (rt && rt.found && rt.close > 0) price = rt.close;
    } catch {}
  }
  if (!price || price <= 0) {
    setStatus('无法获取实时价格，请稍后重试', 'error');
    return;
  }
  const qty = prompt(`模拟买入 ${name}(${symbol})\n现价: ${price.toFixed(2)}\n请输入买入股数:`, '100');
  if (!qty) return;
  try {
    const res = await request('/api/paper/buy', {
      method: 'POST',
      body: JSON.stringify({ symbol, name, price, qty: parseInt(qty) || 100 }),
    });
    if (res.success) setStatus(`模拟买入 ${name} ${qty}股 @ ${price.toFixed(2)}`, 'success');
  } catch (err) {
    setStatus(`模拟买入失败: ${err.message}`, 'error');
  }
}

async function paperSell(positionId, symbol, name) {
  let price = 0;
  try {
    const rt = await request(`/api/stock/${symbol}/realtime`);
    if (rt && rt.found && rt.close > 0) price = rt.close;
  } catch {}
  if (!price) { setStatus('无法获取实时价格', 'error'); return; }
  if (!confirm(`确认模拟卖出 ${name}(${symbol})？\n现价: ${price.toFixed(2)}`)) return;
  try {
    const res = await request('/api/paper/sell', {
      method: 'POST',
      body: JSON.stringify({ position_id: positionId, price }),
    });
    if (res.success) {
      setStatus(`模拟卖出 ${name} @ ${price.toFixed(2)}`, 'success');
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
  root.innerHTML = '';
  const filtered = list.filter(passConceptFilter);
  filtered.forEach((stock) => {
    const div = document.createElement('div');
    div.className = `stock-card ${state.selectedSymbol === stock.symbol ? 'active' : ''}`;
    div.onclick = () => selectSymbol(stock.symbol);
    const delta = Number(stock.score_delta || 0);
    const deltaCls = delta >= 0 ? 'up' : 'down';
    const deltaTxt = delta >= 0 ? `+${fmtNum(delta)}` : fmtNum(delta);
    const tags = (stock.concept_tags || [])
      .map((tag) => `<span class="tag" style="background:${tag.color}" title="热度:${tag.heat} 涨幅:${fmtNum(tag.change_pct)} 涨停:${tag.limit_up_count}">${tag.name} ${fmtNum(tag.change_pct, 1)}%/${tag.limit_up_count}</span>`)
      .join('');
    const badge = stock.recommended_pool
      ? `<span class="badge ${stock.recommended_pool === 'buy' ? 'buy' : 'focus'}">建议进入${stock.recommended_pool === 'buy' ? '买入池' : '重点池'}</span>`
      : '';
    const btns = cardActions(stock)
      .map(([txt, pool]) => `<button data-pool="${pool}" data-symbol="${stock.symbol}">${txt}</button>`)
      .join('');
    const simBuyBtn = poolName === 'buy'
      ? `<button class="btn-sim-buy" data-symbol="${stock.symbol}" data-name="${stock.name}" data-price="${stock.price || stock.breakout_level || 0}">模拟买入</button>`
      : '';
    div.innerHTML = `
      <div class="stock-top">
        <div class="stock-name">${stock.name} (${stock.symbol})</div>
        <div class="score ${deltaCls}">${fmtNum(stock.score)} (${deltaTxt})</div>
      </div>
      <div class="tags">${tags || '<span class="chip muted">暂无概念</span>'}</div>
      <div class="metrics">涨跌 ${fmtNum(stock.pct_change, 2)}% · 放量比 ${fmtNum(stock.volume_ratio, 2)} · 突破位 ${fmtNum(stock.breakout_level, 2)}</div>
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
  if (activeChip) activeChip.textContent = state.selectedConcept || '全部';
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
    const cls = Number(item.change_pct || 0) >= 0 ? 'up' : 'down';
    const sign = Number(item.change_pct || 0) >= 0 ? '+' : '';
    const card = document.createElement('div');
    card.className = `hot-stock-item ${state.selectedHotSymbol === item.symbol ? 'active' : ''}`;
    card.onclick = () => selectHotStock(item);
    card.innerHTML = `
      <div class="hot-stock-main"><div class="hot-stock-rank">#${item.rank}</div><div class="hot-stock-name">${item.name} (${item.symbol})</div></div>
      <div class="hot-stock-side ${cls}"><span>¥${fmtNum(item.latest_price, 2)}</span><span>${sign}${fmtNum(item.change_pct, 2)}%</span></div>
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

function renderDcStats(stats, syncStatus, report) {
  const grid = document.getElementById('dcStatsGrid');
  const statusText = syncStatus.status || 'idle';
  const statusCls = statusText === 'running' ? 'warning' : (statusText === 'success' ? 'success' : 'brand');
  const statusLabel = { idle: '空闲', running: '同步中', success: '已完成', failed: '失败' }[statusText] || statusText;
  const coverage = report?.coverage_pct ?? '--';
  const coverageCls = (typeof coverage === 'number') ? (coverage >= 99 ? 'success' : (coverage >= 90 ? 'warning' : 'error')) : 'brand';

  grid.innerHTML = `
    <div class="dc-stat-card">
      <span class="dc-stat-label">同步状态</span>
      <span class="dc-stat-value ${statusCls}">${statusLabel}</span>
      <span class="dc-stat-sub">${syncStatus.updated_at ? syncStatus.updated_at.slice(0, 16).replace('T', ' ') : '--'}</span>
    </div>
    <div class="dc-stat-card">
      <span class="dc-stat-label">最近同步日</span>
      <span class="dc-stat-value brand">${syncStatus.last_success_trade_date || '--'}</span>
      <span class="dc-stat-sub">${syncStatus.trigger_mode || '--'}</span>
    </div>
    <div class="dc-stat-card">
      <span class="dc-stat-label">缓存股票数</span>
      <span class="dc-stat-value">${(stats.symbol_count || 0).toLocaleString()}</span>
      <span class="dc-stat-sub">只</span>
    </div>
    <div class="dc-stat-card">
      <span class="dc-stat-label">K线总条数</span>
      <span class="dc-stat-value">${(stats.row_count || 0).toLocaleString()}</span>
      <span class="dc-stat-sub">${stats.min_date || '--'} ~ ${stats.max_date || '--'}</span>
    </div>
    <div class="dc-stat-card">
      <span class="dc-stat-label">数据覆盖率</span>
      <span class="dc-stat-value ${coverageCls}">${typeof coverage === 'number' ? coverage.toFixed(1) + '%' : coverage}</span>
      <span class="dc-stat-sub">最近30个交易日</span>
    </div>
    <div class="dc-stat-card">
      <span class="dc-stat-label">数据库大小</span>
      <span class="dc-stat-value">${stats.db_size_mb ?? '--'}</span>
      <span class="dc-stat-sub">MB</span>
    </div>
  `;
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

function renderDcReport(report) {
  const container = document.getElementById('dcReportContent');
  if (!report || report.status === 'none' || report.status === 'error') {
    container.innerHTML = `<div class="detail-empty">${report?.message || '暂无检查报告，点击上方按钮执行检查'}</div>`;
    return;
  }

  const pct = report.coverage_pct || 0;
  const circumference = 2 * Math.PI * 26;
  const offset = circumference * (1 - pct / 100);
  const ringColor = pct >= 99 ? '#22c55e' : (pct >= 90 ? '#f59e0b' : '#ef4444');

  let html = `
    <div class="dc-coverage-bar">
      <div class="dc-coverage-ring">
        <svg viewBox="0 0 64 64">
          <circle class="ring-bg" cx="32" cy="32" r="26"/>
          <circle class="ring-fg" cx="32" cy="32" r="26"
            stroke="${ringColor}"
            stroke-dasharray="${circumference}"
            stroke-dashoffset="${offset}"/>
        </svg>
        <div class="dc-coverage-pct">${pct.toFixed(1)}%</div>
      </div>
      <div class="dc-coverage-info">
        <div class="dc-info-row">检查时间: <strong>${(report.check_time || '').slice(0, 16).replace('T', ' ')}</strong></div>
        <div class="dc-info-row">检查范围: <strong>${report.trade_days_checked || 0}</strong> 个交易日 × <strong>${(report.total_symbols || 0).toLocaleString()}</strong> 只股票</div>
        <div class="dc-info-row">期望: <strong>${(report.total_expected || 0).toLocaleString()}</strong> 条 · 实际: <strong>${(report.total_actual || 0).toLocaleString()}</strong> 条 · 缺失: <strong style="color:${report.total_missing ? '#f59e0b' : '#22c55e'}">${(report.total_missing || 0).toLocaleString()}</strong> 条</div>
      </div>
    </div>
  `;

  const missingDates = report.missing_by_date || [];
  if (missingDates.length > 0) {
    html += `<div style="font-size:12px;font-weight:600;color:var(--text-secondary);margin-top:4px">缺失日期明细 (${missingDates.length}天)</div>`;
    html += '<div class="dc-missing-list">';
    for (const d of missingDates) {
      const barPct = d.coverage_pct || 0;
      const barColor = barPct >= 99 ? '#22c55e' : (barPct >= 90 ? '#f59e0b' : '#ef4444');
      html += `
        <div class="dc-missing-row">
          <span class="dc-missing-date">${d.date}</span>
          <span class="dc-missing-count">缺${d.missing_count}</span>
          <div class="dc-missing-bar-wrap"><div class="dc-missing-bar-fill" style="width:${barPct}%;background:${barColor}"></div></div>
          <span class="dc-missing-pct">${barPct.toFixed(1)}%</span>
        </div>`;
    }
    html += '</div>';
  } else {
    html += '<div style="color:#22c55e;font-size:13px;font-weight:600;padding:8px 0">数据完整，无缺失</div>';
  }

  html += `<div class="dc-report-meta">状态: ${report.status} · 检查于 ${(report.check_time || '').replace('T', ' ')}</div>`;
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

  container.innerHTML = items.map(t => {
    const time = (t.started_at || '').slice(11, 16);
    const failInfo = t.status === 'failed' && t.message ? `<div class="dc-task-error" style="font-size:11px;color:var(--status-error);margin-top:4px;padding-left:18px">${esc(t.message)}</div>` : '';
    return `
      <div class="dc-task-item" style="flex-wrap:wrap">
        <span class="status-dot status-dot--${t.status === 'success' ? 'success' : (t.status === 'running' ? 'warning' : (t.status === 'failed' ? 'error' : 'progress'))}"></span>
        <span class="dc-task-date">${t.trade_date}</span>
        <span class="dc-task-mode">${t.trigger_mode}</span>
        <span class="dc-task-counts">${t.success_symbols || 0}✓ ${t.failed_symbols || 0}✗ / ${t.total_symbols || 0}</span>
        <span class="dc-task-time">${time}</span>
        ${failInfo}
      </div>`;
  }).join('');

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
      { show: true, xAxisIndex: [0, 1], type: 'slider', bottom: '1%', borderColor: '#475569' },
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

function _klinePredictOption(merged, predStartIdx, realtimeMap) {
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
      if (rt && rt.volume) return [i, rt.volume, rt.close >= rt.open ? 1 : -1];
      return [i, x.volume || 0, x.close >= x.open ? 3 : 4];
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
      { show: true, xAxisIndex: [0, 1], type: 'slider', bottom: '1%', borderColor: '#475569' },
    ],
    series: [
      {
        name: '日K', type: 'candlestick', data: candles,
        itemStyle: { color: '#ef4444', color0: '#16a34a', borderColor: '#ef4444', borderColor0: '#16a34a' },
        markArea: predBoundary ? { silent: true, data: [[ { xAxis: predBoundary, itemStyle: { color: 'rgba(250, 204, 21, 0.06)' } }, { xAxis: lastDate } ]] } : undefined,
        markLine: predBoundary ? { silent: true, symbol: 'none', data: [{ xAxis: predBoundary, lineStyle: { type: 'dashed', color: 'rgba(250, 204, 21, 0.5)', width: 1 }, label: { show: true, formatter: '预 测', color: '#facc15', fontSize: 12, fontWeight: 'bold', position: 'insideStartTop', distance: [4, -18] } }] } : undefined,
      },
      {
        name: '实际', type: 'candlestick', data: realCandles,
        itemStyle: { color: '#ef4444', color0: '#16a34a', borderColor: '#ef4444', borderColor0: '#16a34a' },
        barWidth: '40%',
      },
      { name: 'MA5', type: 'line', data: ma5, smooth: true, symbol: 'none', lineStyle: { width: 1, color: '#fbbf24' }, connectNulls: false },
      { name: 'MA10', type: 'line', data: ma10, smooth: true, symbol: 'none', lineStyle: { width: 1, color: '#60a5fa' }, connectNulls: false },
      { name: 'MA30', type: 'line', data: ma30, smooth: true, symbol: 'none', lineStyle: { width: 1, color: '#a78bfa' }, connectNulls: false },
      { name: '成交量', type: 'bar', xAxisIndex: 1, yAxisIndex: 1, data: volumes, itemStyle: { color: (p) => { const flag = p.data[2]; if (flag === 3) return 'rgba(239,68,68,0.3)'; if (flag === 4) return 'rgba(22,163,106,0.3)'; return flag > 0 ? '#ef4444' : '#16a34a'; } } },
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

function renderStockSummary(detail) {
  const root = document.getElementById('stockSummary');
  if (!detail) { root.textContent = '点击左侧股票查看'; return; }
  root.textContent = `${detail.name}(${detail.symbol})  现价:${fmtNum(detail.metrics?.price, 2)}  涨跌:${fmtNum(detail.metrics?.pct_change, 2)}%  放量比:${fmtNum(detail.metrics?.volume_ratio, 2)}  突破位:${fmtNum(detail.metrics?.breakout_level, 2)}`;
}

function renderStockSummaryLite(item, klinePayload) {
  const root = document.getElementById('marketStockSummary');
  root.textContent = `${item.name}(${item.symbol})  现价:${fmtNum(item.latest_price, 2)}  涨跌:${fmtNum(item.change_pct, 2)}%  K线:${Number(klinePayload?.count || 0)}日`;
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
    if (!rt || !rt.found || !rt.open) return {};
    const map = {};
    if (predictedDates.includes(rt.date)) {
      map[rt.date] = { open: rt.open, high: rt.high, low: rt.low, close: rt.close, volume: rt.volume, amount: rt.amount };
    }
    return map;
  } catch { return {}; }
}

async function _fetchAndRenderFunnelPredict(symbol, name) {
  const summaryEl = document.getElementById('stockSummary');
  try {
    const pred = await request(`/api/predict/${symbol}/kronos?lookback=30&horizon=3`);
    if (pred.merged_kline && pred.merged_kline.length) {
      const pk = pred.predicted_kline || [];
      const predDates = pk.map(x => x.date);
      const rtMap = await _fetchRealtimeMap(symbol, predDates);
      renderKlineChart(pred.merged_kline, pred.prediction_start_index, rtMap);
      const hk = pred.history_kline || [];
      const lastClose = hk.length ? hk[hk.length - 1].close : 0;
      if (pk.length && lastClose) {
        const day3Close = pk[pk.length - 1].close;
        const chg = ((day3Close - lastClose) / lastClose * 100).toFixed(2);
        const tag = Number(chg) >= 0 ? `+${chg}%` : `${chg}%`;
        summaryEl.textContent += `  预测${pk.length}日: ${tag}`;
      }
      const rtToday = Object.values(rtMap)[0];
      if (rtToday && pk.length && lastClose) {
        const realChg = ((rtToday.close - lastClose) / lastClose * 100).toFixed(2);
        summaryEl.textContent += `  实际: ${realChg >= 0 ? '+' : ''}${realChg}%`;
      }
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
    const pred = await request(`/api/predict/${symbol}/kronos?lookback=30&horizon=3`);
    if (pred.merged_kline && pred.merged_kline.length) {
      const pk = pred.predicted_kline || [];
      const predDates = pk.map(x => x.date);
      const rtMap = await _fetchRealtimeMap(symbol, predDates);
      const chart = ensureMarketChart();
      if (chart) chart.setOption(_klinePredictOption(pred.merged_kline, pred.prediction_start_index, rtMap), true);
      const hk = pred.history_kline || [];
      const lastClose = hk.length ? hk[hk.length - 1].close : 0;
      if (pk.length && lastClose) {
        const day3Close = pk[pk.length - 1].close;
        const chg = ((day3Close - lastClose) / lastClose * 100).toFixed(2);
        const tag = Number(chg) >= 0 ? `+${chg}%` : `${chg}%`;
        summaryEl.textContent += `  预测${pk.length}日: ${tag}`;
      }
      const rtToday = Object.values(rtMap)[0];
      if (rtToday && pk.length && lastClose) {
        const realChg = ((rtToday.close - lastClose) / lastClose * 100).toFixed(2);
        summaryEl.textContent += `  实际: ${realChg >= 0 ? '+' : ''}${realChg}%`;
      }
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
    const pred = await request(`/api/predict/${symbol}/kronos?lookback=30&horizon=3`);
    if (pred.merged_kline && pred.merged_kline.length) {
      const pk = pred.predicted_kline || [];
      const predDates = pk.map(x => x.date);
      const rtMap = await _fetchRealtimeMap(symbol, predDates);
      renderKlineChart(pred.merged_kline, pred.prediction_start_index, rtMap);
      const hk = pred.history_kline || [];
      const lastClose = hk.length ? hk[hk.length - 1].close : 0;
      if (pk.length && lastClose) {
        const day3Close = pk[pk.length - 1].close;
        const chg = ((day3Close - lastClose) / lastClose * 100).toFixed(2);
        const tag = Number(chg) >= 0 ? `+${chg}%` : `${chg}%`;
        summaryEl.textContent += `  预测${pk.length}日: ${tag}`;
      }
      const rtToday = Object.values(rtMap)[0];
      if (rtToday && pk.length && lastClose) {
        const realChg = ((rtToday.close - lastClose) / lastClose * 100).toFixed(2);
        summaryEl.textContent += `  实际: ${realChg >= 0 ? '+' : ''}${realChg}%`;
      }
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

async function loadPaperData() {
  const [posRes, histRes, sumRes] = await Promise.allSettled([
    request('/api/paper/positions'),
    request('/api/paper/history?limit=50'),
    request('/api/paper/summary'),
  ]);
  if (posRes.status === 'fulfilled') renderPaperPositions(posRes.value.positions || []);
  if (histRes.status === 'fulfilled') renderPaperHistory(histRes.value.positions || []);
  if (sumRes.status === 'fulfilled') renderPaperSummary(sumRes.value);
}

function renderPaperSummary(s) {
  if (!s) return;
  const fpnl = document.getElementById('paperFloatPnl');
  const rpnl = document.getElementById('paperRealizedPnl');
  document.getElementById('paperOpenCount').textContent = s.open_count || 0;
  fpnl.textContent = `¥${fmtNum(s.total_float_pnl, 2)}`;
  fpnl.className = 'paper-stat-value ' + (s.total_float_pnl >= 0 ? 'up' : 'down');
  rpnl.textContent = `¥${fmtNum(s.total_realized_pnl, 2)}`;
  rpnl.className = 'paper-stat-value ' + (s.total_realized_pnl >= 0 ? 'up' : 'down');
  document.getElementById('paperWinRate').textContent = `${fmtNum(s.win_rate, 1)}%`;
  document.getElementById('paperWinLose').textContent = `${s.win_count || 0}/${s.lose_count || 0}`;
  document.getElementById('paperTotalFee').textContent = `¥${fmtNum(s.total_fee || 0, 2)}`;
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
    root.innerHTML = '<div class="empty-state"><div class="empty-state-text">暂无持仓，在买入池点击「模拟买入」开始</div></div>';
    return;
  }
  positions.forEach(p => {
    const pnlCls = p.pnl >= 0 ? 'up' : 'down';
    const pnlSign = p.pnl >= 0 ? '+' : '';
    const div = document.createElement('div');
    div.className = 'paper-card';
    div.innerHTML = `
      <div class="paper-card-top">
        <div class="paper-card-name">${p.name} (${p.symbol})</div>
        <div class="paper-card-pnl ${pnlCls}">${pnlSign}¥${fmtNum(p.pnl, 2)} (${pnlSign}${fmtNum(p.pnl_pct, 2)}%)</div>
      </div>
      <div class="paper-card-info">
        <span>成本: ${fmtNum(p.cost_price, 2)}</span>
        <span>现价: ${fmtNum(p.current_price, 2)}</span>
        <span>数量: ${p.qty}股</span>
        <span>市值: ¥${fmtNum(p.current_price * p.qty, 2)}</span>
      </div>
      <div class="paper-card-info"><span>开仓: ${p.opened_at}</span></div>
      <div class="paper-card-actions">
        <button class="btn-sell" data-id="${p.id}" data-symbol="${p.symbol}" data-name="${p.name}">模拟卖出</button>
      </div>
    `;
    div.querySelector('.btn-sell').onclick = (e) => {
      e.stopPropagation();
      paperSell(e.target.dataset.id, e.target.dataset.symbol, e.target.dataset.name);
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
  positions.forEach(p => {
    const pnlCls = (p.realized_pnl || 0) >= 0 ? 'up' : 'down';
    const pnlSign = (p.realized_pnl || 0) >= 0 ? '+' : '';
    const div = document.createElement('div');
    div.className = 'paper-card closed';
    div.innerHTML = `
      <div class="paper-card-top">
        <div class="paper-card-name">${p.name} (${p.symbol})</div>
        <div class="paper-card-pnl ${pnlCls}">${pnlSign}¥${fmtNum(p.realized_pnl, 2)} (${pnlSign}${fmtNum(p.realized_pnl_pct, 2)}%)</div>
      </div>
      <div class="paper-card-info">
        <span>成本: ${fmtNum(p.cost_price, 2)}</span>
        <span>平仓: ${fmtNum(p.close_price, 2)}</span>
        <span>数量: ${p.qty}股</span>
      </div>
      <div class="paper-card-info">
        <span>开仓: ${p.opened_at}</span>
        <span>平仓: ${p.closed_at}</span>
      </div>
    `;
    root.appendChild(div);
  });
}

/* ==================== Hermes Agent ==================== */

async function loadAgentData() {
  await Promise.all([loadAgentStatus(), loadAgentProposals(), loadAgentTasks(), loadMonitorConfig(), loadMonitorMessages()]);
}

async function loadAgentStatus() {
  try {
    const data = await request('/api/agent/status');
    const dot = document.getElementById('agentStatusDot');
    const txt = document.getElementById('agentStatusText');
    dot.className = 'agent-status-dot ' + (data.running ? 'running' : (data.llm_available ? 'ok' : 'error'));
    const parts = [];
    if (data.running) parts.push('运行中');
    else parts.push('就绪');
    if (data.hermes_agent_available) parts.push('Hermes Agent ✓');
    else if (data.llm_available) parts.push('LLM 可用');
    else parts.push('规则模式（LLM 未配置）');
    if (data.last_run) {
      const t = data.last_run.finished_at ? data.last_run.finished_at.slice(11, 16) : '--';
      parts.push(`上次: ${data.last_run.task_type} ${data.last_run.status} ${t}`);
    }
    txt.textContent = parts.join(' · ');
    const cnt = document.getElementById('agentPendingCount');
    cnt.textContent = data.stats?.pending_proposals ?? 0;
  } catch { /* ignore */ }
}

function _highlightDiff(diffStr) {
  return diffStr.split('\n').map(line => {
    if (line.startsWith('+')) return `<span class="diff-line-add">${esc(line)}</span>`;
    if (line.startsWith('-')) return `<span class="diff-line-del">${esc(line)}</span>`;
    return `<span class="diff-line-ctx">${esc(line)}</span>`;
  }).join('\n');
}

function _toggleCollapse(btn) {
  const target = btn.parentElement.querySelector('.collapsible');
  if (!target) return;
  const expanded = target.classList.toggle('expanded');
  btn.classList.toggle('expanded', expanded);
  btn.textContent = expanded ? '收起' : btn.dataset.label;
}

async function loadAgentProposals() {
  const container = document.getElementById('agentProposals');
  try {
    const data = await request('/api/agent/proposals?limit=20');
    if (!data.items || data.items.length === 0) {
      container.innerHTML = '<div class="empty-state"><div class="empty-state-text">暂无待审批提案，Agent 空闲中</div></div>';
      renderAgentSummary();
      return;
    }
    container.innerHTML = data.items.map(p => {
      const riskCls = `risk-${p.risk_level || 'medium'}`;
      const statusCls = `status-${p.status}`;
      const diffStr = p.diff_payload ? JSON.stringify(p.diff_payload, null, 2) : '';
      const isPending = p.status === 'pending';
      const statusLabel = { pending: '待审批', approved: '已批准', rejected: '已驳回', deferred: '已暂缓' }[p.status] || p.status;
      const statusBadgeCls = { pending: 'badge--warning', approved: 'badge--success', rejected: 'badge--error' }[p.status] || '';
      const reasoningHtml = p.reasoning
        ? `<button class="collapse-toggle" data-label="查看推理过程" onclick="_toggleCollapse(this)">查看推理过程</button>
           <div class="collapsible"><div class="agent-proposal-reasoning">${esc(p.reasoning)}</div></div>`
        : '';
      const diffHtml = diffStr
        ? `<button class="collapse-toggle" data-label="查看变更" onclick="_toggleCollapse(this)">查看变更</button>
           <div class="collapsible"><div class="agent-proposal-diff">${_highlightDiff(diffStr)}</div></div>`
        : '';
      return `
        <div class="agent-proposal-card ${statusCls}">
          <div class="agent-proposal-title">${esc(p.title)}</div>
          <div class="agent-proposal-meta">
            <span>类型: ${esc(p.type)}</span>
            <span class="${riskCls}">风险: ${esc(p.risk_level)}</span>
            <span>置信度: ${Math.round((p.confidence || 0) * 100)}%</span>
            <span class="badge ${statusBadgeCls}" style="margin:0;display:inline-block">${statusLabel}</span>
            <span>${(p.created_at || '').slice(0, 16)}</span>
          </div>
          ${reasoningHtml}
          ${diffHtml}
          ${isPending ? `
            <div class="agent-proposal-actions" style="margin-top:10px">
              <button class="btn-primary" onclick="approveProposal(${p.id})">批准</button>
              <button class="btn-danger" onclick="rejectProposal(${p.id})">驳回</button>
            </div>
          ` : ''}
        </div>`;
    }).join('');
    renderAgentSummary();
  } catch {
    container.innerHTML = '<div class="empty-state"><div class="empty-state-text">提案加载失败</div></div>';
  }
}

function esc(s) { const d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; }

async function loadAgentTasks() {
  const container = document.getElementById('agentTasks');
  try {
    const data = await request('/api/agent/tasks?limit=10');
    if (!data.items || data.items.length === 0) {
      container.innerHTML = '<div class="empty-state"><div class="empty-state-text">尚无运行记录，点击上方「手动触发复盘」启动</div></div>';
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

async function approveProposal(id) {
  if (!confirm('确认批准此提案？参数将自动应用。')) return;
  try {
    await request(`/api/agent/proposals/${id}/approve`, { method: 'POST', body: JSON.stringify({}) });
    setStatus('提案已批准并应用', 'success');
    await loadAgentData();
  } catch (err) {
    setStatus(`批准失败: ${err.message}`, 'error');
  }
}

async function rejectProposal(id) {
  const note = prompt('驳回原因（可选）：') || '';
  try {
    await request(`/api/agent/proposals/${id}/reject`, { method: 'POST', body: JSON.stringify({ note }) });
    setStatus('提案已驳回', 'success');
    await loadAgentData();
  } catch (err) {
    setStatus(`驳回失败: ${err.message}`, 'error');
  }
}

/* ==================== 盘中监控 ==================== */

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
  const countEl = document.getElementById('monitorMsgCount');
  if (!container) return;
  const msgs = state.monitorMessages;
  if (countEl) countEl.textContent = msgs.length;
  if (!msgs.length) {
    container.innerHTML = '<div class="empty-state"><div class="empty-state-text">暂无推送消息，启动监控或手动触发</div></div>';
    return;
  }
  container.innerHTML = msgs.map(m => _renderMonitorCard(m)).join('');
  container.querySelectorAll('.mtheme-stock').forEach(el => {
    el.style.cursor = 'pointer';
    el.onclick = () => _loadMonitorKline(el.dataset.symbol, el.dataset.name);
  });
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
      const payload = await request(`/api/kline/${symbol}?days=29`);
      kline = payload?.items || [];
    } catch {}
    if (!kline.length) {
      try {
        const detail = await request(`/api/stock/${symbol}/detail?kline_days=29`);
        kline = detail?.kline || [];
      } catch {}
    }

    if (!kline.length) {
      state.monitorKlineChart.setOption(_placeholderOption('暂无K线数据'));
      return;
    }

    state.monitorKlineChart.setOption(_klineOption(kline), true);

    try {
      const pred = await request(`/api/predict/${symbol}/kronos?lookback=30&horizon=3`);
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

function _parseMonitorThemes(text) {
  const themes = [];
  const blocks = text.split(/(?=【[高中低]+[高]?】\s*主线)/);
  for (const block of blocks) {
    const m = block.match(/【([高中低]+[高]?)】\s*主线([一二三四五六七八九十\d]+)[：:]\s*(.+?)(?:\n|$)/);
    if (!m) continue;
    const level = m[1];
    const idx = m[2];
    const title = m[3].trim();

    const stocks = [];
    const stockRe = /[-·]\s*(\d{6})\s+([^\s：:]+)[：:]\s*(.+)/g;
    let sm;
    const stockSection = block.match(/关注个股[\s\S]*?(?=\d\)\s|失效条件|【|$)/i);
    const stockText = stockSection ? stockSection[0] : block;
    while ((sm = stockRe.exec(stockText)) !== null) {
      stocks.push({ symbol: sm[1], name: sm[2], reason: sm[3].trim() });
    }

    let analysis = '';
    const logicMatch = block.match(/逻辑链[\s\S]*?(?=\d\)\s*关注|$)/i);
    if (logicMatch) {
      analysis = logicMatch[0].replace(/^.*逻辑链[（(].*?[)）]\s*/i, '').trim();
      analysis = analysis.split('\n').filter(l => l.trim().startsWith('-')).map(l => l.trim().replace(/^-\s*/, '')).join(' ');
    }

    let risk = '';
    const riskMatch = block.match(/失效条件[\s\S]*?(?=\d\)\s|【稳健|【激进|$)/i);
    if (riskMatch) {
      risk = riskMatch[0].replace(/^.*失效条件.*?\n/i, '').trim();
      risk = risk.split('\n').filter(l => l.trim().startsWith('-')).map(l => l.trim().replace(/^-\s*/, '')).slice(0, 2).join('；');
    }

    themes.push({ level, idx, title, analysis, risk, stocks });
  }
  return themes;
}

function _renderMonitorCard(msg) {
  const time = (msg.created_at || '').slice(11, 16) || '--:--';
  const triggerLabel = msg.trigger === 'manual' ? '手动' : '定时';
  const raw = msg.content || '';
  const themes = _parseMonitorThemes(raw);

  if (!themes.length) {
    return `<div class="monitor-msg-card">
      <div class="monitor-msg-header">
        <span class="monitor-msg-time">${time}</span>
        <span class="monitor-msg-trigger">${triggerLabel}</span>
      </div>
      <div class="monitor-msg-body">${_formatPlainContent(raw)}</div>
    </div>`;
  }

  const headerMatch = raw.match(/^(.+?)(?=【)/s);
  let headerText = '';
  if (headerMatch) {
    headerText = headerMatch[1].trim().split('\n').filter(l => l.trim()).slice(0, 2).join(' · ');
  }

  let summaryMatch = raw.match(/10分钟内执行摘要([\s\S]*?)$/i) || raw.match(/执行摘要([\s\S]*?)$/i);
  let summaryHtml = '';
  if (summaryMatch) {
    const lines = summaryMatch[1].trim().split('\n').filter(l => l.trim().startsWith('-')).slice(0, 3);
    if (lines.length) {
      summaryHtml = `<div class="monitor-summary"><b>执行摘要</b> ${lines.map(l => esc(l.replace(/^-\s*/, ''))).join(' | ')}</div>`;
    }
  }

  const themesHtml = themes.map(t => {
    const levelCls = t.level === '高' ? 'high' : t.level.includes('高') ? 'mid-high' : 'mid';
    const stocksHtml = t.stocks.map(s =>
      `<div class="mtheme-stock" data-symbol="${s.symbol}" data-name="${s.name}">` +
      `<code>${s.symbol}</code> <b>${esc(s.name)}</b></div>`
    ).join('');
    const analysisHtml = t.analysis ? `<div class="mtheme-analysis">${esc(t.analysis)}</div>` : '';
    return `<div class="mtheme-card ${levelCls}">
      <div class="mtheme-header">
        <span class="mtheme-level ${levelCls}">${esc(t.level)}</span>
        <span class="mtheme-title">${esc(t.title)}</span>
      </div>
      ${analysisHtml}
      <div class="mtheme-pool-label">关注池 · ${t.stocks.length}只</div>
      <div class="mtheme-pool">${stocksHtml || '<span class="muted">暂无</span>'}</div>
    </div>`;
  }).join('');

  return `<div class="monitor-msg-card">
    <div class="monitor-msg-header">
      <span class="monitor-msg-time">${time}</span>
      <span class="monitor-msg-trigger">${triggerLabel}</span>
    </div>
    <div class="mtheme-grid">${themesHtml}</div>
    ${summaryHtml}
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

async function openPredictModal(symbol, detail) {
  const modal = document.getElementById('predictModal');
  modal.style.display = 'flex';

  const name = detail?.name || symbol;
  document.getElementById('predictModalTitle').textContent = `${name} (${symbol}) — Kronos 三日预测`;

  const m = detail?.metrics || {};
  const pct = Number(m.pct_change || 0);
  const pctCls = pct >= 0 ? 'up' : 'down';
  const sign = pct >= 0 ? '+' : '';
  document.getElementById('predictModalSubtitle').innerHTML = `
    <span>现价: ¥${fmtNum(m.price, 2)}</span>
    <span class="${pctCls}">涨跌: ${sign}${fmtNum(pct, 2)}%</span>
    <span>放量比: ${fmtNum(m.volume_ratio, 2)}</span>
    <span>突破位: ${fmtNum(m.breakout_level, 2)}</span>
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
    const pred = await request(`/api/predict/${symbol}/kronos?lookback=30&horizon=3`);
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
          data: [[
            { xAxis: predBoundary, itemStyle: { color: 'rgba(250, 204, 21, 0.12)' } },
            { xAxis: lastDate }
          ]]
        } : undefined,
        markLine: predBoundary ? {
          silent: true, symbol: 'none',
          data: [{ xAxis: predBoundary, lineStyle: { type: 'dashed', color: 'rgba(250, 204, 21, 0.6)', width: 1 }, label: { show: true, formatter: '预 测', color: '#facc15', fontSize: 12, fontWeight: 'bold', position: 'insideStartTop', distance: [4, -18] } }]
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

function connectWs() {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(`${protocol}//${window.location.host}/ws/realtime`);
  ws.onmessage = (evt) => {
    try {
      const msg = JSON.parse(evt.data);
      if (msg.event === 'snapshot') {
        state.funnel = msg.data.funnel;
        state.hotConcepts = msg.data.hot_concepts;
        state.hotStocks = msg.data.hot_stocks || state.hotStocks;
        renderHotConcepts();
        renderHotStocks();
        renderFunnel();
      } else if (msg.event === 'monitor_update') {
        _appendMonitorMessage({
          id: msg.data.message_id,
          content: msg.data.content,
          created_at: msg.data.created_at,
          trigger: msg.data.trigger || 'scheduled',
        });
        if (state.activeTab === 'agent') {
          setStatus('收到盘中监控推送', 'success');
        }
      }
    } catch (err) {
      console.error('ws parse error', err);
    }
  };
  ws.onclose = () => { setTimeout(connectWs, 2000); };
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
      document.getElementById('agentSubProposal').classList.toggle('active', sub === 'proposal');
    };
  });

  document.getElementById('predictModalClose').onclick = closePredictModal;
  document.getElementById('predictModal').onclick = (e) => {
    if (e.target === e.currentTarget) closePredictModal();
  };

  _updateMarketStatus();
  setInterval(_updateMarketStatus, 30000);

  document.getElementById('btnPaperRefresh').onclick = () => loadPaperData();
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

  const dcSyncDate = document.getElementById('dcSyncDate');
  const today = new Date();
  dcSyncDate.value = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`;

  document.getElementById('btnDcFullSync').onclick = async () => {
    const btn = document.getElementById('btnDcFullSync');
    _btnStart(btn, '同步中...');
    setStatus('全量同步（智能补缺）执行中...', 'info');
    try {
      const payload = await request('/api/jobs/kline-cache/sync?trigger_mode=manual&force=true', { method: 'POST' });
      setStatus(`同步完成: ${payload.message || ''} ${payload.success_symbols || 0}/${payload.total_symbols || 0}`, 'success');
      await loadDataCenter();
    } catch (err) {
      setStatus(`同步失败: ${err.message}`, 'error');
      await loadDataCenter();
    } finally {
      _btnEnd(btn);
    }
  };

  document.getElementById('btnDcIncrSync').onclick = async () => {
    const btn = document.getElementById('btnDcIncrSync');
    const dateVal = dcSyncDate.value;
    if (!dateVal) { setStatus('请选择同步日期', 'error'); return; }
    _btnStart(btn, '同步中...');
    setStatus(`增量同步 ${dateVal} 执行中...`, 'info');
    try {
      const payload = await request(`/api/jobs/kline-cache/incremental-sync?trade_date=${dateVal}&trigger_mode=manual`, { method: 'POST' });
      setStatus(`增量同步完成: ${dateVal} · ${payload.symbol_count || 0}/${payload.total_symbols || 0}`, 'success');
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
    _btnStart(btn, '复盘执行中...');
    setStatus('Hermes 正在采集数据并执行复盘分析...', 'info');
    try {
      const payload = await request('/api/agent/run', { method: 'POST', body: JSON.stringify({ task_type: 'full_diagnosis' }) });
      const proposals = payload.summary?.proposals_created || 0;
      const msg = payload.summary?.message || '全面诊断完成';
      const dailyUsedLLM = payload.summary?.daily?.llm_used;
      const noticeUsedLLM = payload.summary?.notice?.llm_used;
      const llmHint = (dailyUsedLLM || noticeUsedLLM) ? '（LLM 分析）' : '（规则诊断）';
      setStatus(`✅ ${msg} ${llmHint} · 产出 ${proposals} 个提案`, 'success');
      await loadAgentData();
    } catch (err) {
      setStatus(`复盘失败: ${err.message}`, 'error');
    } finally {
      _btnEnd(btn);
    }
  };

  // ── 盘中监控按钮绑定 ──
  document.getElementById('btnMonitorToggle').onclick = async () => {
    const btn = document.getElementById('btnMonitorToggle');
    const isOn = state.monitorConfig && state.monitorConfig.enabled;
    if (isOn) {
      _btnStart(btn, '停止中...');
      try {
        await request('/api/agent/monitor/stop', { method: 'POST' });
        state.monitorConfig.enabled = 0;
        _updateMonitorStatusUI(false);
        setStatus('盘中监控已停止', 'success');
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
        setStatus(`盘中监控已启动，每 ${interval} 分钟推送`, 'success');
      } catch (err) { setStatus(`启动失败: ${err.message}`, 'error'); }
      finally { _btnEnd(btn); }
    }
  };

  document.getElementById('btnMonitorTrigger').onclick = async () => {
    const btn = document.getElementById('btnMonitorTrigger');
    _btnStart(btn, '分析中...');
    setStatus('盘中监控手动触发中，请稍候...', 'info');
    try {
      const result = await request('/api/agent/monitor/trigger', { method: 'POST' });
      if (result.success) {
        setStatus(`盘中监控完成 (${result.elapsed_ms || 0}ms)`, 'success');
      } else {
        setStatus(`监控执行失败: ${result.message || ''}`, 'error');
      }
    } catch (err) {
      setStatus(`手动触发失败: ${err.message}`, 'error');
    } finally { _btnEnd(btn); }
  };

  document.getElementById('btnMonitorPromptToggle').onclick = () => {
    const wrap = document.getElementById('monitorPromptWrap');
    const btn = document.getElementById('btnMonitorPromptToggle');
    if (wrap.style.display === 'none') {
      wrap.style.display = 'block';
      btn.textContent = '收起';
    } else {
      wrap.style.display = 'none';
      btn.textContent = '展开编辑';
    }
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
}

init().catch((err) => {
  document.getElementById('meta').textContent = `初始化失败: ${err.message}`;
  renderChartPlaceholder(`初始化失败: ${err.message}`);
});
