/* Alpha frontend module: strategy.js — 自定义策略中心.
 * 无构建、classic script，按 index.html 中的顺序加载，函数挂在全局作用域。
 */

/* ==================== 自定义策略中心 ==================== */

const strategyCenterState = {
  rules: [],          // [{code,title,category,description,params:[...]}]
  strategies: [],     // [{id,name,...,rules:[{rule_code,enabled,params}]}]
  currentId: null,
  editing: null,      // 当前正在编辑的策略（本地 draft）
  scActionsBound: false,
  scanSnapshot: null,
};

async function loadStrategyCenter() {
  try {
    if (!strategyCenterState.rules.length) {
      const r = await request('/api/strategy/rules');
      strategyCenterState.rules = r.rules || [];
    }
    const lst = await request('/api/strategy/custom');
    strategyCenterState.strategies = lst.items || [];
    if (!strategyCenterState.currentId) {
      strategyCenterState.currentId = lst.default_id
        || (strategyCenterState.strategies[0] && strategyCenterState.strategies[0].id)
        || null;
    }
    _renderStrategySelect();
    _loadEditingStrategy();
    _renderRulesList();
    _renderStrategyMeta();
    await _loadScanSnapshot();
  } catch (err) {
    setStatus(`策略中心加载失败: ${err.message}`, 'error');
  }
}

function _bindStrategyCenterActions() {
  if (strategyCenterState.scActionsBound) return;
  strategyCenterState.scActionsBound = true;

  const $ = (id) => document.getElementById(id);

  $('scStrategySelect').onchange = async (ev) => {
    strategyCenterState.currentId = ev.target.value;
    _loadEditingStrategy();
    _renderRulesList();
    _renderStrategyMeta();
    await _loadScanSnapshot();
  };

  $('btnScNew').onclick = () => {
    strategyCenterState.currentId = null;
    strategyCenterState.editing = {
      id: null,
      name: '新策略',
      description: '',
      is_builtin: false,
      is_default: false,
      rules: strategyCenterState.rules.slice(0, 3).map((r) => ({
        rule_code: r.code,
        enabled: false,
        params: _defaultParams(r),
      })),
    };
    _renderStrategySelect();
    _renderRulesList();
    _renderStrategyMeta();
  };

  $('btnScClone').onclick = () => {
    const src = strategyCenterState.editing;
    if (!src) return;
    strategyCenterState.currentId = null;
    strategyCenterState.editing = JSON.parse(JSON.stringify({
      ...src,
      id: null,
      name: `${src.name} · 副本`,
      is_builtin: false,
      is_default: false,
    }));
    _renderStrategySelect();
    _renderRulesList();
    _renderStrategyMeta();
    setStatus('已克隆策略，可继续编辑后保存', 'success');
  };

  $('btnScSave').onclick = async () => {
    const draft = strategyCenterState.editing;
    if (!draft) return;
    draft.name = $('scStrategyName').value.trim() || draft.name;
    draft.description = $('scStrategyDesc').value.trim();
    if (!draft.name) { setStatus('请填写策略名称', 'error'); return; }
    const body = {
      id: draft.id || undefined,
      name: draft.name,
      description: draft.description,
      rules: draft.rules.filter((r) => r.rule_code),
    };
    try {
      const r = await request('/api/strategy/custom', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      setStatus('策略已保存', 'success');
      strategyCenterState.currentId = r.strategy.id;
      await loadStrategyCenter();
    } catch (err) {
      setStatus(`保存失败: ${err.message}`, 'error');
    }
  };

  $('btnScDelete').onclick = async () => {
    const draft = strategyCenterState.editing;
    if (!draft || !draft.id) { setStatus('未保存的新策略，无需删除', 'error'); return; }
    if (draft.is_builtin) { setStatus('内置策略不可删除', 'error'); return; }
    if (!confirm(`确定删除策略「${draft.name}」？`)) return;
    try {
      await request(`/api/strategy/custom/${draft.id}`, { method: 'DELETE' });
      strategyCenterState.currentId = null;
      setStatus('已删除', 'success');
      await loadStrategyCenter();
    } catch (err) {
      setStatus(`删除失败: ${err.message}`, 'error');
    }
  };

  $('btnScSetDefault').onclick = async () => {
    const draft = strategyCenterState.editing;
    if (!draft || !draft.id) { setStatus('请先保存策略', 'error'); return; }
    try {
      await request(`/api/strategy/custom/${draft.id}/default`, { method: 'POST' });
      setStatus(`已设为默认策略`, 'success');
      await loadStrategyCenter();
    } catch (err) {
      setStatus(`设置失败: ${err.message}`, 'error');
    }
  };

  $('btnScScan').onclick = async () => {
    const draft = strategyCenterState.editing;
    if (!draft || !draft.id) { setStatus('请先保存策略后再扫描', 'error'); return; }
    const btn = $('btnScScan');
    const meta = $('scScanMeta');
    btn.disabled = true;
    btn.textContent = '扫描中…';
    if (meta) meta.textContent = '扫描中，约 30~60 秒（全 A 股）…';
    try {
      const raw = await requestJob(`/api/strategy/custom/${draft.id}/scan`, { method: 'POST' }, {
        intervalMs: 2000,
        onProgress: (job) => { if (meta) meta.textContent = `扫描中… ${_jobProgressText(job)}`; },
      });
      const snap = raw.snapshot || raw.result || raw;
      _renderScanSnapshot(snap);
      setStatus(`扫描完成：命中 ${safeInt(snap.total_hits)} 只`, 'success');
    } catch (err) {
      setStatus(`扫描失败: ${err.message}`, 'error');
      if (meta) meta.textContent = `扫描失败: ${err.message}`;
    } finally {
      btn.disabled = false;
      btn.textContent = '立即扫描';
    }
  };

  $('btnScBacktest').onclick = async () => {
    const draft = strategyCenterState.editing;
    if (!draft || !draft.id) { setStatus('请先保存策略后再回测', 'error'); return; }
    const btn = $('btnScBacktest');
    btn.disabled = true;
    btn.textContent = '回测中…';
    try {
      const raw = await requestJob(
        `/api/strategy/custom/${draft.id}/backtest?history_days=180&hold_days=3&tp_pct=8&sl_pct=-5`,
        { method: 'POST' },
        { intervalMs: 2000, onProgress: (job) => setStatus(`回测${_jobProgressText(job)}`, 'info') },
      );
      const r = raw.result || raw;
      _renderBacktestResult(r);
      setStatus(`回测完成：信号 ${safeInt(r.total_signals)} · 胜率 ${fmtNum(r.hit_rate, 2)}%`, 'success');
    } catch (err) {
      setStatus(`回测失败: ${err.message}`, 'error');
    } finally {
      btn.disabled = false;
      btn.textContent = '回测 180 天';
    }
  };
}

function _defaultParams(ruleSpec) {
  const out = {};
  (ruleSpec.params || []).forEach((p) => { out[p.key] = p.default; });
  return out;
}

function _renderStrategySelect() {
  const sel = document.getElementById('scStrategySelect');
  if (!sel) return;
  const items = strategyCenterState.strategies;
  const cur = strategyCenterState.currentId;
  sel.innerHTML = '';
  items.forEach((s) => {
    const opt = document.createElement('option');
    opt.value = s.id;
    const badges = [];
    if (s.is_builtin) badges.push('内置');
    if (s.is_default) badges.push('默认');
    opt.textContent = badges.length ? `${s.name} · ${badges.join('/')}` : s.name;
    if (s.id === cur) opt.selected = true;
    sel.appendChild(opt);
  });
  if (!cur) {
    const opt = document.createElement('option');
    opt.value = '';
    opt.textContent = '-- 未保存的新策略 --';
    opt.selected = true;
    sel.appendChild(opt);
  }
}

function _loadEditingStrategy() {
  const cur = strategyCenterState.currentId;
  if (!cur) {
    if (!strategyCenterState.editing) return;
    return;
  }
  const src = strategyCenterState.strategies.find((x) => x.id === cur);
  if (!src) return;
  strategyCenterState.editing = JSON.parse(JSON.stringify(src));
  const existingCodes = new Set((strategyCenterState.editing.rules || []).map((r) => r.rule_code));
  strategyCenterState.rules.forEach((spec) => {
    if (!existingCodes.has(spec.code)) {
      strategyCenterState.editing.rules.push({
        rule_code: spec.code,
        enabled: false,
        params: _defaultParams(spec),
      });
    }
  });
}

function _renderStrategyMeta() {
  const draft = strategyCenterState.editing;
  const $ = (id) => document.getElementById(id);
  const badges = $('scStrategyBadges');
  const nameInput = $('scStrategyName');
  const descInput = $('scStrategyDesc');
  const btnDelete = $('btnScDelete');
  const btnSetDefault = $('btnScSetDefault');
  if (!badges || !nameInput) return;
  if (!draft) {
    nameInput.value = '';
    descInput.value = '';
    badges.innerHTML = '';
    return;
  }
  nameInput.value = draft.name || '';
  descInput.value = draft.description || '';
  const parts = [];
  if (draft.is_builtin) parts.push('<span class="sc-chip builtin">内置</span>');
  if (draft.is_default) parts.push('<span class="sc-chip default">默认</span>');
  if (!draft.id) parts.push('<span class="sc-chip new">未保存</span>');
  badges.innerHTML = parts.join('');
  if (btnDelete) btnDelete.disabled = !!(draft.is_builtin || !draft.id);
  if (btnSetDefault) btnSetDefault.disabled = !draft.id || draft.is_default;
}

const SC_CAT_ICONS = {
  filter: '<polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3"/>',
  price: '<path d="M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.83z"/><line x1="7" y1="7" x2="7.01" y2="7"/>',
  volume: '<line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/>',
  pattern: '<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>',
  trend: '<polyline points="23 6 13.5 15.5 8.5 10.5 1 18"/><polyline points="17 6 23 6 23 12"/>',
};

function _renderRulesList() {
  const root = document.getElementById('scRulesList');
  if (!root) return;
  const draft = strategyCenterState.editing;
  if (!draft) { root.innerHTML = '<div class="sc-empty">请先选择或新建策略</div>'; _updateRulesSummary(); return; }
  const specsMap = {};
  strategyCenterState.rules.forEach((s) => { specsMap[s.code] = s; });

  const byCat = {};
  strategyCenterState.rules.forEach((spec) => {
    if (!byCat[spec.category]) byCat[spec.category] = [];
    byCat[spec.category].push(spec);
  });

  const catOrder = ['filter', 'price', 'volume', 'pattern', 'trend'];
  const catTitles = { filter: '过滤条件', price: '价格', volume: '量能', pattern: '形态', trend: '趋势' };

  const html = catOrder.filter((c) => byCat[c]).map((cat) => {
    const cards = byCat[cat].map((spec) => {
      const ref = draft.rules.find((r) => r.rule_code === spec.code) || {
        rule_code: spec.code, enabled: false, params: _defaultParams(spec),
      };
      const paramsHtml = (spec.params || []).map((p) => {
        const val = ref.params[p.key] ?? p.default;
        if (p.type === 'bool') {
          return `<label class="sc-param sc-param-bool">
            <input type="checkbox" data-code="${esc(spec.code)}" data-key="${esc(p.key)}" data-type="bool" ${val ? 'checked' : ''} />
            ${esc(p.label)}
          </label>`;
        }
        if (p.type === 'text') {
          return `<label class="sc-param">
            <span class="sc-param-label">${esc(p.label)}</span>
            <input type="text" class="sc-input sc-param-input" data-code="${esc(spec.code)}" data-key="${esc(p.key)}" data-type="text" value="${esc(String(val))}" />
          </label>`;
        }
        return `<label class="sc-param">
          <span class="sc-param-label">${esc(p.label)}</span>
          <input type="number" class="sc-input sc-param-input" data-code="${esc(spec.code)}" data-key="${esc(p.key)}" data-type="${esc(p.type)}"
            ${p.min !== null && p.min !== undefined ? `min="${p.min}"` : ''}
            ${p.max !== null && p.max !== undefined ? `max="${p.max}"` : ''}
            ${p.step ? `step="${p.step}"` : 'step="any"'}
            value="${Number(val)}" />
        </label>`;
      }).join('');
      return `
        <div class="sc-rule-card ${ref.enabled ? 'enabled' : ''}" data-code="${esc(spec.code)}">
          <div class="sc-rule-main">
            <label class="sc-switch" title="${ref.enabled ? '点击停用' : '点击启用'}">
              <input type="checkbox" class="sc-rule-enabled" data-code="${esc(spec.code)}" ${ref.enabled ? 'checked' : ''} />
              <span class="sc-switch-track"><span class="sc-switch-thumb"></span></span>
            </label>
            <div class="sc-rule-info">
              <span class="sc-rule-title">${esc(spec.title)}</span>
              <span class="sc-rule-desc">${esc(spec.description || '')}</span>
            </div>
          </div>
          <div class="sc-rule-params">${paramsHtml || '<span class="sc-rule-noparam">无参数</span>'}</div>
        </div>
      `;
    }).join('');
    return `
      <section class="sc-rule-group">
        <header class="sc-rule-group-head">
          <span class="sc-rule-group-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${SC_CAT_ICONS[cat] || ''}</svg></span>
          <span class="sc-rule-group-name">${esc(catTitles[cat] || cat)}</span>
          <span class="sc-rule-group-count" data-cat="${cat}"></span>
          <span class="sc-rule-group-line"></span>
        </header>
        <div class="sc-rule-group-body">${cards}</div>
      </section>`;
  }).join('');

  root.innerHTML = html;
  _updateRuleGroupCounts(root);

  root.querySelectorAll('.sc-rule-enabled').forEach((cb) => {
    cb.onchange = () => {
      const code = cb.getAttribute('data-code');
      const ref = _ensureRuleRef(code);
      if (!ref) return;
      ref.enabled = cb.checked;
      const card = root.querySelector(`.sc-rule-card[data-code="${code}"]`);
      if (card) {
        card.classList.toggle('enabled', cb.checked);
        const sw = card.querySelector('.sc-switch');
        if (sw) sw.title = cb.checked ? '点击停用' : '点击启用';
      }
      _updateRuleGroupCounts(root);
    };
  });
  root.querySelectorAll('.sc-param-input, .sc-param-bool input').forEach((el) => {
    el.onchange = () => {
      const code = el.getAttribute('data-code');
      const key = el.getAttribute('data-key');
      const type = el.getAttribute('data-type');
      const ref = _ensureRuleRef(code);
      if (!ref) return;
      let val = el.value;
      if (type === 'bool') val = el.checked;
      else if (type === 'int') val = parseInt(el.value, 10) || 0;
      else if (type === 'float' || type === 'pct') val = parseFloat(el.value) || 0;
      ref.params[key] = val;
    };
  });
}

function _updateRuleGroupCounts(root) {
  const draft = strategyCenterState.editing;
  root.querySelectorAll('.sc-rule-group-count').forEach((pill) => {
    const cat = pill.getAttribute('data-cat');
    const specs = strategyCenterState.rules.filter((s) => s.category === cat);
    const n = !draft ? 0 : specs.filter((s) => {
      const ref = draft.rules.find((r) => r.rule_code === s.code);
      return ref ? !!ref.enabled : false;
    }).length;
    pill.innerHTML = `<b>${n}</b>&thinsp;/&thinsp;${specs.length}`;
    pill.classList.toggle('all', specs.length > 0 && n === specs.length);
  });
  _updateRulesSummary();
}

function _updateRulesSummary() {
  const el = document.getElementById('scRulesSummary');
  if (!el) return;
  const draft = strategyCenterState.editing;
  if (!draft) { el.innerHTML = ''; return; }
  const total = strategyCenterState.rules.length;
  const enabled = (draft.rules || []).filter((r) => r.enabled).length;
  el.innerHTML = `已启用 <b>${enabled}</b> / ${total} 条规则`;
}

function _ensureRuleRef(code) {
  const draft = strategyCenterState.editing;
  if (!draft) return null;
  let ref = draft.rules.find((r) => r.rule_code === code);
  if (!ref) {
    const spec = strategyCenterState.rules.find((s) => s.code === code);
    if (!spec) return null;
    ref = { rule_code: code, enabled: false, params: _defaultParams(spec) };
    draft.rules.push(ref);
  }
  return ref;
}

async function _loadScanSnapshot() {
  const draft = strategyCenterState.editing;
  if (!draft || !draft.id) { _renderScanSnapshot(null); return; }
  try {
    const snap = await request(`/api/strategy/custom/${draft.id}/scan`);
    _renderScanSnapshot(snap);
  } catch {
    _renderScanSnapshot(null);
  }
}

function _renderScanSnapshot(snap) {
  const meta = document.getElementById('scScanMeta');
  if (!meta) return;
  strategyCenterState.scanSnapshot = snap && snap.generated_at ? snap : null;
  state.strategyScanSnapshot = strategyCenterState.scanSnapshot;
  if (state.activeTab === 'funnel') renderFunnel();
  if (!snap || !snap.generated_at) {
    meta.textContent = '尚未扫描，点击"立即扫描"开始（覆盖全 A 股，约 30~60 秒）';
    return;
  }
  const hits = snap.hits || [];
  meta.textContent = `扫描时间 ${(snap.generated_at || '').replace('T', ' ')} · 扫描 ${snap.total_scanned} 只 · 命中 ${snap.total_hits} 只 · 耗时 ${snap.elapsed_seconds}s · 启用规则 ${snap.rules_count}`;
  if (!hits.length) meta.textContent += ' · 当前无命中';
}

function _renderBacktestResult(r) {
  const root = document.getElementById('scBacktestCard');
  if (!root) return;
  if (!r || r.total_signals === undefined) {
    root.style.display = 'none';
    return;
  }
  root.style.display = 'block';
  const pnlCls = Number(r.total_pnl_pct || 0) >= 0 ? 'up' : 'down';
  const mddCls = Number(r.max_drawdown_pct || 0) < 0 ? 'down' : 'muted';
  const params = r.params || {};
  const sample = (r.samples || []).slice(0, 10).map((s) => {
    const cls = Number(s.pnl_pct || 0) >= 0 ? 'up' : 'down';
    return `<tr><td>${esc(s.symbol)}</td><td>${esc(s.name || '')}</td><td>${esc(s.signal_date)}</td><td>${esc(String(s.hold_days))}</td><td class="${cls}">${s.pnl_pct >= 0 ? '+' : ''}${fmtNum(s.pnl_pct, 2)}%</td><td>${esc(s.reason)}</td></tr>`;
  }).join('');
  root.innerHTML = `
    <div class="sc-bt-header">
      <h4>回测结果 · ${esc(params.strategy_name || '')}</h4>
      <span class="sc-bt-meta">${esc(r.generated_at || '')} · ${fmtNum(r.elapsed_seconds, 2)}s · 规则 ${esc(params.rules_count ?? '-')} 条 · 持有 ${safeInt(params.hold_days)}d / tp ${fmtNum(params.tp_pct, 2)}% / sl ${fmtNum(params.sl_pct, 2)}%</span>
    </div>
    <div class="sc-bt-stats">
      <div class="sc-bt-stat"><span class="sc-bt-label">总信号</span><span class="sc-bt-value">${safeInt(r.total_signals)}</span></div>
      <div class="sc-bt-stat"><span class="sc-bt-label">胜 / 负</span><span class="sc-bt-value">${safeInt(r.wins)} / ${safeInt(r.losses)}</span></div>
      <div class="sc-bt-stat"><span class="sc-bt-label">胜率</span><span class="sc-bt-value">${fmtNum(r.hit_rate, 2)}%</span></div>
      <div class="sc-bt-stat"><span class="sc-bt-label">累计收益</span><span class="sc-bt-value ${pnlCls}">${r.total_pnl_pct >= 0 ? '+' : ''}${fmtNum(r.total_pnl_pct, 2)}%</span></div>
      <div class="sc-bt-stat"><span class="sc-bt-label">最大回撤</span><span class="sc-bt-value ${mddCls}">${fmtNum(r.max_drawdown_pct, 2)}%</span></div>
      <div class="sc-bt-stat"><span class="sc-bt-label">平均持有</span><span class="sc-bt-value">${fmtNum(r.avg_hold_days, 1)}d</span></div>
    </div>
    ${sample ? `<details class="sc-bt-samples"><summary>展开样本 Top 10</summary>
      <table class="sc-bt-table">
        <thead><tr><th>代码</th><th>名称</th><th>信号日</th><th>持有</th><th>收益</th><th>出场原因</th></tr></thead>
        <tbody>${sample}</tbody>
      </table>
    </details>` : ''}
  `;
}

