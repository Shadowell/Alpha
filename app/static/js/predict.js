/* Alpha frontend module: predict.js — 预测选股与热门智能.
 * 无构建、classic script，按 index.html 中的顺序加载，函数挂在全局作用域。
 */

/* ==================== 预测选股 ==================== */

function _startPredictPoll() {
  Polling.start('predict');
}

function _stopPredictPoll() {
  Polling.stop('predict');
}

function _startHotAiPoll() {
  Polling.start('hotai');
}

function _stopHotAiPoll() {
  Polling.stop('hotai');
}

async function loadPredictFunnel() {
  refreshKronosStatusBadge();
  _refreshDbEmptyBanners();
  try {
    const snap = await request('/api/predict-funnel');
    state.predictFunnel = snap;
    state.predictRunning = !!snap.running;
    renderPredictFunnel();
    if (state.activeTab === 'predict') setMeta();
  } catch (err) {
    const el = document.getElementById('predictMeta');
    if (el) el.textContent = `加载失败: ${err.message}`;
  }
}

/* ── Kronos 模型状态徽标 ── */
async function refreshKronosStatusBadge() {
  const badge = document.getElementById('kronosStatusBadge');
  if (!badge) return;
  try {
    const s = await request('/api/predict/kronos/status');
    if (s.loaded) {
      badge.className = 'kronos-badge ready';
      badge.textContent = 'Kronos 模型已就绪';
    } else if (s.loading) {
      badge.className = 'kronos-badge loading';
      badge.textContent = 'Kronos 模型加载中（首次需下载权重）…';
    } else {
      badge.className = 'kronos-badge muted';
      badge.textContent = 'Kronos 未加载 · 首次预测时自动下载模型';
    }
  } catch (_) {
    badge.className = 'kronos-badge muted';
    badge.textContent = 'Kronos 状态未知';
  }
}

/* ── 数据库未初始化引导横幅（漏斗 / 预测页） ── */
async function _refreshDbEmptyBanners() {
  try {
    let stats = state.dcStats;
    if (!stats || !Object.keys(stats).length) {
      if (!state._dbStatsPromise) {
        state._dbStatsPromise = request('/api/jobs/kline-cache/stats').catch(() => null);
      }
      stats = await state._dbStatsPromise;
      if (stats && Object.keys(stats).length) state.dcStats = stats;
    }
    const dbEmpty = !!stats && Object.keys(stats).length > 0
      && Number(stats.row_count || 0) === 0 && !stats.max_date;
    ['funnelDbEmptyBanner', 'predictDbEmptyBanner'].forEach((id) => {
      const el = document.getElementById(id);
      if (!el) return;
      el.style.display = dbEmpty ? '' : 'none';
      if (!dbEmpty) return;
      el.innerHTML = '<span>本地行情数据尚未初始化，选股与预测功能暂不可用</span>';
      const btn = document.createElement('button');
      btn.className = 'btn-outline btn-sm';
      btn.textContent = '去初始化 →';
      btn.onclick = () => switchTab('data');
      el.appendChild(btn);
    });
  } catch (_) {}
}

function _predictPctCls(pct) {
  if (pct >= 8) return 'up';
  if (pct >= 4) return 'up';
  if (pct >= 2) return 'up';
  if (pct <= -2) return 'down';
  return 'neutral';
}

function _predictCardHtml(e) {
  const pct = Number(e.pred_max_high_pct || 0);
  const cls = _predictPctCls(pct);
  const boards = (e.boards || []).slice(0, 3)
    .map(b => `<span class="predict-card-board-tag">${esc(b.name)}</span>`).join('');
  const sign = pct >= 0 ? '+' : '';
  return `
    <div class="pool-card" data-symbol="${esc(e.symbol)}">
      <div class="card-head">
        <div class="card-name">
          <span class="stock-name">${esc(e.name || e.symbol)}</span>
          <span class="stock-code">${esc(e.symbol)}</span>
        </div>
        <div class="card-score ${cls}">${sign}${pct.toFixed(1)}%</div>
      </div>
      <div class="predict-card-meta">
        <span class="predict-card-metric">今收 <b>${fmtNum(e.today_close)}</b></span>
        <span class="predict-card-metric">3日高 <b>${fmtNum(e.pred_max_high)}</b></span>
        <span class="predict-card-metric">末日 ${(Number(e.pred_last_close_pct) >= 0 ? '+' : '')}${fmtNum(e.pred_last_close_pct, 1)}%</span>
        <span class="predict-card-metric">均价 ${(Number(e.pred_avg_close_pct) >= 0 ? '+' : '')}${fmtNum(e.pred_avg_close_pct, 1)}%</span>
      </div>
      ${boards ? `<div class="predict-card-boards">${boards}</div>` : ''}
    </div>`;
}

function renderPredictFunnel() {
  const snap = state.predictFunnel;
  const metaEl = document.getElementById('predictMeta');
  const progEl = document.getElementById('predictProgress');
  const btn = document.getElementById('btnPredictRun');
  if (!snap) {
    if (metaEl) metaEl.textContent = '无数据';
    return;
  }
  const cfg = snap.config || {};
  const thC = document.getElementById('predict-th-candidate');
  const thF = document.getElementById('predict-th-focus');
  const thB = document.getElementById('predict-th-buy');
  if (thC) thC.textContent = `≥${cfg.threshold_candidate || 2}%`;
  if (thF) thF.textContent = `≥${cfg.threshold_focus || 4}%`;
  if (thB) thB.textContent = `≥${cfg.threshold_buy || 8}%`;

  const tog = document.getElementById('predictFeishuToggle');
  if (tog && typeof cfg.feishu_enabled === 'boolean') tog.checked = cfg.feishu_enabled;

  const pools = snap.pools || { candidate: [], focus: [], buy: [] };
  const renderPool = (id, list) => {
    const el = document.getElementById(id);
    const cnt = document.getElementById(`predict-count-${id.split('-').pop()}`);
    if (cnt) cnt.textContent = (list || []).length;
    if (!el) return;
    if (!list || list.length === 0) {
      el.innerHTML = `<div class="empty-pool">暂无</div>`;
      return;
    }
    el.innerHTML = list.map(_predictCardHtml).join('');
    el.querySelectorAll('.pool-card[data-symbol]').forEach((card) => {
      card.onclick = () => openPredictDetail(card.dataset.symbol);
    });
    // #56：预测池同样按阈值折叠长列表
    _applyLongListCollapse(el, POOL_LIST_COLLAPSE_THRESHOLD, id);
  };
  renderPool('predict-pool-candidate', pools.candidate);
  renderPool('predict-pool-focus', pools.focus);
  renderPool('predict-pool-buy', pools.buy);

  const prog = snap.progress || {};
  const running = !!snap.running;
  if (btn) {
    btn.disabled = running;
    btn.textContent = running ? '预测中…' : '立即预测';
  }
  if (progEl) {
    if (running) {
      progEl.textContent = `${prog.phase || '...'} ${prog.current || 0}/${prog.total || 0} ${prog.detail || ''}`;
    } else if (prog.error) {
      progEl.textContent = `失败：${prog.error}`;
    } else if (snap.meta && snap.meta.elapsed_sec) {
      progEl.textContent = `上次 ${snap.meta.trigger || '手动'} · ${snap.meta.elapsed_sec}s · 扫描${snap.meta.stocks_scanned || 0}股`;
    } else {
      progEl.textContent = '';
    }
  }
  if (metaEl) {
    const m = snap.meta || {};
    metaEl.textContent = `交易日 ${snap.trade_date || '--'} · 命中 ${m.entries_count || 0} · 板块${m.boards_used || 0}`;
  }
  const summary = document.getElementById('predictPageSummary');
  if (summary) {
    summary.innerHTML = [
      _psItem('候选', (pools.candidate || []).length),
      _psSep(),
      _psItem('关注', (pools.focus || []).length),
      _psSep(),
      _psItem('买入', (pools.buy || []).length, 'warning'),
      _psSep(),
      _psItem('板块', (snap.meta?.boards_used || 0)),
    ].join('');
  }
}

function _hotAiScoreCls(score) {
  const cfg = state.hotStockAI?.config || {};
  if (score >= Number(cfg.threshold_buy || 14.5)) return 'up';
  if (score >= Number(cfg.threshold_focus || 11.5)) return 'up';
  return 'neutral';
}

function _hotAiDecisionCls(decision) {
  const value = String(decision || '').toUpperCase();
  if (value === 'BUY') return 'buy';
  if (value === 'OVERWEIGHT') return 'watch';
  if (value === 'UNDERWEIGHT' || value === 'SELL') return 'risk';
  return 'hold';
}

function _hotAiDecisionText(decision) {
  const value = String(decision || '').toUpperCase();
  return {
    BUY: '买入',
    OVERWEIGHT: '增配',
    HOLD: '观望',
    UNDERWEIGHT: '减配',
    SELL: '回避',
  }[value] || '待讨论';
}

function _hotAiActions(poolName) {
  const actions = [];
  if (poolName === 'candidate') actions.push(['加入重点', 'focus']);
  if (poolName === 'focus') {
    actions.push(['移回候选', 'candidate']);
    actions.push(['加入买入', 'buy']);
  }
  if (poolName === 'buy') actions.push(['降级重点', 'focus']);
  return actions;
}

async function moveHotAiPool(symbol, targetPool) {
  try {
    const res = await request('/api/strategy/hot-stock-ai/pool/move', {
      method: 'POST',
      body: JSON.stringify({ symbol, target_pool: targetPool }),
    });
    if (!res.success) {
      alert(res.message || '迁移失败');
      return;
    }
    state.hotStockAI = res.snapshot;
    renderHotStockAI();
    setStatus(res.message || '迁移成功', 'success');
  } catch (err) {
    alert(err.message || '迁移失败');
  }
}

function _hotAiCardHtml(e, poolName) {
  const score = Number(e.score || 0);
  const cls = _hotAiScoreCls(score);
  const ta = e.tradingagents || {};
  const decision = String(ta.decision || '').toUpperCase();
  const evaluationText = e.evaluation_text || _hotAiDecisionText(decision);
  const baseScore = Number(e.base_score ?? e.score ?? 0);
  const bonus = Number(e.tradingagents_bonus || 0);
  const bonusSign = bonus > 0 ? '+' : '';
  const decisionCls = _hotAiDecisionCls(decision);
  const sourceText = ta.source === 'cache' ? '缓存讨论' : ta.source === 'fresh' ? '实时讨论' : '';
  const taSummary = ta.summary || ta.discussion || '';
  const pct = Number(e.change_pct || 0);
  const pctCls = _chgCls(pct);
  const pctSign = pct > 0 ? '+' : '';
  const price = Number(e.latest_price || 0);
  const priceHtml = price > 0 ? `<span class="stock-price">${fmtNum(price, 2)}</span>` : '';
  const pctHtml = pct !== 0 ? ` <span class="${pctCls}">${pctSign}${fmtNum(pct, 2)}%</span>` : '';
  const tags = (e.tags || []).slice(0, 4)
    .map(tag => `<span class="tag hot-ai-tag">${esc(tag)}</span>`)
    .join('');
  const btns = _hotAiActions(poolName)
    .map(([txt, pool]) => `<button data-hotai-pool="${esc(pool)}" data-symbol="${esc(e.symbol)}">${esc(txt)}</button>`)
    .join('');
  const decisionHtml = ta.status === 'ok'
    ? `<div class="hot-ai-ta-row">
        <span class="hot-ai-ta-pill ${esc(decisionCls)}">TradingAgents ${esc(_hotAiDecisionText(decision))}</span>
        <span class="hot-ai-ta-source">${esc(sourceText)}</span>
        <span class="hot-ai-ta-bonus ${bonus >= 0 ? 'up' : 'down'}">${bonusSign}${fmtNum(bonus, 1)}分</span>
      </div>`
    : ta.status === 'failed'
      ? `<div class="hot-ai-ta-row"><span class="hot-ai-ta-pill risk">讨论失败</span><span class="hot-ai-ta-source">${esc(ta.error || '调用异常')}</span></div>`
      : ta.status === 'skipped'
        ? `<div class="hot-ai-ta-row"><span class="hot-ai-ta-pill hold">未讨论</span><span class="hot-ai-ta-source">${esc(ta.reason || '未进入讨论名单')}</span></div>`
        : ta.status === 'disabled'
          ? `<div class="hot-ai-ta-row"><span class="hot-ai-ta-pill hold">讨论关闭</span></div>`
          : `<div class="hot-ai-ta-row"><span class="hot-ai-ta-pill hold">待讨论</span></div>`;
  return `
    <div class="stock-card hot-ai-card ${state.selectedSymbol === e.symbol ? 'active' : ''}" data-hotai-symbol="${esc(e.symbol)}">
      <div class="stock-top">
        <div class="stock-name">${esc(e.name || e.symbol)} <span class="stock-code">${esc(e.symbol)}</span></div>
        <div class="stock-price-area">${priceHtml}${pctHtml}</div>
      </div>
      ${tags ? `<div class="tags">${tags}</div>` : ''}
      <div class="metrics">
        <span>评分 <b class="${cls}">${fmtNum(score, 1)}</b></span>
        <span class="metrics-sep">·</span>
        <span>基础分 <b>${fmtNum(baseScore, 1)}</b></span>
        <span class="metrics-sep">·</span>
        <span>热度 #<b>${e.rank ? safeInt(e.rank) : '--'}</b></span>
        <span class="metrics-sep">·</span>
        <span>3日高 <b>${(Number(e.pred_max_high_pct || 0) >= 0 ? '+' : '')}${fmtNum(e.pred_max_high_pct, 2)}%</b></span>
      </div>
      <div class="metrics">
        <span>距MA20 <b>${(Number(e.dist_ma20_pct || 0) >= 0 ? '+' : '')}${fmtNum(e.dist_ma20_pct, 2)}%</b></span>
        <span class="metrics-sep">·</span>
        <span>距20日高 <b>${(Number(e.dist_high20_pct || 0) >= 0 ? '+' : '')}${fmtNum(e.dist_high20_pct, 2)}%</b></span>
        <span class="metrics-sep">·</span>
        <span>量额比20日 <b>${fmtNum(e.amount_ratio_20d, 2)}</b></span>
      </div>
      <div class="metrics hot-ai-breakdown">
        <span>热度分 <b>${fmtNum(e.score_breakdown?.popularity, 1)}</b></span>
        <span class="metrics-sep">·</span>
        <span>趋势分 <b>${fmtNum(e.score_breakdown?.trend, 1)}</b></span>
        <span class="metrics-sep">·</span>
        <span>预测分 <b>${fmtNum(e.score_breakdown?.prediction, 1)}</b></span>
        <span class="metrics-sep">·</span>
        <span>风险扣分 <b>${fmtNum(e.score_breakdown?.risk_penalty, 1)}</b></span>
      </div>
      <div class="metrics"><span>评价 <b>${esc(evaluationText)}</b></span></div>
      ${decisionHtml}
      ${taSummary ? `<div class="hot-ai-ta-summary">${esc(taSummary)}</div>` : ''}
      <div class="hot-ai-analysis">${esc(e.analysis || '暂无分析摘要')}</div>
      <div class="card-actions">${btns}</div>
    </div>`;
}

function renderHotStockAI() {
  const snap = state.hotStockAI;
  const metaEl = document.getElementById('hotAiMeta');
  const progEl = document.getElementById('hotAiProgress');
  const btn = document.getElementById('btnHotAiRun');
  const summary = document.getElementById('hotAiPageSummary');
  const toggle = document.getElementById('hotAiAutoToggle');
  if (!snap) {
    if (metaEl) metaEl.textContent = '无数据';
    if (summary) summary.innerHTML = '';
    return;
  }
  const cfg = snap.config || {};
  state.hotStockAIConfig = cfg;
  if (toggle) toggle.checked = !!cfg.auto_refresh_enabled;
  const thC = document.getElementById('hotai-th-candidate');
  const thF = document.getElementById('hotai-th-focus');
  const thB = document.getElementById('hotai-th-buy');
  if (thC) thC.textContent = `≥${cfg.threshold_candidate || 8}分`;
  if (thF) thF.textContent = `≥${cfg.threshold_focus || 11.5}分`;
  if (thB) thB.textContent = `≥${cfg.threshold_buy || 14.5}分`;

  const pools = snap.pools || { candidate: [], focus: [], buy: [] };
  const renderPool = (id, list) => {
    const el = document.getElementById(id);
    const cnt = document.getElementById(`hotai-count-${id.split('-').pop()}`);
    if (cnt) cnt.textContent = (list || []).length;
    if (!el) return;
    if (!list || list.length === 0) {
      el.innerHTML = '<div class="empty-pool">暂无</div>';
      return;
    }
    el.innerHTML = list.map((item) => _hotAiCardHtml(item, id.split('-').pop())).join('');
    _applyLongListCollapse(el, POOL_LIST_COLLAPSE_THRESHOLD, id);
    el.querySelectorAll('.hot-ai-card[data-hotai-symbol]').forEach((card) => {
      card.onclick = () => openHotAiDetail(card.dataset.hotaiSymbol);
    });
    el.querySelectorAll('button[data-hotai-pool]').forEach((btn) => {
      btn.onclick = (e) => {
        e.stopPropagation();
        moveHotAiPool(btn.dataset.symbol, btn.dataset.hotaiPool);
      };
    });
  };
  renderPool('hotai-pool-candidate', pools.candidate);
  renderPool('hotai-pool-focus', pools.focus);
  renderPool('hotai-pool-buy', pools.buy);

  const prog = snap.progress || {};
  const running = !!snap.running;
  if (btn) {
    btn.disabled = running;
    btn.textContent = running ? '分析中…' : '立即分析';
  }
  if (progEl) {
    if (running) {
      progEl.textContent = `${prog.phase || '...'} ${prog.current || 0}/${prog.total || 0} ${prog.detail || ''}`;
    } else if (prog.error) {
      progEl.textContent = `失败：${prog.error}`;
    } else if (snap.meta?.elapsed_sec) {
      progEl.textContent = `上次${snap.meta.trigger || '手动'} · ${snap.meta.elapsed_sec}s · 失败${snap.meta.failed_count || 0}`;
    } else {
      progEl.textContent = '';
    }
  }
  if (metaEl) {
    const m = snap.meta || {};
    const modeText = m.execution_mode === 'light_auto' ? ' · 自动轻量模式' : '';
    const taText = m.runtime_tradingagents_enabled
      ? ` · 讨论 ${m.tradingagents_discussed || 0} · 缓存 ${m.tradingagents_cache_hits || 0}`
      : (m.tradingagents_enabled ? ' · 自动任务已跳过讨论' : '');
    const backendText = m.tradingagents_backend ? ` · ${m.tradingagents_backend}` : '';
    metaEl.textContent = `交易日 ${snap.trade_date || '--'} · 分析 ${m.entries_count || 0}/${m.stocks_scanned || cfg.top_n || 20}${modeText} · Kronos ${m.kronos_device || '--'}${taText}${backendText} · 均分 ${fmtNum(m.avg_score || 0, 1)}`;
  }
  if (summary) {
    summary.innerHTML = [
      _psItem('候选', (pools.candidate || []).length),
      _psSep(),
      _psItem('关注', (pools.focus || []).length),
      _psSep(),
      _psItem('买入', (pools.buy || []).length, 'warning'),
      _psSep(),
      _psItem('均分', fmtNum(snap.meta?.avg_score || 0, 1)),
      _psSep(),
      _psItem('扫描', `${snap.meta?.stocks_scanned || 0}只`),
      _psSep(),
      _psItem('讨论', `${snap.meta?.tradingagents_discussed || 0}只`, snap.meta?.tradingagents_discussed ? 'brand' : ''),
    ].join('');
  }
}

async function loadHotStockAI() {
  try {
    const snap = await request('/api/strategy/hot-stock-ai');
    state.hotStockAI = snap;
    state.hotStockAIRunning = !!snap.running;
    renderHotStockAI();
    if (state.activeTab === 'hotai') setMeta();
  } catch (err) {
    const el = document.getElementById('hotAiMeta');
    if (el) el.textContent = `加载失败: ${err.message}`;
  }
}

async function openHotAiDetail(symbol) {
  if (!symbol) return;
  state.selectedSymbol = symbol;
  let detail = { name: symbol, kline: [], metrics: {} };
  try {
    const d = await request(`/api/stock/${symbol}/detail?kline_days=30`);
    detail = {
      name: d?.name || symbol,
      kline: d?.kline || [],
      metrics: d?.metrics || {},
    };
  } catch (_) {}
  try {
    await openPredictModal(symbol, detail, {
      titleSuffix: '热门股票智能分析',
      badge: 'Kronos 预测 + TradingAgents 讨论',
      badgeClass: 'predict-modal-badge',
    });
  } catch (err) {
    setStatus(`详情加载失败: ${err?.message || err}`, 'error');
  }
}

async function openPredictDetail(symbol, name) {
  if (!symbol) return;
  state.selectedSymbol = symbol;
  let detail = { name: name || symbol, kline: [], metrics: {} };
  try {
    const d = await request(`/api/stock/${symbol}/detail?kline_days=30`);
    detail = {
      name: d?.name || name || symbol,
      kline: d?.kline || [],
      metrics: d?.metrics || {},
    };
  } catch (_) { /* fallback 到基础信息 */ }
  try {
    if (typeof openPredictModal === 'function') {
      await openPredictModal(symbol, detail);
    }
  } catch (err) {
    setStatus(`预测加载失败: ${err?.message || err}`, 'error');
  }
}

function _bindPredictActions() {
  const btn = document.getElementById('btnPredictRun');
  if (btn) {
    btn.onclick = async () => {
      if (state.predictRunning) return;
      try {
        setStatus('预测任务已启动，预计 3-5 分钟完成…', 'info');
        await request('/api/predict-funnel/trigger', { method: 'POST' });
        state.predictRunning = true;
        _startPredictPoll();
      } catch (err) {
        setStatus(`启动失败: ${err.message}`, 'error');
      }
    };
  }
  const tog = document.getElementById('predictFeishuToggle');
  if (tog) {
    tog.onchange = async () => {
      try {
        await request('/api/predict-funnel/config', {
          method: 'POST',
          body: JSON.stringify({ feishu_enabled: tog.checked }),
          headers: { 'Content-Type': 'application/json' },
        });
        setStatus(`飞书推送已${tog.checked ? '开启' : '关闭'}`, 'success');
      } catch (err) {
        setStatus(`设置失败: ${err.message}`, 'error');
        tog.checked = !tog.checked;
      }
    };
  }
}

function _bindHotAiActions() {
  const btn = document.getElementById('btnHotAiRun');
  if (btn) {
    btn.onclick = async () => {
      if (state.hotStockAIRunning) return;
      try {
        setStatus('热门股票智能分析已启动，正在逐股打分…', 'info');
        state.hotStockAIRunning = true;
        renderHotStockAI();
        await request('/api/strategy/hot-stock-ai/run', { method: 'POST' });
        _startHotAiPoll();
      } catch (err) {
        state.hotStockAIRunning = false;
        renderHotStockAI();
        setStatus(`启动失败: ${err.message}`, 'error');
      }
    };
  }
  const toggle = document.getElementById('hotAiAutoToggle');
  if (toggle) {
    toggle.onchange = async () => {
      try {
        await request('/api/strategy/hot-stock-ai/config', {
          method: 'POST',
          body: JSON.stringify({ auto_refresh_enabled: toggle.checked }),
          headers: { 'Content-Type': 'application/json' },
        });
        if (state.hotStockAI?.config) state.hotStockAI.config.auto_refresh_enabled = toggle.checked;
        setStatus(`热门智能自动刷新已${toggle.checked ? '开启' : '关闭'}`, 'success');
      } catch (err) {
        toggle.checked = !toggle.checked;
        setStatus(`设置失败: ${err.message}`, 'error');
      }
    };
  }
}

