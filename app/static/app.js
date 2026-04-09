const state = {
  funnel: null,
  hotConcepts: null,
  hotStocks: null,
  selectedSymbol: null,
  selectedConcept: null,
  chart: null,
};

function fmtNum(v, digits = 2) {
  const n = Number(v || 0);
  return Number.isFinite(n) ? n.toFixed(digits) : '0.00';
}

function setMeta() {
  const meta = document.getElementById('meta');
  if (!state.funnel) {
    meta.textContent = '暂无数据';
    return;
  }
  meta.textContent = `交易日 ${state.funnel.trade_date} · 更新 ${state.funnel.updated_at}`;
}

async function request(path, options = {}) {
  const resp = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
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
  if (stock.pool === 'candidate') {
    actions.push(['加入重点', 'focus']);
  }
  if (stock.pool === 'focus') {
    actions.push(['移回候选', 'candidate']);
    actions.push(['加入买入', 'buy']);
  }
  if (stock.pool === 'buy') {
    actions.push(['降级重点', 'focus']);
  }
  return actions;
}

async function movePool(symbol, targetPool) {
  try {
    const res = await request('/api/pool/move', {
      method: 'POST',
      body: JSON.stringify({ symbol, target_pool: targetPool }),
    });
    if (!res.success) {
      alert(res.message || '迁移失败');
    }
    await reload();
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
      btn.onclick = (e) => {
        e.stopPropagation();
        movePool(btn.dataset.symbol, btn.dataset.pool);
      };
    });

    root.appendChild(div);
  });

  if (!filtered.length) {
    root.innerHTML = '<div class="detail-empty">暂无股票</div>';
  }
}

function renderFunnel() {
  if (!state.funnel) return;
  renderCounts();
  renderPool('candidate', state.funnel.pools.candidate || []);
  renderPool('focus', state.funnel.pools.focus || []);
  renderPool('buy', state.funnel.pools.buy || []);
  setMeta();
}

function renderHotConcepts() {
  const root = document.getElementById('hotConcepts');
  root.innerHTML = '';
  const activeChip = document.getElementById('activeConcept');
  activeChip.textContent = state.selectedConcept || '全部';

  const items = state.hotConcepts?.items || [];
  items.forEach((item) => {
    const div = document.createElement('div');
    div.className = `hot-item ${state.selectedConcept === item.name ? 'active' : ''}`;
    div.onclick = () => {
      state.selectedConcept = state.selectedConcept === item.name ? null : item.name;
      renderHotConcepts();
      renderFunnel();
    };

    div.innerHTML = `
      <div class="hot-title">
        <span>${item.name}</span>
        <span>${fmtNum(item.change_pct, 2)}%</span>
      </div>
      <div class="hot-meta">热度 ${fmtNum(item.heat, 3)} · 涨停 ${item.limit_up_count} · 上涨 ${item.up_count} / 下跌 ${item.down_count}</div>
      <div class="hot-meta">领涨 ${item.leader || '-'} · 入选 ${item.selected_count}</div>
    `;
    root.appendChild(div);
  });

  if (!items.length) {
    root.innerHTML = '<div class="detail-empty">暂无概念数据</div>';
  }
}

function renderHotStocks() {
  const root = document.getElementById('hotStocks');
  root.innerHTML = '';
  const items = state.hotStocks?.items || [];

  items.forEach((item) => {
    const cls = Number(item.change_pct || 0) >= 0 ? 'up' : 'down';
    const sign = Number(item.change_pct || 0) >= 0 ? '+' : '';
    const card = document.createElement('div');
    card.className = 'hot-stock-item';
    card.innerHTML = `
      <div class="hot-stock-main">
        <div class="hot-stock-rank">#${item.rank}</div>
        <div class="hot-stock-name">${item.name} (${item.symbol})</div>
      </div>
      <div class="hot-stock-side ${cls}">
        <div>¥${fmtNum(item.latest_price, 2)}</div>
        <div>${sign}${fmtNum(item.change_pct, 2)}%</div>
      </div>
    `;
    root.appendChild(card);
  });

  if (!items.length) {
    root.innerHTML = '<div class="detail-empty">暂无热门个股数据</div>';
  }
}

function ensureChart() {
  if (state.chart) return state.chart;
  const dom = document.getElementById('klineChart');
  if (!dom || !window.echarts) return null;
  state.chart = window.echarts.init(dom);
  window.addEventListener('resize', () => {
    if (state.chart) state.chart.resize();
  });
  return state.chart;
}

function renderChartPlaceholder(text) {
  const chart = ensureChart();
  if (!chart) return;
  chart.clear();
  chart.setOption({
    animation: false,
    xAxis: { show: false },
    yAxis: { show: false },
    series: [],
    graphic: {
      type: 'text',
      left: 'center',
      top: 'middle',
      style: {
        text,
        fill: '#94a3b8',
        font: '14px sans-serif',
      },
    },
  });
}

function renderKlineChart(detail) {
  const chart = ensureChart();
  if (!chart) return;

  const rows = detail.kline || [];
  if (!rows.length) {
    renderChartPlaceholder('暂无K线数据');
    return;
  }

  const categoryData = rows.map((x) => x.date);
  const candleData = rows.map((x) => [x.open, x.close, x.low, x.high]);
  const volumeData = rows.map((x, idx) => {
    const up = x.close >= x.open ? 1 : -1;
    return [idx, x.volume, up];
  });

  chart.setOption(
    {
      animation: false,
      backgroundColor: 'transparent',
      legend: { show: false },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'cross' },
      },
      axisPointer: {
        link: [{ xAxisIndex: 'all' }],
        label: { backgroundColor: '#334155' },
      },
      grid: [
        { left: 52, right: 18, top: 14, height: '62%' },
        { left: 52, right: 18, top: '77%', height: '16%' },
      ],
      xAxis: [
        {
          type: 'category',
          data: categoryData,
          boundaryGap: true,
          axisLine: { lineStyle: { color: '#475569' } },
          axisLabel: { color: '#64748b' },
          splitLine: { show: false },
          min: 'dataMin',
          max: 'dataMax',
        },
        {
          type: 'category',
          gridIndex: 1,
          data: categoryData,
          boundaryGap: true,
          axisLine: { lineStyle: { color: '#475569' } },
          axisLabel: { show: false },
          splitLine: { show: false },
          min: 'dataMin',
          max: 'dataMax',
        },
      ],
      yAxis: [
        {
          scale: true,
          splitArea: { show: false },
          axisLine: { lineStyle: { color: '#475569' } },
          axisLabel: { color: '#64748b' },
          splitLine: { lineStyle: { color: '#1f2937' } },
        },
        {
          scale: true,
          gridIndex: 1,
          splitNumber: 2,
          axisLine: { lineStyle: { color: '#475569' } },
          axisLabel: { color: '#64748b' },
          splitLine: { lineStyle: { color: '#1f2937' } },
        },
      ],
      dataZoom: [
        { type: 'inside', xAxisIndex: [0, 1], start: 0, end: 100 },
        { show: true, xAxisIndex: [0, 1], type: 'slider', bottom: '1%', borderColor: '#334155' },
      ],
      series: [
        {
          name: '日K',
          type: 'candlestick',
          data: candleData,
          itemStyle: {
            color: '#ef4444',
            color0: '#2563eb',
            borderColor: '#ef4444',
            borderColor0: '#2563eb',
          },
        },
        {
          name: '成交量',
          type: 'bar',
          xAxisIndex: 1,
          yAxisIndex: 1,
          data: volumeData,
          itemStyle: {
            color: (params) => (params.data[2] > 0 ? '#ef4444' : '#2563eb'),
          },
        },
      ],
    },
    true,
  );
}

function renderStockSummary(detail) {
  const root = document.getElementById('stockSummary');
  if (!detail) {
    root.textContent = '点击左侧股票查看';
    return;
  }
  const price = fmtNum(detail.metrics?.price, 2);
  const pct = fmtNum(detail.metrics?.pct_change, 2);
  const volumeRatio = fmtNum(detail.metrics?.volume_ratio, 2);
  const breakout = fmtNum(detail.metrics?.breakout_level, 2);
  root.textContent = `${detail.name}(${detail.symbol})  现价:${price}  涨跌:${pct}%  放量比:${volumeRatio}  突破位:${breakout}`;
}

async function selectSymbol(symbol) {
  state.selectedSymbol = symbol;
  renderFunnel();
  renderStockSummary(null);
  renderChartPlaceholder('加载中...');

  try {
    const detail = await request(`/api/stock/${symbol}/detail?kline_days=30`);
    renderStockSummary(detail);
    renderKlineChart(detail);
  } catch (err) {
    renderStockSummary(null);
    renderChartPlaceholder(`加载失败: ${err.message}`);
  }
}

async function reload() {
  const [funnel, hotConcepts, hotStocks] = await Promise.all([
    request('/api/funnel'),
    request('/api/market/hot-concepts'),
    request('/api/market/hot-stocks'),
  ]);
  state.funnel = funnel;
  state.hotConcepts = hotConcepts;
  state.hotStocks = hotStocks;

  renderHotConcepts();
  renderHotStocks();
  renderFunnel();

  if (state.selectedSymbol) {
    const found = ['candidate', 'focus', 'buy']
      .flatMap((x) => state.funnel.pools[x] || [])
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

  ws.onclose = () => {
    setTimeout(connectWs, 2000);
  };
}

async function init() {
  document.getElementById('btnRefresh').onclick = async () => {
    await request('/api/score/recompute', { method: 'POST', body: JSON.stringify({}) });
    await reload();
  };

  document.getElementById('btnEod').onclick = async () => {
    const btn = document.getElementById('btnEod');
    btn.disabled = true;
    const oldText = btn.textContent;
    btn.textContent = '执行中...';
    try {
      await request('/api/jobs/eod-screen', { method: 'POST' });
      await reload();
      alert('盘后筛选完成');
    } catch (err) {
      alert(`盘后筛选失败: ${err.message}`);
    } finally {
      btn.textContent = oldText;
      btn.disabled = false;
    }
  };

  document.getElementById('clearConcept').onclick = () => {
    state.selectedConcept = null;
    renderHotConcepts();
    renderFunnel();
  };

  await reload();
  connectWs();
}

init().catch((err) => {
  document.getElementById('meta').textContent = `初始化失败: ${err.message}`;
  renderChartPlaceholder(`初始化失败: ${err.message}`);
});
