/* Alpha frontend module: charts.js — ECharts K线与预测图表.
 * 无构建、classic script，按 index.html 中的顺序加载，函数挂在全局作用域。
 */

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
        let html = `<div style="font-weight:600;margin-bottom:4px">${esc(k.date)}</div>`;
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
  root.innerHTML = `<span style="font-weight:600">${esc(item.name)}(${esc(item.symbol)})</span>`
    + `<span class="summary-sep" style="margin:0 6px;color:var(--text-dim)">·</span>`
    + `<span>现价 <b>${fmtNum(item.latest_price, 2)}</b></span>`
    + `<span class="summary-sep" style="margin:0 6px;color:var(--text-dim)">·</span>`
    + `<span class="${pctCls}">涨跌 ${pctSign}${fmtNum(pct, 2)}%</span>`
    + `<span class="summary-sep" style="margin:0 6px;color:var(--text-dim)">·</span>`
    + `<span>K线 ${Number(klinePayload?.count || 0)}日</span>`;
}

