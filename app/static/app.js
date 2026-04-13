const state = {
  activeTab: 'funnel',
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
  rulesEngine: null,
  rulesDirty: {},
};

function fmtNum(v, digits = 2) {
  const n = Number(v || 0);
  return Number.isFinite(n) ? n.toFixed(digits) : '0.00';
}

function setMeta() {
  const meta = document.getElementById('meta');
  if (state.activeTab === 'funnel') {
    if (!state.funnel) { meta.textContent = '暂无数据'; return; }
    meta.textContent = `交易日 ${state.funnel.trade_date} · 更新 ${state.funnel.updated_at}`;
  } else {
    if (!state.noticeFunnel) { meta.textContent = '暂无数据'; return; }
    const nf = state.noticeFunnel;
    meta.textContent = `交易日 ${nf.trade_date} · 更新 ${nf.updated_at} · 打分源 ${nf.source} · LLM ${nf.llm_enabled ? '开启' : '关闭'}`;
  }
}

function setStatus(text, tone = 'info') {
  const el = document.getElementById('statusBar');
  if (!el) return;
  el.textContent = text;
  if (tone === 'error') {
    el.style.background = 'rgba(127, 29, 29, 0.25)';
    el.style.borderColor = 'rgba(239, 68, 68, 0.5)';
  } else if (tone === 'success') {
    el.style.background = 'rgba(20, 83, 45, 0.25)';
    el.style.borderColor = 'rgba(34, 197, 94, 0.5)';
  } else {
    el.style.background = 'rgba(30, 64, 175, 0.22)';
    el.style.borderColor = 'rgba(59, 130, 246, 0.45)';
  }
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

const TAB_TITLES = { funnel: '策略选股', notice: '公告选股', rules: '规则引擎' };

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
  document.getElementById('pageTitle').textContent = TAB_TITLES[tab] || 'Alpha';
  setMeta();
  if (tab === 'notice' && !state.noticeFunnel) {
    reloadNotice();
  }
  if (tab === 'rules' && !state.rulesEngine) {
    loadRulesEngine();
  }
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
  activeChip.textContent = state.selectedConcept || '全部';
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
      renderFunnel();
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

function renderSyncPanel() {
  const summary = document.getElementById('syncSummary');
  const bar = document.getElementById('syncProgressBar');
  const logsRoot = document.getElementById('syncLogs');
  const sync = state.syncStatus || {};
  const status = sync.status || 'idle';
  const synced = Number(sync.synced_symbols || 0);
  const total = Number(sync.total_symbols || 0);
  const pct = Number(sync.progress_pct || 0);
  summary.textContent = `状态:${status} · 进度:${synced}/${total} (${pct.toFixed(2)}%) · 最近:${sync.updated_at || '-'}`;
  bar.style.width = `${Math.max(0, Math.min(100, pct))}%`;
  const logs = state.syncLogs?.items || [];
  logsRoot.innerHTML = '';
  logs.slice(0, 8).forEach((item) => {
    const div = document.createElement('div');
    div.className = 'sync-log-item';
    div.textContent = `[${item.status}] ${item.trade_date} ${item.synced_symbols}/${item.total_symbols} (${item.trigger_mode}) ${item.message || ''}`;
    logsRoot.appendChild(div);
  });
  if (!logs.length) logsRoot.innerHTML = '<div class="detail-empty">暂无同步日志</div>';
}

/* ==================== Chart (shared) ==================== */

function ensureChart() {
  if (state.chart) return state.chart;
  const dom = document.getElementById('klineChart');
  if (!dom || !window.echarts) return null;
  state.chart = window.echarts.init(dom);
  window.addEventListener('resize', () => { if (state.chart) state.chart.resize(); });
  return state.chart;
}

function renderChartPlaceholder(text) {
  const chart = ensureChart();
  if (!chart) return;
  chart.clear();
  chart.setOption({
    animation: false, xAxis: { show: false }, yAxis: { show: false }, series: [],
    graphic: { type: 'text', left: 'center', top: 'middle', style: { text, fill: '#94a3b8', font: '14px sans-serif' } },
  });
}

function renderKlineChart(rows) {
  const chart = ensureChart();
  if (!chart) return;
  if (!rows.length) { renderChartPlaceholder('暂无K线数据'); return; }
  const categoryData = rows.map((x) => x.date);
  const candleData = rows.map((x) => [x.open, x.close, x.low, x.high]);
  const volumeData = rows.map((x, idx) => [idx, x.volume, x.close >= x.open ? 1 : -1]);
  chart.setOption({
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
  }, true);
}

function renderStockSummary(detail) {
  const root = document.getElementById('stockSummary');
  if (!detail) { root.textContent = '点击左侧股票查看'; return; }
  root.textContent = `${detail.name}(${detail.symbol})  现价:${fmtNum(detail.metrics?.price, 2)}  涨跌:${fmtNum(detail.metrics?.pct_change, 2)}%  放量比:${fmtNum(detail.metrics?.volume_ratio, 2)}  突破位:${fmtNum(detail.metrics?.breakout_level, 2)}`;
}

function renderStockSummaryLite(item, klinePayload) {
  const root = document.getElementById('stockSummary');
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
  } catch (err) {
    renderStockSummary(null);
    renderChartPlaceholder(`加载失败: ${err.message}`);
  }
}

async function selectHotStock(item) {
  state.selectedHotSymbol = item.symbol;
  renderHotStocks();
  renderChartPlaceholder('加载中...');
  try {
    let payload = await request(`/api/kline/${item.symbol}?days=30`);
    if (!payload?.items?.length) {
      try {
        const detail = await request(`/api/stock/${item.symbol}/detail?kline_days=30`);
        payload = { items: detail?.kline || [], count: Number(detail?.kline?.length || 0) };
      } catch (_) {}
    }
    renderStockSummaryLite(item, payload || {});
    renderKlineChart(payload?.items || []);
    setStatus(`热门个股 ${item.symbol} K线已加载`, 'success');
  } catch (err) {
    renderChartPlaceholder(`加载失败: ${err.message}`);
    setStatus(`热门个股加载失败: ${err.message}`, 'error');
  }
}

/* ==================== Notice tab ==================== */

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

/* ==================== Rules Engine ==================== */

async function loadRulesEngine() {
  const root = document.getElementById('rulesEngine');
  root.innerHTML = '<div class="rules-loading">加载规则引擎配置...</div>';
  try {
    state.rulesEngine = await request('/api/rules/engine');
    state.rulesDirty = {};
    renderRulesEngine();
  } catch (err) {
    root.innerHTML = `<div class="rules-loading">加载失败: ${err.message}</div>`;
  }
}

function formatDisplayValue(val, fieldMeta) {
  if (fieldMeta.display_divisor) {
    return Number((val / fieldMeta.display_divisor).toFixed(2));
  }
  return val;
}

function parseInputValue(inputVal, fieldMeta) {
  const n = Number(inputVal);
  if (fieldMeta.display_divisor) {
    return n * fieldMeta.display_divisor;
  }
  return n;
}

function renderRulesEngine() {
  const root = document.getElementById('rulesEngine');
  if (!state.rulesEngine) { root.innerHTML = '<div class="rules-loading">暂无数据</div>'; return; }
  const { config, groups } = state.rulesEngine;
  root.innerHTML = '';

  groups.forEach((group) => {
    const section = document.createElement('div');
    section.className = 'rule-group';
    const header = document.createElement('div');
    header.className = 'rule-group-header';
    header.innerHTML = `
      <div><span class="rule-group-title">${group.label}</span><span class="rule-group-desc">${group.description}</span></div>
      <span class="rule-group-toggle">&#9660;</span>
    `;
    header.onclick = () => section.classList.toggle('collapsed');

    const body = document.createElement('div');
    body.className = 'rule-group-body';

    group.fields.forEach((f) => {
      const val = config[f.key];
      if (f.type === 'boolean') {
        const wrap = document.createElement('div');
        wrap.className = 'rule-field';
        const toggle = document.createElement('div');
        toggle.className = 'rule-toggle';
        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.id = `rule-${f.key}`;
        cb.checked = !!val;
        cb.onchange = () => { state.rulesDirty[f.key] = cb.checked; markDirtyFields(); };
        const lbl = document.createElement('label');
        lbl.htmlFor = `rule-${f.key}`;
        lbl.textContent = f.label;
        toggle.appendChild(cb);
        toggle.appendChild(lbl);
        wrap.appendChild(toggle);
        body.appendChild(wrap);
      } else {
        const wrap = document.createElement('div');
        wrap.className = 'rule-field';
        const lbl = document.createElement('label');
        lbl.htmlFor = `rule-${f.key}`;
        lbl.textContent = f.label + (f.display_unit ? ` (${f.display_unit})` : f.unit ? ` (${f.unit})` : '');
        const inp = document.createElement('input');
        inp.type = 'number';
        inp.id = `rule-${f.key}`;
        inp.value = formatDisplayValue(val, f);
        inp.dataset.key = f.key;
        if (f.step != null) inp.step = f.display_divisor ? f.step / f.display_divisor : f.step;
        if (f.min != null) inp.min = f.display_divisor ? f.min / f.display_divisor : f.min;
        if (f.max != null) inp.max = f.display_divisor ? f.max / f.display_divisor : f.max;
        inp.oninput = () => {
          const newVal = parseInputValue(inp.value, f);
          if (newVal !== val) {
            state.rulesDirty[f.key] = newVal;
            inp.classList.add('changed');
          } else {
            delete state.rulesDirty[f.key];
            inp.classList.remove('changed');
          }
          markDirtyFields();
        };
        wrap.appendChild(lbl);
        wrap.appendChild(inp);
        body.appendChild(wrap);
      }
    });

    section.appendChild(header);
    section.appendChild(body);
    root.appendChild(section);
  });

  const actions = document.createElement('div');
  actions.className = 'rules-actions';
  actions.innerHTML = `
    <button id="btnSaveRules" class="btn-primary" disabled>保存修改</button>
    <button id="btnResetRules" class="btn-reset">恢复默认</button>
  `;
  root.appendChild(actions);

  document.getElementById('btnSaveRules').onclick = saveRules;
  document.getElementById('btnResetRules').onclick = resetRules;
}

function markDirtyFields() {
  const btn = document.getElementById('btnSaveRules');
  if (!btn) return;
  const hasDirty = Object.keys(state.rulesDirty).length > 0;
  btn.disabled = !hasDirty;
  btn.textContent = hasDirty ? `保存修改 (${Object.keys(state.rulesDirty).length}项)` : '保存修改';
}

async function saveRules() {
  if (!Object.keys(state.rulesDirty).length) return;
  const btn = document.getElementById('btnSaveRules');
  btn.disabled = true;
  btn.textContent = '保存中...';
  try {
    const result = await request('/api/rules/engine', {
      method: 'PUT',
      body: JSON.stringify(state.rulesDirty),
    });
    state.rulesEngine = result;
    state.rulesDirty = {};
    renderRulesEngine();
    setStatus('规则引擎参数已保存', 'success');
  } catch (err) {
    setStatus(`保存失败: ${err.message}`, 'error');
    btn.disabled = false;
    btn.textContent = '保存修改';
  }
}

async function resetRules() {
  if (!confirm('确定恢复所有参数为默认值？')) return;
  const btn = document.getElementById('btnResetRules');
  btn.disabled = true;
  btn.textContent = '重置中...';
  try {
    const defaultConfig = {};
    const groups = state.rulesEngine?.groups || [];
    groups.forEach((g) => g.fields.forEach((f) => { defaultConfig[f.key] = undefined; }));
    const result = await request('/api/rules/engine', {
      method: 'PUT',
      body: JSON.stringify({ _reset: true }),
    });
    state.rulesEngine = result;
    state.rulesDirty = {};
    renderRulesEngine();
    setStatus('规则引擎已恢复默认值', 'success');
  } catch (err) {
    setStatus(`重置失败: ${err.message}`, 'error');
    btn.disabled = false;
    btn.textContent = '恢复默认';
  }
}

/* ==================== Data loading ==================== */

async function reloadFunnel() {
  const [funnelRes, hotConceptsRes, hotStocksRes, syncRes, logsRes, strategyRes] = await Promise.allSettled([
    request('/api/funnel'),
    request('/api/market/hot-concepts'),
    request('/api/market/hot-stocks'),
    request('/api/jobs/kline-cache/progress'),
    request('/api/jobs/kline-cache/logs?page=1&page_size=20'),
    request('/api/strategy/profile'),
  ]);
  if (funnelRes.status === 'fulfilled') state.funnel = funnelRes.value;
  if (hotConceptsRes.status === 'fulfilled') state.hotConcepts = hotConceptsRes.value;
  if (hotStocksRes.status === 'fulfilled') state.hotStocks = hotStocksRes.value;
  if (syncRes.status === 'fulfilled') state.syncStatus = syncRes.value;
  if (logsRes.status === 'fulfilled') state.syncLogs = logsRes.value;
  if (strategyRes.status === 'fulfilled') state.strategyProfile = strategyRes.value;

  renderHotConcepts();
  renderHotStocks();
  renderFunnel();
  renderSyncPanel();
  if (state.strategyProfile?.name) setStatus(`规则引擎: ${state.strategyProfile.name}`, 'info');

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

  document.getElementById('btnRefreshSidebar').onclick = async (e) => {
    e.preventDefault();
    if (state.activeTab === 'funnel') {
      setStatus('刷新策略选股...', 'info');
      await request('/api/score/recompute', { method: 'POST', body: JSON.stringify({}) });
      await reloadFunnel();
      setStatus('策略选股已刷新', 'success');
    } else if (state.activeTab === 'notice') {
      await reloadNotice();
      setStatus('已刷新公告池', 'success');
    } else if (state.activeTab === 'rules') {
      await loadRulesEngine();
      setStatus('规则引擎配置已刷新', 'success');
    }
  };

  document.getElementById('btnEod').onclick = async () => {
    const btn = document.getElementById('btnEod');
    btn.disabled = true;
    const oldText = btn.textContent;
    btn.textContent = '执行中...';
    setStatus('盘后筛选执行中...', 'info');
    try {
      const payload = await request('/api/jobs/eod-screen', { method: 'POST' });
      await reloadFunnel();
      setStatus(`盘后筛选完成: 候选${payload.candidate_count || 0}只 · 来源${payload.source_used || '-'} · ${payload.elapsed_ms || 0}ms`, 'success');
    } catch (err) {
      setStatus(`盘后筛选失败: ${err.message}`, 'error');
    } finally {
      btn.textContent = oldText;
      btn.disabled = false;
    }
  };

  document.getElementById('btnRunNotice').onclick = async () => {
    const btn = document.getElementById('btnRunNotice');
    btn.disabled = true;
    const old = btn.textContent;
    btn.textContent = '执行中...';
    setStatus('公告筛选执行中...', 'info');
    if (state.activeTab !== 'notice') switchTab('notice');
    try {
      const today = new Date();
      const y = today.getFullYear();
      const m = String(today.getMonth() + 1).padStart(2, '0');
      const d = String(today.getDate()).padStart(2, '0');
      const payload = await request(`/api/jobs/notice-screen?notice_date=${y}${m}${d}&limit=50`, { method: 'POST' });
      await reloadNotice();
      setStatus(`公告筛选完成: ${payload.candidate_count || 0}只 · 源:${payload.source || '-'}`, 'success');
    } catch (err) {
      setStatus(`公告筛选失败: ${err.message}`, 'error');
    } finally {
      btn.textContent = old;
      btn.disabled = false;
    }
  };

  const syncDateInput = document.getElementById('syncDate');
  const today = new Date();
  syncDateInput.value = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`;

  document.getElementById('btnSyncKline').onclick = async () => {
    const btn = document.getElementById('btnSyncKline');
    btn.disabled = true;
    const old = btn.textContent;
    btn.textContent = '同步中...';
    setStatus('全量同步执行中...', 'info');
    try {
      const payload = await request('/api/jobs/kline-cache/sync?trigger_mode=manual&force=true', { method: 'POST' });
      setStatus(`全量同步完成: ${payload.symbol_count || 0}/${payload.total_symbols || 0}`, 'success');
      await reloadFunnel();
    } catch (err) {
      setStatus(`全量同步失败: ${err.message}`, 'error');
      await reloadFunnel();
    } finally {
      btn.textContent = old;
      btn.disabled = false;
    }
  };

  document.getElementById('btnIncrementalSync').onclick = async () => {
    const btn = document.getElementById('btnIncrementalSync');
    const dateVal = syncDateInput.value;
    if (!dateVal) { setStatus('请选择同步日期', 'error'); return; }
    btn.disabled = true;
    const old = btn.textContent;
    btn.textContent = '同步中...';
    setStatus(`增量同步 ${dateVal} 执行中...`, 'info');
    try {
      const payload = await request(`/api/jobs/kline-cache/incremental-sync?trade_date=${dateVal}&trigger_mode=manual`, { method: 'POST' });
      setStatus(`增量同步完成: ${dateVal} · ${payload.symbol_count || 0}/${payload.total_symbols || 0}`, 'success');
      await reloadFunnel();
    } catch (err) {
      setStatus(`增量同步失败: ${err.message}`, 'error');
      await reloadFunnel();
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

  const urlTab = new URLSearchParams(window.location.search).get('tab');
  if (urlTab && TAB_TITLES[urlTab]) switchTab(urlTab);

  await reload();
  connectWs();
  setInterval(async () => {
    try {
      const [sync, logs] = await Promise.all([
        request('/api/jobs/kline-cache/progress'),
        request('/api/jobs/kline-cache/logs?page=1&page_size=20'),
      ]);
      state.syncStatus = sync;
      state.syncLogs = logs;
      renderSyncPanel();
    } catch (_) {}
  }, 8000);
}

init().catch((err) => {
  document.getElementById('meta').textContent = `初始化失败: ${err.message}`;
  renderChartPlaceholder(`初始化失败: ${err.message}`);
});
