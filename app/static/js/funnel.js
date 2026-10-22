/* Alpha frontend module: funnel.js — 策略选股 / 热门概念 / 热门个股渲染.
 * 无构建、classic script，按 index.html 中的顺序加载，函数挂在全局作用域。
 */

/* ==================== Funnel tab ==================== */

function passConceptFilter(stock) {
  if (!state.selectedConcept) return true;
  return (stock.concept_tags || []).some((t) => t.name === state.selectedConcept);
}

function _getStrategyScanHits() {
  return state.strategyScanSnapshot?.hits || [];
}

function _getCompositeCandidateCount() {
  const strategyHits = _getStrategyScanHits().length;
  const funnelCandidates = (state.funnel?.pools?.candidate || []).filter(passConceptFilter).length;
  return strategyHits + funnelCandidates;
}

function renderCounts() {
  if (!state.funnel) return;
  const c = _getCompositeCandidateCount();
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

// #57：闸门统一走东八区/服务端口径，不再用浏览器本地时钟。
function _isMarketOpen(now = new Date()) {
  return isChinaMarketOpen(now);
}

function _marketGateText(action, now = new Date()) {
  const info = getMarketSession(now);
  const sourceHint = info.source === 'server' ? '服务端口径' : '前端东八区兜底（不含节假日日历）';
  return `当前非交易时段（北京时间 ${info.parts.clock} · ${info.label} · ${sourceHint}），无法模拟${action}`;
}

async function paperBuy(symbol, name, _price) {
  if (!_isMarketOpen()) {
    setStatus(_marketGateText('买入'), 'error');
    return;
  }
  const qty = prompt(`模拟买入 ${name}(${symbol})\n将以当前实时价成交\n请输入买入股数:`, '100');
  if (!qty) return;
  try {
    const res = await request('/api/paper/buy', {
      method: 'POST',
      body: JSON.stringify({ symbol, name, qty: parseInt(qty) || 100 }),
    });
    if (res.success) {
      const p = res.position || {};
      setStatus(`模拟买入 ${name} ${qty}股 @ ${fmtNum(res.realtime_price, 2)} (成本含滑点: ${fmtNum(p.cost_price, 2)})`, 'success');
      loadPaperData();
    }
  } catch (err) {
    setStatus(`模拟买入失败: ${err.message}`, 'error');
  }
}

async function paperSell(positionId, symbol, name) {
  if (!_isMarketOpen()) {
    setStatus(_marketGateText('卖出'), 'error');
    return;
  }
  if (!confirm(`确认模拟卖出 ${name}(${symbol})？\n将以当前实时价成交`)) return;
  try {
    const res = await request('/api/paper/sell', {
      method: 'POST',
      body: JSON.stringify({ position_id: positionId }),
    });
    if (res.success) {
      const p = res.position || {};
      setStatus(`模拟卖出 ${name} @ ${fmtNum(res.realtime_price, 2)} · 盈亏 ${fmtNum(p.realized_pnl, 2)}`, 'success');
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
  if (poolName === 'candidate') {
    renderCompositeCandidatePool(root, list || []);
    return;
  }
  root.innerHTML = '';
  const filtered = list.filter(passConceptFilter);
  filtered.forEach((stock) => {
    const div = document.createElement('div');
    div.className = `stock-card ${state.selectedSymbol === stock.symbol ? 'active' : ''}`;
    div.onclick = () => selectSymbol(stock.symbol);
    const delta = Number(stock.score_delta || 0);
    const deltaCls = _chgCls(delta);
    const deltaTxt = delta > 0 ? `+${fmtNum(delta)}` : fmtNum(delta);
    const conceptTags = stock.concept_tags || [];
    const tags = conceptTags
      .map((tag) => `<span class="tag" style="background:${safeCssColor(tag.color)}" title="热度:${esc(tag.heat)} 涨幅:${fmtNum(tag.change_pct)} 涨停:${safeInt(tag.limit_up_count)}">${esc(tag.name)} ${fmtNum(tag.change_pct, 1)}%/${safeInt(tag.limit_up_count)}</span>`)
      .join('');
    const tagsHtml = tags ? `<div class="tags">${tags}</div>` : '';
    const badge = stock.recommended_pool
      ? `<span class="badge ${stock.recommended_pool === 'buy' ? 'buy' : 'focus'}">建议进入${stock.recommended_pool === 'buy' ? '买入池' : '重点池'}</span>`
      : '';
    const btns = cardActions(stock)
      .map(([txt, pool]) => `<button data-pool="${esc(pool)}" data-symbol="${esc(stock.symbol)}">${esc(txt)}</button>`)
      .join('');
    const simBuyBtn = poolName === 'buy'
      ? `<button class="btn-sim-buy" data-symbol="${esc(stock.symbol)}" data-name="${esc(stock.name)}" data-price="${Number(stock.price || stock.breakout_level) || 0}">模拟买入</button>`
      : '';
    const pct = Number(stock.pct_change || 0);
    const pctCls = _chgCls(pct);
    const pctSign = pct > 0 ? '+' : '';
    const price = Number(stock.price || 0);
    const priceHtml = price > 0 ? `<span class="stock-price">${fmtNum(price, 2)}</span>` : '';
    const pctHtml = pct !== 0 ? ` <span class="${pctCls}">${pctSign}${fmtNum(pct, 2)}%</span>` : '';
    div.innerHTML = `
      <div class="stock-top">
        <div class="stock-name">${esc(stock.name)} <span class="stock-code">${esc(stock.symbol)}</span></div>
        <div class="stock-price-area">${priceHtml}${pctHtml}</div>
      </div>
      ${tagsHtml}
      <div class="metrics">
        <span>评分 <b class="${deltaCls}">${fmtNum(stock.score)}</b> (${deltaTxt})</span>
        <span class="metrics-sep">·</span>
        <span>放量比 ${fmtNum(stock.volume_ratio, 2)}</span>
        <span class="metrics-sep">·</span>
        <span>突破位 ${fmtNum(stock.breakout_level, 2)}</span>
      </div>
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
      : '当前暂无系统候选';
    root.innerHTML = `<div class="empty-state"><div class="empty-state-text">${esc(hint)}</div></div>`;
  }
}

function _renderCompositeSection(title, hint, bodyHtml, count, kind = '') {
  const countHtml = `<span class="composite-section-count">${esc(count)}</span>`;
  return `
    <section class="composite-section ${esc(kind)}">
      <div class="composite-section-head">
        <div class="composite-section-title">${esc(title)}${countHtml}</div>
        <div class="composite-section-hint">${esc(hint)}</div>
      </div>
      <div class="composite-section-body">${bodyHtml}</div>
    </section>
  `;
}

function _strategyHitCardHtml(hit) {
  const cls = Number(hit.change_pct || 0) >= 0 ? 'up' : 'down';
  const sign = Number(hit.change_pct || 0) >= 0 ? '+' : '';
  const badges = (hit.rule_hits || []).slice(0, 4).map((rh) => (
    `<span class="sc-hit-tag">${esc(rh.title)}: ${esc(String(rh.label || '✓'))}</span>`
  )).join('');
  return `
    <div class="qb-card sc-hit-card sc-hit-card-inline" data-symbol="${esc(hit.symbol)}" data-name="${esc(hit.name || '')}">
      <div class="qb-card-top">
        <div class="qb-card-name">${esc(hit.name || '--')}<small>${esc(hit.symbol)}</small></div>
        <div class="qb-card-pct ${cls}">${sign}${fmtNum(hit.change_pct, 2)}%</div>
      </div>
      <div class="qb-card-meta">
        <span class="qb-tag">收 ${fmtNum(hit.close, 2)}</span>
        <span class="qb-tag spike">综合分 ${fmtNum(hit.composite_score, 1)}</span>
      </div>
      ${badges ? `<div class="sc-hit-tags">${badges}</div>` : ''}
    </div>
  `;
}

function renderCompositeCandidatePool(root, systemCandidates) {
  if (!root) return;
  const strategyHits = _getStrategyScanHits();
  const filteredSystem = (systemCandidates || []).filter(passConceptFilter);
  const parts = [];

  if (strategyHits.length) {
    parts.push(_renderCompositeSection(
      '策略中心命中',
      '来自当前策略的最新扫描结果',
      strategyHits.slice(0, 200).map(_strategyHitCardHtml).join(''),
      strategyHits.length,
      'strategy',
    ));
  } else {
    parts.push(_renderCompositeSection(
      '策略中心命中',
      '暂无策略命中，先运行上方扫描',
      '<div class="empty-state compact-empty"><div class="empty-state-text">当前策略还没有扫描命中结果</div></div>',
      0,
      'strategy',
    ));
  }

  if (filteredSystem.length) {
    const host = document.createElement('div');
    filteredSystem.forEach((stock) => {
      const div = document.createElement('div');
      div.className = `stock-card system-stock-card ${state.selectedSymbol === stock.symbol ? 'active' : ''}`;
      div.dataset.symbol = stock.symbol;
      div.dataset.name = stock.name || '';
      const delta = Number(stock.score_delta || 0);
      const deltaCls = _chgCls(delta);
      const deltaTxt = delta > 0 ? `+${fmtNum(delta)}` : fmtNum(delta);
      const conceptTags = stock.concept_tags || [];
      const tags = conceptTags
        .map((tag) => `<span class="tag" style="background:${safeCssColor(tag.color)}" title="热度:${esc(tag.heat)} 涨幅:${fmtNum(tag.change_pct)} 涨停:${safeInt(tag.limit_up_count)}">${esc(tag.name)} ${fmtNum(tag.change_pct, 1)}%/${safeInt(tag.limit_up_count)}</span>`)
        .join('');
      const tagsHtml = tags ? `<div class="tags">${tags}</div>` : '';
      const badge = stock.recommended_pool
        ? `<span class="badge ${stock.recommended_pool === 'buy' ? 'buy' : 'focus'}">建议进入${stock.recommended_pool === 'buy' ? '买入池' : '重点池'}</span>`
        : '';
      const btns = cardActions(stock)
        .map(([txt, pool]) => `<button data-pool="${esc(pool)}" data-symbol="${esc(stock.symbol)}">${esc(txt)}</button>`)
        .join('');
      const pct = Number(stock.pct_change || 0);
      const pctCls = _chgCls(pct);
      const pctSign = pct > 0 ? '+' : '';
      const price = Number(stock.price || 0);
      const priceHtml = price > 0 ? `<span class="stock-price">${fmtNum(price, 2)}</span>` : '';
      const pctHtml = pct !== 0 ? ` <span class="${pctCls}">${pctSign}${fmtNum(pct, 2)}%</span>` : '';
      div.innerHTML = `
        <div class="stock-top">
          <div class="stock-name">${esc(stock.name)} <span class="stock-code">${esc(stock.symbol)}</span></div>
          <div class="stock-price-area">${priceHtml}${pctHtml}</div>
        </div>
        ${tagsHtml}
        <div class="metrics">
          <span>评分 <b class="${deltaCls}">${fmtNum(stock.score)}</b> (${deltaTxt})</span>
          <span class="metrics-sep">·</span>
          <span>放量比 ${fmtNum(stock.volume_ratio, 2)}</span>
          <span class="metrics-sep">·</span>
          <span>突破位 ${fmtNum(stock.breakout_level, 2)}</span>
        </div>
        ${badge}
        <div class="card-actions">${btns}</div>
      `;
      host.appendChild(div);
    });
    parts.push(_renderCompositeSection(
      '系统候选',
      state.selectedConcept ? `当前概念：${state.selectedConcept}` : '来自系统漏斗的调整期候选',
      host.innerHTML,
      filteredSystem.length,
      'system',
    ));
  } else {
    const hint = state.selectedConcept
      ? `当前概念「${state.selectedConcept}」下无系统候选`
      : '当前暂无系统候选';
    parts.push(_renderCompositeSection(
      '系统候选',
      '来自系统漏斗的调整期候选',
      `<div class="empty-state compact-empty"><div class="empty-state-text">${esc(hint)}</div></div>`,
      0,
      'system',
    ));
  }

  root.innerHTML = parts.join('');
  root.querySelectorAll('.system-stock-card').forEach((card) => {
    card.onclick = () => selectSymbol(card.dataset.symbol);
    card.querySelectorAll('button[data-pool]').forEach((btn) => {
      btn.onclick = (e) => {
        e.stopPropagation();
        movePool(btn.dataset.symbol, btn.dataset.pool);
      };
    });
  });
  root.querySelectorAll('.sc-hit-card').forEach((el) => {
    el.onclick = (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      openPredictDetail(el.getAttribute('data-symbol'), el.getAttribute('data-name'));
      return false;
    };
  });
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
  if (activeChip) {
    activeChip.textContent = state.selectedConcept || '全部';
    activeChip.classList.toggle('muted', !state.selectedConcept);
    activeChip.classList.toggle('active', !!state.selectedConcept);
  }
  const filterHint = document.getElementById('conceptFilterHint');
  if (filterHint) {
    filterHint.textContent = state.selectedConcept ? `已筛选「${state.selectedConcept}」` : '';
  }
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
      <div class="hot-title"><span>${esc(item.name)}</span><span class="${cls}">${sign}${fmtNum(item.change_pct, 2)}%</span></div>
      <div class="hot-meta">热度 ${fmtNum(item.heat, 3)} · 涨停 ${safeInt(item.limit_up_count)} · 上涨 ${safeInt(item.up_count)} / 下跌 ${safeInt(item.down_count)}</div>
      <div class="hot-meta">领涨 ${esc(item.leader || '-')} · 入选 ${safeInt(item.selected_count)}</div>
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
    const dayPct = Number(item.change_pct || 0);
    const tenPct = Number(item.cumulative_10d_pct || 0);
    const dayCls = dayPct >= 0 ? 'up' : 'down';
    const tenCls = tenPct >= 0 ? 'up' : 'down';
    const daySign = dayPct >= 0 ? '+' : '';
    const tenSign = tenPct >= 0 ? '+' : '';
    const card = document.createElement('div');
    card.className = `hot-stock-item ${state.selectedHotSymbol === item.symbol ? 'active' : ''}`;
    card.onclick = () => selectHotStock(item);
    card.innerHTML = `
      <div class="hot-stock-main"><div class="hot-stock-rank">#${safeInt(item.rank)}</div><div class="hot-stock-name">${esc(item.name)} (${esc(item.symbol)})</div></div>
      <div class="hot-stock-side">
        <span class="hot-stock-price">${fmtNum(item.latest_price, 2)}</span>
        <span class="${dayCls}">${daySign}${fmtNum(dayPct, 2)}%</span>
        <span class="${tenCls}">${tenSign}${fmtNum(tenPct, 2)}%</span>
      </div>
    `;
    root.appendChild(card);
  });
  if (!items.length) root.innerHTML = '<div class="empty-state"><div class="empty-state-text">暂无热门个股数据</div></div>';
}

