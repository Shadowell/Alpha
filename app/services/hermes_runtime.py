"""Hermes 运行时 — 调度、工具、LLM 调用、复盘任务。

支持两种模式：
1. Agent 模式：Hermes Agent 可用时，发送任务描述让 Agent 自主调用 MCP 工具
2. 降级模式：Agent 不可用时，内部采集数据 + 单次 LLM 调用
"""
from __future__ import annotations

import asyncio
import os
import time
import traceback
from typing import Any

from app.services.hermes_memory import HermesMemory
from app.services.time_utils import now_cn

_TASK_TIMEOUT = 180  # Agent 模式需要更长超时
_MAX_CONCURRENT = 2
_CIRCUIT_BREAKER_THRESHOLD = 3
_CIRCUIT_BREAKER_COOLDOWN = 3600


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

    # ── P0 工具集（降级模式使用）──

    async def _tool_get_funnel_snapshot(self, trade_date: str | None = None) -> dict:
        f = await self.funnel.get_funnel(trade_date)
        return f.model_dump() if hasattr(f, "model_dump") else dict(f)

    async def _tool_get_notice_snapshot(self, trade_date: str | None = None) -> dict:
        f = await self.notice.get_notice_funnel(trade_date)
        return f.model_dump() if hasattr(f, "model_dump") else dict(f)

    async def _tool_get_strategy_profile(self) -> dict:
        return await self.funnel.get_strategy_profile()

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
                    self._dispatch(task_type, task_id, tool_calls, params or {}),
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

    async def _dispatch(self, task_type: str, task_id: int, tool_calls: list, params: dict) -> dict:
        if task_type == "daily_review":
            return await self._do_daily_review(task_id, tool_calls, params)
        if task_type == "notice_review":
            return await self._do_notice_review(task_id, tool_calls, params)
        if task_type == "full_diagnosis":
            return await self._do_full_diagnosis(task_id, tool_calls, params)
        return {"summary": {"message": f"未知任务类型: {task_type}"}, "observations": {}}

    # ── 数据采集（Observer 层，降级模式使用）──

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

    # ── LLM / Agent 调用 ──

    _HERMES_AGENT_BASE = os.environ.get("HERMES_AGENT_URL", "http://127.0.0.1:8642/v1")

    async def _check_hermes_agent(self) -> bool:
        import httpx
        health_url = self._HERMES_AGENT_BASE.replace("/v1", "/health")
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                resp = await client.get(health_url)
                return resp.status_code == 200
        except Exception:
            return False

    async def _delegate_to_hermes_agent(self, task_description: str) -> dict | None:
        """向 Hermes Agent 发送任务描述，让其自主调用 MCP 工具进行诊断。

        Hermes Agent 已通过 MCP 连接到 Alpha API，并且拥有 SKILL.md 中的领域知识
        和 MEMORY.md 中的历史经验。它会自主决定调哪些工具、分析什么数据。
        """
        import httpx
        import json as _json

        api_key = os.environ.get("API_SERVER_KEY", "")
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        payload = {
            "model": "hermes-agent",
            "messages": [
                {"role": "system", "content": self._AGENT_SYSTEM_PROMPT},
                {"role": "user", "content": task_description},
            ],
            "temperature": 0.3,
        }

        try:
            async with httpx.AsyncClient(timeout=150) as client:
                resp = await client.post(
                    f"{self._HERMES_AGENT_BASE}/chat/completions",
                    headers=headers,
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                return _json.loads(content)
        except _json.JSONDecodeError:
            try:
                raw = data["choices"][0]["message"]["content"]
                start = raw.find("{")
                end = raw.rfind("}") + 1
                if start >= 0 and end > start:
                    return _json.loads(raw[start:end])
            except Exception:
                pass
            return None
        except Exception as e:
            print(f"[hermes] agent delegation failed: {e}")
            return None

    async def _call_llm(self, system_prompt: str, user_prompt: str) -> dict | None:
        """降级模式：直接调用 LLM API（不经过 Hermes Agent）。"""
        import httpx
        import json as _json

        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            hermes_available = await self._check_hermes_agent()
            if hermes_available:
                api_key = os.environ.get("API_SERVER_KEY", "")
                base_url = self._HERMES_AGENT_BASE
                model = "hermes-agent"
                timeout = 90
                use_json_format = False
                print("[hermes] fallback: using local hermes-agent API Server")
            else:
                return None
        else:
            model = os.environ.get("HERMES_MODEL", "gpt-4o-mini")
            base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
            timeout = 30
            use_json_format = True

        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.3,
        }
        if use_json_format:
            payload["response_format"] = {"type": "json_object"}

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    f"{base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                return _json.loads(content)
        except _json.JSONDecodeError:
            print(f"[hermes] LLM returned non-JSON, extracting...")
            try:
                raw = data["choices"][0]["message"]["content"]
                start = raw.find("{")
                end = raw.rfind("}") + 1
                if start >= 0 and end > start:
                    return _json.loads(raw[start:end])
            except Exception:
                pass
            return None
        except Exception as e:
            print(f"[hermes] LLM call failed: {e}")
            return None

    # ── System Prompts ──

    _AGENT_SYSTEM_PROMPT = """你是 Hermes，Alpha 量化选股系统的内嵌投研代理。

你可以通过 MCP 工具直接访问 Alpha 系统的实时数据。请主动调用工具获取你需要的信息，
不要等待数据被喂给你。

## 职责
1. 主动调用工具观察系统运行数据（漏斗状态、公告筛选结果、策略参数、K线同步质量）
2. 根据收集到的数据，诊断问题（候选池过空/过满、转化率异常、参数偏差、数据质量问题）
3. 如果发现需要调整的地方，使用提案工具（propose_rule_patch / propose_notice_rule_patch）直接创建提案

## 约束
- 你只能建议，不能直接修改系统配置
- 所有建议必须包含理由、证据、预期影响和置信度
- 参数调整建议每次变动不超过 20%，一次最多调 2 个参数
- 最终输出一个 JSON 对象作为任务总结

## 诊断参考
- 候选池健康范围: 5-30 只（0=过严, >50=过松）
- box_range_threshold 默认 0.18，正常范围 0.14-0.24
- buy_score_threshold 默认 78，不低于 70
- 公告分池: ≥80→buy, ≥65→focus, 其余→candidate
- 检查 MEMORY.md 中的历史调参经验，避免重复过去被驳回的建议

## 技能经验库
你的 skills/alpha/references/ 目录下有详细的诊断流程和参数调优经验，
在做复杂诊断时请参考这些经验文档。

## 预测数据规则（严格遵守）
- 使用 predict_kronos 工具获取股票走势预测，这是真实的 AI 模型推理结果
- 使用 get_stock_realtime 工具获取个股实时行情
- 严禁自行编造、推算、臆测任何股价预测数值
- 若 Kronos 预测失败（K线不足等），不对该股票做价格预测

请用中文回答。"""

    _FALLBACK_SYSTEM_PROMPT = """你是 Hermes，Alpha 量化选股系统的内嵌投研代理。

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

    async def _do_daily_review(self, task_id: int, tool_calls: list, params: dict) -> dict:
        hermes_available = await self._check_hermes_agent()

        if hermes_available:
            return await self._do_daily_review_agent(task_id, tool_calls)
        return await self._do_daily_review_fallback(task_id, tool_calls)

    async def _do_daily_review_agent(self, task_id: int, tool_calls: list) -> dict:
        """Agent 模式：让 Hermes Agent 自主采集数据并诊断。"""
        task_description = f"""## 任务：盘后复盘

请完成以下工作：

1. **数据采集**：调用以下工具获取系统当前状态
   - get_funnel_snapshot：获取策略漏斗状态
   - get_strategy_profile：获取当前策略参数
   - get_kline_sync_status：检查K线数据同步状态
   - get_hot_concepts：查看热门板块

2. **诊断分析**：
   - 评估候选池数量是否合理（0只=过严, >30只=过松）
   - 检查各池转化率
   - 评估参数是否存在偏差
   - 检查数据完整性

3. **产出建议**：
   - 如果发现问题，调用 propose_rule_patch 创建参数调优提案
   - 每个提案必须包含：理由、证据、预期影响、置信度(0-1)

4. **返回总结**：输出以下 JSON 格式
```json
{{
  "summary": "一句话总结今日状态",
  "diagnosis": [
    {{"issue": "问题描述", "severity": "low/medium/high", "detail": "详细说明"}}
  ],
  "proposals_created": 0
}}
```

当前时间：{now_cn().isoformat()}
"""
        tool_calls.append({"tool": "hermes_agent_delegate", "status": "started"})
        result = await self._delegate_to_hermes_agent(task_description)
        tool_calls.append({"tool": "hermes_agent_delegate", "status": "ok" if result else "failed"})

        if result:
            return {
                "summary": {
                    "task": "daily_review",
                    "mode": "agent",
                    "llm_used": True,
                    "proposals_created": result.get("proposals_created", 0),
                    "message": result.get("summary", "Agent 模式复盘完成"),
                },
                "observations": {"diagnosis": result.get("diagnosis", [])},
            }

        print("[hermes] agent delegation failed, falling back to legacy mode")
        return await self._do_daily_review_fallback(task_id, tool_calls)

    async def _do_daily_review_fallback(self, task_id: int, tool_calls: list) -> dict:
        """降级模式：内部采集数据 + 单次 LLM 调用。"""
        obs = await self._collect_observations(tool_calls)
        metrics = obs.get("metrics", {})

        strategy = obs.get("get_strategy_profile") or {}
        sync_status = obs.get("get_kline_sync_status") or {}

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

        llm_result = await self._call_llm(self._FALLBACK_SYSTEM_PROMPT, user_prompt)
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
                "mode": "fallback",
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

    async def _do_notice_review(self, task_id: int, tool_calls: list, params: dict) -> dict:
        hermes_available = await self._check_hermes_agent()

        if hermes_available:
            return await self._do_notice_review_agent(task_id, tool_calls)
        return await self._do_notice_review_fallback(task_id, tool_calls)

    async def _do_notice_review_agent(self, task_id: int, tool_calls: list) -> dict:
        """Agent 模式：让 Hermes Agent 自主分析公告筛选质量。"""
        task_description = f"""## 任务：公告选股复盘

请完成以下工作：

1. **数据采集**：调用以下工具获取公告筛选数据
   - get_notice_funnel：获取公告池状态
   - get_notice_keywords：获取关键词规则列表
   - get_funnel_snapshot：对照策略漏斗（看重叠度）

2. **诊断分析**：
   - 评估公告筛选命中质量
   - 分析各关键词类型的命中分布是否合理
   - 检查是否有新类型的利好公告未被覆盖

3. **产出建议**：
   - 如果发现权重偏差或规则缺失，调用 propose_notice_rule_patch 创建提案
   - 每个提案包含具体的调整建议

4. **返回总结**：输出以下 JSON 格式
```json
{{
  "summary": "一句话总结公告筛选状态",
  "diagnosis": [
    {{"issue": "问题描述", "severity": "low/medium/high", "detail": "详细说明"}}
  ],
  "proposals_created": 0
}}
```

当前时间：{now_cn().isoformat()}
"""
        tool_calls.append({"tool": "hermes_agent_delegate", "status": "started"})
        result = await self._delegate_to_hermes_agent(task_description)
        tool_calls.append({"tool": "hermes_agent_delegate", "status": "ok" if result else "failed"})

        if result:
            return {
                "summary": {
                    "task": "notice_review",
                    "mode": "agent",
                    "llm_used": True,
                    "proposals_created": result.get("proposals_created", 0),
                    "message": result.get("summary", "Agent 模式公告复盘完成"),
                },
                "observations": {"diagnosis": result.get("diagnosis", [])},
            }

        print("[hermes] agent delegation failed, falling back to legacy mode")
        return await self._do_notice_review_fallback(task_id, tool_calls)

    async def _do_notice_review_fallback(self, task_id: int, tool_calls: list) -> dict:
        """降级模式：内部采集 + 单次 LLM。"""
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

        llm_result = await self._call_llm(self._FALLBACK_SYSTEM_PROMPT, user_prompt)
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
                "mode": "fallback",
                "llm_used": llm_result is not None,
                "proposals_created": proposals_created,
                "message": result_data.get("summary", "公告复盘完成"),
            },
            "observations": metrics,
        }

    # ── full_diagnosis 全面诊断 ──

    async def _do_full_diagnosis(self, task_id: int, tool_calls: list, params: dict) -> dict:
        daily = await self._do_daily_review(task_id, tool_calls, params)
        notice = await self._do_notice_review(task_id, tool_calls, params)
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

    # ── 智能监控 ──

    _monitor_running = False

    async def run_monitor_tick(self, trigger: str = "scheduled") -> dict:
        """单次智能监控：收集实时数据 → 调 LLM → 返回分析文本。"""
        if self._monitor_running:
            return {"success": False, "message": "监控任务正在执行中"}
        self._monitor_running = True
        t0 = time.time()
        try:
            config = self.memory.get_monitor_config()
            system_prompt = config.get("system_prompt") or self.memory._DEFAULT_MONITOR_PROMPT

            market_data = await self._collect_monitor_data()

            n = now_cn()
            kronos_section = ""
            if market_data.get("kronos_predictions"):
                kronos_section = f"""
#### Kronos 模型预测（真实 AI 推理结果）
{market_data['kronos_predictions']}
"""

            user_prompt = f"""## A股盘中机会监控（采样时间：{n.strftime('%H:%M')}）

### 实时市场数据

#### 热门概念板块
{market_data.get('hot_concepts', '暂无数据')}

#### 热门个股排行
{market_data.get('hot_stocks', '暂无数据')}

#### 策略漏斗状态
{market_data.get('funnel_summary', '暂无数据')}
{kronos_section}
请根据以上实时数据，输出本轮盘中机会分析报告。如需引用股价预测，只能使用上方 Kronos 模型提供的数据，禁止自行编造预测值。"""

            content = await self._call_monitor_llm(system_prompt, user_prompt)

            if not content:
                content = f"[{n.strftime('%H:%M')}] 智能监控：LLM 未返回结果，请检查 API 配置。\n\n市场概况：\n{market_data.get('hot_concepts', '暂无')}"

            msg_id = self.memory.create_monitor_message(content, trigger)
            elapsed = int((time.time() - t0) * 1000)
            return {"success": True, "message_id": msg_id, "content": content, "elapsed_ms": elapsed}

        except Exception as exc:
            print(f"[hermes] monitor tick failed: {exc}")
            import traceback
            traceback.print_exc()
            return {"success": False, "message": str(exc)}
        finally:
            self._monitor_running = False

    async def _collect_monitor_data(self) -> dict:
        """收集智能监控所需的实时市场数据 + 漏斗池股票的 Kronos 预测。"""
        data: dict[str, str] = {}

        try:
            hot = await self.funnel.get_hot_concepts()
            items = hot.items if hasattr(hot, "items") else (hot.get("items", []) if isinstance(hot, dict) else [])
            if items:
                lines = []
                for i, c in enumerate(items[:15], 1):
                    name = c.get("name", "") if isinstance(c, dict) else getattr(c, "name", "")
                    pct = c.get("change_pct", 0) if isinstance(c, dict) else getattr(c, "change_pct", 0)
                    leader = c.get("leader", "") if isinstance(c, dict) else getattr(c, "leader", "")
                    lines.append(f"{i}. {name}  涨跌幅:{pct}%  领涨:{leader}")
                data["hot_concepts"] = "\n".join(lines)
            else:
                data["hot_concepts"] = "暂无数据"
        except Exception as e:
            data["hot_concepts"] = f"获取失败: {e}"

        try:
            hot_stocks = await self.funnel.get_hot_stocks()
            items = hot_stocks.items if hasattr(hot_stocks, "items") else (hot_stocks.get("items", []) if isinstance(hot_stocks, dict) else [])
            if items:
                lines = []
                for i, s in enumerate(items[:20], 1):
                    if isinstance(s, dict):
                        sym, name, price, pct = s.get("symbol", ""), s.get("name", ""), s.get("latest_price", 0), s.get("change_pct", 0)
                    else:
                        sym, name, price, pct = getattr(s, "symbol", ""), getattr(s, "name", ""), getattr(s, "latest_price", 0), getattr(s, "change_pct", 0)
                    lines.append(f"{i}. {sym} {name}  价格:{price}  涨跌幅:{pct}%")
                data["hot_stocks"] = "\n".join(lines)
            else:
                data["hot_stocks"] = "暂无数据"
        except Exception as e:
            data["hot_stocks"] = f"获取失败: {e}"

        # 漏斗状态 + 收集 buy/focus 池股票
        pool_symbols: list[tuple[str, str, str]] = []  # (symbol, name, pool)
        try:
            funnel = await self.funnel.get_funnel()
            stats = funnel.stats if hasattr(funnel, "stats") else (funnel.get("stats", {}) if isinstance(funnel, dict) else {})
            if isinstance(stats, dict):
                data["funnel_summary"] = f"候选池: {stats.get('candidate', 0)}只 | 重点池: {stats.get('focus', 0)}只 | 买入池: {stats.get('buy', 0)}只"
            else:
                data["funnel_summary"] = f"候选池: {getattr(stats, 'candidate', 0)}只 | 重点池: {getattr(stats, 'focus', 0)}只 | 买入池: {getattr(stats, 'buy', 0)}只"

            pools = funnel.pools if hasattr(funnel, "pools") else (funnel.get("pools", {}) if isinstance(funnel, dict) else {})
            for pool_name in ("buy", "focus"):
                pool_list = pools.get(pool_name, []) if isinstance(pools, dict) else getattr(pools, pool_name, [])
                for card in pool_list:
                    sym = card.get("symbol", "") if isinstance(card, dict) else getattr(card, "symbol", "")
                    nm = card.get("name", "") if isinstance(card, dict) else getattr(card, "name", "")
                    if sym:
                        pool_symbols.append((sym, nm, pool_name))
        except Exception as e:
            data["funnel_summary"] = f"获取失败: {e}"

        # Kronos 预测 — 对 buy/focus 池的股票调真实模型
        if pool_symbols:
            from app.services.kronos_predict_service import KronosPredictService
            kronos = KronosPredictService()
            pred_lines = []
            for sym, nm, pool in pool_symbols[:10]:
                try:
                    result = await kronos.predict(sym, lookback=180, horizon=3)
                    pk = result.get("predicted_kline", [])
                    if pk:
                        parts = []
                        for k in pk:
                            chg = round((k["close"] - k["open"]) / k["open"] * 100, 2) if k["open"] else 0
                            parts.append(f"{k['date']}:收{k['close']:.2f}({'+' if chg>=0 else ''}{chg}%)")
                        pred_lines.append(f"- {sym} {nm} [{pool}池] → {' | '.join(parts)}")
                    else:
                        pred_lines.append(f"- {sym} {nm} [{pool}池] → K线不足，无法预测")
                except Exception:
                    pred_lines.append(f"- {sym} {nm} [{pool}池] → 预测失败")
            data["kronos_predictions"] = "\n".join(pred_lines)

        return data

    async def _call_monitor_llm(self, system_prompt: str, user_prompt: str) -> str | None:
        """调用 LLM 生成智能监控报告。优先用本地 hermes CLI，其次 HTTP API。"""
        import shutil

        # 1) 本地 hermes CLI（通过 hermes chat -q 调用，使用其已配置的 LLM）
        hermes_bin = shutil.which("hermes")
        if hermes_bin:
            try:
                result = await self._call_hermes_cli(system_prompt, user_prompt)
                if result:
                    return result
            except Exception as e:
                print(f"[hermes] CLI call failed: {e}")

        # 2) HTTP API fallback
        import httpx
        hermes_available = await self._check_hermes_agent()
        if hermes_available:
            api_key = os.environ.get("API_SERVER_KEY", "")
            base_url = self._HERMES_AGENT_BASE
            model = "hermes-agent"
            timeout = 120
        else:
            api_key = os.environ.get("OPENAI_API_KEY", "")
            if not api_key:
                return None
            model = os.environ.get("HERMES_MODEL", "gpt-4o-mini")
            base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
            timeout = 60

        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.4,
        }

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(f"{base_url}/chat/completions", headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"]["content"]
        except Exception as e:
            print(f"[hermes] monitor HTTP LLM call failed: {e}")
            return None

    async def _call_hermes_cli(self, system_prompt: str, user_prompt: str) -> str | None:
        """通过 hermes chat -q 子进程调用本地 Hermes Agent。"""
        combined_prompt = f"{system_prompt}\n\n---\n\n{user_prompt}"

        proc = await asyncio.create_subprocess_exec(
            "hermes", "chat", "-q", combined_prompt, "--quiet",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
        except asyncio.TimeoutError:
            proc.kill()
            print("[hermes] CLI call timed out (180s)")
            return None

        if proc.returncode != 0:
            err = stderr.decode(errors="replace").strip()[:500]
            print(f"[hermes] CLI exit code {proc.returncode}: {err}")
            return None

        raw = stdout.decode(errors="replace").strip()
        lines = raw.split("\n")
        content_lines = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("session_id:"):
                continue
            # 跳过 logging 行
            if stripped.startswith("[") and "INFO" in line and ".py:" in line:
                continue
            # 跳过 Hermes ASCII 框线
            if stripped.startswith("╭─") or stripped.startswith("╰─") or stripped.startswith("│"):
                continue
            content_lines.append(line)

        content = "\n".join(content_lines).strip()
        # hermes --quiet 有时重复输出（box + plain），取前半段
        if content:
            half = len(content) // 2
            if half > 200:
                first_half = content[:half].strip()
                second_half = content[half:].strip()
                # 检查前后是否高度相似（取前 200 字符比较）
                if first_half[:200] == second_half[:200]:
                    content = first_half

        if not content:
            return None
        print(f"[hermes] CLI response: {len(content)} chars")
        return content

    # ── 状态查询 ──

    async def get_status_async(self) -> dict:
        last = self.memory.get_last_task()
        counts = self.memory.count_by_status()
        api_key = os.environ.get("OPENAI_API_KEY", "")
        hermes_ok = await self._check_hermes_agent()
        return {
            "running": self._running,
            "llm_available": hermes_ok or bool(api_key),
            "hermes_agent_available": hermes_ok,
            "hermes_agent_url": self._HERMES_AGENT_BASE,
            "mode": "agent" if hermes_ok else "fallback",
            "last_run": _format_last_run(last) if last else None,
            "stats": {
                "total_proposals": sum(counts.values()),
                "pending_proposals": counts.get("pending", 0),
                "approved_proposals": counts.get("approved", 0),
                "rejected_proposals": counts.get("rejected", 0),
            },
        }

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

            # 效果追踪检查 16:00（盘后数据已更新）
            if h == 16 and 0 <= m < 10:
                await _check_outcome_tracking(runtime)

        except Exception as e:
            print(f"[hermes] scheduler error: {e}")

        await asyncio.sleep(300)


async def monitor_loop(runtime: HermesRuntime, hub: Any = None) -> None:
    """智能监控定时循环——独立于 hermes_scheduler_loop 运行。"""
    await asyncio.sleep(10)
    while True:
        try:
            config = runtime.memory.get_monitor_config()
            if config.get("enabled"):
                n = now_cn()
                h, m = n.hour, n.minute
                in_morning = (h == 9 and m >= 30) or (h == 10) or (h == 11 and m <= 30)
                in_afternoon = (h >= 13 and h < 15)
                if in_morning or in_afternoon:
                    last = runtime.memory.get_latest_monitor_message()
                    interval = max(config.get("interval_minutes", 10), 1)
                    should_run = True
                    if last and last.get("created_at"):
                        from datetime import datetime
                        try:
                            last_ts = datetime.fromisoformat(last["created_at"])
                            diff_min = (n - last_ts).total_seconds() / 60
                            should_run = diff_min >= interval
                        except Exception:
                            pass
                    if should_run:
                        print(f"[monitor] tick at {n.strftime('%H:%M')}")
                        result = await runtime.run_monitor_tick(trigger="scheduled")
                        if result.get("success") and hub:
                            await hub.broadcast("monitor_update", {
                                "message_id": result["message_id"],
                                "content": result["content"],
                                "created_at": n.isoformat(),
                                "trigger": "scheduled",
                            })
        except Exception as e:
            print(f"[monitor] loop error: {e}")
        await asyncio.sleep(30)


async def _check_outcome_tracking(runtime: HermesRuntime) -> None:
    """检查到期的提案效果追踪，比较基线与当前指标。"""
    from app.services.hermes_memory_bridge import record_outcome_to_hermes_memory

    pending = runtime.memory.get_pending_outcome_checks()
    if not pending:
        return

    print(f"[hermes] checking {len(pending)} outcome tracking records")

    try:
        funnel = await runtime.funnel.get_funnel()
        current = {
            "candidate_count": funnel.stats.get("candidate", 0) if hasattr(funnel, "stats") else 0,
            "focus_count": funnel.stats.get("focus", 0) if hasattr(funnel, "stats") else 0,
            "buy_count": funnel.stats.get("buy", 0) if hasattr(funnel, "stats") else 0,
        }
    except Exception as e:
        print(f"[hermes] outcome tracking: cannot get current funnel: {e}")
        return

    for record in pending:
        try:
            import json
            baseline = record.get("baseline") or {}
            if isinstance(baseline, str):
                baseline = json.loads(baseline)

            outcome = {
                "baseline": baseline,
                "current": current,
                "delta": {
                    "candidate": current["candidate_count"] - baseline.get("candidate_count", 0),
                    "focus": current["focus_count"] - baseline.get("focus_count", 0),
                    "buy": current["buy_count"] - baseline.get("buy_count", 0),
                },
                "days_elapsed": record.get("check_after_days", 3),
            }

            delta = outcome["delta"]
            if delta["candidate"] > 0:
                effect = f"候选池+{delta['candidate']}"
            elif delta["candidate"] < 0:
                effect = f"候选池{delta['candidate']}"
            else:
                effect = "候选池不变"

            if delta["focus"] != 0:
                effect += f", 重点池{'+'if delta['focus']>0 else ''}{delta['focus']}"
            if delta["buy"] != 0:
                effect += f", 买入池{'+'if delta['buy']>0 else ''}{delta['buy']}"

            runtime.memory.complete_outcome_check(record["id"], outcome)

            title = record.get("proposal_title", "未知提案")
            record_outcome_to_hermes_memory(title, effect)

            print(f"[hermes] outcome tracked: {title} -> {effect}")
        except Exception as e:
            print(f"[hermes] outcome tracking error for record {record.get('id')}: {e}")


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
