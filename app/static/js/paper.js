/* Alpha frontend module: paper.js — 模拟盘.
 * 无构建、classic script，按 index.html 中的顺序加载，函数挂在全局作用域。
 */

/* ==================== 模拟盘 ==================== */

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

function _startPaperPoll() {
  Polling.start('paper-prices');
}

function _stopPaperPoll() {
  Polling.stop('paper-prices');
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
        <button class="btn-primary btn-sm" data-goto-tab="funnel">前往策略选股</button>
        <button class="btn-secondary btn-sm" data-goto-tab="notice">前往公告选股</button>
      </div>
    </div>`;
    root.querySelectorAll('[data-goto-tab]').forEach((btn) => {
      btn.onclick = () => switchTab(btn.dataset.gotoTab);
    });
    return;
  }
  positions.forEach(p => {
    const cls = pnlCls(p.pnl);
    const sign = p.pnl > 0 ? '+' : '';
    const div = document.createElement('div');
    div.className = 'paper-card';
    div.innerHTML = `
      <div class="paper-card-top">
        <div class="paper-card-name">${esc(p.name)} (${esc(p.symbol)})</div>
        <div class="paper-card-pnl ${cls}">${sign}¥${fmtNum(p.pnl, 2)} (${sign}${fmtNum(p.pnl_pct, 2)}%)</div>
      </div>
      <div class="paper-card-info">
        <span>成本 ${fmtNum(p.cost_price, 2)}</span>
        <span>现价 ${fmtNum(p.current_price, 2)}</span>
        <span>数量 ${safeInt(p.qty)}股</span>
        <span>市值 ¥${fmtNum(p.current_price * p.qty, 2)}</span>
        <span>开仓 ${fmtShortTime(p.opened_at)}</span>
      </div>
      <div class="paper-card-actions">
        <button class="btn-sell" data-id="${esc(p.id)}" data-symbol="${esc(p.symbol)}" data-name="${esc(p.name)}">模拟卖出</button>
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
      <td class="paper-hist-name">${esc(p.name)}<br><code>${esc(p.symbol)}</code></td>
      <td>${safeInt(p.qty)}</td>
      <td>${fmtNum(p.cost_price, 2)}</td>
      <td>${fmtNum(p.close_price, 2)}</td>
      <td class="${cls}">${sign}¥${fmtNum(rpnl, 2)}${pctStr}</td>
      <td title="${esc(p.opened_at || '')}">${esc(fmtShortTime(p.opened_at))}</td>
      <td title="${esc(p.closed_at || '')}">${esc(fmtShortTime(p.closed_at))}</td>
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
      <td class="paper-trade-time">${esc(time)}</td>
      <td class="paper-hist-name">${esc(t.name)}<br><code>${esc(t.symbol)}</code></td>
      <td><span class="${esc(actionCls)}">${esc(actionText)}</span></td>
      <td>${safeInt(t.qty)}</td>
      <td>${fmtNum(t.price, 4)}</td>
      <td>¥${fmtNum(t.fee, 2)}</td>
    </tr>`;
  });
  html += '</tbody></table>';
  root.innerHTML = html;
}

