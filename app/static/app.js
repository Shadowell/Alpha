/*
 * Alpha 前端入口（无构建，classic scripts 按顺序加载）。
 *
 * 模块顺序见 index.html：
 *   js/security.js -> js/core.js -> js/summary.js -> js/api.js -> js/polling.js
 *   -> js/navigation.js -> js/funnel.js -> js/data-center.js -> js/charts.js
 *   -> js/stock.js -> js/notice-tab.js -> js/loaders.js -> js/paper.js
 *   -> js/agent.js -> js/monitor.js -> js/hover.js -> js/predict-modal.js
 *   -> js/ws.js -> js/boot-helpers.js -> js/boot.js -> js/predict.js
 *   -> js/strategy.js -> app.js
 *
 * app.js 只负责启动与兜底错误展示，不再是 4000 行单体。
 */
(function bootstrapAlpha() {
  if (typeof init !== 'function') {
    const meta = document.getElementById('meta');
    if (meta) meta.textContent = '初始化失败: 前端模块未加载完整，请刷新页面';
    return;
  }
  init().catch((err) => {
    const meta = document.getElementById('meta');
    if (meta) meta.textContent = `初始化失败: ${err.message}`;
    if (typeof renderChartPlaceholder === 'function') {
      renderChartPlaceholder(`初始化失败: ${err.message}`);
    }
  });
})();
