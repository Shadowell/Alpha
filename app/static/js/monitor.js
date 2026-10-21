/* Alpha frontend module: monitor.js — 智能监控消息流.
 * 无构建、classic script，按 index.html 中的顺序加载，函数挂在全局作用域。
 */

/* ==================== 智能监控 ==================== */

async function loadMonitorConfig() {
  try {
    const config = await request('/api/agent/monitor/config');
    state.monitorConfig = config;
    const intervalSel = document.getElementById('monitorInterval');
    if (intervalSel) intervalSel.value = String(config.interval_minutes || 10);
    const promptEl = document.getElementById('monitorPrompt');
    if (promptEl) promptEl.value = config.system_prompt || '';
    _updateMonitorStatusUI(!!config.enabled);
  } catch (_) {}
}

async function loadMonitorMessages() {
  try {
    const data = await request('/api/agent/monitor/messages?limit=50&today_only=true');
    state.monitorMessages = data.items || [];
    renderMonitorFeed();
  } catch (_) {}
}

function _updateMonitorStatusUI(enabled) {
  const badge = document.getElementById('monitorStatus');
  const btn = document.getElementById('btnMonitorToggle');
  if (badge) {
    badge.textContent = enabled ? '运行中' : '已停止';
    badge.className = 'monitor-status-badge ' + (enabled ? 'on' : 'off');
  }
  if (btn) btn.textContent = enabled ? '停止监控' : '启动监控';
}

function renderMonitorFeed() {
  const container = document.getElementById('monitorFeed');
  if (!container) return;
  const msgs = state.monitorMessages;
  const countEl = document.getElementById('monitorMsgCount');
  if (countEl) countEl.textContent = msgs.length || '';
  if (!msgs.length) {
    container.innerHTML = '<div class="empty-state"><div class="empty-state-text">暂无推送消息，启动监控或手动触发</div></div>';
    return;
  }
  container.innerHTML = msgs.map(m => _renderMonitorCard(m)).join('');
  container.querySelectorAll('[data-summary-toggle]').forEach((el) => {
    el.onclick = () => {
      const detail = el.previousElementSibling;
      if (!detail) return;
      const hidden = detail.style.display === 'none';
      detail.style.display = hidden ? 'block' : 'none';
      el.textContent = hidden ? '收起' : `展开 ${el.dataset.more || ''} 条`;
    };
  });
  bindHoverKline(container);
}

async function _loadMonitorKline(symbol, name) {
  const section = document.getElementById('monitorKlineSection');
  const titleEl = document.getElementById('monitorKlineTitle');
  const chartEl = document.getElementById('monitorKlineChart');
  section.style.display = 'block';
  titleEl.textContent = `${name}(${symbol}) K线 + 预测`;

  if (state.monitorKlineChart) state.monitorKlineChart.dispose();
  state.monitorKlineChart = echarts.init(chartEl);
  state.monitorKlineChart.setOption(_placeholderOption('加载中...'));

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

    if (!kline.length) {
      state.monitorKlineChart.setOption(_placeholderOption('暂无K线数据'));
      return;
    }

    state.monitorKlineChart.setOption(_klineOption(kline), true);

    try {
      const pred = await request(`/api/predict/${symbol}/kronos?lookback=180&horizon=3`);
      if (pred.merged_kline && pred.merged_kline.length) {
        const pk = pred.predicted_kline || [];
        const predDates = pk.map(x => x.date);
        const rtMap = await _fetchRealtimeMap(symbol, predDates);
        state.monitorKlineChart.setOption(_klinePredictOption(pred.merged_kline, pred.prediction_start_index, rtMap), true);
      }
    } catch {}
  } catch (err) {
    state.monitorKlineChart.setOption(_placeholderOption(`加载失败: ${err.message}`));
  }
  section.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function _parseMonitorThemes(text) {
  const themes = [];
  const cnIdx = '一二三四五六七八九十';

  const splitRe = new RegExp(
    `(?=(?:^|\\n)\\s*(?:【[高中低]+[高]?】\\s*主线[${cnIdx}\\d]|[${cnIdx}]+[、\\.]\\s*【[高中低]+[高]?】))`
  );
  const blocks = text.split(splitRe);
  const seenTitles = new Set();

  const headRe = new RegExp(
    `(?:【([高中低]+[高]?)】\\s*主线([${cnIdx}\\d]+)[：:]\\s*(.+?)(?:\\n|$))` +
    `|(?:([${cnIdx}\\d]+)[、\\.]\\s*【([高中低]+[高]?)】\\s*([^\\n]+?)(?:\\n|$))`
  );

  for (const rawBlock of blocks) {
    const tailIdx = rawBlock.search(/(?:={6,}|-{10,}|\n\s*(?:10\s*分钟内)?执行摘要)/);
    const block = tailIdx >= 0 ? rawBlock.slice(0, tailIdx) : rawBlock;
    const m = block.match(headRe);
    if (!m) continue;
    let level, idx, title;
    if (m[1]) { level = m[1]; idx = m[2]; title = m[3]; }
    else { idx = m[4]; level = m[5]; title = m[6]; }
    title = (title || '').trim().replace(/[（(][^）)]*[）)]\s*$/, '').trim();
    if (!title) continue;
    const dedupeKey = `${level}|${title}`;
    if (seenTitles.has(dedupeKey)) continue;
    seenTitles.add(dedupeKey);

    const stocks = [];
    const stockRe = /[-·]\s*(\d{6})\s+([^\s：:]+)[：:]\s*(.+)/g;
    const stockSection = block.match(/关注个股[\s\S]*?(?=\n\s*\d\)\s|失效条件|【|$)/i);
    const stockText = stockSection ? stockSection[0] : block;
    let sm;
    while ((sm = stockRe.exec(stockText)) !== null) {
      stocks.push({ symbol: sm[1], name: sm[2], reason: sm[3].trim() });
    }

    let analysis = '';
    const logicMatch = block.match(/逻辑链[\s\S]*?(?=\n\s*\d\)\s*关注|$)/i);
    if (logicMatch) {
      analysis = logicMatch[0].replace(/^.*逻辑链[（(].*?[)）]\s*/i, '').replace(/^.*逻辑链[：:\s]*/i, '').trim();
      analysis = analysis.split('\n').filter(l => l.trim().startsWith('-')).map(l => l.trim().replace(/^-\s*/, '')).join(' ');
    }

    let risk = '';
    const riskMatch = block.match(/失效条件[\s\S]*?(?=\n\s*\d\)\s|【稳健|【激进|$)/i);
    if (riskMatch) {
      risk = riskMatch[0].replace(/^.*失效条件[^\n]*\n/i, '').trim();
      risk = risk.split('\n').filter(l => l.trim().startsWith('-')).map(l => l.trim().replace(/^-\s*/, '')).slice(0, 2).join('；');
    }

    themes.push({ level, idx, title, analysis, risk, stocks });
  }
  return themes;
}

function _renderMonitorCard(msg) {
  const fullTime = fmtShortTime(msg.created_at);
  const triggerLabel = msg.trigger === 'manual' ? '手动触发' : '自动轮询';
  const triggerCls = msg.trigger === 'manual' ? 'trigger-manual' : 'trigger-auto';
  const raw = msg.content || '';
  const themes = _parseMonitorThemes(raw);

  if (!themes.length) {
    return `<div class="monitor-msg-card">
      <div class="monitor-msg-header">
        <span class="monitor-msg-time">${esc(fullTime)}</span>
        <span class="monitor-msg-trigger ${triggerCls}">${triggerLabel}</span>
      </div>
      <div class="monitor-msg-body">${_formatPlainContent(raw)}</div>
    </div>`;
  }

  let summaryMatch = raw.match(/10分钟内执行摘要([\s\S]*?)$/i) || raw.match(/执行摘要([\s\S]*?)$/i);
  let summaryHtml = '';
  if (summaryMatch) {
    const allLines = summaryMatch[1].trim().split('\n').filter(l => l.trim());
    const keyLines = allLines.filter(l => l.trim().startsWith('-')).map(l => l.trim().replace(/^-\s*/, ''));
    if (keyLines.length) {
      const preview = keyLines.slice(0, 2).map(l => esc(l)).join(' | ');
      const rest = keyLines.slice(2);
      const restHtml = rest.length
        ? `<div class="monitor-summary-detail" style="display:none">${rest.map(l => `<div>· ${esc(l)}</div>`).join('')}</div>
           <span class="monitor-summary-toggle" data-summary-toggle data-more="${rest.length}">展开 ${rest.length} 条</span>`
        : '';
      summaryHtml = `<div class="monitor-summary"><b>执行摘要</b> ${preview}${restHtml}</div>`;
    }
  }

  const themesHtml = themes.map(t => {
    const levelCls = t.level === '高' ? 'high' : t.level.includes('高') ? 'mid-high' : t.level === '中' ? 'mid' : 'low';
    const stocksHtml = t.stocks.map(s =>
      `<div class="mtheme-stock" data-symbol="${esc(s.symbol)}" data-name="${esc(s.name)}" title="点击查看 ${esc(s.name)} K线预测">` +
      `<code>${esc(s.symbol)}</code> <b>${esc(s.name)}</b><svg viewBox="0 0 20 20" fill="currentColor" class="mtheme-stock-arrow"><path fill-rule="evenodd" d="M7.21 14.77a.75.75 0 01.02-1.06L11.168 10 7.23 6.29a.75.75 0 111.04-1.08l4.5 4.25a.75.75 0 010 1.08l-4.5 4.25a.75.75 0 01-1.06-.02z" clip-rule="evenodd"/></svg></div>`
    ).join('');
    const analysisHtml = t.analysis ? `<div class="mtheme-analysis">${esc(t.analysis)}</div>` : '';
    const riskHtml = t.risk ? `<div class="mtheme-risk">风险: ${esc(t.risk)}</div>` : '';
    return `<div class="mtheme-card ${levelCls}">
      <div class="mtheme-header">
        <span class="mtheme-level ${levelCls}">${esc(t.level)}</span>
        <span class="mtheme-title">${esc(t.title)}</span>
      </div>
      ${analysisHtml}
      ${riskHtml}
      <div class="mtheme-pool-section">
        <div class="mtheme-pool-label">关注池 · ${t.stocks.length}只</div>
        <div class="mtheme-pool">${stocksHtml || '<span class="muted">暂无</span>'}</div>
      </div>
    </div>`;
  }).join('');

  return `<div class="monitor-msg-card">
    <div class="monitor-msg-header">
      <span class="monitor-msg-time">${esc(fullTime)}</span>
      <span class="monitor-msg-trigger ${triggerCls}">${triggerLabel}</span>
    </div>
    ${summaryHtml}
    <div class="mtheme-grid">${themesHtml}</div>
  </div>`;
}

function _formatPlainContent(text) {
  let html = esc(text);
  html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  html = html.replace(/【(.+?)】/g, '<span class="monitor-tag">$1</span>');
  html = html.replace(/\n/g, '<br>');
  return html;
}

function _appendMonitorMessage(msg) {
  state.monitorMessages.unshift(msg);
  renderMonitorFeed();
  const feed = document.getElementById('monitorFeed');
  if (feed) feed.scrollTop = 0;
}

