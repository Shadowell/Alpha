/* Alpha frontend module: api.js — HTTP 请求与 jobs（202+轮询）兼容层.
 * 无构建、classic script，按 index.html 中的顺序加载，函数挂在全局作用域。
 */

/* ==================== 请求超时与中止（#55） ==================== */
// 默认预算：普通 GET 10s、写操作 20s；可由 window.API_TIMEOUT_MS = {read, write} 覆盖，
// 也可在单次调用里传 { timeoutMs }。前端超时预算必须大于后端最慢正常路径，避免误杀。
const API_TIMEOUT_DEFAULTS = { read: 10000, write: 20000 };

function _apiTimeoutMs(method, options) {
  if (Number.isFinite(options.timeoutMs) && options.timeoutMs > 0) return options.timeoutMs;
  const cfg = (typeof window !== 'undefined' && window.API_TIMEOUT_MS) || {};
  const isWrite = String(method || 'GET').toUpperCase() !== 'GET';
  const configured = isWrite ? cfg.write : cfg.read;
  return Number.isFinite(configured) && configured > 0 ? configured : API_TIMEOUT_DEFAULTS[isWrite ? 'write' : 'read'];
}

function _isAbortError(err) {
  return !!err && (err.name === 'AbortError' || String(err.name).includes('Abort'));
}

async function request(path, options = {}) {
  const method = String(options.method || 'GET').toUpperCase();
  const timeoutMs = _apiTimeoutMs(method, options);
  const { timeoutMs: _drop, ...fetchOptions } = options;
  const controller = new AbortController();
  // 调用方自带 signal 时一并转发，保持外部中止能力。
  if (fetchOptions.signal) {
    const external = fetchOptions.signal;
    if (external.aborted) controller.abort(external.reason);
    else external.addEventListener('abort', () => controller.abort(external.reason), { once: true });
  }
  let timer = null;
  let timedOut = false;
  if (timeoutMs > 0) {
    timer = setTimeout(() => { timedOut = true; controller.abort(new Error(`timeout after ${timeoutMs}ms`)); }, timeoutMs);
  }
  let resp;
  try {
    resp = await fetch(path, {
      headers: { 'Content-Type': 'application/json' },
      ...fetchOptions,
      method,
      signal: controller.signal,
    });
  } catch (err) {
    const seconds = (timeoutMs / 1000).toFixed(timeoutMs % 1000 ? 1 : 0);
    if (timedOut) {
      const error = new Error(`请求超时（${seconds}s）：${path}`);
      error.code = 'timeout';
      error.timeoutMs = timeoutMs;
      error.path = path;
      throw error;
    }
    if (_isAbortError(err)) {
      // 调用方主动中止（如切页），不当作后端故障。
      err.code = 'aborted';
      err.path = path;
      throw err;
    }
    const error = new Error('后端服务不可用(连接失败)，请确认服务已启动: ./start.sh');
    error.code = 'network';
    error.path = path;
    error.cause = err;
    throw error;
  } finally {
    if (timer) clearTimeout(timer);
  }
  if (!resp.ok) {
    const raw = await resp.text();
    let payload = null;
    let message = '';
    try {
      payload = JSON.parse(raw);
      message = payload.detail || payload.message || raw;
    } catch (_) {
      message = raw;
    }
    const error = new Error(message || `HTTP ${resp.status}`);
    error.code = 'http';
    error.status = resp.status;
    error.path = path;
    error.payload = payload;
    throw error;
  }
  try {
    return await resp.json();
  } catch (err) {
    const error = new Error(`响应不是合法 JSON：${path}`);
    error.code = 'http';
    error.status = resp.status;
    error.path = path;
    throw error;
  }
}

/* ==================== Jobs API (兼容同步/异步两种响应) ==================== */
// 后端长任务在 P2a 之后会返回 202 {job_id,status,poll_url}，旧版本直接返回结果。
// 前端统一通过 requestJob() 兼容两种形态，不依赖后端是否已升级。

function _sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function _jobProgressText(job) {
  const status = job?.status || '';
  const progress = job?.progress;
  if (progress == null) return status;
  if (typeof progress === 'number') return `${status} ${progress}%`;
  if (typeof progress === 'object') {
    const cur = progress.current ?? '';
    const total = progress.total ?? '';
    const totalPart = total === '' ? '' : `/${total}`;
    const message = progress.message || progress.detail || '';
    return `${status} ${cur}${totalPart} ${message}`.trim();
  }
  return `${status} ${progress}`;
}

async function pollJob(jobId, { intervalMs = 1500, timeoutMs = 30 * 60 * 1000, onProgress, transientRetries = 3 } = {}) {
  const deadline = Date.now() + timeoutMs;
  let transient = 0;
  while (Date.now() < deadline) {
    let job;
    try {
      // #55：轮询的每一轮都受超时保护；偶发超时/断连不直接终止整个任务等待。
      job = await request(`/api/jobs/${encodeURIComponent(jobId)}`);
      transient = 0;
    } catch (err) {
      if ((err.code === 'timeout' || err.code === 'network') && transient < transientRetries) {
        transient += 1;
        await _sleep(intervalMs);
        continue;
      }
      throw err;
    }
    if (onProgress) {
      try { onProgress(job); } catch (_) { /* 进度回调失败不影响任务 */ }
    }
    const status = String(job?.status || '');
    if (status === 'succeeded') return job;
    if (status === 'failed' || status === 'interrupted') {
      throw new Error(job?.error || (status === 'interrupted' ? '任务被中断' : '任务执行失败'));
    }
    await _sleep(intervalMs);
  }
  throw new Error(`任务 ${jobId} 长时间未完成，请稍后在任务列表查看`);
}

function _mergeJobResult(job, fallbackJobId) {
  const result = job?.result;
  if (result && typeof result === 'object' && !Array.isArray(result)) {
    return { ...result, job_id: job.id || fallbackJobId, status: job.status };
  }
  return result ?? job;
}

async function _awaitJob(jobId, { intervalMs, onProgress }) {
  const job = await pollJob(jobId, { intervalMs, onProgress });
  return _mergeJobResult(job, jobId);
}

async function requestJob(path, options = {}, { intervalMs = 1500, onProgress } = {}) {
  let payload;
  try {
    payload = await request(path, options);
  } catch (err) {
    // P2a 契约：同类任务运行中重复提交返回 409 + 现有 job_id，继续轮询该任务。
    if (err && err.status === 409 && err.payload && err.payload.job_id) {
      return _awaitJob(err.payload.job_id, { intervalMs, onProgress });
    }
    throw err;
  }
  if (!payload || !payload.job_id) return payload;
  return _awaitJob(payload.job_id, { intervalMs, onProgress });
}

