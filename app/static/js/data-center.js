/* Alpha frontend module: data-center.js — 数据中心：概览、任务、完整性与日志.
 * 无构建、classic script，按 index.html 中的顺序加载，函数挂在全局作用域。
 */

/* ==================== 数据中心 ==================== */
/*
 * #54 轮询拆分（配合 boot.js 的两个 Polling 任务）：
 *   - status/progress：任务 running 时 5s，空闲时 60s（顶部进度条只依赖这个）；
 *   - stats / enrichment：running 30s、idle 60s；
 *   - report / logs：只在"关键状态变化"或用户操作后立即刷新，否则最慢 300s 兜底复算
 *     —— 未变化的完整性报告与任务日志不再每轮整包重传。
 * 每个区块自带 TTL 缓存与独立重试（#55），单个接口挂起只会让该区块显示超时+重试按钮。
 */

let _dcTaskPage = 1;

const DC_POLL = {
  status: { running: 5000, idle: 60000 },
  stats: { running: 30000, idle: 60000 },
  enrichment: { running: 30000, idle: 60000 },
  report: { running: 60000, idle: 300000 },
  logs: { running: 15000, idle: 300000 },
};

const _dcCache = {
  status: { at: 0, hasData: false },
  stats: { at: 0, hasData: false },
  enrichment: { at: 0, hasData: false },
  report: { at: 0, hasData: false },
  logs: { at: 0, hasData: false },
};
const _dcDirty = { status: true, stats: true, enrichment: true, report: true, logs: true };
let _dcStatusSignature = '';
let _dcStatusWasRunning = null;


window.DC_POLL = DC_POLL;

function _dcIsRunning() {
  return String(state.syncStatus?.status || '') === 'running';
}

function _dcTtl(kind) {
  const cfg = DC_POLL[kind] || { running: 30000, idle: 60000 };
  return _dcIsRunning() ? cfg.running : cfg.idle;
}

function _dcShouldFetch(kind, force) {
  if (force || _dcDirty[kind]) return true;
  return Date.now() - _dcCache[kind].at >= _dcTtl(kind);
}

function _dcMarkStale(kinds) {
  (Array.isArray(kinds) ? kinds : [kinds]).forEach((kind) => { _dcDirty[kind] = true; });
}

// 只取"阶段跃迁"相关字段做签名：运行中的 updated_at / progress_pct / synced_symbols 每秒都在变，
// 纳入签名会把 report/logs 每一轮都标脏，等于回到每 3~5s 全量拉取（#54）。
function _dcStatusSignatureOf(status) {
  if (!status) return '';
  return [status.status, status.last_success_trade_date, status.trigger_mode].join('|');
}

function _dcAfterFetch(kind) {
  _dcCache[kind].at = Date.now();
  _dcCache[kind].hasData = true;
  _dcDirty[kind] = false;
}

// #55：同一区块上一轮没结束时不叠加新请求（配合 request() 的超时，挂起区块不会排队的雪球）。
const _dcKindInFlight = {};

function _dcRunOnce(key, fn) {
  if (_dcKindInFlight[key]) return _dcKindInFlight[key];
  const p = Promise.resolve().then(fn).finally(() => { delete _dcKindInFlight[key]; });
  _dcKindInFlight[key] = p;
  return p;
}

function _dcFailBlock(kind, targetId, err, retry) {
  // 已经有数据时保留旧数据只提示，首次加载失败才就地给重试按钮（#55）。
  reportBlockFailure(targetId, err, retry, {
    title: _dcBlockTitles[kind] || '该区块',
    hasData: !!_dcCache[kind].hasData,
  });
}

const _dcBlockTitles = {
  status: '同步状态',
  stats: 'KPI 概览',
  enrichment: '数据增强覆盖',
  report: '完整性报告',
  logs: '任务历史',
};

function _updateDcPollMeta() {
  const el = document.getElementById('dcPollMeta');
  if (!el) return;
  const mode = _dcIsRunning() ? '任务运行中' : '空闲';
  el.textContent = `轮询：状态 ${Math.round(_dcTtl('status') / 1000)}s · 概览 ${Math.round(_dcTtl('stats') / 1000)}s · 报告/日志 状态变化时刷新（${mode}）`;
}

// ── 区块 1：同步状态 / 进度 ──
async function loadDcStatus(opts = {}) {
  return _dcRunOnce('status', () => _loadDcStatus(opts));
}

async function _loadDcStatus({ force = false } = {}) {
  if (!_dcShouldFetch('status', force)) {
    renderDcProgress(state.syncStatus);
    return state.syncStatus;
  }
  try {
    const res = await request('/api/jobs/kline-cache/status');
    _dcAfterFetch('status');
    const signature = _dcStatusSignatureOf(res);
    const changed = !!_dcStatusSignature && signature !== _dcStatusSignature;
    _dcStatusSignature = signature;
    state.syncStatus = res;
    // 关键状态跃迁（idle <-> running <-> 终态）才让 report/logs/stats/enrichment 重取，
    // 未变化时它们按各自更长的兜底 TTL 走，不再每轮整包重传（#54）。
    if (changed) {
      _dcMarkStale(['stats', 'enrichment', 'report', 'logs']);
      refreshDcBlocks(['stats', 'enrichment', 'report', 'logs']);
    }
    // running/空闲切换后要把 dc-status 的节奏立刻收口，否则会继续按跃迁前的间隔排队。
    const runningNow = _dcIsRunning();
    if (runningNow !== _dcStatusWasRunning) {
      _dcStatusWasRunning = runningNow;
      Polling.reschedule('dc-status');
    }
    clearBlockError('dcHealthCard');
    renderDcStats(state.dcStats || {}, res, state.dcReport);
    renderDcProgress(res);
    renderDataSummary();
    _updateDcPollMeta();
    return res;
  } catch (err) {
    _dcFailBlock('status', 'dcHealthCard', err, () => loadDcStatus({ force: true }));
    return null;
  }
}

// ── 区块 2：KPI 统计 ──
async function loadDcStats(opts = {}) {
  return _dcRunOnce('stats', () => _loadDcStats(opts));
}

async function _loadDcStats({ force = false } = {}) {
  if (!_dcShouldFetch('stats', force)) {
    renderDcStats(state.dcStats || {}, state.syncStatus || {}, state.dcReport);
    return state.dcStats;
  }
  try {
    const stats = await request('/api/jobs/kline-cache/stats');
    _dcAfterFetch('stats');
    state.dcStats = stats;
    clearBlockError('dcKpiRow');
    renderDcStats(stats, state.syncStatus || {}, state.dcReport);
    return stats;
  } catch (err) {
    _dcFailBlock('stats', 'dcKpiRow', err, () => loadDcStats({ force: true }));
    return null;
  }
}

// ── 区块 3：数据增强覆盖 ──
async function loadDcEnrichment(opts = {}) {
  return _dcRunOnce('enrichment', () => _loadDcEnrichment(opts));
}

async function _loadDcEnrichment({ force = false } = {}) {
  if (!_dcShouldFetch('enrichment', force)) {
    renderDcEnrichment(state.dcEnrichment || { items: [] });
    return state.dcEnrichment;
  }
  try {
    const enrichment = await request('/api/jobs/kline-cache/enrichment-status');
    _dcAfterFetch('enrichment');
    state.dcEnrichment = enrichment;
    clearBlockError('dcEnrichmentGrid');
    renderDcEnrichment(enrichment);
    return enrichment;
  } catch (err) {
    _dcFailBlock('enrichment', 'dcEnrichmentGrid', err, () => loadDcEnrichment({ force: true }));
    return null;
  }
}

// ── 区块 4：完整性报告 ──
async function loadDcReport(opts = {}) {
  return _dcRunOnce('report', () => _loadDcReport(opts));
}

async function _loadDcReport({ force = false } = {}) {
  if (!_dcShouldFetch('report', force)) {
    renderDcReport(state.dcReport);
    return state.dcReport;
  }
  try {
    const report = await request('/api/jobs/kline-cache/report');
    _dcAfterFetch('report');
    state.dcReport = report;
    clearBlockError('dcReportContent');
    renderDcReport(report);
    renderDcStats(state.dcStats || {}, state.syncStatus || {}, report);
    renderDataSummary();
    return report;
  } catch (err) {
    _dcFailBlock('report', 'dcReportContent', err, () => loadDcReport({ force: true }));
    return null;
  }
}

// ── 区块 5：任务历史 / 同步日志（同一份 logs 数据驱动两个视图） ──
async function loadDcLogs(opts = {}) {
  return _dcRunOnce('logs', () => _loadDcLogs(opts));
}

async function _loadDcLogs({ force = false } = {}) {
  if (!_dcShouldFetch('logs', force)) {
    const cached = state.syncLogs || { items: [], total: 0 };
    renderDcTaskList(cached);
    renderDcLogStream(cached);
    return cached;
  }
  try {
    const logs = await request('/api/jobs/kline-cache/logs?page=' + _dcTaskPage + '&page_size=15');
    _dcAfterFetch('logs');
    state.syncLogs = logs;
    clearBlockError('dcTaskList');
    clearBlockError('dcLogStream');
    renderDcTaskList(logs);
    renderDcLogStream(logs);
    return logs;
  } catch (err) {
    _dcFailBlock('logs', 'dcTaskList', err, () => loadDcLogs({ force: true }));
    showBlockError('dcLogStream', err, () => loadDcLogs({ force: true }), { title: '同步日志' });
    return null;
  }
}

const _DC_LOADERS = {
  status: () => loadDcStatus(),
  stats: () => loadDcStats(),
  enrichment: () => loadDcEnrichment(),
  report: () => loadDcReport(),
  logs: () => loadDcLogs(),
};

// 轮询入口：每个区块按各自 TTL 自门控，未到期的区块这一轮不发请求。
async function refreshDcBlocks(kinds = ['stats', 'enrichment', 'report', 'logs']) {
  await Promise.allSettled(kinds.map((kind) => _DC_LOADERS[kind]()));
}

// 全量刷新（Tab 切换、按钮回调、分页）：状态与其余区块一起刷，
// 每个区块自带"上一轮未完成则直接复用"的去重，避免请求堆积（#55）。
async function loadDataCenter(opts = {}) {
  return _dcRunOnce('all', () => _loadDataCenter(opts));
}

async function _loadDataCenter({ force = false } = {}) {
  if (force) _dcMarkStale(Object.keys(_DC_LOADERS));
  await refreshDcBlocks(['status', 'stats', 'enrichment', 'report', 'logs']);
}

function renderDcEnrichment(payload) {
  const grid = document.getElementById('dcEnrichmentGrid');
  if (!grid) return;
  const items = payload?.items || [];
  if (!items.length) {
    grid.innerHTML = '<div class="detail-empty">暂无增强数据，等待盘后自动同步</div>';
    return;
  }
  const statusMeta = {
    success: ['已同步', 'success'],
    partial: ['部分成功', 'warning'],
    failed: ['失败', 'error'],
    empty: ['无数据', 'warning'],
    none: ['未同步', ''],
  };
  grid.innerHTML = items.map((item) => {
    const [statusLabel, statusCls] = statusMeta[item.status] || [item.status || '--', ''];
    const isMinute = item.dataset === 'minute';
    const latest = isMinute
      ? (item.latest_bar_time || '--')
      : (item.latest_trade_date || '--');
    const rows = isMinute ? Number(item.row_count || 0) : Number(item.total_rows || 0);
    const detail = item.message && item.status !== 'success'
      ? ` · ${esc(String(item.message))}`
      : '';
    return `
      <div class="dc-enrichment-item">
        <div class="dc-enrichment-top">
          <span class="dc-enrichment-label">${esc(item.label || item.dataset || '--')}</span>
          <span class="dc-enrichment-status ${statusCls}">${statusLabel}</span>
        </div>
        <div class="dc-enrichment-date" title="${esc(latest)}">${esc(latest)}</div>
        <div class="dc-enrichment-meta" title="${esc(String(rows))} 条${detail}">${rows.toLocaleString()} 条${detail}</div>
      </div>
    `;
  }).join('');
}

function _dcHealthLevel(coverage) {
  if (coverage == null) return { label: '未检查', cls: 'brand', icon: '○' };
  if (coverage >= 99) return { label: '健康', cls: 'success', icon: '●' };
  if (coverage >= 90) return { label: '警告', cls: 'warning', icon: '▲' };
  return { label: '严重缺失', cls: 'error', icon: '✖' };
}

function renderDcStats(stats, syncStatus, report) {
  const cov = report?.coverage_pct;
  const health = _dcHealthLevel(cov);
  const syncLabel = { idle: '空闲', running: '同步中', success: '已完成', partial: '部分成功', failed: '失败' }[syncStatus.status] || '--';
  const syncCls = syncStatus.status === 'running' ? 'warning' : (syncStatus.status === 'success' ? 'success' : (syncStatus.status === 'partial' ? 'warning' : (syncStatus.status === 'failed' ? 'error' : 'brand')));
  const missing = report?.total_missing || 0;

  // 冷启动引导：库为空时展示一键初始化卡片；同步进行中禁用按钮
  const welcomeCard = document.getElementById('dcWelcomeCard');
  if (welcomeCard) {
    const hasStats = stats && Object.keys(stats).length > 0;
    const dbEmpty = hasStats && Number(stats.row_count || 0) === 0;
    const isRunning = syncStatus?.status === 'running';
    welcomeCard.style.display = dbEmpty ? '' : 'none';
    const initBtn = document.getElementById('btnDcInit');
    if (initBtn) initBtn.disabled = isRunning;
  }

  const alertBar = document.getElementById('dcAlertBar');
  // 报告过期判定：完整性检查早于最近一次同步完成时间 → 数据已变，旧数字不可信
  const reportStale = !!(report?.check_time && syncStatus?.updated_at && report.check_time < syncStatus.updated_at);
  if (cov != null && cov < 90 && !reportStale) {
    alertBar.style.display = '';
    alertBar.className = `dc-alert-bar dc-alert-${health.cls}`;
    alertBar.innerHTML = `<span class="dc-alert-icon">${esc(health.icon)}</span>数据覆盖率仅 ${Number(cov).toFixed(1)}%，缺失 ${safeInt(missing).toLocaleString()} 条，建议执行 <strong>全量补缺</strong>`;
  } else if (cov != null && cov < 90 && reportStale) {
    alertBar.style.display = '';
    alertBar.className = 'dc-alert-bar dc-alert-warning';
    alertBar.innerHTML = `<span class="dc-alert-icon">▲</span>检查报告已过期（生成于最近同步完成前，当时覆盖率 ${cov.toFixed(1)}%），同步完成后将自动重检`;
  } else {
    alertBar.style.display = 'none';
  }

  const healthCard = document.getElementById('dcHealthCard');
  const circumference = 2 * Math.PI * 26;
  const offset = cov != null ? circumference * (1 - cov / 100) : circumference;
  const ringColor = cov == null ? 'var(--muted)' : (cov >= 99 ? '#22c55e' : (cov >= 90 ? '#f59e0b' : '#ef4444'));
  healthCard.innerHTML = `
    <div class="dc-health-ring">
      <svg viewBox="0 0 64 64"><circle class="ring-bg" cx="32" cy="32" r="26"/><circle class="ring-fg" cx="32" cy="32" r="26" stroke="${ringColor}" stroke-dasharray="${circumference}" stroke-dashoffset="${offset}"/></svg>
      <div class="dc-health-pct">${cov != null ? cov.toFixed(1) + '%' : '--'}</div>
    </div>
    <div class="dc-health-info">
      <div class="dc-health-title"><span class="dc-health-dot ${health.cls}"></span>数据健康: <strong class="${health.cls}">${health.label}</strong></div>
      <div class="dc-health-sub">任务状态: <span class="${esc(syncCls)}">${esc(syncLabel)}</span>${syncStatus.updated_at ? ' · ' + esc(fmtDateTime(syncStatus.updated_at)) : ''}</div>
      <div class="dc-health-sub">最近同步: ${esc(syncStatus.last_success_trade_date || '--')} (${esc(syncStatus.trigger_mode || '--')})${missing > 0 ? ' · 缺失 <strong style="color:#f59e0b">' + missing.toLocaleString() + '</strong> 条' : ''}</div>
    </div>
  `;

  const kpiRow = document.getElementById('dcKpiRow');
  kpiRow.innerHTML = `
    <div class="dc-kpi"><span class="dc-kpi-val">${safeInt(stats.symbol_count).toLocaleString()}</span><span class="dc-kpi-label">股票数</span></div>
    <div class="dc-kpi"><span class="dc-kpi-val">${safeInt(stats.row_count).toLocaleString()}</span><span class="dc-kpi-label">K线总条数</span></div>
    <div class="dc-kpi"><span class="dc-kpi-val">${esc(stats.min_date || '--')}</span><span class="dc-kpi-label">最早日期</span></div>
    <div class="dc-kpi"><span class="dc-kpi-val">${esc(stats.max_date || '--')}</span><span class="dc-kpi-label">最新日期</span></div>
    <div class="dc-kpi"><span class="dc-kpi-val">${esc(stats.db_size_mb ?? '--')} MB</span><span class="dc-kpi-label">数据库</span></div>
  `;

  const hint = document.getElementById('dcSyncHint');
  if (hint) {
    const lastDate = syncStatus.last_success_trade_date;
    hint.textContent = lastDate ? `最近同步: ${lastDate}` : '';
  }
}

function renderDcProgress(syncStatus) {
  const section = document.getElementById('dcProgressSection');
  const textEl = document.getElementById('dcProgressText');
  const barEl = document.getElementById('dcProgressBar');
  if (!syncStatus || syncStatus.status !== 'running') {
    section.style.display = 'none';
    return;
  }
  section.style.display = '';
  const synced = Number(syncStatus.synced_symbols || 0);
  const total = Number(syncStatus.total_symbols || 0);
  const pct = Number(syncStatus.progress_pct || 0);
  textEl.textContent = `${syncStatus.message || '同步中'} · ${synced}/${total} (${pct.toFixed(1)}%)`;
  barEl.style.width = Math.max(0, Math.min(100, pct)) + '%';
}

let _dcReportSortBy = 'date';

function renderDcReport(report) {
  const container = document.getElementById('dcReportContent');
  const sortEl = document.getElementById('dcReportSort');

  if (!report || report.status === 'none' || report.status === 'error') {
    container.innerHTML = `<div class="detail-empty">${esc(report?.message || '暂无检查报告，点击上方按钮执行检查')}</div>`;
    if (sortEl) sortEl.innerHTML = '';
    return;
  }

  const pct = report.coverage_pct || 0;
  const health = _dcHealthLevel(pct);
  const checkTime = (report.check_time || '').slice(0, 19).replace('T', ' ');

  if (sortEl) {
    sortEl.innerHTML = `<button class="text-btn text-btn-sm dc-sort-btn" id="btnDcReportSort">${_dcReportSortBy === 'date' ? '按日期' : '按缺失量'} ▾</button>`;
    document.getElementById('btnDcReportSort').onclick = () => {
      _dcReportSortBy = _dcReportSortBy === 'date' ? 'missing' : 'date';
      renderDcReport(state.dcReport);
    };
  }

  let html = `<div class="dc-report-conclusion ${esc(health.cls)}">最近 ${safeInt(report.trade_days_checked)} 个交易日覆盖率 <strong>${Number(pct).toFixed(1)}%</strong>，${safeInt(report.total_missing) ? '缺失 <strong>' + safeInt(report.total_missing).toLocaleString() + '</strong> 条' : '数据完整'}${pct < 90 ? '，建议执行全量补缺' : ''}</div>`;

  html += `<div class="dc-report-metrics">
    <div class="dc-rm"><span class="dc-rm-val">${safeInt(report.total_expected).toLocaleString()}</span><span class="dc-rm-label">期望条数</span></div>
    <div class="dc-rm"><span class="dc-rm-val">${safeInt(report.total_actual).toLocaleString()}</span><span class="dc-rm-label">实际条数</span></div>
    <div class="dc-rm"><span class="dc-rm-val ${safeInt(report.total_missing) ? 'warning' : 'success'}">${safeInt(report.total_missing).toLocaleString()}</span><span class="dc-rm-label">缺失条数</span></div>
    <div class="dc-rm"><span class="dc-rm-val">${esc(checkTime)}</span><span class="dc-rm-label">检查时间</span></div>
  </div>`;

  let missingDates = [...(report.missing_by_date || [])];
  if (missingDates.length > 0) {
    if (_dcReportSortBy === 'missing') {
      missingDates.sort((a, b) => (b.missing_count || 0) - (a.missing_count || 0));
    } else {
      missingDates.sort((a, b) => String(b.date || '').localeCompare(String(a.date || '')));
    }
    html += `<div class="dc-missing-header">缺失日期 (${missingDates.length} 天)</div>`;
    html += '<div class="dc-missing-list">';
    for (const d of missingDates) {
      const barPct = Math.max(0, Math.min(100, Number(d.coverage_pct) || 0));
      const barColor = barPct >= 99 ? '#22c55e' : (barPct >= 90 ? '#f59e0b' : '#ef4444');
      html += `<div class="dc-missing-row">
          <span class="dc-missing-date">${esc(d.date)}</span>
          <span class="dc-missing-count">缺 ${safeInt(d.missing_count).toLocaleString()}</span>
          <div class="dc-missing-bar-wrap"><div class="dc-missing-bar-fill" style="width:${barPct}%;background:${barColor}"></div></div>
          <span class="dc-missing-pct">覆盖 ${barPct.toFixed(1)}%</span>
        </div>`;
    }
    html += '</div>';
  } else {
    html += '<div style="color:#22c55e;font-size:13px;font-weight:600;padding:8px 0">数据完整，无缺失</div>';
  }

  container.innerHTML = html;
}

function renderDcTaskFilters() {
  const container = document.getElementById('dcTaskFilters');
  if (!container) return;
  const filters = [
    { key: 'all', label: '全部' },
    { key: 'success', label: '成功' },
    { key: 'failed', label: '失败' },
    { key: 'running', label: '运行中' },
  ];
  container.innerHTML = filters.map(f => {
    const active = state.dcTaskFilter === f.key ? ' btn-secondary active' : ' btn-secondary';
    return `<button class="${active}" data-filter="${f.key}">${f.label}</button>`;
  }).join('');
  container.querySelectorAll('button').forEach(btn => {
    btn.onclick = () => {
      state.dcTaskFilter = btn.dataset.filter;
      renderDcTaskFilters();
      renderDcTaskList(state.syncLogs || { items: [], total: 0 });
    };
  });
}

function renderDcTaskList(logs) {
  const container = document.getElementById('dcTaskList');
  const pager = document.getElementById('dcTaskPager');
  let items = logs.items || [];

  renderDcTaskFilters();

  if (state.dcTaskFilter !== 'all') {
    items = items.filter(t => t.status === state.dcTaskFilter);
  }

  if (!items.length) {
    container.innerHTML = '<div class="empty-state"><div class="empty-state-text">暂无历史任务，点击上方按钮发起同步</div></div>';
    pager.innerHTML = '';
    return;
  }

  let html = `<div class="dc-task-header">
    <span class="dc-th dc-th-status"></span>
    <span class="dc-th dc-th-date">交易日</span>
    <span class="dc-th dc-th-mode">触发</span>
    <span class="dc-th dc-th-ok">成功</span>
    <span class="dc-th dc-th-fail">失败</span>
    <span class="dc-th dc-th-total">总数</span>
    <span class="dc-th dc-th-time">时间</span>
  </div>`;

  html += items.map(t => {
    const time = (t.started_at || '').slice(11, 16);
    const dotCls = t.status === 'success' ? 'success' : (t.status === 'running' ? 'warning' : (t.status === 'failed' ? 'error' : 'progress'));
    const failRow = t.status === 'failed' && t.message ? `<div class="dc-task-error">${esc(t.message)}</div>` : '';
    const rawMode = t.trigger_mode || '';
    const shortMode = rawMode.split('_')[0] || rawMode;
    return `<div class="dc-task-item">
        <span class="dc-td dc-th-status"><span class="status-dot status-dot--${dotCls}"></span></span>
        <span class="dc-td dc-th-date">${esc(t.trade_date)}</span>
        <span class="dc-td dc-th-mode" title="${esc(rawMode)}">${esc(shortMode)}</span>
        <span class="dc-td dc-th-ok success">${safeInt(t.success_symbols)}</span>
        <span class="dc-td dc-th-fail ${safeInt(t.failed_symbols) > 0 ? 'error' : ''}">${safeInt(t.failed_symbols)}</span>
        <span class="dc-td dc-th-total">${safeInt(t.total_symbols)}</span>
        <span class="dc-td dc-th-time">${esc(time)}</span>
        ${failRow}
      </div>`;
  }).join('');

  container.innerHTML = html;

  const totalPages = Math.ceil((logs.total || 0) / 15);
  pager.innerHTML = '';
  if (totalPages > 1) {
    const prev = document.createElement('button');
    prev.textContent = '上一页';
    prev.disabled = _dcTaskPage <= 1;
    prev.onclick = () => dcTaskPageNav(-1);
    const label = document.createElement('span');
    label.textContent = `${_dcTaskPage}/${totalPages}`;
    const next = document.createElement('button');
    next.textContent = '下一页';
    next.disabled = _dcTaskPage >= totalPages;
    next.onclick = () => dcTaskPageNav(1);
    pager.append(prev, label, next);
  }
}

function dcTaskPageNav(delta) {
  _dcTaskPage = Math.max(1, _dcTaskPage + delta);
  loadDcLogs({ force: true });
}

function renderDcLogStream(logs) {
  const container = document.getElementById('dcLogStream');
  const toggleBtn = document.getElementById('btnDcLogsToggle');
  const items = logs.items || [];
  if (!items.length) {
    container.innerHTML = '<div class="empty-state"><div class="empty-state-text">暂无同步日志，发起同步后将实时显示</div></div>';
    if (toggleBtn) toggleBtn.style.display = 'none';
    return;
  }
  const showCount = state.dcLogsExpanded ? items.length : Math.min(5, items.length);
  container.innerHTML = items.slice(0, showCount).map(t => `
    <div class="sync-log-item">[${esc(t.status)}] ${esc(t.trade_date)} ${safeInt(t.synced_symbols)}/${safeInt(t.total_symbols)} (${esc(t.trigger_mode)}) ${esc(t.message || '')}</div>
  `).join('');
  if (toggleBtn) {
    toggleBtn.style.display = items.length > 5 ? '' : 'none';
    toggleBtn.textContent = state.dcLogsExpanded ? '收起日志' : `展开全部 (${items.length})`;
    toggleBtn.className = 'collapse-toggle' + (state.dcLogsExpanded ? ' expanded' : '');
  }
}

