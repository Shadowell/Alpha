/* Alpha frontend module: notice-tab.js — 公告选股 Tab 渲染与详情.
 * 无构建、classic script，按 index.html 中的顺序加载，函数挂在全局作用域。
 */

/* ==================== Notice tab ==================== */

async function loadNoticeKeywords() {
  if (state.noticeKeywords.length) return;
  try {
    const data = await request('/api/notice/keywords');
    state.noticeKeywords = data.keywords || [];
  } catch { state.noticeKeywords = []; }
  renderKeywordTags();
}

function _passNoticeKeywordFilter(stock) {
  if (!state.activeKeywords.size) return true;
  const reason = (stock.reason || '').toLowerCase();
  const title = (stock.title || '').toLowerCase();
  for (const kw of state.activeKeywords) {
    if (reason.includes(kw.toLowerCase()) || title.includes(kw.toLowerCase())) return true;
  }
  return false;
}

function renderKeywordTags() {
  const container = document.getElementById('keywordTags');
  if (!container) return;
  container.innerHTML = '';
  state.noticeKeywords.forEach(kw => {
    const tag = document.createElement('span');
    tag.className = 'keyword-tag' + (state.activeKeywords.has(kw.tag) ? ' active' : '');
    tag.textContent = kw.tag;
    tag.onclick = () => {
      if (state.activeKeywords.has(kw.tag)) {
        state.activeKeywords.delete(kw.tag);
      } else {
        state.activeKeywords.add(kw.tag);
      }
      renderKeywordTags();
      renderNoticeFunnel();
    };
    container.appendChild(tag);
  });
}

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
      .map(([label, pool]) => `<button data-symbol="${esc(stock.symbol)}" data-pool="${esc(pool)}">${esc(label)}</button>`)
      .join('');
    const simBuyBtn = poolName === 'buy'
      ? `<button class="btn-sim-buy" data-symbol="${esc(stock.symbol)}" data-name="${esc(stock.name)}">模拟买入</button>`
      : '';
    card.innerHTML = `
      <div class="stock-top">
        <div class="stock-name">${esc(stock.name)} (${esc(stock.symbol)})</div>
        <div class="score up">${fmtNum(stock.score)}</div>
      </div>
      <div class="metrics">${esc(stock.notice_type)} · ${esc(stock.notice_date)}</div>
      <div class="metrics">${esc(stock.title)}</div>
      <div class="metrics">理由: ${esc(stock.reason || '-')}</div>
      <div class="metrics">风险: ${esc(stock.risk || '-')}</div>
      <div class="card-actions">${actions}${simBuyBtn}</div>
    `;
    card.querySelectorAll('button[data-pool]').forEach((btn) => {
      btn.onclick = (e) => { e.stopPropagation(); moveNoticePool(btn.dataset.symbol, btn.dataset.pool); };
    });
    const sbBtn = card.querySelector('.btn-sim-buy');
    if (sbBtn) {
      sbBtn.onclick = (e) => { e.stopPropagation(); paperBuy(sbBtn.dataset.symbol, sbBtn.dataset.name, 0); };
    }
    root.appendChild(card);
  });
  if (!list.length) {
    const hint = state.activeKeywords.size > 0 ? '当前关键词下无匹配公告' : '尚未运行公告筛选';
    root.innerHTML = `<div class="empty-state"><div class="empty-state-text">${esc(hint)}</div></div>`;
  }
}

function renderNoticeFunnel() {
  if (!state.noticeFunnel) return;
  const filteredPools = {};
  for (const poolName of ['candidate', 'focus', 'buy']) {
    filteredPools[poolName] = (state.noticeFunnel.pools[poolName] || []).filter(_passNoticeKeywordFilter);
  }
  document.getElementById('notice-count-candidate').textContent = filteredPools.candidate.length;
  document.getElementById('notice-count-focus').textContent = filteredPools.focus.length;
  document.getElementById('notice-count-buy').textContent = filteredPools.buy.length;
  renderNoticePool('candidate', filteredPools.candidate);
  renderNoticePool('focus', filteredPools.focus);
  renderNoticePool('buy', filteredPools.buy);
  renderNoticeMeta();
  _renderNoticeSummaryBarFiltered(filteredPools);
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
    const kline = detail.kline || [];
    document.getElementById('noticeSummary').textContent = `${detail.name}(${detail.symbol}) 分数:${fmtNum(detail.score)} 池:${detail.pool}`;
    const first = (detail.notices || [])[0] || {};
    const kwHighlight = (text) => {
      if (!text || state.activeKeywords.size === 0) return esc(text || '-');
      let result = esc(text);
      for (const kw of state.activeKeywords) {
        const regex = new RegExp(kw.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi');
        result = result.replace(regex, '<span class="notice-keyword-hit">$&</span>');
      }
      return result;
    };
    document.getElementById('noticeDetail').innerHTML = `
      <div class="metrics"><b>${kwHighlight(first.title)}</b></div>
      <div class="metrics">类型: ${esc(first.notice_type || '-')}</div>
      <div class="metrics">理由: ${kwHighlight(detail.reason)}</div>
      <div class="metrics">风险: ${kwHighlight(detail.risk)}</div>
      <div class="metrics">${safeLinkHtml(first.url, '公告链接')}</div>
    `;
    document.getElementById('stockSummary').textContent = `${detail.name}(${detail.symbol}) 30日日K`;
    if (kline.length) {
      renderKlineChart(kline);
    } else {
      renderChartPlaceholder('K线数据未同步，请先执行数据同步');
    }
    _fetchAndRenderNoticePredict(symbol, detail.name);
    _scrollToRightPanel();
  } catch (err) {
    document.getElementById('noticeDetail').textContent = `加载失败: ${err.message}`;
    renderChartPlaceholder(`加载失败: ${err.message}`);
  }
}

async function _fetchAndRenderNoticePredict(symbol, name) {
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

