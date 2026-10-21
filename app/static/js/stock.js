/* Alpha frontend module: stock.js — 个股选择、详情与预测摘要.
 * 无构建、classic script，按 index.html 中的顺序加载，函数挂在全局作用域。
 */

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

