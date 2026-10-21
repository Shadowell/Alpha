/* Alpha frontend module: agent.js — Hermes Agent.
 * 无构建、classic script，按 index.html 中的顺序加载，函数挂在全局作用域。
 */

/* ==================== Hermes Agent ==================== */

async function loadAgentData() {
  await Promise.all([loadAgentStatus(), loadAgentTasks(), loadMonitorConfig(), loadMonitorMessages()]);
}

async function loadAgentStatus() {
  try {
    const data = await request('/api/agent/status');
    const dot = document.getElementById('agentStatusDot');
    const txt = document.getElementById('agentStatusText');
    if (dot) dot.className = 'agent-status-dot ' + (data.running ? 'running' : (data.llm_available ? 'ok' : 'error'));

    let mode = '规则模式';
    if (data.hermes_agent_available) mode = 'Hermes Agent';
    else if (data.llm_available) mode = 'LLM 模式';
    if (txt) txt.textContent = data.running ? `运行中 · ${mode}` : `就绪 · ${mode}`;

    const lastEl = document.getElementById('agentLastRun');
    const nextEl = document.getElementById('agentNextRun');
    if (lastEl) {
      if (data.last_run) {
        const t = fmtShortTime(data.last_run.finished_at);
        lastEl.textContent = `上次: ${t} · ${data.last_run.status}`;
      } else {
        lastEl.textContent = '上次: 尚未执行';
      }
    }

    if (nextEl) {
      if (state.monitorConfig?.enabled) {
        const interval = state.monitorConfig.interval_minutes || 10;
        nextEl.textContent = `自动: 每${interval}分钟`;
      } else {
        nextEl.textContent = '自动: 未启用';
      }
    }

  } catch { /* ignore */ }
}

async function loadAgentTasks() {
  const container = document.getElementById('agentTasks');
  try {
    const data = await request('/api/agent/tasks?limit=10');
    if (!data.items || data.items.length === 0) {
      container.innerHTML = '<div class="empty-state"><div class="empty-state-text">尚无运行记录，点击上方「手动诊断」启动</div></div>';
      return;
    }
    container.innerHTML = data.items.map(t => {
      const typeLabel = { daily_review: '盘后复盘', notice_review: '公告复盘', full_diagnosis: '全面诊断' }[t.task_type] || t.task_type;
      const statusLabel = { success: '成功', failed: '失败', timeout: '超时', running: '运行中' }[t.status] || t.status;
      const statusDotCls = { success: 'success', failed: 'error', timeout: 'warning', running: 'progress' }[t.status] || 'progress';
      const time = (t.finished_at || t.started_at || '').slice(11, 16);
      const elapsed = t.elapsed_ms ? `${(t.elapsed_ms / 1000).toFixed(1)}s` : '--';
      const failDetail = t.status === 'failed' && t.error
        ? `<div style="grid-column:1/-1;font-size:11px;color:var(--status-error);padding-left:20px;margin-top:-4px">${esc(t.error)}</div>`
        : '';
      return `
        <div class="agent-task-row" style="flex-wrap:wrap">
          <span class="status-dot status-dot--${esc(statusDotCls)}"></span>
          <span class="agent-task-type">${esc(typeLabel)}</span>
          <span class="agent-task-status ${esc(t.status)}">${esc(statusLabel)}</span>
          <span class="agent-task-time">${esc(time)}</span>
          <span class="agent-task-elapsed">${esc(elapsed)}</span>
          <span class="agent-task-time">${esc(t.trigger || '')}</span>
          ${failDetail}
        </div>`;
    }).join('');
  } catch {
    container.innerHTML = '<div class="empty-state"><div class="empty-state-text">运行记录加载失败</div></div>';
  }
}

