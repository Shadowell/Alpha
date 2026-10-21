/* Alpha frontend module: boot.js — init 启动流程与轮询任务注册.
 * 无构建、classic script，按 index.html 中的顺序加载，函数挂在全局作用域。
 */

/* ==================== Init ==================== */

async function init() {
  document.querySelectorAll('.sidebar-item[data-tab]').forEach((el) => {
    el.onclick = (e) => { e.preventDefault(); switchTab(el.dataset.tab); };
  });

  document.querySelectorAll('.agent-inner-tab').forEach(btn => {
    btn.onclick = () => {
      const sub = btn.dataset.subtab;
      document.querySelectorAll('.agent-inner-tab').forEach(b => b.classList.toggle('active', b === btn));
      document.getElementById('agentSubMonitor').classList.toggle('active', sub === 'monitor');
    };
  });

  document.getElementById('predictModalClose').onclick = closePredictModal;
  document.getElementById('predictModal').onclick = (e) => {
    if (e.target === e.currentTarget) closePredictModal();
  };

  _updateMarketStatus();

  document.getElementById('btnPaperRefresh').onclick = () => {
    refreshPaperPrices();
    _startPaperPoll();
  };
  document.getElementById('btnPaperSettings').onclick = () => {
    const panel = document.getElementById('paperSettingsPanel');
    panel.style.display = panel.style.display === 'none' ? '' : 'none';
  };
  document.getElementById('btnPaperSettingsSave').onclick = async () => {
    try {
      await request('/api/paper/settings', {
        method: 'POST',
        body: JSON.stringify({
          commission_rate: parseFloat(document.getElementById('psCommission').value),
          min_commission: parseFloat(document.getElementById('psMinComm').value),
          stamp_tax_rate: parseFloat(document.getElementById('psStampTax').value),
          slippage_rate: parseFloat(document.getElementById('psSlippage').value),
        }),
      });
      setStatus('费用设置已保存', 'success');
      document.getElementById('paperSettingsPanel').style.display = 'none';
      loadPaperData();
    } catch (err) {
      setStatus(`保存失败: ${err.message}`, 'error');
    }
  };
  document.getElementById('btnMonitorKlineClose').onclick = () => {
    document.getElementById('monitorKlineSection').style.display = 'none';
    if (state.monitorKlineChart) { state.monitorKlineChart.dispose(); state.monitorKlineChart = null; }
  };

  _initHoverKlinePopupEvents();

  const dcStart = document.getElementById('dcSyncStart');
  const dcEnd = document.getElementById('dcSyncEnd');
  const _fmtDate = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  const today = new Date();
  dcEnd.value = _fmtDate(today);
  const weekAgo = new Date(today); weekAgo.setDate(weekAgo.getDate() - 7);
  dcStart.value = _fmtDate(weekAgo);

  document.getElementById('btnDcFullSync').onclick = async () => {
    const btn = document.getElementById('btnDcFullSync');
    _btnStart(btn, '提交中...');
    setStatus('正在提交全量补缺任务...', 'info');
    try {
      const payload = await request('/api/jobs/kline-cache/sync?trigger_mode=manual&force=true', { method: 'POST' });
      const queued = Number(payload.queue_size || 0);
      setStatus(`全量补缺任务已提交后台执行${queued > 1 ? ` · 队列${queued}个任务` : ''}`, 'success');
      await loadDataCenter();
    } catch (err) {
      setStatus(`提交失败: ${err.message}`, 'error');
      await loadDataCenter();
    } finally {
      _btnEnd(btn);
    }
  };

  document.getElementById('btnDcInit').onclick = async () => {
    const btn = document.getElementById('btnDcInit');
    btn.disabled = true;
    btn.textContent = '提交中...';
    setStatus('正在提交一键初始化任务...', 'info');
    try {
      const payload = await request('/api/jobs/kline-cache/initialize', { method: 'POST' });
      state._dbStatsPromise = null; // 初始化后失效缓存，让引导横幅重新判断
      if (payload.already_initialized) {
        setStatus('数据库已有数据，本次为强制重新初始化（全量回补）', 'info');
      } else {
        setStatus(`初始化任务已提交：回补全市场近 ${payload.window_days} 个自然日K线，进度将在下方实时展示`, 'success');
      }
      await loadDataCenter();
    } catch (err) {
      setStatus(`初始化提交失败: ${err.message}`, 'error');
    } finally {
      btn.disabled = false;
      btn.textContent = '一键初始化数据';
    }
  };

  function _tradeDatesBetween(start, end) {
    const dates = [];
    const cur = new Date(start + 'T00:00:00');
    const last = new Date(end + 'T00:00:00');
    while (cur <= last) {
      const day = cur.getDay();
      if (day !== 0 && day !== 6) dates.push(_fmtDate(cur));
      cur.setDate(cur.getDate() + 1);
    }
    return dates;
  }

  document.getElementById('btnDcIncrSync').onclick = async () => {
    const btn = document.getElementById('btnDcIncrSync');
    const startVal = dcStart.value;
    const endVal = dcEnd.value;
    if (!startVal || !endVal) { setStatus('请选择起止日期', 'error'); return; }
    if (startVal > endVal) { setStatus('开始日期不能晚于结束日期', 'error'); return; }
    const dates = _tradeDatesBetween(startVal, endVal);
    if (!dates.length) { setStatus('所选范围内无交易日', 'error'); return; }
    _btnStart(btn, '提交中...');
    setStatus(`正在提交增量同步任务 ${startVal} ~ ${endVal} (${dates.length}天)...`, 'info');
    try {
      const qs = new URLSearchParams({ start_date: startVal, end_date: endVal, trigger_mode: 'manual' });
      const payload = await request(`/api/jobs/kline-cache/batch-incremental-sync?${qs.toString()}`, { method: 'POST' });
      const submitted = Number(payload.submitted || 0);
      const queued = Number(payload.queue_size || 0);
      setStatus(`增量同步已批量提交: ${submitted}天${queued > submitted ? ` · 队列${queued}个任务` : ''}`, submitted > 0 ? 'success' : 'error');
      await loadDataCenter();
    } catch (err) {
      setStatus(`增量同步提交失败: ${err.message}`, 'error');
      await loadDataCenter();
    } finally {
      _btnEnd(btn);
    }
  };

  document.getElementById('btnDcLogsToggle').onclick = () => {
    state.dcLogsExpanded = !state.dcLogsExpanded;
    renderDcLogStream(state.syncLogs || { items: [], total: 0 });
  };

  document.getElementById('btnDcCheck').onclick = async () => {
    const btn = document.getElementById('btnDcCheck');
    _btnStart(btn, '检查中...');
    setStatus('数据完整性检查中...', 'info');
    try {
      const report = await request('/api/jobs/kline-cache/check', { method: 'POST' });
      setStatus(`检查完成: 覆盖率 ${report.coverage_pct || 0}% 缺失 ${report.total_missing || 0} 条`, 'success');
      await loadDataCenter();
      document.getElementById('dcReportContent')?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    } catch (err) {
      setStatus(`检查失败: ${err.message}`, 'error');
    } finally {
      _btnEnd(btn);
    }
  };

  document.getElementById('clearConcept').onclick = () => {
    state.selectedConcept = null;
    renderHotConcepts();
    renderFunnel();
  };

  document.getElementById('btnNoticeScreen').onclick = async () => {
    const btn = document.getElementById('btnNoticeScreen');
    _btnStart(btn, '执行中...');
    setStatus('公告筛选执行中...', 'info');
    try {
      const today = new Date();
      const y = today.getFullYear();
      const m = String(today.getMonth() + 1).padStart(2, '0');
      const d = String(today.getDate()).padStart(2, '0');
      let url = `/api/jobs/notice-screen?notice_date=${y}${m}${d}&limit=50`;
      if (state.activeKeywords.size > 0) {
        url += `&keywords=${encodeURIComponent([...state.activeKeywords].join(','))}`;
      }
      const payload = await requestJob(url, { method: 'POST' }, {
        onProgress: (job) => setStatus(`公告筛选 ${_jobProgressText(job)}`, 'info'),
      });
      await reloadNotice();
      if (payload.degraded) {
        setStatus(payload.message || '公告数据源超时，已返回上次公告池', 'error');
      } else {
        setStatus(`公告筛选完成: ${payload.candidate_count || 0}只`, 'success');
      }
    } catch (err) {
      setStatus(`公告筛选失败: ${err.message}`, 'error');
    } finally {
      _btnEnd(btn);
    }
  };

  document.getElementById('btnAgentRun').onclick = async () => {
    const btn = document.getElementById('btnAgentRun');
    _btnStart(btn, '诊断执行中...');
    setStatus('Hermes 正在采集数据并执行诊断分析...', 'info');
    try {
      const payload = await requestJob(
        '/api/agent/run',
        { method: 'POST', body: JSON.stringify({ task_type: 'full_diagnosis' }) },
        { intervalMs: 2000, onProgress: (job) => setStatus(`诊断${_jobProgressText(job)}`, 'info') },
      );
      const msg = payload.summary?.message || payload.message || '全面诊断完成';
      const dailyUsedLLM = payload.summary?.daily?.llm_used;
      const noticeUsedLLM = payload.summary?.notice?.llm_used;
      const llmHint = (dailyUsedLLM || noticeUsedLLM) ? '（LLM 分析）' : '（规则诊断）';
      setStatus(`✅ ${msg} ${llmHint}`, 'success');
      await loadAgentData();
    } catch (err) {
      setStatus(`诊断失败: ${err.message}`, 'error');
    } finally {
      _btnEnd(btn);
    }
  };

  // ── 智能监控按钮绑定 ──
  document.getElementById('btnMonitorToggle').onclick = async () => {
    const btn = document.getElementById('btnMonitorToggle');
    const isOn = state.monitorConfig && state.monitorConfig.enabled;
    if (isOn) {
      _btnStart(btn, '停止中...');
      try {
        await request('/api/agent/monitor/stop', { method: 'POST' });
        state.monitorConfig.enabled = false;
        _updateMonitorStatusUI(false);
        setStatus('智能监控已停止', 'success');
      } catch (err) { setStatus(`停止失败: ${err.message}`, 'error'); }
      finally { _btnEnd(btn); }
    } else {
      _btnStart(btn, '启动中...');
      try {
        const interval = parseInt(document.getElementById('monitorInterval').value) || 10;
        const result = await request('/api/agent/monitor/config', {
          method: 'POST',
          body: JSON.stringify({ enabled: true, interval_minutes: interval }),
        });
        state.monitorConfig = result;
        _updateMonitorStatusUI(true);
        setStatus(`智能监控已启动，每 ${interval} 分钟推送`, 'success');
      } catch (err) { setStatus(`启动失败: ${err.message}`, 'error'); }
      finally { _btnEnd(btn); }
    }
  };

  document.getElementById('btnMonitorTrigger').onclick = async () => {
    const btn = document.getElementById('btnMonitorTrigger');
    _btnStart(btn, '分析中...');
    setStatus('智能监控手动触发中，请稍候...', 'info');
    try {
      const result = await request('/api/agent/monitor/trigger', { method: 'POST' });
      if (result.success) {
        setStatus(`智能监控完成 (${result.elapsed_ms || 0}ms)`, 'success');
      } else {
        setStatus(`监控执行失败: ${result.message || ''}`, 'error');
      }
    } catch (err) {
      setStatus(`手动触发失败: ${err.message}`, 'error');
    } finally { _btnEnd(btn); }
  };

  document.getElementById('btnMonitorPromptToggle').onclick = () => {
    const wrap = document.getElementById('monitorPromptWrap');
    wrap.style.display = wrap.style.display === 'none' ? 'block' : 'none';
  };

  document.getElementById('btnMonitorPromptSave').onclick = async () => {
    const btn = document.getElementById('btnMonitorPromptSave');
    const prompt = document.getElementById('monitorPrompt').value.trim();
    if (!prompt) { setStatus('提示词不能为空', 'error'); return; }
    _btnStart(btn, '保存中...');
    try {
      const result = await request('/api/agent/monitor/config', {
        method: 'POST', body: JSON.stringify({ system_prompt: prompt }),
      });
      state.monitorConfig = result;
      setStatus('提示词已保存', 'success');
    } catch (err) { setStatus(`保存失败: ${err.message}`, 'error'); }
    finally { _btnEnd(btn); }
  };

  document.getElementById('btnMonitorPromptReset').onclick = async () => {
    try {
      const result = await request('/api/agent/monitor/config', {
        method: 'POST', body: JSON.stringify({ system_prompt: null }),
      });
      state.monitorConfig = result;
      document.getElementById('monitorPrompt').value = result.system_prompt || '';
      setStatus('提示词已恢复默认', 'success');
    } catch (err) { setStatus(`恢复失败: ${err.message}`, 'error'); }
  };

  document.getElementById('monitorInterval').onchange = async () => {
    const interval = parseInt(document.getElementById('monitorInterval').value) || 10;
    try {
      const result = await request('/api/agent/monitor/config', {
        method: 'POST', body: JSON.stringify({ interval_minutes: interval }),
      });
      state.monitorConfig = result;
    } catch (_) {}
  };

  const urlTab = new URLSearchParams(window.location.search).get('tab');
  switchTab((urlTab && TAB_TITLES[urlTab]) ? urlTab : (state.activeTab || 'market'));

  await reload();
  _refreshDbEmptyBanners();
  connectWs();

  _registerPollingTasks();

  _bindPredictActions();
  _bindHotAiActions();
  _bindStrategyCenterActions();
}

function _pollMarketHot() {
  return Promise.allSettled([
    request('/api/market/hot-concepts'),
    request('/api/market/hot-stocks'),
  ]).then(([hc, hs]) => {
    if (hc.status === 'fulfilled') state.hotConcepts = hc.value;
    if (hs.status === 'fulfilled') state.hotStocks = hs.value;
    renderHotConcepts();
    renderHotStocks();
  });
}

function _registerPollingTasks() {
  Polling.register('market-status', _updateMarketStatus, { interval: 30000 });
  Polling.register('market-hot', _pollMarketHot, { interval: 10000, tab: 'market' });
  Polling.register('data-center', loadDataCenter, { interval: 3000, tab: 'data' });
  Polling.register('paper-prices', refreshPaperPrices, { interval: _paperPollInterval, tab: 'paper' });
  Polling.register('paper-hint', _updatePaperRefreshHint, { interval: 5000, tab: 'paper' });
  Polling.register('predict', loadPredictFunnel, {
    interval: () => (state.predictRunning ? 2500 : 15000),
    tab: 'predict',
  });
  Polling.register('hotai', loadHotStockAI, {
    interval: () => (state.hotStockAIRunning ? 2500 : 15000),
    tab: 'hotai',
  });
}

