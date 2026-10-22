/* Alpha frontend module: core.js — 全局状态、格式化、页头/市场状态、Toast.
 * 无构建、classic script，按 index.html 中的顺序加载，函数挂在全局作用域。
 */

const state = {
  activeTab: 'market',
  funnel: null,
  // #57：服务端会话口径的备用载体（后端在 snapshot 里补 market_session / is_trading_day 后即生效）。
  snapshot: null,
  marketSnapshot: null,
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
  dcEnrichment: null,
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
  // #50：后端 POST /api/pool/enroll 未上线（404）时置位，候选计数与卡片操作区随之降级说明。
  poolEnrollUnsupported: false,
  poolEnrollError: '',
};

// 测试与浏览器调试入口；classic module 的顶层 const 本身不会自动成为 window 属性。
window.state = state;

/* ==================== 东八区时间（#57） ==================== */
// 全站"现在几点 / 是不是盘中"的判断统一走 getChinaNowParts()，禁止再各处内联
// new Date().getHours()，否则在非 Asia/Shanghai 设备（境外 VPS 面板、UTC CI 容器）上会整体偏移。
//
// TODO(后端契约接入)：/api/funnel 与 /ws/realtime snapshot 一旦提供
//   market_session: 'pre' | 'open' | 'lunch' | 'closed' | 'holiday'
//   is_trading_day: boolean
//   china_time: ISO 字符串
// 前端即优先采用服务端口径（getMarketSession() 的 source 会是 'server'）。
// 前端拿不到法定节假日日历，本地兜底只能识别周六周日 —— 节假日的盘中时段仍会被
// 误判为开盘，这里不假装支持，等 #57 建议的服务端交易日历接入后消除。

const CHINA_TZ_ID = 'Asia/Shanghai';
const MARKET_SESSION_VALUES = ['pre', 'open', 'lunch', 'closed', 'holiday'];
const _MARKET_SESSION_LABELS = {
  pre: { cls: 'pre', text: '盘前' },
  open: { cls: 'open', text: '盘中' },
  lunch: { cls: 'pre', text: '午间休市' },
  closed: { cls: 'close', text: '已收盘' },
  holiday: { cls: 'close', text: '休市' },
  weekend: { cls: 'close', text: '休市' },
};

let _chinaTimeFormatter = null;

function _pad2(n) { return String(n).padStart(2, '0'); }

function _getChinaTimeFormatter() {
  if (!_chinaTimeFormatter) {
    _chinaTimeFormatter = new Intl.DateTimeFormat('en-US', {
      timeZone: CHINA_TZ_ID,
      hour12: false,
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
    });
  }
  return _chinaTimeFormatter;
}

/**
 * 以 Asia/Shanghai 为准拆解"现在"，与浏览器所在时区无关。
 * @returns {{year:number,month:number,day:number,isoDate:string,hour:number,minute:number,
 *   second:number,weekday:number,minutesOfDay:number,isWeekend:boolean,clock:string}}
 */
function getChinaNowParts(now = new Date()) {
  const raw = {};
  for (const part of _getChinaTimeFormatter().formatToParts(now)) raw[part.type] = part.value;
  const year = Number(raw.year);
  const month = Number(raw.month);
  const day = Number(raw.day);
  // hour12:false 在部分引擎实现对午夜返回 "24"，归一化为 0。
  const hour = Number(raw.hour) === 24 ? 0 : Number(raw.hour);
  const minute = Number(raw.minute);
  const second = Number(raw.second);
  // weekday 由北京日期反查（0=周日 … 6=周六），同样不受本地时区影响。
  const weekday = new Date(Date.UTC(year, month - 1, day)).getUTCDay();
  return {
    year, month, day,
    isoDate: `${year}-${_pad2(month)}-${_pad2(day)}`,
    hour, minute, second,
    weekday,
    minutesOfDay: hour * 60 + minute,
    isWeekend: weekday === 0 || weekday === 6,
    clock: `${_pad2(hour)}:${_pad2(minute)}`,
  };
}

// A 股时段（分钟口径）：9:30–11:30 上午盘中、11:30–13:00 午间休市、13:00–15:00 下午盘中。
const _CN_MARKET_RANGES = [
  { session: 'open', label: '上午盘中', from: 9 * 60 + 30, to: 11 * 60 + 30 },
  { session: 'lunch', label: '午间休市', from: 11 * 60 + 30, to: 13 * 60 },
  { session: 'open', label: '下午盘中', from: 13 * 60, to: 15 * 60 },
];

function _localMarketSession(parts) {
  if (parts.isWeekend) return { session: 'weekend', label: '休市' };
  for (const range of _CN_MARKET_RANGES) {
    if (parts.minutesOfDay >= range.from && parts.minutesOfDay < range.to) {
      return { session: range.session, label: range.label };
    }
  }
  return parts.minutesOfDay >= 15 * 60
    ? { session: 'closed', label: '已收盘' }
    : { session: 'pre', label: '盘前' };
}

// 服务端会话口径优先：只要 snapshot 给了合法 market_session 就直接采用。
function _serverMarketSession() {
  const sources = [state.funnel, state.snapshot, state.marketSnapshot];
  for (const src of sources) {
    const value = src && src.market_session;
    if (typeof value === 'string' && MARKET_SESSION_VALUES.includes(value)) {
      const tradingDay = typeof src.is_trading_day === 'boolean' ? src.is_trading_day : value !== 'holiday';
      if (!tradingDay) return { session: 'holiday', label: '休市（非交易日）', source: 'server' };
      const meta = _MARKET_SESSION_LABELS[value];
      return { session: value, label: value === 'open' ? '盘中' : (meta ? meta.text : value), source: 'server' };
    }
    if (src && src.is_trading_day === false) {
      return { session: 'holiday', label: '休市（非交易日）', source: 'server' };
    }
  }
  return null;
}

/**
 * 当前交易时段（服务端优先，本地按东八区兜底）。
 * @returns {{session:string,label:string,source:'server'|'local',parts:object}}
 */
function getMarketSession(now = new Date()) {
  const parts = getChinaNowParts(now);
  const server = _serverMarketSession();
  if (server) return { ...server, parts };
  const local = _localMarketSession(parts);
  return { ...local, source: 'local', parts };
}

// 买卖闸门与行情频率共用：只有真正的盘中才允许模拟成交。
function isChinaMarketOpen(now = new Date()) {
  return getMarketSession(now).session === 'open';
}

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

function _updateMarketStatus(now = new Date()) {
  const dot = document.getElementById('marketDot');
  const label = document.getElementById('marketLabel');
  if (!dot || !label) return;
  // #57：不再读浏览器本地时钟，统一用东八区口径（服务端 market_session 优先）。
  const info = getMarketSession(now);
  const meta = _MARKET_SESSION_LABELS[info.session] || { cls: 'close', text: '未知' };
  const text = info.label || meta.text;
  dot.className = 'market-dot ' + meta.cls;
  dot.dataset.session = info.session;
  dot.dataset.source = info.source;
  label.textContent = text;
  label.dataset.session = info.session;
  label.dataset.source = info.source;
  label.title = `北京时间 ${info.parts.clock}（${info.parts.isoDate}）· 口径 ${info.source === 'server' ? '服务端' : '前端东八区兜底'}`;
}

/* ==================== 区块级失败与重试（#55） ==================== */
// 并发区块里某一个请求挂起/失败时，只让该区块显示错误 + 独立重试按钮，
// 其余区块照常渲染，页面不再停在整片"加载中"。

function _blockErrorText(err) {
  if (!err) return '未知错误';
  if (err.code === 'timeout') return err.message || '接口超时';
  if (err.code === 'network') return err.message || '后端服务不可用(连接失败)';
  if (err.code === 'http') return err.message || `接口返回 HTTP ${err.status}`;
  return err.message || '未知错误';
}

/**
 * 在容器内渲染「区块加载失败 + 重试」。
 * @param {HTMLElement|string} target 容器元素或其 id
 * @param {Error} err request() 抛出的错误
 * @param {Function} retry 点击重试时执行的异步函数（通常是该区块的加载函数）
 * @param {{title?:string}} [opts]
 */
function showBlockError(target, err, retry, opts = {}) {
  const el = typeof target === 'string' ? document.getElementById(target) : target;
  if (!el) return;
  el.innerHTML = '';
  const box = document.createElement('div');
  box.className = 'block-error';
  box.dataset.block = el.id || opts.title || 'block';
  const text = document.createElement('div');
  text.className = 'block-error-text';
  text.textContent = `${opts.title || '该区块'}加载失败：${_blockErrorText(err)}`;
  const hint = document.createElement('div');
  hint.className = 'block-error-hint';
  hint.textContent = err && err.code === 'timeout'
    ? '接口在超时预算内没有返回，其余区块不受影响，可单独重试。'
    : '后端接口异常，其余区块不受影响，可单独重试。';
  const btn = document.createElement('button');
  btn.className = 'btn-secondary btn-sm block-retry';
  btn.textContent = '重试该区块';
  btn.onclick = async () => {
    btn.disabled = true;
    btn.textContent = '重试中…';
    try { await retry(); } catch (_) { /* 重试失败由 retry 自己再次渲染错误 */ }
    finally { btn.disabled = false; btn.textContent = '重试该区块'; }
  };
  box.append(text, hint, btn);
  el.appendChild(box);
}

function clearBlockError(target) {
  const el = typeof target === 'string' ? document.getElementById(target) : target;
  if (!el) return;
  el.querySelectorAll('.block-error').forEach((node) => node.remove());
}

/**
 * 区块取数失败的统一出口（#55）：
 *  - 该区块从来没有数据 → 就地显示错误 + 独立重试按钮；
 *  - 已有数据（只是刷新失败）→ 保留旧数据，只弹提示，避免把可用内容擦成错误态。
 */
function reportBlockFailure(targetId, err, retry, opts = {}) {
  if (opts.hasData) {
    setStatus(`${opts.title || '该区块'}刷新失败：${_blockErrorText(err)}（仍展示上次数据）`, 'error');
    return;
  }
  showBlockError(targetId, err, retry, { title: opts.title });
}

/* ==================== 长列表折叠（#56） ==================== */
// 候选池一次铺几十上百张卡片会拖慢首屏与滚动，这里统一按阈值折叠并保留展开入口。
const POOL_LIST_COLLAPSE_THRESHOLD = 20;
const _listExpandFlags = {};
// 暴露阈值，便于调试与 E2E 断言（const 不会自动挂到 window）。
window.POOL_LIST_COLLAPSE_THRESHOLD = POOL_LIST_COLLAPSE_THRESHOLD;

/**
 * 折叠容器内超出阈值的卡片（容器直接子元素即卡片）。
 * @returns {{total:number, visible:number, collapsed:boolean}}
 */
function _applyLongListCollapse(container, threshold = POOL_LIST_COLLAPSE_THRESHOLD, expandKey = '') {
  if (!container) return { total: 0, visible: 0, collapsed: false };
  const key = expandKey || container.id || '';
  const cards = Array.from(container.children).filter((node) => !node.classList.contains('list-collapse-toggle'));
  const total = cards.length;
  container.querySelectorAll('.list-collapse-toggle').forEach((node) => node.remove());
  const expanded = !!_listExpandFlags[key];
  if (total <= threshold) {
    cards.forEach((card) => { card.style.display = ''; });
    container.dataset.collapsed = '0';
    container.dataset.visibleCount = String(total);
    return { total, visible: total, collapsed: false };
  }
  const visible = expanded ? total : threshold;
  cards.forEach((card, idx) => { card.style.display = idx < visible ? '' : 'none'; });
  container.dataset.collapsed = expanded ? '0' : '1';
  container.dataset.visibleCount = String(visible);
  container.dataset.totalCount = String(total);
  const btn = document.createElement('button');
  btn.className = 'btn-secondary btn-sm list-collapse-toggle';
  btn.textContent = expanded ? `收起列表（仅显示前 ${threshold} 条）` : `展开全部 (${total})`;
  btn.dataset.expanded = expanded ? '1' : '0';
  btn.onclick = (e) => {
    e.preventDefault();
    e.stopPropagation();
    _listExpandFlags[key] = !expanded;
    _applyLongListCollapse(container, threshold, key);
  };
  container.appendChild(btn);
  return { total, visible, collapsed: !expanded };
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

