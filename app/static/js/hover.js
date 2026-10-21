/* Alpha frontend module: hover.js — 悬停 K线浮窗.
 * 无构建、classic script，按 index.html 中的顺序加载，函数挂在全局作用域。
 */

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
            titleEl.innerHTML = `${esc(name)}(${esc(symbol)}) · <span class="${cls}">预测3日 ${sign}${chg}%</span>`;
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
