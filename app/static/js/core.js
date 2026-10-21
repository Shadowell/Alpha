/* Alpha frontend module: core.js — 全局状态、格式化、页头/市场状态、Toast.
 * 无构建、classic script，按 index.html 中的顺序加载，函数挂在全局作用域。
 */

const state = {
  activeTab: 'market',
  funnel: null,
  hotConcepts: null,
  hotStocks: null,
  syncStatus: null,
  syncLogs: null,
  strategyProfile: null,
  selectedSymbol: null,
  selectedHotSymbol: null,
  selectedConcept: null,
  chart: null,
  noticeFunnel: null,
  noticeSelectedSymbol: null,
  noticeKeywords: [],
  activeKeywords: new Set(),
  predictChart: null,
  dcStats: null,
  dcReport: null,
  dcTaskFilter: 'all',
  dcLogsExpanded: false,
  monitorConfig: null,
  monitorMessages: [],
  monitorKlineChart: null,
  paperUpdatedAt: null,
  predictFunnel: null,
  predictConfig: null,
  predictRunning: false,
  hotStockAI: null,
  hotStockAIConfig: null,
  hotStockAIRunning: false,
  strategyScanSnapshot: null,
};

function fmtNum(v, digits = 2) {
  const n = Number(v || 0);
  return Number.isFinite(n) ? n.toFixed(digits) : '0.00';
}

function pnlCls(v) { return v > 0 ? 'up' : v < 0 ? 'down' : 'neutral'; }

function fmtShortTime(iso) {
  if (!iso) return '--';
  const m = iso.match(/(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/);
  return m ? `${m[2]}-${m[3]} ${m[4]}:${m[5]}` : iso.slice(0, 16);
}

function fmtDateTime(iso) {
  if (!iso) return '--';
  const m = iso.match(/(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})/);
  return m ? `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}:${m[6]}` : iso.slice(0, 19).replace('T', ' ');
}

function fmtMoney(v) {
  const n = Number(v || 0);
  if (!Number.isFinite(n)) return '¥0.00';
  return '¥' + n.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function _scrollToRightPanel() {
  setTimeout(() => {
    const rp = document.querySelector('.right-panel');
    if (rp) rp.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }, 200);
}

function setMeta() {
  const meta = document.getElementById('meta');
  if (state.activeTab === 'market') {
    if (!state.funnel) { meta.textContent = '加载中...'; return; }
    meta.textContent = `交易日 ${state.funnel.trade_date} · 更新 ${fmtDateTime(state.funnel.updated_at)}`;
  } else if (state.activeTab === 'data') {
    meta.textContent = '数据同步 · 完整性检查 · 任务管理';
  } else if (state.activeTab === 'funnel') {
    if (!state.funnel) { meta.textContent = '暂无数据'; return; }
    meta.textContent = `交易日 ${state.funnel.trade_date} · 更新 ${fmtDateTime(state.funnel.updated_at)}`;
  } else if (state.activeTab === 'notice') {
    if (!state.noticeFunnel) { meta.textContent = '暂无数据'; return; }
    const nf = state.noticeFunnel;
    meta.textContent = `公告日 ${nf.trade_date} · 更新 ${fmtDateTime(nf.updated_at)} · 打分源 ${nf.source}`;
  } else if (state.activeTab === 'agent') {
    meta.textContent = '智能监控 / 进化 — 自动追踪市场主线 · 推送投研情报 · 输出诊断建议';
  } else if (state.activeTab === 'paper') {
    const ts = state.paperUpdatedAt || '--';
    meta.textContent = `模拟账户 · 初始资金 ¥1,000,000 · 更新 ${ts}`;
  } else if (state.activeTab === 'predict') {
    const snap = state.predictFunnel;
    if (!snap) { meta.textContent = '预测选股 · 加载中…'; return; }
    const m = snap.meta || {};
    const extra = m.stocks_scanned ? `扫描 ${m.stocks_scanned} · 命中 ${m.entries_count || 0} · 用时 ${m.elapsed_sec || 0}s` : '尚未执行';
    meta.textContent = `交易日 ${snap.trade_date || '--'} · 更新 ${fmtDateTime(snap.updated_at)} · ${extra}`;
  } else if (state.activeTab === 'hotai') {
    const snap = state.hotStockAI;
    if (!snap) { meta.textContent = '热门智能分析 · 加载中…'; return; }
    const m = snap.meta || {};
    const modeText = m.execution_mode === 'light_auto' ? ' · 自动轻量' : '';
    const taPart = m.runtime_tradingagents_enabled
      ? ` · 讨论 ${m.tradingagents_discussed || 0}`
      : (m.tradingagents_enabled ? ' · 已跳过讨论' : '');
    const extra = m.stocks_scanned ? `分析 ${m.entries_count || 0}/${m.stocks_scanned}${modeText} · 均分 ${fmtNum(m.avg_score || 0, 1)}${taPart} · ${m.elapsed_sec || 0}s` : (m.error || '尚未执行');
    meta.textContent = `交易日 ${snap.trade_date || '--'} · 更新 ${fmtDateTime(snap.updated_at)} · ${extra}`;
  }
}

function _updateMarketStatus() {
  const dot = document.getElementById('marketDot');
  const label = document.getElementById('marketLabel');
  if (!dot || !label) return;
  const now = new Date();
  const h = now.getHours(), m = now.getMinutes();
  const t = h * 60 + m;
  const day = now.getDay();
  const isWeekend = day === 0 || day === 6;
  let cls, text;
  if (isWeekend) {
    cls = 'close'; text = '休市';
  } else if (t >= 570 && t < 690) {
    cls = 'open'; text = '上午盘中';
  } else if (t >= 690 && t < 780) {
    cls = 'pre'; text = '午间休市';
  } else if (t >= 780 && t < 900) {
    cls = 'open'; text = '下午盘中';
  } else if (t >= 900) {
    cls = 'close'; text = '已收盘';
  } else {
    cls = 'pre'; text = '盘前';
  }
  dot.className = 'market-dot ' + cls;
  label.textContent = text;
  const timeStr = `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}`;
  label.title = timeStr;
}

function setStatus(text, tone = 'info') {
  const toast = document.createElement('div');
  toast.className = `toast toast-${tone}`;
  toast.textContent = text;
  document.body.appendChild(toast);
  requestAnimationFrame(() => toast.classList.add('show'));
  const duration = tone === 'error' ? 5000 : tone === 'success' ? 3500 : 2500;
  setTimeout(() => {
    toast.classList.remove('show');
    setTimeout(() => toast.remove(), 300);
  }, duration);
}

