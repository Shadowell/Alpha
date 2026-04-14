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
};

function fmtNum(v, digits = 2) {
  const n = Number(v || 0);
  return Number.isFinite(n) ? n.toFixed(digits) : '0.00';
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

function setStatus(text, tone = 'info') {
  const toast = document.createElement('div');
  toast.className = `toast toast-${tone}`;
  toast.textContent = text;
  document.body.appendChild(toast);
  requestAnimationFrame(() => toast.classList.add('show'));
  setTimeout(() => {
    toast.classList.remove('show');
    setTimeout(() => toast.remove(), 300);
  }, tone === 'error' ? 4000 : 2500);
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

const TAB_TITLES = { market: '大盘', data: '数据中心', funnel: '策略选股', notice: '公告选股', agent: 'Hermes Agent' };

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
  document.getElementById('count-candidate').textContent = state.funnel.stats.candidate;
  document.getElementById('count-focus').textContent = state.funnel.stats.focus;
  document.getElementById('count-buy').textContent = state.funnel.stats.buy;
}

function cardActions(stock) {
  const actions = [];
  if (stock.pool === 'candidate') actions.push(['加入重点', 'focus']);
  if (stock.pool === 'focus') { actions.push(['移回候选', 'candidate']); actions.push(['加入买入', 'buy']); }
  if (stock.pool === 'buy') actions.push(['降级重点', 'focus']);
  return actions;
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
    div.innerHTML = `
      <div class="stock-top">
        <div class="stock-name">${stock.name} (${stock.symbol})</div>
        <div class="score ${deltaCls}">${fmtNum(stock.score)} (${deltaTxt})</div>
      </div>
      <div class="tags">${tags || '<span class="chip muted">暂无概念</span>'}</div>
      <div class="metrics">涨跌 ${fmtNum(stock.pct_change, 2)}% · 放量比 ${fmtNum(stock.volume_ratio, 2)} · 突破位 ${fmtNum(stock.breakout_level, 2)}</div>
      ${badge}
      <div class="card-actions">${btns}</div>
    `;
    div.querySelectorAll('button').forEach((btn) => {
      btn.onclick = (e) => { e.stopPropagation(); movePool(btn.dataset.symbol, btn.dataset.pool); };
    });
    root.appendChild(div);
  });
  if (!filtered.length) root.innerHTML = '<div class="detail-empty">暂无股票</div>';
}

function renderFunnel() {
  if (!state.funnel) return;
  renderCounts();
  renderPool('candidate', state.funnel.pools.candidate || []);
  renderPool('focus', state.funnel.pools.focus || []);
  renderPool('buy', state.funnel.pools.buy || []);
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
  if (!items.length) root.innerHTML = '<div class="detail-empty">暂无概念数据</div>';
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
  if (!items.length) root.innerHTML = '<div class="detail-empty">暂无热门个股数据</div>';
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

  renderDcStats(stats, syncStatus, report);
  renderDcProgress(syncStatus);
  renderDcReport(report);
  renderDcTaskList(logs);
  renderDcLogStream(logs);
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

function renderDcTaskList(logs) {
  const container = document.getElementById('dcTaskList');
  const pager = document.getElementById('dcTaskPager');
  const items = logs.items || [];

  if (!items.length) {
    container.innerHTML = '<div class="detail-empty">暂无任务记录</div>';
    pager.innerHTML = '';
    return;
  }

  container.innerHTML = items.map(t => {
    const time = (t.started_at || '').slice(11, 16);
    return `
      <div class="dc-task-item">
        <span class="dc-task-status ${t.status}"></span>
        <span class="dc-task-date">${t.trade_date}</span>
        <span class="dc-task-mode">${t.trigger_mode}</span>
        <span class="dc-task-counts">${t.success_symbols || 0}✓ ${t.failed_symbols || 0}✗ / ${t.total_symbols || 0}</span>
        <span class="dc-task-time">${time}</span>
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
  const items = logs.items || [];
  if (!items.length) {
    container.innerHTML = '<div class="detail-empty">暂无日志</div>';
    return;
  }
  container.innerHTML = items.slice(0, 10).map(t => `
    <div class="sync-log-item">[${t.status}] ${t.trade_date} ${t.synced_symbols}/${t.total_symbols} (${t.trigger_mode}) ${t.message || ''}</div>
  `).join('');
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

function _klineOption(rows) {
  const categoryData = rows.map((x) => x.date);
  const candleData = rows.map((x) => [x.open, x.close, x.low, x.high]);
  const volumeData = rows.map((x, idx) => [idx, x.volume, x.close >= x.open ? 1 : -1]);
  return {
    animation: false, backgroundColor: 'transparent', legend: { show: false },
    tooltip: { trigger: 'axis', axisPointer: { type: 'cross' } },
    axisPointer: { link: [{ xAxisIndex: 'all' }], label: { backgroundColor: '#334155' } },
    grid: [{ left: 52, right: 18, top: 14, height: '62%' }, { left: 52, right: 18, top: '77%', height: '16%' }],
    xAxis: [
      { type: 'category', data: categoryData, boundaryGap: true, axisLine: { lineStyle: { color: '#475569' } }, axisLabel: { color: '#64748b' }, splitLine: { show: false }, min: 'dataMin', max: 'dataMax' },
      { type: 'category', gridIndex: 1, data: categoryData, boundaryGap: true, axisLine: { lineStyle: { color: '#475569' } }, axisLabel: { show: false }, splitLine: { show: false }, min: 'dataMin', max: 'dataMax' },
    ],
    yAxis: [
      { scale: true, splitArea: { show: false }, axisLine: { lineStyle: { color: '#475569' } }, axisLabel: { color: '#64748b' }, splitLine: { lineStyle: { color: '#1f2937' } } },
      { scale: true, gridIndex: 1, splitNumber: 2, axisLine: { lineStyle: { color: '#475569' } }, axisLabel: { color: '#64748b' }, splitLine: { lineStyle: { color: '#1f2937' } } },
    ],
    dataZoom: [
      { type: 'inside', xAxisIndex: [0, 1], start: 0, end: 100 },
      { show: true, xAxisIndex: [0, 1], type: 'slider', bottom: '1%', borderColor: '#334155' },
    ],
    series: [
      { name: '日K', type: 'candlestick', data: candleData, itemStyle: { color: '#ef4444', color0: '#16a34a', borderColor: '#ef4444', borderColor0: '#16a34a' } },
      { name: '成交量', type: 'bar', xAxisIndex: 1, yAxisIndex: 1, data: volumeData, itemStyle: { color: (p) => (p.data[2] > 0 ? '#ef4444' : '#16a34a') } },
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

function renderKlineChart(rows) {
  const chart = ensureChart();
  if (!chart) return;
  if (!rows.length) { renderChartPlaceholder('暂无K线数据'); return; }
  chart.setOption(_klineOption(rows), true);
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
    openPredictModal(symbol, detail);
  } catch (err) {
    renderStockSummary(null);
    renderChartPlaceholder(`加载失败: ${err.message}`);
  }
}

async function selectHotStock(item) {
  state.selectedHotSymbol = item.symbol;
  renderHotStocks();
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
    setStatus(`热门个股 ${item.symbol} K线已加载`, 'success');
    const pseudoDetail = {
      name: item.name,
      metrics: {
        price: item.latest_price,
        pct_change: item.change_pct,
        volume_ratio: item.volume_ratio || 0,
        breakout_level: 0,
      },
      kline: payload?.items || [],
    };
    openPredictModal(item.symbol, pseudoDetail);
  } catch (err) {
    renderMarketChartPlaceholder(`加载失败: ${err.message}`);
    setStatus(`热门个股加载失败: ${err.message}`, 'error');
  }
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
    card.innerHTML = `
      <div class="stock-top">
        <div class="stock-name">${stock.name} (${stock.symbol})</div>
        <div class="score up">${fmtNum(stock.score)}</div>
      </div>
      <div class="metrics">${stock.notice_type} · ${stock.notice_date}</div>
      <div class="metrics">${stock.title}</div>
      <div class="metrics">理由: ${stock.reason || '-'}</div>
      <div class="metrics">风险: ${stock.risk || '-'}</div>
      <div class="card-actions">${actions}</div>
    `;
    card.querySelectorAll('button').forEach((btn) => {
      btn.onclick = (e) => { e.stopPropagation(); moveNoticePool(btn.dataset.symbol, btn.dataset.pool); };
    });
    root.appendChild(card);
  });
  if (!list.length) root.innerHTML = '<div class="detail-empty">暂无股票</div>';
}

function renderNoticeFunnel() {
  if (!state.noticeFunnel) return;
  renderNoticeCounts();
  renderNoticePool('candidate', state.noticeFunnel.pools.candidate || []);
  renderNoticePool('focus', state.noticeFunnel.pools.focus || []);
  renderNoticePool('buy', state.noticeFunnel.pools.buy || []);
  renderNoticeMeta();
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
    let kline = detail.kline || [];
    if (!kline.length) {
      const fallback = await request(`/api/kline/${symbol}?days=30`);
      kline = fallback.items || [];
    }
    document.getElementById('noticeSummary').textContent = `${detail.name}(${detail.symbol}) 分数:${fmtNum(detail.score)} 池:${detail.pool}`;
    const first = (detail.notices || [])[0] || {};
    document.getElementById('noticeDetail').innerHTML = `
      <div class="metrics"><b>${first.title || '-'}</b></div>
      <div class="metrics">类型: ${first.notice_type || '-'}</div>
      <div class="metrics">理由: ${detail.reason || '-'}</div>
      <div class="metrics">风险: ${detail.risk || '-'}</div>
      <div class="metrics"><a href="${first.url || '#'}" target="_blank" style="color:var(--brand)">公告链接</a></div>
    `;
    document.getElementById('stockSummary').textContent = `${detail.name}(${detail.symbol}) 30日日K`;
    renderKlineChart(kline);
  } catch (err) {
    document.getElementById('noticeDetail').textContent = `加载失败: ${err.message}`;
    renderChartPlaceholder(`加载失败: ${err.message}`);
  }
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
    document.getElementById('noticeDetail').innerHTML = '<div class="detail-empty">暂无详情</div>';
    if (state.activeTab === 'notice') renderChartPlaceholder('点击左侧股票查看');
  }
}

async function reload() {
  await reloadFunnel();
  if (state.noticeFunnel) await reloadNotice();
}

/* ==================== Hermes Agent ==================== */

async function loadAgentData() {
  await Promise.all([loadAgentStatus(), loadAgentProposals(), loadAgentTasks()]);
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
    parts.push(data.llm_available ? 'LLM 可用' : 'LLM 未配置');
    if (data.last_run) {
      const t = data.last_run.finished_at ? data.last_run.finished_at.slice(11, 16) : '--';
      parts.push(`上次: ${data.last_run.task_type} ${data.last_run.status} ${t}`);
    }
    txt.textContent = parts.join(' · ');
    const cnt = document.getElementById('agentPendingCount');
    cnt.textContent = data.stats?.pending_proposals ?? 0;
  } catch { /* ignore */ }
}

async function loadAgentProposals() {
  const container = document.getElementById('agentProposals');
  try {
    const data = await request('/api/agent/proposals?limit=20');
    if (!data.items || data.items.length === 0) {
      container.innerHTML = '<div class="agent-empty">暂无提案</div>';
      return;
    }
    container.innerHTML = data.items.map(p => {
      const riskCls = `risk-${p.risk_level || 'medium'}`;
      const statusCls = `status-${p.status}`;
      const diffStr = p.diff_payload ? JSON.stringify(p.diff_payload, null, 2) : '';
      const isPending = p.status === 'pending';
      const statusLabel = { pending: '待审批', approved: '已批准', rejected: '已驳回', deferred: '已暂缓' }[p.status] || p.status;
      return `
        <div class="agent-proposal-card ${statusCls}">
          <div class="agent-proposal-title">${esc(p.title)}</div>
          <div class="agent-proposal-meta">
            <span>类型: ${esc(p.type)}</span>
            <span class="${riskCls}">风险: ${esc(p.risk_level)}</span>
            <span>置信度: ${Math.round((p.confidence || 0) * 100)}%</span>
            <span>状态: ${statusLabel}</span>
            <span>${(p.created_at || '').slice(0, 16)}</span>
          </div>
          ${p.reasoning ? `<div class="agent-proposal-reasoning">${esc(p.reasoning)}</div>` : ''}
          ${diffStr ? `<div class="agent-proposal-diff">${esc(diffStr)}</div>` : ''}
          ${isPending ? `
            <div class="agent-proposal-actions">
              <button class="btn-approve" onclick="approveProposal(${p.id})">批准</button>
              <button class="btn-reject" onclick="rejectProposal(${p.id})">驳回</button>
            </div>
          ` : ''}
        </div>`;
    }).join('');
  } catch {
    container.innerHTML = '<div class="agent-empty">加载失败</div>';
  }
}

function esc(s) { const d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; }

async function loadAgentTasks() {
  const container = document.getElementById('agentTasks');
  try {
    const data = await request('/api/agent/tasks?limit=10');
    if (!data.items || data.items.length === 0) {
      container.innerHTML = '<div class="agent-empty">暂无运行记录</div>';
      return;
    }
    container.innerHTML = data.items.map(t => {
      const typeLabel = { daily_review: '盘后复盘', notice_review: '公告复盘', full_diagnosis: '全面诊断' }[t.task_type] || t.task_type;
      const statusLabel = { success: '成功', failed: '失败', timeout: '超时', running: '运行中' }[t.status] || t.status;
      const time = (t.finished_at || t.started_at || '').slice(11, 16);
      const elapsed = t.elapsed_ms ? `${(t.elapsed_ms / 1000).toFixed(1)}s` : '--';
      return `
        <div class="agent-task-row">
          <span class="agent-task-type">${typeLabel}</span>
          <span class="agent-task-status ${t.status}">${statusLabel}</span>
          <span class="agent-task-time">${time}</span>
          <span class="agent-task-elapsed">${elapsed}</span>
          <span class="agent-task-time">${t.trigger || ''}</span>
        </div>`;
    }).join('');
  } catch {
    container.innerHTML = '<div class="agent-empty">加载失败</div>';
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
        html += `<div>开: ${fmtNum(k.open, 2)} &nbsp; 高: ${fmtNum(k.high, 2)}</div>`;
        html += `<div>低: ${fmtNum(k.low, 2)} &nbsp; 收: ${fmtNum(k.close, 2)}</div>`;
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
          data: [{ xAxis: predBoundary, lineStyle: { type: 'dashed', color: 'rgba(250, 204, 21, 0.6)', width: 1 }, label: { show: true, formatter: '预测区', color: '#facc15', fontSize: 10, position: 'insideStartTop' } }]
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
      if (msg.event !== 'snapshot') return;
      state.funnel = msg.data.funnel;
      state.hotConcepts = msg.data.hot_concepts;
      state.hotStocks = msg.data.hot_stocks || state.hotStocks;
      renderHotConcepts();
      renderHotStocks();
      renderFunnel();
    } catch (err) {
      console.error('ws parse error', err);
    }
  };
  ws.onclose = () => { setTimeout(connectWs, 2000); };
}

/* ==================== Init ==================== */

async function init() {
  document.querySelectorAll('.sidebar-item[data-tab]').forEach((el) => {
    el.onclick = (e) => { e.preventDefault(); switchTab(el.dataset.tab); };
  });

  document.getElementById('predictModalClose').onclick = closePredictModal;
  document.getElementById('predictModal').onclick = (e) => {
    if (e.target === e.currentTarget) closePredictModal();
  };

  document.getElementById('btnRefreshSidebar').onclick = async (e) => {
    e.preventDefault();
    setStatus('刷新中...', 'info');
    if (state.activeTab === 'market') {
      await reloadFunnel();
      setStatus('大盘数据已刷新', 'success');
    } else if (state.activeTab === 'data') {
      await loadDataCenter();
      setStatus('数据中心已刷新', 'success');
    } else if (state.activeTab === 'funnel') {
      await request('/api/score/recompute', { method: 'POST', body: JSON.stringify({}) });
      await reloadFunnel();
      setStatus('策略选股已刷新', 'success');
    } else if (state.activeTab === 'notice') {
      await reloadNotice();
      setStatus('公告池已刷新', 'success');
    } else if (state.activeTab === 'agent') {
      await loadAgentData();
      setStatus('Agent 数据已刷新', 'success');
    }
  };

  const dcSyncDate = document.getElementById('dcSyncDate');
  const today = new Date();
  dcSyncDate.value = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`;

  document.getElementById('btnDcFullSync').onclick = async () => {
    const btn = document.getElementById('btnDcFullSync');
    btn.disabled = true;
    const old = btn.textContent;
    btn.textContent = '同步中...';
    setStatus('全量同步（智能补缺）执行中...', 'info');
    try {
      const payload = await request('/api/jobs/kline-cache/sync?trigger_mode=manual&force=true', { method: 'POST' });
      setStatus(`同步完成: ${payload.message || ''} ${payload.success_symbols || 0}/${payload.total_symbols || 0}`, 'success');
      await loadDataCenter();
    } catch (err) {
      setStatus(`同步失败: ${err.message}`, 'error');
      await loadDataCenter();
    } finally {
      btn.textContent = old;
      btn.disabled = false;
    }
  };

  document.getElementById('btnDcIncrSync').onclick = async () => {
    const btn = document.getElementById('btnDcIncrSync');
    const dateVal = dcSyncDate.value;
    if (!dateVal) { setStatus('请选择同步日期', 'error'); return; }
    btn.disabled = true;
    const old = btn.textContent;
    btn.textContent = '同步中...';
    setStatus(`增量同步 ${dateVal} 执行中...`, 'info');
    try {
      const payload = await request(`/api/jobs/kline-cache/incremental-sync?trade_date=${dateVal}&trigger_mode=manual`, { method: 'POST' });
      setStatus(`增量同步完成: ${dateVal} · ${payload.symbol_count || 0}/${payload.total_symbols || 0}`, 'success');
      await loadDataCenter();
    } catch (err) {
      setStatus(`增量同步失败: ${err.message}`, 'error');
      await loadDataCenter();
    } finally {
      btn.textContent = old;
      btn.disabled = false;
    }
  };

  document.getElementById('btnDcCheck').onclick = async () => {
    const btn = document.getElementById('btnDcCheck');
    btn.disabled = true;
    const old = btn.textContent;
    btn.textContent = '检查中...';
    setStatus('数据完整性检查中...', 'info');
    try {
      const report = await request('/api/jobs/kline-cache/check', { method: 'POST' });
      setStatus(`检查完成: 覆盖率 ${report.coverage_pct || 0}% 缺失 ${report.total_missing || 0} 条`, 'success');
      await loadDataCenter();
    } catch (err) {
      setStatus(`检查失败: ${err.message}`, 'error');
    } finally {
      btn.textContent = old;
      btn.disabled = false;
    }
  };

  document.getElementById('clearConcept').onclick = () => {
    state.selectedConcept = null;
    renderHotConcepts();
    renderFunnel();
  };

  document.getElementById('btnEodScreen').onclick = async () => {
    const btn = document.getElementById('btnEodScreen');
    btn.disabled = true;
    const old = btn.textContent;
    btn.textContent = '执行中...';
    setStatus('盘后筛选执行中...', 'info');
    try {
      const payload = await request('/api/jobs/eod-screen', { method: 'POST' });
      await reloadFunnel();
      setStatus(`盘后筛选完成: 候选${payload.candidate_count || 0}只`, 'success');
    } catch (err) {
      setStatus(`盘后筛选失败: ${err.message}`, 'error');
    } finally {
      btn.textContent = old;
      btn.disabled = false;
    }
  };

  document.getElementById('btnNoticeScreen').onclick = async () => {
    const btn = document.getElementById('btnNoticeScreen');
    btn.disabled = true;
    const old = btn.textContent;
    btn.textContent = '执行中...';
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
      btn.textContent = old;
      btn.disabled = false;
    }
  };

  document.getElementById('btnAgentRun').onclick = async () => {
    const btn = document.getElementById('btnAgentRun');
    btn.disabled = true;
    btn.textContent = '运行中...';
    setStatus('Hermes 复盘执行中...', 'info');
    try {
      const payload = await request('/api/agent/run', { method: 'POST', body: JSON.stringify({ task_type: 'full_diagnosis' }) });
      setStatus(`复盘完成: ${payload.summary?.proposals_created || 0} 个提案`, 'success');
      await loadAgentData();
    } catch (err) {
      setStatus(`复盘失败: ${err.message}`, 'error');
    } finally {
      btn.textContent = '手动触发复盘';
      btn.disabled = false;
    }
  };

  const urlTab = new URLSearchParams(window.location.search).get('tab');
  if (urlTab && TAB_TITLES[urlTab]) switchTab(urlTab);

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
  const _origSwitchTab = switchTab;
  switchTab = function(tab) {
    _origSwitchTab(tab);
    if (tab === 'data') startDcPolling();
  };
}

init().catch((err) => {
  document.getElementById('meta').textContent = `初始化失败: ${err.message}`;
  renderChartPlaceholder(`初始化失败: ${err.message}`);
});
