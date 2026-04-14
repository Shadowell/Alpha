"""Hermes 运行时 — 调度、工具、LLM 调用、复盘任务。"""
from __future__ import annotations

import asyncio
import os
import time
import traceback
from typing import Any

from app.services.hermes_memory import HermesMemory
from app.services.time_utils import now_cn

_TASK_TIMEOUT = 120  # 单任务超时秒数
_MAX_CONCURRENT = 2
_CIRCUIT_BREAKER_THRESHOLD = 3
_CIRCUIT_BREAKER_COOLDOWN = 3600  # 熔断冷却 1h


class HermesRuntime:
    def __init__(
        self,
        memory: HermesMemory,
        funnel_service: Any,
        notice_service: Any,
        kline_cache_service: Any,
    ) -> None:
        self.memory = memory
        self.funnel = funnel_service
        self.notice = notice_service
        self.kline = kline_cache_service
        self._sem = asyncio.Semaphore(_MAX_CONCURRENT)
        self._failures: dict[str, list[float]] = {}
        self._running = False

    # ── P0 工具集 ──

    async def _tool_get_funnel_snapshot(self, trade_date: str | None = None) -> dict:
        f = await self.funnel.get_funnel(trade_date)
        return f.model_dump() if hasattr(f, "model_dump") else dict(f)

    async def _tool_get_notice_snapshot(self, trade_date: str | None = None) -> dict:
        f = await self.notice.get_notice_funnel(trade_date)
        return f.model_dump() if hasattr(f, "model_dump") else dict(f)

    async def _tool_get_strategy_profile(self) -> dict:
        return await self.funnel.get_strategy_profile()

    async def _tool_get_rule_engine(self) -> dict:
        return await self.funnel.get_rule_engine()

    async def _tool_get_kline_sync_status(self) -> dict:
        return self.kline.get_sync_state()

    async def _tool_get_hot_concepts(self, trade_date: str | None = None) -> dict:
        r = await self.funnel.get_hot_concepts(trade_date)
        return r.model_dump() if hasattr(r, "model_dump") else dict(r)

    async def _tool_get_hot_stocks(self, trade_date: str | None = None) -> dict:
        r = await self.funnel.get_hot_stocks(trade_date)
        return r.model_dump() if hasattr(r, "model_dump") else dict(r)

    # ── 熔断检查 ──

    def _is_circuit_open(self, task_type: str) -> bool:
        failures = self._failures.get(task_type, [])
        if len(failures) < _CIRCUIT_BREAKER_THRESHOLD:
            return False
        return (time.time() - failures[-1]) < _CIRCUIT_BREAKER_COOLDOWN

    def _record_failure(self, task_type: str) -> None:
        self._failures.setdefault(task_type, [])
        self._failures[task_type].append(time.time())
        if len(self._failures[task_type]) > 10:
            self._failures[task_type] = self._failures[task_type][-10:]

    def _clear_failures(self, task_type: str) -> None:
        self._failures.pop(task_type, None)

    # ── 核心调度 ──

    async def run_task(self, task_type: str, trigger: str = "manual", params: dict | None = None) -> dict:
        if self._is_circuit_open(task_type):
            return {"success": False, "message": f"任务 {task_type} 处于熔断状态，请稍后重试"}

        async with self._sem:
            self._running = True
            task_id = self.memory.create_task(task_type, trigger, params)
            t0 = time.time()
            tool_calls: list[dict] = []

            try:
                result = await asyncio.wait_for(
                    self._dispatch(task_type, task_id, tool_calls),
                    timeout=_TASK_TIMEOUT,
                )
                elapsed = int((time.time() - t0) * 1000)
                self.memory.finish_task(
                    task_id,
                    status="success",
                    output_summary=result.get("summary"),
                    observations=result.get("observations"),
                    tool_calls=tool_calls,
                    elapsed_ms=elapsed,
                )
                self._clear_failures(task_type)
                return {"success": True, "task_id": task_id, **result}

            except asyncio.TimeoutError:
                elapsed = int((time.time() - t0) * 1000)
                self.memory.finish_task(task_id, status="timeout", error_message="任务超时", elapsed_ms=elapsed)
                self._record_failure(task_type)
                return {"success": False, "task_id": task_id, "message": "任务超时"}

            except Exception as exc:
                elapsed = int((time.time() - t0) * 1000)
                self.memory.finish_task(
                    task_id, status="failed", error_message=str(exc), elapsed_ms=elapsed
                )
                self._record_failure(task_type)
                print(f"[hermes] task {task_type} failed: {exc}\n{traceback.format_exc()}")
                return {"success": False, "task_id": task_id, "message": str(exc)}

            finally:
                self._running = False

    async def _dispatch(self, task_type: str, task_id: int, tool_calls: list) -> dict:
        if task_type == "daily_review":
            return await self._do_daily_review(task_id, tool_calls)
        if task_type == "notice_review":
            return await self._do_notice_review(task_id, tool_calls)
        if task_type == "full_diagnosis":
            return await self._do_full_diagnosis(task_id, tool_calls)
        return {"summary": {"message": f"未知任务类型: {task_type}"}, "observations": {}}

    # ── 数据采集（Observer 层）──

    async def _collect_observations(self, tool_calls: list) -> dict:
        obs: dict[str, Any] = {}
        calls = [
            ("get_funnel_snapshot", self._tool_get_funnel_snapshot, {}),
            ("get_notice_snapshot", self._tool_get_notice_snapshot, {}),
            ("get_strategy_profile", self._tool_get_strategy_profile, {}),
            ("get_kline_sync_status", self._tool_get_kline_sync_status, {}),
            ("get_hot_concepts", self._tool_get_hot_concepts, {}),
        ]
        for name, fn, kwargs in calls:
            try:
                result = await fn(**kwargs)
                obs[name] = result
                tool_calls.append({"tool": name, "status": "ok"})
            except Exception as e:
                obs[name] = None
                tool_calls.append({"tool": name, "status": "error", "error": str(e)})

        metrics = {}
        funnel = obs.get("get_funnel_snapshot") or {}
        stats = funnel.get("stats", {})
        metrics["funnel_candidate_count"] = stats.get("candidate", 0)
        metrics["funnel_focus_count"] = stats.get("focus", 0)
        metrics["funnel_buy_count"] = stats.get("buy", 0)

        notice = obs.get("get_notice_snapshot") or {}
        nstats = notice.get("stats", {})
        metrics["notice_candidate_count"] = nstats.get("candidate", 0)
        metrics["notice_focus_count"] = nstats.get("focus", 0)
        metrics["notice_buy_count"] = nstats.get("buy", 0)

        sync = obs.get("get_kline_sync_status") or {}
        metrics["kline_sync_status"] = sync.get("status", "unknown")

        concepts = obs.get("get_hot_concepts") or {}
        top_items = concepts.get("items", [])
        metrics["hot_concepts_top3"] = [c.get("name", "") for c in top_items[:3]]

        obs["metrics"] = metrics
        return obs

    # ── LLM 调用 ──

    async def _call_llm(self, system_prompt: str, user_prompt: str) -> dict | None:
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            return None

        model = os.environ.get("HERMES_MODEL", "gpt-4o-mini")
        base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")

        import httpx
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.3,
            "response_format": {"type": "json_object"},
        }
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                import json
                return json.loads(content)
        except Exception as e:
            print(f"[hermes] LLM call failed: {e}")
            return None

    # ── 系统 Prompt ──

    _SYSTEM_PROMPT = """你是 Hermes，Alpha 量化选股系统的内嵌投研代理。

你的职责是：
1. 观察系统运行数据（漏斗状态、公告筛选结果、策略参数、K线同步质量）
2. 诊断问题（候选池过空/过满、转化率异常、参数偏差、数据质量问题）
3. 产出结构化优化提案（参数建议、关键词调整、实验设计）

你的约束是：
- 你只能建议，不能直接修改系统配置
- 所有建议必须包含理由、证据、预期影响和置信度
- 你的输出必须是严格的 JSON 格式

请用中文回答。"""

    # ── daily_review 盘后复盘 ──

    async def _do_daily_review(self, task_id: int, tool_calls: list) -> dict:
        obs = await self._collect_observations(tool_calls)
        metrics = obs.get("metrics", {})

        # 尝试 LLM 分析
        funnel_data = obs.get("get_funnel_snapshot") or {}
        strategy = obs.get("get_strategy_profile") or {}
        sync_status = obs.get("get_kline_sync_status") or {}
        concepts = obs.get("get_hot_concepts") or {}

        user_prompt = f"""## 任务：盘后复盘

### 今日漏斗状态
候选池: {metrics.get('funnel_candidate_count', 0)} 只
重点池: {metrics.get('funnel_focus_count', 0)} 只
买入池: {metrics.get('funnel_buy_count', 0)} 只

### 当前策略参数摘要
{_safe_json_str(strategy)}

### K线同步状态
{_safe_json_str(sync_status)}

### 热门概念 Top3
{metrics.get('hot_concepts_top3', [])}

### 请你完成：
1. 评估今日策略表现（候选池质量、转化率、筛选效率）
2. 识别参数可能存在的偏差
3. 如果发现问题，产出参数调整建议

### 输出 JSON 格式：
{{
  "summary": "一句话总结",
  "diagnosis": [
    {{
      "issue": "问题描述",
      "severity": "low/medium/high",
      "detail": "详细说明"
    }}
  ],
  "proposals": [
    {{
      "type": "rule_patch",
      "title": "建议标题",
      "risk_level": "low/medium/high",
      "diff": {{"参数名": {{"from": 原值, "to": 建议值}}}},
      "reasoning": "推理过程",
      "expected_impact": "预期影响",
      "confidence": 0.0到1.0,
      "evidence": ["证据1", "证据2"]
    }}
  ]
}}"""

        llm_result = await self._call_llm(self._SYSTEM_PROMPT, user_prompt)
        tool_calls.append({"tool": "llm_daily_review", "status": "ok" if llm_result else "skipped"})

        proposals_created = 0
        if llm_result and "proposals" in llm_result:
            for p in llm_result["proposals"]:
                if not p.get("title"):
                    continue
                self.memory.create_proposal(
                    task_id,
                    proposal_type=p.get("type", "rule_patch"),
                    title=p["title"],
                    risk_level=p.get("risk_level", "medium"),
                    reasoning=p.get("reasoning", ""),
                    diff_payload=p.get("diff"),
                    expected_impact=p.get("expected_impact", ""),
                    confidence=float(p.get("confidence", 0.5)),
                    evidence=p.get("evidence", []),
                )
                proposals_created += 1

        # 即使 LLM 不可用，也做规则化诊断
        if not llm_result:
            llm_result = self._rule_based_daily_diagnosis(metrics)
            for p in llm_result.get("proposals", []):
                self.memory.create_proposal(
                    task_id,
                    proposal_type=p.get("type", "rule_patch"),
                    title=p["title"],
                    risk_level=p.get("risk_level", "medium"),
                    reasoning=p.get("reasoning", ""),
                    diff_payload=p.get("diff"),
                    expected_impact=p.get("expected_impact", ""),
                    confidence=float(p.get("confidence", 0.5)),
                    evidence=p.get("evidence", []),
                )
                proposals_created += 1

        return {
            "summary": {
                "task": "daily_review",
                "llm_used": llm_result is not None,
                "proposals_created": proposals_created,
                "message": (llm_result or {}).get("summary", "复盘完成"),
            },
            "observations": metrics,
        }

    def _rule_based_daily_diagnosis(self, metrics: dict) -> dict:
        """无 LLM 时的规则化诊断。"""
        proposals = []
        diagnosis = []

        cc = metrics.get("funnel_candidate_count", 0)
        if cc == 0:
            diagnosis.append({
                "issue": "候选池为空",
                "severity": "medium",
                "detail": "当日盘后筛选未产出任何候选股票",
            })
            proposals.append({
                "type": "rule_patch",
                "title": "候选池为空，建议检查选股参数",
                "risk_level": "medium",
                "diff": None,
                "reasoning": "候选池为空可能因为参数过严或非交易日。建议检查 box_range_threshold、volume_shrink_threshold 等参数。",
                "expected_impact": "增加候选池覆盖度",
                "confidence": 0.4,
                "evidence": [f"候选池数量: {cc}"],
            })
        elif cc > 50:
            diagnosis.append({
                "issue": "候选池过满",
                "severity": "low",
                "detail": f"候选池 {cc} 只，可能参数过松",
            })

        return {"summary": "规则化诊断完成", "diagnosis": diagnosis, "proposals": proposals}

    # ── notice_review 公告复盘 ──

    async def _do_notice_review(self, task_id: int, tool_calls: list) -> dict:
        obs = await self._collect_observations(tool_calls)
        metrics = obs.get("metrics", {})
        notice_data = obs.get("get_notice_snapshot") or {}

        from app.services.notice_service import BULLISH_RULES
        keywords_info = [{"tag": r[0], "weight": r[1]} for r in BULLISH_RULES]

        user_prompt = f"""## 任务：公告复盘

### 今日公告筛选结果
候选池: {metrics.get('notice_candidate_count', 0)} 只
重点池: {metrics.get('notice_focus_count', 0)} 只
买入池: {metrics.get('notice_buy_count', 0)} 只
数据源: {notice_data.get('source', 'unknown')}
LLM 打分: {'开启' if notice_data.get('llm_enabled') else '关闭'}

### 公告关键词规则
{_safe_json_str(keywords_info)}

### 请你完成：
1. 评估公告筛选的命中质量
2. 分析关键词类型的命中分布是否合理
3. 如果发现权重偏差，产出调整建议

### 输出 JSON 格式：
{{
  "summary": "一句话总结",
  "diagnosis": [
    {{
      "issue": "问题描述",
      "severity": "low/medium/high",
      "detail": "详细说明"
    }}
  ],
  "proposals": [
    {{
      "type": "notice_rule_patch",
      "title": "建议标题",
      "risk_level": "low/medium/high",
      "diff": {{"说明": "具体建议"}},
      "reasoning": "推理过程",
      "expected_impact": "预期影响",
      "confidence": 0.0到1.0,
      "evidence": ["证据1"]
    }}
  ]
}}"""

        llm_result = await self._call_llm(self._SYSTEM_PROMPT, user_prompt)
        tool_calls.append({"tool": "llm_notice_review", "status": "ok" if llm_result else "skipped"})

        proposals_created = 0
        result_data = llm_result or {"summary": "公告复盘完成（无 LLM）", "proposals": []}

        for p in result_data.get("proposals", []):
            if not p.get("title"):
                continue
            self.memory.create_proposal(
                task_id,
                proposal_type=p.get("type", "notice_rule_patch"),
                title=p["title"],
                risk_level=p.get("risk_level", "medium"),
                reasoning=p.get("reasoning", ""),
                diff_payload=p.get("diff"),
                expected_impact=p.get("expected_impact", ""),
                confidence=float(p.get("confidence", 0.5)),
                evidence=p.get("evidence", []),
            )
            proposals_created += 1

        return {
            "summary": {
                "task": "notice_review",
                "llm_used": llm_result is not None,
                "proposals_created": proposals_created,
                "message": result_data.get("summary", "公告复盘完成"),
            },
            "observations": metrics,
        }

    # ── full_diagnosis 全面诊断 ──

    async def _do_full_diagnosis(self, task_id: int, tool_calls: list) -> dict:
        daily = await self._do_daily_review(task_id, tool_calls)
        notice = await self._do_notice_review(task_id, tool_calls)
        total_proposals = (daily["summary"].get("proposals_created", 0)
                          + notice["summary"].get("proposals_created", 0))
        return {
            "summary": {
                "task": "full_diagnosis",
                "proposals_created": total_proposals,
                "message": "全面诊断完成",
                "daily": daily["summary"],
                "notice": notice["summary"],
            },
            "observations": daily.get("observations", {}),
        }

    # ── 状态查询 ──

    def get_status(self) -> dict:
        last = self.memory.get_last_task()
        counts = self.memory.count_by_status()
        api_key = os.environ.get("OPENAI_API_KEY", "")
        return {
            "running": self._running,
            "llm_available": bool(api_key),
            "last_run": _format_last_run(last) if last else None,
            "stats": {
                "total_proposals": sum(counts.values()),
                "pending_proposals": counts.get("pending", 0),
                "approved_proposals": counts.get("approved", 0),
                "rejected_proposals": counts.get("rejected", 0),
            },
        }


# ── 定时调度循环 ──

async def hermes_scheduler_loop(runtime: HermesRuntime) -> None:
    """后台定时调度 Hermes 任务。"""
    await asyncio.sleep(30)
    while True:
        try:
            n = now_cn()
            h, m = n.hour, n.minute

            # 盘后复盘 15:30
            if h == 15 and 30 <= m < 40:
                last = runtime.memory.get_last_task("daily_review")
                if not last or last.get("started_at", "")[:10] != n.date().isoformat():
                    print("[hermes] scheduled daily_review")
                    await runtime.run_task("daily_review", trigger="scheduled")

            # 公告复盘 21:00
            if h == 21 and 0 <= m < 10:
                last = runtime.memory.get_last_task("notice_review")
                if not last or last.get("started_at", "")[:10] != n.date().isoformat():
                    print("[hermes] scheduled notice_review")
                    await runtime.run_task("notice_review", trigger="scheduled")

        except Exception as e:
            print(f"[hermes] scheduler error: {e}")

        await asyncio.sleep(300)


# ── helpers ──

def _safe_json_str(obj: Any, max_len: int = 2000) -> str:
    import json
    try:
        s = json.dumps(obj, ensure_ascii=False, indent=2)
        return s[:max_len] + "..." if len(s) > max_len else s
    except Exception:
        return str(obj)[:max_len]


def _format_last_run(task: dict) -> dict:
    return {
        "task_type": task.get("task_type"),
        "status": task.get("status"),
        "finished_at": task.get("finished_at"),
        "elapsed_ms": task.get("elapsed_ms"),
    }
