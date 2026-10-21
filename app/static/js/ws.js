/* Alpha frontend module: ws.js — WebSocket 实时快照.
 * 无构建、classic script，按 index.html 中的顺序加载，函数挂在全局作用域。
 */

/* ==================== WebSocket ==================== */

let _wsRetryDelay = 2000;
function connectWs() {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(`${protocol}//${window.location.host}/ws/realtime`);
  ws.onopen = () => { _wsRetryDelay = 2000; };
  ws.onmessage = (evt) => {
    try {
      const msg = JSON.parse(evt.data);
      if (!msg || !msg.data) return;
      if (msg.event === 'snapshot') {
        state.funnel = msg.data.funnel;
        state.hotConcepts = msg.data.hot_concepts;
        state.hotStocks = msg.data.hot_stocks || state.hotStocks;
        renderHotConcepts();
        renderHotStocks();
        renderFunnel();
        setMeta();
      } else if (msg.event === 'monitor_update') {
        _appendMonitorMessage({
          id: msg.data.message_id || '',
          content: msg.data.content || '',
          created_at: msg.data.created_at || '',
          trigger: msg.data.trigger || 'scheduled',
        });
        if (state.activeTab === 'agent') {
          setStatus('收到智能监控推送', 'success');
        }
      }
    } catch (err) {
      console.error('ws parse error', err);
    }
  };
  ws.onerror = () => {};
  ws.onclose = () => {
    setTimeout(connectWs, _wsRetryDelay);
    _wsRetryDelay = Math.min(_wsRetryDelay * 1.5, 30000);
  };
}

