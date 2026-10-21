/* Alpha frontend module: api.js — HTTP 请求与 jobs（202+轮询）兼容层.
 * 无构建、classic script，按 index.html 中的顺序加载，函数挂在全局作用域。
 */

async function request(path, options = {}) {
  let resp;
  try {
    resp = await fetch(path, {
      headers: { 'Content-Type': 'application/json' },
      ...options,
    });
  } catch (_) {
    throw new Error('后端服务不可用(连接失败)，请确认服务已启动: ./start.sh');
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
    error.status = resp.status;
    error.payload = payload;
    throw error;
  }
  return await resp.json();
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

async function pollJob(jobId, { intervalMs = 1500, timeoutMs = 30 * 60 * 1000, onProgress } = {}) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const job = await request(`/api/jobs/${encodeURIComponent(jobId)}`);
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

