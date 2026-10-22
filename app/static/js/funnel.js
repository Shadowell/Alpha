/* Alpha frontend module: funnel.js — 策略选股 / 热门概念 / 热门个股渲染.
 * 无构建、classic script，按 index.html 中的顺序加载，函数挂在全局作用域。
 */

/* ==================== Funnel tab ==================== */

const POOL_ENROLL_PATH = '/api/pool/enroll';
const POOL_MOVE_PATH = '/api/pool/move';
const POOL_LABELS = { candidate: '候选池', focus: '重点池', buy: '买入池' };

function _poolLabel(pool) {
  return POOL_LABELS[pool] || pool;
}

/* ==================== 概念筛选（#53） ==================== */
// 口径：概念标签既可能是后端的 [{name,color,...}] 对象，也可能是 ['name'] 字符串。
function _conceptTagNames(stock) {
  return (stock?.concept_tags || stock?.concepts || [])
    .map((tag) => (typeof tag === 'string' ? tag : (tag && tag.name) || ''))
    .filter(Boolean);
}

function passConceptFilter(stock) {
  if (!state.selectedConcept) return true;
  return _conceptTagNames(stock).includes(state.selectedConcept);
}

// 策略命中是否"带概念标签"决定筛选能否作用到该分区；不带标签时不假装已过滤。
function _hitsConceptCoverage(hits) {
  if (!hits.length) return 'empty';
  const tagged = hits.filter((hit) => _conceptTagNames(hit).length > 0).length;
  if (tagged === hits.length) return 'full';
  return tagged > 0 ? 'partial' : 'none';
}

// mode 语义：off=未选概念 · filtered=已按概念裁剪 · no-tags=命中无标签不裁剪 · partial-tags=标签不全
function _filterHitsByConcept(hits) {
  if (!state.selectedConcept) return { items: hits, mode: 'off', coverage: _hitsConceptCoverage(hits) };
  const coverage = _hitsConceptCoverage(hits);
  if (coverage === 'none') return { items: hits, mode: 'no-tags', coverage };
  if (coverage === 'partial') return { items: hits, mode: 'partial-tags', coverage };
  return { items: hits.filter(passConceptFilter), mode: 'filtered', coverage };
}

function _conceptSectionHint(mode) {
  if (mode === 'off') return '来自当前策略的最新扫描结果 · 与系统候选同级操作';
  if (mode === 'filtered') return `已按概念「${state.selectedConcept}」过滤策略命中`;
  if (mode === 'partial-tags') {
    return `策略命中仅部分带概念标签，暂不裁剪该分区（计数含全部命中，系统候选按「${state.selectedConcept}」过滤）`;
  }
  return `策略命中未带概念标签，暂不受筛选影响（计数含全部命中，系统候选按「${state.selectedConcept}」过滤）`;
}

function _getStrategyScanHits() {
  return state.strategyScanSnapshot?.hits || [];
}

// #50：只有"卡片上真的有可操作按钮"的命中才计入候选数；
// 后端入池接口未上线（404）后，命中分区降级为仅预览，不再虚报候选数。
function _strategyHitsAreActionable() {
  return !state.poolEnrollUnsupported;
}

function _getCompositeCandidateParts() {
  const hits = _getStrategyScanHits();
  const hitResult = _filterHitsByConcept(hits);
  const system = (state.funnel?.pools?.candidate || []).filter(passConceptFilter);
  const strategyCount = _strategyHitsAreActionable() ? hitResult.items.length : 0;
  return {
    hits,
    hitResult,
    systemCount: system.length,
    strategyCount,
    total: system.length + strategyCount,
  };
}

function _getCompositeCandidateCount() {
  return _getCompositeCandidateParts().total;
}

function renderCounts() {
  if (!state.funnel) return;
  const parts = _getCompositeCandidateParts();
  const candidateEl = document.getElementById('count-candidate');
  const f = (state.funnel.pools.focus || []).filter(passConceptFilter).length;
  const b = (state.funnel.pools.buy || []).filter(passConceptFilter).length;
  candidateEl.textContent = parts.total;
  candidateEl.dataset.system = parts.systemCount;
  candidateEl.dataset.strategy = parts.strategyCount;
  candidateEl.dataset.strategyHitTotal = parts.hits.length;
  candidateEl.dataset.conceptMode = parts.hitResult.mode;
  candidateEl.title = `候选 ${parts.total} = 系统候选 ${parts.systemCount} + 策略命中可入池 ${parts.strategyCount}`
    + (state.poolEnrollUnsupported ? '（入池接口未上线，策略命中仅预览）' : '');
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

async function movePool(symbol, targetPool, { silent = false } = {}) {
  try {
    const res = await request(POOL_MOVE_PATH, {
      method: 'POST',
      body: JSON.stringify({ symbol, target_pool: targetPool }),
    });
    if (!res.success && !silent) alert(res.message || '迁移失败');
    await reloadFunnel();
    return res;
  } catch (err) {
    if (!silent) alert(err.message || '迁移失败');
    return { success: false, message: err.message };
  }
}

/* ==================== 策略命中入池（#50） ==================== */
// 目标语义：策略命中 → 入池（后端 POST /api/pool/enroll 写入候选池）→ 再逐级晋升重点池。
// 后端尚未提供该接口时（404），降级尝试 /api/pool/move 并给出可理解的原因，禁止静默失败。
async function enrollStrategyHit(symbol, name, targetPool = 'focus', btn = null) {
  if (btn) { btn.disabled = true; btn.dataset.prevText = btn.textContent; btn.textContent = '入池中…'; }
  const restore = () => {
    if (btn) { btn.disabled = false; btn.textContent = btn.dataset.prevText || '加入重点池'; }
  };
  try {
    const res = await request(POOL_ENROLL_PATH, {
      method: 'POST',
      body: JSON.stringify({ symbol, name, source: 'strategy_scan' }),
    });
    if (!res || res.success === false) {
      setStatus(`加入${_poolLabel(targetPool)}失败：${(res && res.message) || '后端拒绝入池'}`, 'error');
      restore();
      return res || { success: false };
    }
    const enrolledPool = res.pool || 'candidate';
    if (enrolledPool !== targetPool && targetPool) {
      const moved = await movePool(symbol, targetPool, { silent: true });
      if (!moved || moved.success === false) {
        setStatus(`已入${_poolLabel(enrolledPool)}，但推进${_poolLabel(targetPool)}失败：${(moved && moved.message) || '未知原因'}`, 'error');
        restore();
        await reloadFunnel();
        return moved;
      }
    }
    setStatus(`${name || symbol} 已加入${_poolLabel(targetPool)}（来源：策略命中）`, 'success');
    restore();
    await reloadFunnel();
    return { success: true, symbol, pool: targetPool };
  } catch (err) {
    restore();
    if (err && err.status === 404) return _fallbackEnrollViaMove(symbol, name, targetPool, err);
    const reason = err && err.code === 'timeout'
      ? `入池请求超时（${Math.round((err.timeoutMs || 0) / 1000)}s）`
      : `入池请求失败：${err.message}`;
    setStatus(`${reason}，请稍后重试或改用系统候选`, 'error');
    return { success: false, message: reason };
  }
}

async function _fallbackEnrollViaMove(symbol, name, targetPool, enrollErr) {
  // 记住"该后端版本没有入池接口"，让候选计数与卡片状态保持诚实。
  state.poolEnrollUnsupported = true;
  state.poolEnrollError = `${POOL_ENROLL_PATH} 返回 404，后端入池接口尚未上线`;
  const moved = await movePool(symbol, targetPool, { silent: true });
  if (moved && moved.success) {
    setStatus(`入池接口未上线，已用迁移接口把 ${name || symbol} 推进${_poolLabel(targetPool)}`, 'success');
    await reloadFunnel();
    return moved;
  }
  setStatus(
    `${state.poolEnrollError}；该股也不在系统选股池中（${(moved && moved.message) || '迁移接口拒绝'}），`
    + `因此无法加入${_poolLabel(targetPool)}。需后端先支持 POST /api/pool/enroll`,
    'error',
  );
  renderFunnel();
  return { success: false, message: state.poolEnrollError };
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
    return;
  }
  _applyLongListCollapse(root, POOL_LIST_COLLAPSE_THRESHOLD, `pool-${poolName}`);
}

function _renderCompositeSection(title, hint, bodyHtml, count, kind = '', meta = {}) {
  const countHtml = `<span class="composite-section-count">${esc(count)}</span>`;
  const actionableAttr = meta.actionable === undefined ? '' : ` data-actionable="${meta.actionable ? '1' : '0'}"`;
  const coverageAttr = meta.coverage ? ` data-concept-coverage="${esc(meta.coverage)}" data-concept-mode="${esc(meta.mode || '')}"` : '';
  return `
    <section class="composite-section ${esc(kind)}"${actionableAttr}${coverageAttr}>
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
  const tagNames = _conceptTagNames(hit);
  const tagsHtml = tagNames.length
    ? `<div class="tags">${tagNames.slice(0, 3).map((t) => `<span class="tag">${esc(t)}</span>`).join('')}</div>`
    : '';
  // #50：命中卡片给出一致的操作区（可入池 / 查看预测），并保留卡片本体的预测 modal。
  // 后端入池接口缺失时按钮保留但禁用并说明原因，避免"有卡片无操作"的假象。
  const actionable = _strategyHitsAreActionable();
  const enrollBtn = actionable
    ? `<button class="btn-enroll-focus" data-enroll-symbol="${esc(hit.symbol)}" data-enroll-name="${esc(hit.name || '')}">加入重点池</button>`
    : `<button class="btn-enroll-focus" disabled data-enroll-symbol="${esc(hit.symbol)}" data-enroll-name="${esc(hit.name || '')}" title="${esc(state.poolEnrollError || '入池接口不可用')}">入池接口未上线</button>`;
  const predictBtn = `<button class="btn-hit-predict" data-predict-symbol="${esc(hit.symbol)}" data-predict-name="${esc(hit.name || '')}">查看预测</button>`;
  const actions = `${enrollBtn}${predictBtn}`;
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
      ${tagsHtml}
      ${badges ? `<div class="sc-hit-tags">${badges}</div>` : ''}
      <div class="card-actions sc-hit-actions">${actions}</div>
    </div>
  `;
}

function renderCompositeCandidatePool(root, systemCandidates) {
  if (!root) return;
  const hits = _getStrategyScanHits();
  const hitResult = _filterHitsByConcept(hits);
  const filteredSystem = (systemCandidates || []).filter(passConceptFilter);
  const parts = [];
  const strategyHint = _conceptSectionHint(hitResult.mode);
  const bodyCards = hitResult.items.slice(0, 200).map(_strategyHitCardHtml).join('');

  parts.push(_renderCompositeSection(
    '策略中心命中',
    strategyHint,
    hitResult.items.length
      ? bodyCards
      : `<div class="empty-state compact-empty"><div class="empty-state-text">${
          state.selectedConcept
            ? `当前概念「${esc(state.selectedConcept)}」下无策略命中`
            : '当前策略还没有扫描命中结果'
        }</div></div>`,
    hitResult.items.length,
    'strategy',
    { actionable: _strategyHitsAreActionable(), coverage: hitResult.coverage, mode: hitResult.mode },
  ));

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
      state.selectedConcept ? `已按概念「${state.selectedConcept}」过滤系统候选` : '来自系统漏斗的调整期候选',
      host.innerHTML,
      filteredSystem.length,
      'system',
      { mode: state.selectedConcept ? 'filtered' : 'off', coverage: 'full' },
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
      { mode: state.selectedConcept ? 'filtered' : 'off', coverage: 'full' },
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
  // #50：操作按钮必须阻断冒泡，否则卡片本体的预测 modal 会被顺带打开。
  root.querySelectorAll('.sc-hit-card button[data-enroll-symbol]').forEach((btn) => {
    btn.onclick = (e) => {
      e.preventDefault();
      e.stopPropagation();
      if (btn.disabled) return false;
      enrollStrategyHit(btn.dataset.enrollSymbol, btn.dataset.enrollName, 'focus', btn);
      return false;
    };
  });
  root.querySelectorAll('.sc-hit-card button[data-predict-symbol]').forEach((btn) => {
    btn.onclick = (e) => {
      e.preventDefault();
      e.stopPropagation();
      openPredictDetail(btn.dataset.predictSymbol, btn.dataset.predictName);
      return false;
    };
  });
  // #56：命中/系统候选两个分区都按阈值折叠，避免一次性铺几百张卡片。
  root.querySelectorAll('.composite-section').forEach((section) => {
    const kind = section.classList.contains('strategy') ? 'strategy' : 'system';
    _applyLongListCollapse(
      section.querySelector('.composite-section-body'),
      POOL_LIST_COLLAPSE_THRESHOLD,
      `composite-${kind}`,
    );
  });
  root.querySelectorAll('.sc-hit-card').forEach((el) => {
    el.onclick = (ev) => {
      if (ev.target.closest('button')) return;
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

