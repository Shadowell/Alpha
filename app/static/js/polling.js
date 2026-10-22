/* Alpha frontend module: polling.js — 统一轮询调度器（页面隐藏/切 Tab 自动暂停）.
 * 无构建、classic script，按 index.html 中的顺序加载，函数挂在全局作用域。
 */

/* ==================== Polling scheduler ==================== */
// 所有周期任务集中注册；页面隐藏时统一暂停，切换 Tab 时只保留当前 Tab 的任务。
// switchTab 已经包含首屏加载，因此调度器默认等一个 interval 再首次执行，避免重复请求。

const Polling = {
  tasks: new Map(),

  register(name, fn, { interval = 15000, tab = null, runWhenHidden = false } = {}) {
    const task = {
      name, fn, interval, tab, runWhenHidden,
      timer: null, active: false, running: false,
    };
    this.tasks.set(name, task);
    if (!task.tab || task.tab === state.activeTab) this._start(task, false);
    return task;
  },

  _intervalOf(task) {
    const value = typeof task.interval === 'function' ? task.interval() : task.interval;
    return Math.max(500, Number(value) || 1000);
  },

  _schedule(task, delay) {
    if (task.timer) clearTimeout(task.timer);
    task.timer = setTimeout(() => this._tick(task), Math.max(0, delay));
  },

  _start(task, immediate) {
    if (task.active) return;
    task.active = true;
    this._schedule(task, immediate ? 0 : this._intervalOf(task));
  },

  async _tick(task) {
    task.timer = null;
    if (!task.active) return;
    if (document.hidden && !task.runWhenHidden) {
      // 理论上 visibilitychange 已经暂停；这里兜底避免后台请求。
      this._schedule(task, 1000);
      return;
    }
    if (!task.running) {
      task.running = true;
      try { await task.fn(); } catch (_) { /* 单次失败不应终止轮询 */ }
      finally { task.running = false; }
    }
    if (task.active) this._schedule(task, this._intervalOf(task));
  },

  start(name) {
    const task = this.tasks.get(name);
    if (task) this._start(task, true);
  },

  /**
   * 重新按"当前" interval 排一次定时器。
   * 间隔是函数（例如 running 5s / 空闲 60s）时，状态跃迁后需要显式收口，
   * 否则会按跃迁前的旧节奏继续排队（#54）。
   */
  reschedule(name) {
    const task = this.tasks.get(name);
    if (!task || !task.active) return;
    if (task.running) return; // 正在执行：_tick 结束时会按新间隔排程
    this._schedule(task, this._intervalOf(task));
  },

  stop(name) {
    const task = this.tasks.get(name);
    if (!task) return;
    task.active = false;
    if (task.timer) clearTimeout(task.timer);
    task.timer = null;
  },

  stopAll() {
    for (const name of this.tasks.keys()) this.stop(name);
  },

  onTabChange(tab) {
    for (const task of this.tasks.values()) {
      if (task.tab && task.tab !== tab) this.stop(task.name);
      else this._start(task, false);
    }
  },

  onVisibilityChange() {
    if (document.hidden) {
      this.stopAll();
      return;
    }
    for (const task of this.tasks.values()) {
      if (!task.tab || task.tab === state.activeTab) this._start(task, true);
    }
  },
};

document.addEventListener('visibilitychange', () => Polling.onVisibilityChange());

// 暴露给 E2E/调试（const 声明不会自动挂到 window）。
window.Polling = Polling;

/* ==================== Tab switching ==================== */

