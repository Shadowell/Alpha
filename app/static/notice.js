const state = {
  funnel: null,
  selectedSymbol: null,
  chart: null,
};

function fmtNum(v, digits = 2) {
  const n = Number(v || 0);
  return Number.isFinite(n) ? n.toFixed(digits) : '0.00';
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

function setMeta() {
  const meta = document.getElementById('meta');
  if (!state.funnel) {
    meta.textContent = '暂无数据';
    return;
  }
  meta.textContent = `交易日 ${state.funnel.trade_date} · 更新 ${state.funnel.updated_at} · 打分源 ${state.funnel.source} · LLM ${state.funnel.llm_enabled ? '开启' : '关闭'}`;
}

function renderCounts() {
  if (!state.funnel) return;
  document.getElementById('count-candidate').textContent = state.funnel.stats.candidate;
  document.getElementById('count-focus').textContent = state.funnel.stats.focus;
  document.getElementById('count-buy').textContent = state.funnel.stats.buy;
}

async function movePool(symbol, target_pool) {
  try {
    await request('/api/notice/pool/move', {
      method: 'POST',
      body: JSON.stringify({ symbol, target_pool }),
    });
    setStatus(`已迁移 ${symbol} -> ${target_pool}`, 'success');
    await reload();
  } catch (err) {
    setStatus(`迁移失败: ${err.message}`, 'error');
  }
}

function cardActions(stock) {
  if (stock.pool === 'candidate') return [['加入重点', 'focus'], ['加入买入', 'buy']];
  if (stock.pool === 'focus') return [['移回候选', 'candidate'], ['加入买入', 'buy']];
  return [['降级重点', 'focus']];
}

function renderPool(poolName, list) {
  const root = document.getElementById(`pool-${poolName}`);
  root.innerHTML = '';
  list.forEach((stock) => {
    const card = document.createElement('div');
    card.className = `stock-card ${state.selectedSymbol === stock.symbol ? 'active' : ''}`;
    card.onclick = () => selectSymbol(stock.symbol);
    const actions = cardActions(stock)
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
      btn.onclick = (e) => {
        e.stopPropagation();
        movePool(btn.dataset.symbol, btn.dataset.pool);
      };
    });
    root.appendChild(card);
  });
  if (!list.length) root.innerHTML = '<div class="detail-empty">暂无股票</div>';
}

function ensureChart() {
  if (state.chart) return state.chart;
  const dom = document.getElementById('klineChart');
  if (!dom || !window.echarts) return null;
  state.chart = window.echarts.init(dom);
  window.addEventListener('resize', () => state.chart && state.chart.resize());
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
      style: { text, fill: '#94a3b8', font: '14px sans-serif' },
    },
  });
}

function renderKlineChart(rows) {
  const chart = ensureChart();
  if (!chart) return;
  if (!rows.length) {
    renderChartPlaceholder('暂无K线数据');
    return;
  }
  const categoryData = rows.map((x) => x.date);
  const candleData = rows.map((x) => [x.open, x.close, x.low, x.high]);
  const volumeData = rows.map((x, idx) => [idx, x.volume, x.close >= x.open ? 1 : -1]);
  chart.setOption(
    {
      animation: false,
      backgroundColor: 'transparent',
      legend: { show: false },
      tooltip: { trigger: 'axis', axisPointer: { type: 'cross' } },
      axisPointer: { link: [{ xAxisIndex: 'all' }], label: { backgroundColor: '#334155' } },
      grid: [
        { left: 52, right: 18, top: 14, height: '62%' },
        { left: 52, right: 18, top: '77%', height: '16%' },
      ],
      xAxis: [
        { type: 'category', data: categoryData, boundaryGap: true, axisLine: { lineStyle: { color: '#475569' } }, axisLabel: { color: '#64748b' }, splitLine: { show: false } },
        { type: 'category', gridIndex: 1, data: categoryData, boundaryGap: true, axisLine: { lineStyle: { color: '#475569' } }, axisLabel: { show: false }, splitLine: { show: false } },
      ],
      yAxis: [
        { scale: true, axisLine: { lineStyle: { color: '#475569' } }, axisLabel: { color: '#64748b' }, splitLine: { lineStyle: { color: '#1f2937' } } },
        { scale: true, gridIndex: 1, splitNumber: 2, axisLine: { lineStyle: { color: '#475569' } }, axisLabel: { color: '#64748b' }, splitLine: { lineStyle: { color: '#1f2937' } } },
      ],
      dataZoom: [
        { type: 'inside', xAxisIndex: [0, 1], start: 0, end: 100 },
        { show: true, xAxisIndex: [0, 1], type: 'slider', bottom: '1%', borderColor: '#334155' },
      ],
      series: [
        {
          type: 'candlestick',
          data: candleData,
          itemStyle: { color: '#ef4444', color0: '#16a34a', borderColor: '#ef4444', borderColor0: '#16a34a' },
        },
        {
          type: 'bar',
          xAxisIndex: 1,
          yAxisIndex: 1,
          data: volumeData,
          itemStyle: { color: (p) => (p.data[2] > 0 ? '#ef4444' : '#16a34a') },
        },
      ],
    },
    true,
  );
}

async function selectSymbol(symbol) {
  state.selectedSymbol = symbol;
  ['candidate', 'focus', 'buy'].forEach((p) => renderPool(p, state.funnel?.pools?.[p] || []));
  renderChartPlaceholder('加载中...');
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
      <div class="metrics"><a href="${first.url || '#'}" target="_blank">公告链接</a></div>
    `;
    document.getElementById('stockSummary').textContent = `${detail.name}(${detail.symbol}) 30日日K`;
    renderKlineChart(kline);
  } catch (err) {
    document.getElementById('noticeDetail').textContent = `加载失败: ${err.message}`;
    renderChartPlaceholder(`加载失败: ${err.message}`);
  }
}

async function reload() {
  const payload = await request('/api/notice/funnel');
  state.funnel = payload;
  renderCounts();
  renderPool('candidate', payload.pools.candidate || []);
  renderPool('focus', payload.pools.focus || []);
  renderPool('buy', payload.pools.buy || []);
  setMeta();
  if (!state.selectedSymbol) {
    renderChartPlaceholder('点击左侧股票查看');
  }
}

async function init() {
  document.getElementById('btnGotoMain').onclick = () => { window.location.href = '/'; };
  document.getElementById('btnRefreshNotice').onclick = async () => {
    await reload();
    setStatus('已刷新公告池', 'success');
  };
  document.getElementById('btnRunNotice').onclick = async () => {
    const btn = document.getElementById('btnRunNotice');
    btn.disabled = true;
    const old = btn.textContent;
    btn.textContent = '执行中...';
    setStatus('公告筛选执行中...', 'info');
    try {
      const today = new Date();
      const y = today.getFullYear();
      const m = String(today.getMonth() + 1).padStart(2, '0');
      const d = String(today.getDate()).padStart(2, '0');
      const payload = await request(`/api/jobs/notice-screen?notice_date=${y}${m}${d}&limit=50`, { method: 'POST' });
      await reload();
      setStatus(`公告筛选完成: ${payload.candidate_count || 0}只 · 源:${payload.source || '-'}`, 'success');
    } catch (err) {
      setStatus(`公告筛选失败: ${err.message}`, 'error');
    } finally {
      btn.textContent = old;
      btn.disabled = false;
    }
  };
  await reload();
}

init().catch((err) => {
  setStatus(`初始化失败: ${err.message}`, 'error');
  renderChartPlaceholder(`初始化失败: ${err.message}`);
});
