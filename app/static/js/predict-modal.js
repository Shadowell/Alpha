/* Alpha frontend module: predict-modal.js — Kronos 预测弹窗与摘要.
 * 无构建、classic script，按 index.html 中的顺序加载，函数挂在全局作用域。
 */

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
  const badge = options.badge
    ? `<span class="${esc(options.badgeClass || 'predict-modal-badge')}">${esc(options.badge)}</span>`
    : '';
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
    document.getElementById('predictStatus').innerHTML = `<span class="error">预测失败: ${esc(err.message)}</span>`;
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
        let html = `<div style="font-weight:600;margin-bottom:4px">${esc(k.date)} ${tag}</div>`;
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
      <span class="predict-summary-label">第${i + 1}日预测收盘 (${esc(k.date)})</span>
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
    模型: ${esc(pred.model)} · 设备: ${esc(pred.device)} · 窗口: ${safeInt(pred.lookback)}→${safeInt(pred.horizon)} · 生成: ${esc((pred.generated_at || '').replace('T', ' '))}
  </div>`);

  container.innerHTML = items.join('');
}

function chgSign2(val, base) {
  if (!base) return { cls: '', txt: '' };
  const pct = (val - base) / base * 100;
  const sign = pct >= 0 ? '+' : '';
  return { cls: pct >= 0 ? 'up' : 'down', txt: `(${sign}${fmtNum(pct, 2)}%)` };
}

