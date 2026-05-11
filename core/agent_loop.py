"""AgentLoop — 基于 LangGraph 的状态机 Agent，支持 ReAct、Plan-Execute 和 Multi-Agent 三种模式。

流程：
  think → route → plan_or_react
                        ├─ 简单任务 → act（含权限检查 + HITL 审批） → think
                        └─ 复杂任务 → plan → execute_plan → END

Multi-Agent 模式通过 /team 命令触发，使用独立的 MultiAgentOrchestrator。
"""

import logging
import os
import re
import time
from typing import Annotated, Literal, Optional, Sequence, TypedDict

from rich.console import Console
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    message_chunk_to_message,
)
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages

from core.compaction import ContextCompactor
from core.llm_factory import create_llm
from core.memory import MemoryManager
from core.plan_models import Plan
from core.planner import Planner
from core.plan_executor import PlanExecutor
from hooks.manager import HookManager
from permissions.policy import PermissionPolicy
from tools.registry import ToolRegistry

logger = logging.getLogger(__name__)
console = Console()

MAX_ITERATIONS = 50
MAX_CONSECUTIVE_TOOL_FAILURES = 2

COMPLEXITY_PROMPT = (
    "判断以下任务的复杂度，只回复一个词：simple 或 complex\n\n"
    "simple：简单查询、单步操作、单文件修改、简单问答、查看文件、搜索代码\n"
    "complex：多步骤任务、创建项目、多文件创建/修改、需要规划和审查的任务、"
    "涉及多种工具配合的任务\n\n"
    "任务："
)


class AgentState(TypedDict):
    """Agent 循环状态。"""
    messages: Annotated[list[BaseMessage], add_messages]
    plan: Optional[dict]  # 当前执行计划（序列化后的 Plan）


class AgentLoop:
    """LangGraph 状态机 Agent，编排 think → route → act 循环。"""

    def __init__(
        self,
        tool_registry: ToolRegistry,
        permission_policy: PermissionPolicy,
        hook_manager: Optional[HookManager] = None,
        memory: Optional[MemoryManager] = None,
        provider: str = None,
        model: str = None,
        force_plan_mode: bool = False,
    ):
        self.tool_registry = tool_registry
        self.permission_policy = permission_policy
        self.hook_manager = hook_manager
        self.memory = memory
        self.provider = provider
        self.model = model
        self.force_plan_mode = force_plan_mode
        self.mode = "default"  # 权限模式，可选: default, ask, acceptEdits, bypassPermissions
        self._model_call_count = 0
        self._tool_unavailable_reasons: dict[str, str] = {}
        self._tool_failure_counts: dict[str, int] = {}
        self._disabled_tools: dict[str, str] = {}

        self.llm = create_llm(provider, model)
        self.tools = self._filter_available_tools(tool_registry.get_langchain_tools())
        self.llm_with_tools = self.llm.bind_tools(self.tools)

        # 上下文压缩器（在 LLM 初始化之后创建，因为需要 LLM 做摘要）
        self.compactor = ContextCompactor(llm=self.llm, memory_manager=self.memory)

        self.planner = Planner(provider, model)
        self.plan_executor = PlanExecutor(
            tool_registry=tool_registry,
            permission_policy=permission_policy,
            hook_manager=hook_manager,
        )

        self.graph = self._build_graph()

    def _filter_available_tools(self, tools: list) -> list:
        """过滤当前环境不可用的工具，避免模型反复调用失败工具。"""
        available_tools = []
        for tool in tools:
            ok, reason = self._check_tool_availability(tool)
            if ok:
                available_tools.append(tool)
                continue
            self._tool_unavailable_reasons[tool.name] = reason
            logger.warning("工具 %s 不可用: %s", tool.name, reason)
        return available_tools

    @staticmethod
    def _check_tool_availability(tool) -> tuple[bool, str]:
        """检查工具是否在当前运行环境可用。"""
        tool_name = getattr(tool, "name", "")
        if tool_name == "WebSearch":
            try:
                import tavily  # noqa: F401
            except Exception:
                return False, "缺少 tavily-python 依赖"
            if not os.environ.get("TAVILY_API_KEY"):
                return False, "未配置环境变量 TAVILY_API_KEY"
        return True, ""

    def _check_hitl_approval(self, tool_name: str, tool_args: dict):
        """检查工具是否需要 HITL 审批，如需要则发起审批流程。

        Returns:
            None — 不需要审批
            ApprovalResult — 审批结果
        """
        from core.hitl_models import ApprovalResult, ApprovalDecision

        # 仅当 tool_registry 是 HitlToolRegistry 时才进行审批检查
        if not hasattr(self.tool_registry, 'hitl_handler'):
            return None

        hitl_handler = self.tool_registry.hitl_handler
        if not hitl_handler or not hitl_handler.is_enabled():
            return None

        return self.tool_registry.check_approval(tool_name, tool_args)

    def _reset_runtime_state(self):
        """每次新问题前重置本轮执行状态。"""
        self._model_call_count = 0
        self._tool_failure_counts = {}
        self._disabled_tools = {}

    def _record_tool_failure(self, tool_name: str, error: str):
        """记录工具失败次数，超过阈值后本轮禁用。"""
        failed = self._tool_failure_counts.get(tool_name, 0) + 1
        self._tool_failure_counts[tool_name] = failed
        if failed >= MAX_CONSECUTIVE_TOOL_FAILURES:
            self._disabled_tools[tool_name] = (
                f"工具 {tool_name} 已连续失败 {failed} 次，本轮将不再执行。"
                "请不要继续调用该工具，直接向用户说明原因并给出替代方案。"
            )

    def _build_graph(self) -> StateGraph:
        """构建 LangGraph 状态图。"""
        graph = StateGraph(AgentState)

        # 添加节点
        graph.add_node("think", self._think)
        graph.add_node("route", self._route)
        graph.add_node("plan_or_react", self._plan_or_react)
        graph.add_node("act", self._act)
        graph.add_node("plan", self._plan)
        graph.add_node("execute_plan", self._execute_plan)

        # 设置入口
        graph.set_entry_point("think")

        # 添加边
        graph.add_edge("think", "route")
        graph.add_conditional_edges("route", self._should_continue, {
            "continue": "plan_or_react",
            "end": END,
        })
        graph.add_conditional_edges("plan_or_react", self._choose_path, {
            "react": "act",          # 直接进 act，权限检查和 HITL 审批都在 act 中统一处理
            "plan": "plan",
        })
        graph.add_edge("act", "think")
        graph.add_edge("plan", "execute_plan")
        # Plan-Execute 一次执行即结束，避免进入下一轮 think 产生重复/矛盾输出
        graph.add_edge("execute_plan", END)

        return graph.compile()

    # ── 节点实现 ──────────────────────────────────────────

    def _think(self, state: AgentState) -> dict:
        """调用 LLM 思考，决定下一步行动。"""
        messages = state["messages"]

        # 首次调用时注入系统提示（每次重新构建，包含最新记忆和检索结果）
        if self.memory:
            # 从最新用户消息中提取查询词，用于记忆检索
            query = ""
            for m in reversed(messages):
                if isinstance(m, HumanMessage):
                    query = m.content[:100]
                    break

            system_msg = self.memory.build_system_message(query)
            if system_msg:
                if messages and isinstance(messages[0], SystemMessage):
                    messages = [system_msg] + list(messages[1:])
                else:
                    messages = [system_msg] + list(messages)

        # 自动压缩检查（在 LLM 调用前）
        if self.compactor.should_compact(messages):
            messages = self.compactor.compact(messages)

        self._model_call_count += 1
        call_index = self._model_call_count
        start_at = time.perf_counter()

        if self.hook_manager:
            self.hook_manager.emit("LLMStart", {
                "call_index": call_index,
            })

        full_chunk = None
        stream_failed = False
        emitted_any_delta = False
        try:
            for chunk in self.llm_with_tools.stream(messages):
                full_chunk = chunk if full_chunk is None else full_chunk + chunk
                delta_text = self._extract_text_content(getattr(chunk, "content", ""))
                if delta_text and self.hook_manager:
                    emitted_any_delta = True
                    self.hook_manager.emit("LLMDelta", {
                        "call_index": call_index,
                        "delta": delta_text,
                    })
        except Exception as e:
            stream_failed = True
            logger.debug(f"LLM stream 失败，回退 invoke: {e}")

        if full_chunk is None or stream_failed:
            response = self.llm_with_tools.invoke(messages)
            if self.hook_manager and not emitted_any_delta:
                response_text = self._extract_text_content(getattr(response, "content", ""))
                if response_text:
                    self.hook_manager.emit("LLMDelta", {
                        "call_index": call_index,
                        "delta": response_text,
                    })
        else:
            response = message_chunk_to_message(full_chunk)

        input_tokens, output_tokens, total_tokens = self._extract_usage(response)
        if self.hook_manager:
            self.hook_manager.emit("LLMEnd", {
                "call_index": call_index,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
                "duration_seconds": round(time.perf_counter() - start_at, 3),
                "has_tool_calls": bool(getattr(response, "tool_calls", None)),
            })

        # 调试日志：记录 LLM 响应详情
        response_content = getattr(response, "content", "")
        response_tool_calls = getattr(response, "tool_calls", None)
        logger.debug(
            f"LLM 响应详情: call_index={call_index}, "
            f"content_type={type(response_content).__name__}, "
            f"content_len={len(str(response_content))}, "
            f"content_preview={repr(str(response_content)[:200])}, "
            f"tool_calls={response_tool_calls}, "
            f"output_tokens={output_tokens}"
        )

        # 检测空响应：MiMo 模型可能返回 thinking-only 的响应（有 output tokens 但无内容）
        has_content = bool(self._extract_text_content(response_content))
        has_tool_calls = bool(response_tool_calls)
        if not has_content and not has_tool_calls and output_tokens > 0:
            logger.warning(
                f"检测到空响应（thinking-only）: output_tokens={output_tokens}, "
                f"content={repr(response_content)}, tool_calls={response_tool_calls}"
            )
            # 如果是工具执行后的空响应，添加提示让模型重新回答
            if any(isinstance(m, ToolMessage) for m in messages[-3:]):
                retry_msg = HumanMessage(
                    content="[系统提示] 你的上一轮回复为空。请不要调用任何工具，"
                            "直接用文字回答用户的问题。基于工具返回的结果，用中文回复。"
                )
                messages = list(messages) + [retry_msg]
                logger.info("检测到工具执行后空响应，自动重试...")
                retry_response = self.llm_with_tools.invoke(messages)

                # 提取重试结果的文本内容，构造干净的 AIMessage（去掉 tool_calls）
                # 防止重试结果带 tool_calls 导致 graph 多绕一圈
                retry_text = self._extract_text_content(getattr(retry_response, "content", ""))
                if retry_text:
                    response = AIMessage(content=retry_text)
                    if self.hook_manager:
                        self.hook_manager.emit("LLMDelta", {
                            "call_index": call_index,
                            "delta": retry_text,
                        })
                # 如果重试也为空，保持原 response（后续由 _run_agent 处理空响应提示）

        return {"messages": [response]}

    @staticmethod
    def _extract_text_content(content) -> str:
        """从字符串或分块内容中提取可打印文本。"""
        if isinstance(content, str):
            return AgentLoop._sanitize_stream_text(content)
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict) and item.get("type") == "text":
                    parts.append(item.get("text", ""))
            return AgentLoop._sanitize_stream_text("".join(parts))
        return ""

    @staticmethod
    def _sanitize_stream_text(text: str) -> str:
        """清理流式文本中的控制字符，避免终端出现 ^0/^R 等乱码。"""
        if not text:
            return ""
        normalized = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
        return re.sub(r"[\x01-\x08\x0b\x0c\x0e-\x1f\x7f]", "", normalized)

    @staticmethod
    def _extract_usage(message: AIMessage) -> tuple[int, int, int]:
        """兼容不同 provider 的 token 统计结构。"""
        usage = getattr(message, "usage_metadata", None) or {}
        meta = getattr(message, "response_metadata", None) or {}
        token_usage = (
            meta.get("token_usage")
            or meta.get("usage")
            or {}
        )

        input_tokens = (
            usage.get("input_tokens")
            or usage.get("prompt_tokens")
            or token_usage.get("input_tokens")
            or token_usage.get("prompt_tokens")
            or 0
        )
        output_tokens = (
            usage.get("output_tokens")
            or usage.get("completion_tokens")
            or token_usage.get("output_tokens")
            or token_usage.get("completion_tokens")
            or 0
        )
        total_tokens = (
            usage.get("total_tokens")
            or token_usage.get("total_tokens")
            or (input_tokens + output_tokens)
        )
        return int(input_tokens), int(output_tokens), int(total_tokens)

    def _route(self, state: AgentState) -> dict:
        """路由：检查 LLM 是否产生了 tool_calls。"""
        last_message = state["messages"][-1]
        if not isinstance(last_message, AIMessage):
            return {}
        has_tool_calls = bool(last_message.tool_calls)
        return {}

    def _should_continue(self, state: AgentState) -> str:
        """判断是否继续循环。"""
        last_message = state["messages"][-1]
        # Plan 模式：首次进入时继续，计划执行后结束，避免循环
        if self.force_plan_mode:
            return "continue" if state.get("plan") is None else "end"
        # 普通模式：有 tool_calls 则继续
        if isinstance(last_message, AIMessage) and last_message.tool_calls:
            return "continue"
        return "end"

    def _plan_or_react(self, state: AgentState) -> dict:
        """路由决策：走 ReAct 还是 Plan-Execute。

        判断逻辑：
        1. force_plan_mode 为 True → 走 Plan
        2. LLM 产生多个 tool_calls 且无依赖关系 → 走 Plan
        3. 其他情况 → 走 ReAct
        """
        if self.force_plan_mode:
            return {"plan": None}

        last_message = state["messages"][-1]
        if isinstance(last_message, AIMessage) and last_message.tool_calls:
            tool_calls = last_message.tool_calls
            # 多个独立工具调用 → 适合 Plan-Execute
            if len(tool_calls) > 1:
                return {"plan": None}

        return {}

    def _choose_path(self, state: AgentState) -> str:
        """根据路由决策选择执行路径。"""
        if self.force_plan_mode:
            return "plan"

        last_message = state["messages"][-1]
        if isinstance(last_message, AIMessage) and last_message.tool_calls:
            if len(last_message.tool_calls) > 1:
                return "plan"

        return "react"

    def _check_permissions(self, state: AgentState) -> dict:
        """检查工具调用权限（已从 Graph 中移除，权限检查统一在 _act 中处理）。

        保留此方法供外部直接调用。
        """
        last_message = state["messages"][-1]
        if not isinstance(last_message, AIMessage):
            return {}

        for tool_call in last_message.tool_calls:
            tool_name = tool_call["name"]
            if not self.permission_policy.is_allowed(tool_name):
                logger.warning(f"工具 {tool_name} 被权限策略拒绝")

        return {}

    def _act(self, state: AgentState) -> dict:
        """执行工具调用。"""
        last_message = state["messages"][-1]
        if not isinstance(last_message, AIMessage):
            return {}

        from langchain_core.messages import ToolMessage
        tool_messages = []
        for tool_call in last_message.tool_calls:
            tool_name = tool_call["name"]
            tool_args = tool_call["args"]

            # 运行环境不可用：直接返回可解释错误，避免模型反复重试
            unavailable_reason = self._tool_unavailable_reasons.get(tool_name)
            if unavailable_reason:
                error_text = (
                    f"工具 {tool_name} 当前不可用：{unavailable_reason}。"
                    "请不要再次调用该工具，改用其他工具或直接向用户说明。"
                )
                if self.hook_manager:
                    self.hook_manager.emit("PostToolUse", {
                        "tool": tool_name,
                        "args": tool_args,
                        "result": error_text,
                        "error": True,
                    })
                tool_messages.append(ToolMessage(
                    content=error_text,
                    tool_call_id=tool_call["id"],
                ))
                continue

            # 本轮已达到失败阈值：短路并阻止继续重试
            disabled_reason = self._disabled_tools.get(tool_name)
            if disabled_reason:
                if self.hook_manager:
                    self.hook_manager.emit("PostToolUse", {
                        "tool": tool_name,
                        "args": tool_args,
                        "result": disabled_reason,
                        "error": True,
                    })
                tool_messages.append(ToolMessage(
                    content=disabled_reason,
                    tool_call_id=tool_call["id"],
                ))
                continue

            # 权限检查
            if not self.permission_policy.is_allowed(tool_name):
                error_text = f"工具 {tool_name} 被权限策略拒绝"
                self._record_tool_failure(tool_name, error_text)
                if self.hook_manager:
                    self.hook_manager.emit("PostToolUse", {
                        "tool": tool_name,
                        "args": tool_args,
                        "result": error_text,
                        "error": True,
                    })
                tool_messages.append(ToolMessage(
                    content=error_text,
                    tool_call_id=tool_call["id"],
                ))
                continue

            # HITL 审批检查（完整审批流程，支持全部放行等高级功能）
            # 如果 HITL 启用，使用 HITL 的审批流程；否则使用简单的确认流程
            approval_result = self._check_hitl_approval(tool_name, tool_args)
            if approval_result is not None:
                # HITL 已处理审批，根据结果决定后续行为
                if approval_result.decision.value == "rejected":
                    reason = approval_result.reason or "用户拒绝了此操作"
                    hitl_msg = f"[HITL] 操作已被拒绝：{reason}"
                    if self.hook_manager:
                        self.hook_manager.emit("PostToolUse", {
                            "tool": tool_name,
                            "args": tool_args,
                            "result": hitl_msg,
                            "error": True,
                        })
                    tool_messages.append(ToolMessage(
                        content=hitl_msg,
                        tool_call_id=tool_call["id"],
                    ))
                    continue

                if approval_result.decision.value == "skipped":
                    if self.hook_manager:
                        self.hook_manager.emit("PostToolUse", {
                            "tool": tool_name,
                            "args": tool_args,
                            "result": "[HITL] 操作已被跳过",
                            "error": True,
                        })
                    tool_messages.append(ToolMessage(
                        content="[HITL] 操作已被跳过",
                        tool_call_id=tool_call["id"],
                    ))
                    continue

                # 修改参数后执行
                if approval_result.decision.value == "modified" and approval_result.modified_args:
                    tool_args = approval_result.modified_args

                # approved / approved_all 继续执行
            elif self.mode == "default" and self.permission_policy.needs_confirmation(tool_name, "default"):
                # HITL 未启用，使用简单的确认流程
                from cli.hitl_renderer import render_approval_panel, render_choice_hint
                from core.hitl_models import ApprovalRequest
                from core.hitl_policy import get_danger_info
                
                danger_level, risk_description = get_danger_info(tool_name)
                request = ApprovalRequest(
                    tool_name=tool_name,
                    arguments=tool_args,
                    danger_level=danger_level,
                    risk_description=risk_description,
                )
                
                console.print()
                console.print(render_approval_panel(request))
                console.print(render_choice_hint())
                console.print()
                
                user_input = console.input("[bold]> [/bold]").strip().lower()
                
                if user_input == "n":
                    error_text = "[权限] 用户拒绝了此操作"
                    if self.hook_manager:
                        self.hook_manager.emit("PostToolUse", {
                            "tool": tool_name,
                            "args": tool_args,
                            "result": error_text,
                            "error": True,
                        })
                    tool_messages.append(ToolMessage(
                        content=error_text,
                        tool_call_id=tool_call["id"],
                    ))
                    continue
                elif user_input == "s":
                    skip_text = "[权限] 用户跳过了此操作"
                    if self.hook_manager:
                        self.hook_manager.emit("PostToolUse", {
                            "tool": tool_name,
                            "args": tool_args,
                            "result": skip_text,
                            "error": True,
                        })
                    tool_messages.append(ToolMessage(
                        content=skip_text,
                        tool_call_id=tool_call["id"],
                    ))
                    continue
                # y 或其他 → 继续执行

            # flush 渲染缓冲区，避免审批面板与流式输出混淆
            if self.hook_manager:
                self.hook_manager.emit("BeforeToolExecution", {})

            # PreToolUse Hook
            if self.hook_manager:
                self.hook_manager.emit("PreToolUse", {
                    "tool": tool_name,
                    "args": tool_args,
                })

            # 执行工具
            had_error = False
            tool = self.tool_registry.get(tool_name)
            if tool:
                try:
                    result = tool.invoke(tool_args)
                    self._tool_failure_counts[tool_name] = 0
                except Exception as e:
                    had_error = True
                    self._record_tool_failure(tool_name, str(e))
                    result = f"工具执行错误: {e}"
                    disabled_reason = self._disabled_tools.get(tool_name)
                    if disabled_reason:
                        result = f"{result}\n{disabled_reason}"
                    if self.hook_manager:
                        self.hook_manager.emit("PostToolUse", {
                            "tool": tool_name,
                            "args": tool_args,
                            "result": str(result)[:500],
                            "error": True,
                        })
            else:
                had_error = True
                result = f"工具 {tool_name} 不存在"
                self._record_tool_failure(tool_name, result)
                if self.hook_manager:
                    self.hook_manager.emit("PostToolUse", {
                        "tool": tool_name,
                        "args": tool_args,
                        "result": str(result)[:500],
                        "error": True,
                    })

            # PostToolUse Hook
            if self.hook_manager and not had_error:
                self.hook_manager.emit("PostToolUse", {
                    "tool": tool_name,
                    "args": tool_args,
                    "result": str(result)[:500],
                })

            from langchain_core.messages import ToolMessage
            tool_messages.append(ToolMessage(
                content=str(result),
                tool_call_id=tool_call["id"],
            ))

        return {"messages": tool_messages}

    def _plan(self, state: AgentState) -> dict:
        """生成执行计划。"""
        # 从对话历史中提取用户目标
        user_messages = [
            m for m in state["messages"] if isinstance(m, HumanMessage)
        ]
        goal = user_messages[-1].content if user_messages else ""

        if self.hook_manager:
            self.hook_manager.emit("PlanStart", {
                "goal": goal,
            })

        # 如果 LLM 已经给出了 tool_calls，将其转化为目标描述
        last_message = state["messages"][-1]
        if isinstance(last_message, AIMessage) and last_message.tool_calls:
            tool_descriptions = []
            for tc in last_message.tool_calls:
                tool_descriptions.append(f"- {tc['name']}({tc['args']})")
            goal = f"{goal}\n需要执行以下操作：\n" + "\n".join(tool_descriptions)

        try:
            plan = self.planner.create_plan(goal)
            plan_dict = plan.model_dump()
            if self.hook_manager:
                self.hook_manager.emit("PlanCreated", {
                    "plan_id": plan.id,
                    "task_count": len(plan.tasks),
                })
            logger.info(f"计划生成完成: {plan.id}, 共 {len(plan.tasks)} 个任务")
            return {"plan": plan_dict}
        except Exception as e:
            logger.error(f"计划生成失败: {e}")
            if self.hook_manager:
                self.hook_manager.emit("PlanError", {
                    "error": str(e),
                })
            return {
                "messages": [AIMessage(content=f"计划生成失败: {e}")],
                "plan": None,
            }

    def _execute_plan(self, state: AgentState) -> dict:
        """执行已生成的计划。"""
        plan_dict = state.get("plan")
        if not plan_dict:
            return {"messages": [AIMessage(content="无可执行的计划")]}

        plan = Plan.model_validate(plan_dict)
        if self.hook_manager:
            self.hook_manager.emit("PlanExecuteStart", {
                "plan_id": plan.id,
                "task_count": len(plan.tasks),
            })
        plan = self.plan_executor.execute(plan)
        if self.hook_manager:
            self.hook_manager.emit("PlanExecuteEnd", {
                "plan_id": plan.id,
                "status": plan.status.value,
            })

        # 将执行结果汇总为 AIMessage
        results = []
        for task in plan.tasks:
            if task.status.value == "completed":
                results.append(f"[完成] {task.description}: {task.result[:200] if task.result else ''}")
            elif task.status.value == "failed":
                results.append(f"[失败] {task.description}: {task.error}")
            elif task.status.value == "skipped":
                results.append(f"[跳过] {task.description}: {task.error}")

        summary = f"计划执行{'成功' if plan.status.value == 'completed' else '失败'}\n" + "\n".join(results)
        return {
            "messages": [AIMessage(content=summary)],
            "plan": plan.model_dump(),
        }

    # ── 公共接口 ──────────────────────────────────────────

    def stream(self, user_input: str):
        """流式执行 Agent 循环，yield 每步状态。"""
        self._reset_runtime_state()
        initial_state = {
            "messages": [HumanMessage(content=user_input)],
            "plan": None,
        }

        config = {"recursion_limit": 100}
        for event in self.graph.stream(initial_state, config=config):
            yield event

    def stream_with_history(self, conversation: list):
        """流式执行 Agent 循环，传入完整对话历史。"""
        self._reset_runtime_state()

        # 压缩检查（对话历史过长时自动压缩）
        if self.compactor.should_compact(conversation):
            conversation = self.compactor.compact(conversation)

        initial_state = {
            "messages": list(conversation),
            "plan": None,
        }

        config = {"recursion_limit": 100}
        for event in self.graph.stream(initial_state, config=config):
            yield event

    def invoke(self, user_input: str) -> dict:
        """同步执行 Agent 循环，返回最终状态。"""
        self._reset_runtime_state()
        initial_state = {
            "messages": [HumanMessage(content=user_input)],
            "plan": None,
        }

        config = {"recursion_limit": 100}
        return self.graph.invoke(initial_state, config=config)

    def classify_complexity(self, user_input: str) -> str:
        """用 LLM 判断任务复杂度。

        优先使用 config.yaml 中 auto_team.classifier 配置的模型，
        默认回退到当前会话使用的 LLM（provider + model）。
        """
        from core.llm_factory import create_llm
        import settings
        try:
            # 读取配置的 classifier 模型，没有则用当前 LLM
            cls_provider = settings.get("team.classifier_provider", self.provider)
            cls_model = settings.get("team.classifier_model", self.model)
            classifier_llm = create_llm(provider=cls_provider, model=cls_model)
            messages = [
                SystemMessage(content=COMPLEXITY_PROMPT),
                HumanMessage(content=user_input),
            ]
            response = classifier_llm.invoke(messages)
            text = response.content.strip().lower()
            if "complex" in text:
                return "complex"
            return "simple"
        except Exception as e:
            logger.warning(f"复杂度判断失败，保守走简单路径: {e}")
            return "simple"

    def run_multi_agent(self, user_input: str) -> dict:
        """以 Multi-Agent 模式执行任务。

        使用独立的 MultiAgentOrchestrator，基于 Supervisor 模式
        管理 Planner/Worker/Reviewer 三角色协作。

        Args:
            user_input: 用户的任务描述

        Returns:
            最终状态字典，包含 messages、step_results 等
        """
        from agents.orchestrator import MultiAgentOrchestrator

        if self.hook_manager:
            self.hook_manager.emit("TeamModeStart", {"goal": user_input})

        orchestrator = MultiAgentOrchestrator(
            llm=self.llm,
            tool_registry=self.tool_registry,
            permission_policy=self.permission_policy,
            hook_manager=self.hook_manager,
            memory=self.memory,
        )
        result = orchestrator.run(user_input)

        if self.hook_manager:
            self.hook_manager.emit("TeamModeEnd", {
                "step_count": len(result.get("step_results", {})),
            })

        return result
