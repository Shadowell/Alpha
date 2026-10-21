/* Alpha frontend module: boot-helpers.js — 按钮加载态与通用 UI 助手.
 * 无构建、classic script，按 index.html 中的顺序加载，函数挂在全局作用域。
 */

/* ==================== Button Loading Helper ==================== */

function _btnStart(btn, loadingText) {
  btn.disabled = true;
  btn._origText = btn.textContent;
  btn.textContent = loadingText || '处理中...';
  btn.classList.add('btn-loading');
}

function _btnEnd(btn) {
  btn.textContent = btn._origText || '操作';
  btn.disabled = false;
  btn.classList.remove('btn-loading');
}

