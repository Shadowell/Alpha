/*
 * Alpha 统一安全渲染函数（classic script，全局可用）。
 *
 * 项目没有构建步骤，index.html / notice.html 均按顺序加载本文件。
 * 外部数据（公告、板块、个股名、LLM 输出、后端错误信息）一律不可信：
 * innerHTML 插值必须经过 esc()/safeInt()/safeCssColor()/safeLinkHtml()，
 * 或者使用 html`` 模板（默认转义全部插值）。
 */

/* eslint-disable no-unused-vars */

function text(v) {
  return v == null ? '' : String(v);
}

function esc(v) {
  return text(v)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

// 仅允许 http(s) 地址；javascript:、data:、vbscript: 等返回 ''。
function safeUrl(v) {
  const raw = text(v).trim();
  if (!raw) return '';
  try {
    const parsed = new URL(raw, window.location.origin);
    if (parsed.protocol === 'http:' || parsed.protocol === 'https:') return parsed.href;
  } catch (_) { /* 非法 URL */ }
  return '';
}

// 安全外链：非法协议降级为纯文本，绝不生成可点击的 href。
function safeLinkHtml(url, label) {
  const href = safeUrl(url);
  const caption = esc(label || '公告链接');
  if (!href) {
    return `<span class="link-disabled" title="仅支持 http(s) 链接，已禁用">${caption}</span>`;
  }
  return `<a href="${esc(href)}" target="_blank" rel="noopener noreferrer">${caption}</a>`;
}

function safeCssColor(v) {
  const raw = text(v).trim();
  return /^#[0-9a-f]{3,8}$/i.test(raw) ? raw : 'transparent';
}

function safeInt(v, fallback = 0) {
  const n = Number(v);
  return Number.isFinite(n) ? Math.trunc(n) : fallback;
}

// 安全模板标签：默认转义全部插值，只有 raw() 包裹的内容按 HTML 输出。
function raw(markup) {
  return { __alphaRawHtml: text(markup) };
}

function html(strings, ...values) {
  return strings.reduce((acc, chunk, idx) => {
    if (idx >= values.length) return acc + chunk;
    const value = values[idx];
    const rendered = value && value.__alphaRawHtml !== undefined
      ? value.__alphaRawHtml
      : esc(value);
    return acc + chunk + rendered;
  }, '');
}
