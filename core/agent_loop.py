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
from core.cancellation import AgentCancelledError, CancellationToken
from core.llm_factory import create_llm
from core.memory import MemoryManager
from core.plan_models import Plan
from core.planner import Planner
from core.plan_executor import PlanExecutor
from hooks.manager import HookManager
from permissions.policy import PermissionPolicy
from tools.registry import ToolRegistry
import settings

logger = logging.getLogger(__name__)
console = Console()

MAX_ITERATIONS = 50
MAX_CONSECUTIVE_TOOL_FAILURES = 2

COMPLEXITY_PROMPT = (
    "判断以下任务的复杂度，只回复一个词：simple 或 complex\n\n"
    "simple：简单查询、单步操作、单文件修改、简单问答、查看文件、搜索代码、"
    "搜索互联网信息、查看网页内容、抓取网页、查找资料、获取信息\n"
    "complex：创建项目、多文件创建/修改、需要规划和审查的任务、"
    "涉及多种工具配合的任务、重构代码、实现新功能\n\n"
    "注意：搜索和查看网页是简单任务，不要判为 complex\n\n"
    "任务："
)


class AgentState(TypedDict):
    """Agent 循环状态。"""
    messages: Annotated[list[BaseMessage], add_messages]
    plan: Optional[dict]  # 当前执行计划（序列化后的 Plan）  plan 的直接作用是：把 Planner 生成的计划保存到图状态里，让下一个节点能拿到。


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
        mcp_manager=None,
        skill_registry=None,
        skill_buffer=None,
        cancellation_token: Optional[CancellationToken] = None,
    ):
        self.tool_registry = tool_registry
        self.permission_policy = permission_policy
        self.hook_manager = hook_manager
        self.memory = memory
        self.provider = provider
        self.model = model
        self.force_plan_mode = force_plan_mode
        self.mcp_manager = mcp_manager
        self.skill_registry = skill_registry
        self.skill_buffer = skill_buffer
        self.cancellation_token = cancellation_token
        self.mode = "default"
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
            cancellation_token=cancellation_token,
        )

        self.graph = self._build_graph()

    def _check_cancelled(self):
        """Stop at safe orchestration boundaries when a channel cancels."""
        token = getattr(self, "cancellation_token", None)
        if token:
            token.raise_if_cancelled()

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
        """检查工具是否在当前运行环境可用。

        WebSearch 支持多种搜索引擎（Tavily/SearXNG/DuckDuckGo），
        通过 SearchProviderFactory 自动检测，任一可用即可。
        """
        tool_name = getattr(tool, "name", "")
        if tool_name == "WebSearch":
            from web.providers.factory import SearchProviderFactory
            provider = SearchProviderFactory.create()
            if not provider.is_ready():
                return False, "无可用搜索引擎（需配置 TAVILY_API_KEY、SEARXNG_URL 或安装 duckduckgo-search）"
        return True, ""

    # ── MiMo reasoning_content 回传支持 ──────────────────────────

    def _preserve_reasoning_content(self, response):
        """从 LLM 响应中捕获 reasoning_content 并存入 additional_kwargs。

        MiMo 的 thinking 模式会在 assistant 消息中返回 reasoning_content，
        但 LangChain 不认识该字段，序列化时会丢失。
        MiMoChatOpenAI 子类已将 reasoning_content 存入 additional_kwargs，
        此方法做兜底检查，并处理 streaming 场景下保存在 LLM 实例上的值。
        """
        if not isinstance(response, AIMessage):
            return

        # reasoning_content 可能在不同位置，依次检查
        reasoning = getattr(response, "reasoning_content", None)
        if reasoning is None:
            reasoning = response.additional_kwargs.get("reasoning_content")
        if reasoning is None:
            meta = getattr(response, "response_metadata", None) or {}
            reasoning = meta.get("reasoning_content")

        # 兜底：检查 LLM 实例上保存的 streaming reasoning_content
        if reasoning is None:
            llm = getattr(self, "llm", None)
            if llm is not None:
                reasoning = getattr(llm, "_last_reasoning_content", None)
                if reasoning:
                    llm._last_reasoning_content = None  # 用完即清

        if reasoning:
            response.additional_kwargs["reasoning_content"] = reasoning
            logger.debug("已捕获 reasoning_content (%d 字符)", len(str(reasoning)))

    @staticmethod
    def _inject_reasoning_content(messages: list) -> list:
        """reasoning_content 回传现在由 MiMoChatOpenAI._get_request_payload 处理。

        此方法保留为空操作，避免影响非 MiMo provider 的逻辑。
        MiMoChatOpenAI 在消息序列化阶段会自动将 additional_kwargs["reasoning_content"]
        注入到 API 请求的 assistant 消息中。
        """
        return messages

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

    # 纯重复性工具（滚动+截图循环的标志）
    _BROWSER_REPEAT_TOOLS = frozenset({
        "take_screenshot", "take_snapshot",
    })
    # 导航类工具 — 表示 Agent 在不同页面间移动（有进展）
    _BROWSER_NAVIGATION_TOOLS = frozenset({
        "navigate_page", "new_page", "close_page",
    })
    _BROWSER_LOOP_WINDOW = 20  # 检查窗口大小（扩大以容纳多页提取场景）

    def _detect_browser_loop(self, messages: list) -> bool:
        """检测浏览器工具是否陷入重复循环。

        策略：
        1. 检查最近窗口中 screenshot/snapshot 是否 ≥5 次（纯重复截图）
        2. 检查是否出现相同的 3-tool 序列重复 ≥3 次（模式重复）
           但排除包含 evaluate_script 且窗口内有导航操作的情况（合法的多页提取）
        3. 检查 evaluate_script 是否在无导航的情况下连续调用 ≥6 次（单页死循环）
        触发后注入停止指令。
        """
        # 从 AIMessage.tool_calls 提取工具名（比 ToolMessage.name 更可靠）
        recent_tool_names = []
        for m in reversed(messages):
            if isinstance(m, AIMessage) and m.tool_calls:
                for tc in reversed(m.tool_calls):
                    recent_tool_names.append(tc.get("name", ""))
                    if len(recent_tool_names) >= self._BROWSER_LOOP_WINDOW:
                        break
            if len(recent_tool_names) >= self._BROWSER_LOOP_WINDOW:
                break

        # 过滤掉空名称
        recent_tool_names = [n for n in recent_tool_names if n]
        if len(recent_tool_names) < 6:
            return False

        # 检测 1：截图/快照工具过多
        repeat_count = sum(1 for t in recent_tool_names if t in self._BROWSER_REPEAT_TOOLS)
        if repeat_count >= 5:
            self._inject_stop_message(messages, "重复截图/快照过多")
            return True

        recent_tool_names.reverse()  # 恢复时间顺序

        # 检测 2：evaluate_script 无导航连续调用（单页死循环）
        # 找最后一次导航操作的位置，之后连续 evaluate_script 的次数
        last_nav_idx = -1
        for i in range(len(recent_tool_names) - 1, -1, -1):
            if recent_tool_names[i] in self._BROWSER_NAVIGATION_TOOLS:
                last_nav_idx = i
                break
        # 从最后一次导航之后算连续 evaluate_script
        tail = recent_tool_names[last_nav_idx + 1:]
        consecutive_eval = 0
        for t in reversed(tail):
            if t == "evaluate_script":
                consecutive_eval += 1
            else:
                break
        if consecutive_eval >= 6:
            self._inject_stop_message(messages, f"evaluate_script 无导航连续调用 {consecutive_eval} 次")
            return True

        # 检测 3：3-gram 模式重复（仅针对浏览器相关工具）
        _BROWSER_TOOLS = self._BROWSER_REPEAT_TOOLS | self._BROWSER_NAVIGATION_TOOLS | {
            "evaluate_script", "click", "drag", "type_text",
            "browser_connect", "browser_disconnect",
        }
        has_navigation = any(t in self._BROWSER_NAVIGATION_TOOLS for t in recent_tool_names)
        trigrams = [tuple(recent_tool_names[i:i+3]) for i in range(len(recent_tool_names) - 2)]
        if trigrams:
            from collections import Counter
            most_common = Counter(trigrams).most_common(1)[0]
            if most_common[1] >= 3:
                pattern = most_common[0]
                # 只有模式中至少包含一个浏览器工具时才视为浏览器循环
                has_browser_tool = any(t in _BROWSER_TOOLS for t in pattern)
                if not has_browser_tool:
                    return False  # 非浏览器工具重复（如 Read/Glob），不算循环
                # 如果模式中包含导航工具，说明 Agent 在不同页面间移动，是正常浏览
                has_nav_in_pattern = any(t in self._BROWSER_NAVIGATION_TOOLS for t in pattern)
                # 如果模式全是 evaluate_script 且窗口内有导航，是多页提取
                all_eval = set(pattern) == {"evaluate_script"}
                if has_nav_in_pattern or (all_eval and has_navigation):
                    pass  # 合法的多页内容提取/浏览模式
                else:
                    self._inject_stop_message(messages, f"工具序列 {pattern} 重复 {most_common[1]} 次")
                    return True

        return False

    def _inject_stop_message(self, messages: list, reason: str):
        """注入停止指令。"""
        logger.warning("检测到浏览器循环（%s），注入停止指令", reason)
        messages.append(SystemMessage(content=(
            "[系统中断] 你已经陷入重复的浏览器操作循环。\n"
            "请立即停止所有浏览器工具调用，根据已获取的内容进行总结回复用户。\n"
            "不要再调用 take_screenshot、take_snapshot 或 evaluate_script。"
        )))

    def _maybe_force_search_code(self, messages: list) -> Optional[AIMessage]:
        """对代码库实现类问题强制首跳 SearchCode。

        仅靠 prompt 要求模型优先用 SearchCode 不够稳定；当用户明确询问
        本地代码库里的实现、架构、调用关系等问题时，这里在第一次 LLM
        调用前直接构造一个 SearchCode tool_call。SearchCode 执行过后，
        后续是否 Read 关键文件仍交给模型决定。
        """
        if getattr(self, "force_plan_mode", False):
            return None
        if not settings.get("rag.force_search_code", True):
            return None
        if not self._is_search_code_available():
            return None

        latest_index, query = self._latest_human_query(messages)
        if latest_index < 0 or not query:
            return None
        if self._search_code_used_after(messages, latest_index):
            return None
        if not self._looks_like_codebase_question(query):
            return None

        tool_call_id = f"forced_searchcode_{int(time.time() * 1000)}"
        logger.info("代码库问题强制首跳 SearchCode: %s", query[:120])
        return AIMessage(
            content="",
            tool_calls=[{
                "name": "SearchCode",
                "args": {"query": query, "top_k": 5},
                "id": tool_call_id,
            }],
        )

    def _maybe_force_search_knowledge(self, messages: list) -> Optional[AIMessage]:
        """对上传资料/知识库类问题强制首跳 AskKnowledge。"""
        if getattr(self, "force_plan_mode", False):
            return None
        if not settings.get("knowledge_rag.force_search_knowledge", True):
            return None
        if not self._is_tool_available("AskKnowledge"):
            return None

        latest_index, query = self._latest_human_query(messages)
        if latest_index < 0 or not query:
            return None
        if self._tool_used_after(messages, latest_index, {"AskKnowledge", "SearchKnowledge"}):
            return None
        if not self._looks_like_knowledge_question(query):
            return None

        tool_call_id = f"forced_askknowledge_{int(time.time() * 1000)}"
        logger.info("资料问题强制首跳 AskKnowledge: %s", query[:120])
        return AIMessage(
            content="",
            tool_calls=[{
                "name": "AskKnowledge",
                "args": {"query": query, "top_k": 6},
                "id": tool_call_id,
            }],
        )

    def _is_search_code_available(self) -> bool:
        """检查 SearchCode 是否已注册且本轮未被禁用。"""
        return self._is_tool_available("SearchCode")

    def _is_tool_available(self, tool_name: str) -> bool:
        """检查工具是否已注册且本轮未被禁用。"""
        if tool_name in getattr(self, "_tool_unavailable_reasons", {}):
            return False
        if tool_name in getattr(self, "_disabled_tools", {}):
            return False
        try:
            return self.tool_registry.get(tool_name) is not None
        except Exception:
            return False

    @staticmethod
    def _latest_human_query(messages: list) -> tuple[int, str]:
        """返回最近一条用户消息的位置和内容。"""
        for idx in range(len(messages) - 1, -1, -1):
            msg = messages[idx]
            if isinstance(msg, HumanMessage):
                content = getattr(msg, "content", "")
                return idx, content if isinstance(content, str) else str(content)
        return -1, ""

    @staticmethod
    def _search_code_used_after(messages: list, index: int) -> bool:
        """最近用户消息之后是否已经调用过 SearchCode。"""
        return AgentLoop._tool_used_after(messages, index, {"SearchCode"})

    @staticmethod
    def _tool_used_after(messages: list, index: int, tool_names: set[str]) -> bool:
        """最近用户消息之后是否已经调用过指定工具。"""
        for msg in messages[index + 1:]:
            if isinstance(msg, AIMessage):
                for tool_call in getattr(msg, "tool_calls", []) or []:
                    if tool_call.get("name") in tool_names:
                        return True
            if isinstance(msg, ToolMessage) and getattr(msg, "name", None) in tool_names:
                return True
        return False

    @staticmethod
    def _looks_like_codebase_question(query: str) -> bool:
        """启发式判断是否为本地代码库问题。"""
        text = (query or "").lower()
        if re.search(r"https?://", text):
            return False

        implementation_terms = (
            "实现", "解释", "说明", "分析", "架构", "原理", "逻辑", "流程",
            "调用", "入口", "模块", "源码", "代码", "类", "方法", "函数",
            "在哪里", "在哪", "怎么", "如何",
            "implement", "implementation", "explain", "architecture",
            "flow", "logic", "code", "class", "method", "function",
            "module", "where", "how",
        )
        codebase_cues = (
            "weavemind", "这个项目", "本项目", "当前项目", "代码库", "源码",
            "里面", "本地", "repo", "repository", "codebase",
            "mcp", "mcp_client", "cli", "core", "rag", "tools",
            "agents", "skills", "web", "permissions",
        )
        has_intent = any(term in text for term in implementation_terms)
        has_codebase_cue = any(cue in text for cue in codebase_cues)
        return has_intent and has_codebase_cue

    @staticmethod
    def _looks_like_knowledge_question(query: str) -> bool:
        """启发式判断是否为用户资料/企业知识库问题。"""
        text = (query or "").lower()
        if re.search(r"https?://", text):
            return False

        # 强意图词：用户明确指向知识库/上传资料
        strong_cues = (
            "知识库", "资料", "上传", "文档", "手册", "制度", "合同",
            "规范", "政策", "条款", "报告", "附件", "pdf", "word", "docx",
            "knowledge", "uploaded", "attachment", "document", "manual",
            "policy", "contract", "report",
        )
        if any(cue in text for cue in strong_cues):
            return True

        # 弱意图词需要配合疑问词才触发
        weak_cues = (
            "文件", "简历", "这份", "这篇", "里面说", "文中", "材料", "图片",
            "file", "image",
        )
        question_terms = (
            "是什么", "有哪些", "说明", "总结", "概括", "提取", "查找",
            "检索", "引用", "依据", "出处", "第几页", "怎么规定", "怎么说",
            "如何规定", "内容",
            "what", "which", "summarize", "find", "search", "cite", "how",
        )
        return any(cue in text for cue in weak_cues) and any(term in text for term in question_terms)

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

        # 固定流转边
        graph.add_edge("think", "route")
        # 条件分支边
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
        self._check_cancelled()
        messages = state["messages"]

        # 首次调用时注入系统提示（每次重新构建，包含最新记忆和检索结果）
        if self.memory:
            # 注入 Skill 索引到 system prompt
            if self.skill_registry:
                from skills.formatter import SkillIndexFormatter
                self.memory._skill_index = SkillIndexFormatter.format(self.skill_registry.enabled_skills())
            else:
                self.memory._skill_index = ""

            # 从最新用户消息中提取查询词，用于记忆检索
            query = ""
            for m in reversed(messages):
                if isinstance(m, HumanMessage):
                    query = m.content[:100]
                    break

            # 构建 system message（含 CLAUDE.md + MEMORY.md + skill 索引）
            system_msg = self.memory.build_system_message(query)
            if system_msg:
                if messages and isinstance(messages[0], SystemMessage):
                    messages = [system_msg] + list(messages[1:])
                else:
                    messages = [system_msg] + list(messages)

        # Skill body 注入：drain buffer 并 prepend 到最新 user message
        skill_buffer = getattr(self, "skill_buffer", None)
        if skill_buffer and not skill_buffer.is_empty():
            drained = skill_buffer.drain()
            if drained:
                for i in range(len(messages) - 1, -1, -1):
                    if isinstance(messages[i], HumanMessage):
                        original = messages[i].content
                        messages[i] = HumanMessage(content=drained + "用户输入：\n" + original)
                        break

        # 自动压缩检查（在 LLM 调用前）
        if self.compactor.should_compact(messages):
            messages = self.compactor.compact(messages)

        forced_search = self._maybe_force_search_knowledge(messages)
        if forced_search:
            return {"messages": [forced_search]}

        forced_search = self._maybe_force_search_code(messages)
        if forced_search:
            return {"messages": [forced_search]}

        # 注入浏览器模式指令（强制 LLM 按工作流处理登录态问题）
        mcp_manager = getattr(self, "mcp_manager", None)
        if mcp_manager and mcp_manager._chrome_server_name:
            mode = mcp_manager.get_chrome_mode()
            browser_hint = {
                "isolated": (
                    "[浏览器操作指令] 当前为 isolated 模式（独立浏览器，无登录态）。\n"
                    "操作规则（必须遵守）：\n"
                    "1. 先用浏览器工具（new_page/navigate_page + take_snapshot）尝试访问页面\n"
                    "2. 如果返回登录页/401/403/需要登录，立即执行以下步骤：\n"
                    "   a) 关闭当前页面（close_page）\n"
                    "   b) 调用 browser_connect 切换到 shared 模式\n"
                    "   c) 切换成功后，重新用 new_page 或 navigate_page 打开同一 URL\n"
                    "   d) 获取页面内容\n"
                    "3. 如果 browser_connect 返回失败信息（提示 Chrome 未开启远程调试），\n"
                    "   则停止尝试，向用户说明需要先在 Chrome 中开启远程调试。\n"
                    "4. 不要反复询问用户，按上述步骤自动执行。"
                ),
                "shared": (
                    "[浏览器操作指令] 当前为 shared 模式（连接用户 Chrome，有登录态）。\n"
                    "可以直接访问需要登录的页面。如果仍遇到权限问题，可能是用户未在该网站登录。\n\n"
                    "内容抓取策略（必须遵守）：\n"
                    "1. 搜索结果页：用 evaluate_script 提取帖子标题和链接列表，不要只看快照\n"
                    "2. 深入阅读：从搜索结果中选择 3-5 个最相关的帖子，逐个点击进入提取正文内容\n"
                    "3. 质量判断：进入帖子后用 evaluate_script 提取正文文本，如果内容太短（<100字）\n"
                    "   或与用户需求不相关，返回搜索结果页（navigate_page 回退）尝试下一个帖子\n"
                    "4. 内容提取：优先用 evaluate_script 提取文本，而非 take_snapshot/take_screenshot\n"
                    "5. 完成条件：成功提取 2-3 个有实质内容的帖子后，立即总结回复用户\n\n"
                    "禁止行为：\n"
                    "- 不要反复滚动加载更多搜索结果\n"
                    "- 不要对同一页面重复截图/快照\n"
                    "- 总共最多访问 5 个帖子详情页，之后必须根据已有内容总结"
                ),
            }.get(mode, "")
            if browser_hint:
                # 避免重复注入（检查最近 3 条消息中是否已有浏览器指令）
                has_hint = any(
                    "[浏览器操作指令]" in str(m.content)
                    for m in messages[-3:]
                    if hasattr(m, "content") and m.content
                )
                if not has_hint:
                    messages.append(SystemMessage(content=browser_hint))

        self._model_call_count += 1
        call_index = self._model_call_count
        start_at = time.perf_counter()

        if self.hook_manager:
            self.hook_manager.emit("LLMStart", {
                "call_index": call_index,
            })

        # MiMo reasoning_content 回传：将之前保存的 reasoning_content 注入回消息
        messages = self._inject_reasoning_content(list(messages))

        full_chunk = None
        stream_failed = False
        emitted_any_delta = False

        # MiMo 模型禁用了 streaming（_stream 为空生成器），直接走 invoke 避免无效尝试
        from core.llm_factory import MiMoChatOpenAI
        skip_stream = isinstance(getattr(self, "llm", None), MiMoChatOpenAI)

        if not skip_stream:
            try:
                for chunk in self.llm_with_tools.stream(messages):
                    self._check_cancelled()
                    full_chunk = chunk if full_chunk is None else full_chunk + chunk
                    delta_text = self._extract_text_content(getattr(chunk, "content", ""))
                    if delta_text and self.hook_manager:
                        emitted_any_delta = True
                        # 实时 emit 给前端渲染
                        self.hook_manager.emit("LLMDelta", {
                            "call_index": call_index,
                            "delta": delta_text,
                        })
            except AgentCancelledError:
                raise
            except Exception as e:
                stream_failed = True
                logger.debug(f"LLM stream 失败，回退 invoke: {e}")

        if full_chunk is None or stream_failed:
            self._check_cancelled()
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

        self._check_cancelled()

        # MiMo reasoning_content 捕获：保存 thinking 内容以便后续回传
        self._preserve_reasoning_content(response)

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
                    # 保留重试响应的 reasoning_content（如有）
                    retry_kwargs = {}
                    retry_rc = getattr(retry_response, "reasoning_content", None)
                    if retry_rc is None and hasattr(retry_response, "additional_kwargs"):
                        retry_rc = retry_response.additional_kwargs.get("reasoning_content")
                    if retry_rc:
                        retry_kwargs["reasoning_content"] = retry_rc
                    response = AIMessage(content=retry_text, additional_kwargs=retry_kwargs)
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
        # 如果是 Plan 模式，并且 state["plan"] 还没有生成 → 继续走 plan
        # 如果 state["plan"] 已经存在 → 说明计划已经生成/执行过 → 结束
        if self.force_plan_mode:
            return "continue" if state.get("plan") is None else "end"
        # 硬性迭代上限
        if self._model_call_count >= MAX_ITERATIONS:
            logger.warning("达到最大迭代次数 %d，强制结束", MAX_ITERATIONS)
            return "end"
        # 浏览器工具重复检测（防止无限滚动/截图循环）
        if self._detect_browser_loop(state["messages"]):
            return "end"
        # 普通模式：有 tool_calls 则继续
        if isinstance(last_message, AIMessage) and last_message.tool_calls:
            return "continue"
        return "end"

    def _plan_or_react(self, state: AgentState) -> dict:
        """路由决策：走 ReAct 还是 Plan-Execute。

        策略：只有 force_plan_mode 时走 Plan，其他都走 ReAct。
        """
        if self.force_plan_mode:
            return {"plan": None}
        return {}

    def _choose_path(self, state: AgentState) -> str:
        """选择执行路径。

        策略：
        - force_plan_mode → plan（用户显式请求）
        - 其他 → react（逐步执行，稳定可靠）

        不再根据 tool_calls 数量判断，因为：
        1. LLM 产生 tool_calls 数量不稳定
        2. 多个 tool_calls 可能有依赖关系，应逐步执行
        3. Plan/Multi-Agent 对简单任务太重
        """
        if self.force_plan_mode:
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

    @staticmethod
    def _format_recent_chat_history(messages: list, max_messages: int = 8) -> str:
        """Format recent human/assistant text for contextual RAG query rewrite."""
        lines = []
        for msg in messages:
            if isinstance(msg, (ToolMessage, SystemMessage)):
                continue
            content = getattr(msg, "content", "")
            if not content or not isinstance(content, str):
                continue
            if isinstance(msg, HumanMessage):
                role = "user"
            elif isinstance(msg, AIMessage):
                role = "assistant"
            else:
                role = msg.__class__.__name__
            lines.append(f"{role}: {content.replace(chr(10), ' ')[:500]}")
        return "\n".join(lines[-max_messages:])

    def _act(self, state: AgentState) -> dict:
        """执行工具调用。"""
        self._check_cancelled()
        last_message = state["messages"][-1]
        if not isinstance(last_message, AIMessage):
            return {}

        from langchain_core.messages import ToolMessage
        tool_messages = []
        for tool_call in last_message.tool_calls:
            self._check_cancelled()
            tool_name = tool_call["name"]
            tool_args = tool_call["args"]

            # Give SearchCode enough context to rewrite follow-up questions like
            # "那它在哪里调用" into a code-searchable query. Existing callers can
            # still pass chat_history explicitly; this only fills the blank.
            if tool_name == "SearchCode" and isinstance(tool_args, dict) and not tool_args.get("chat_history"):
                history = self._format_recent_chat_history(state["messages"][:-1])
                if history:
                    tool_args = dict(tool_args)
                    tool_args["chat_history"] = history

            # Same contextual help for knowledge RAG tools.
            if tool_name in ("SearchKnowledge", "AskKnowledge") and isinstance(tool_args, dict) and not tool_args.get("chat_history"):
                history = self._format_recent_chat_history(state["messages"][:-1])
                if history:
                    tool_args = dict(tool_args)
                    tool_args["chat_history"] = history

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
                    self._check_cancelled()
                    result = tool.invoke(tool_args)
                    self._check_cancelled()
                    self._tool_failure_counts[tool_name] = 0
                except AgentCancelledError:
                    raise
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

            # browser_connect/browser_disconnect 执行后刷新工具列表
            if not had_error and tool_name in ("browser_connect", "browser_disconnect") and self.mcp_manager:
                self._refresh_tools_after_browser_switch()

            from langchain_core.messages import ToolMessage
            tool_messages.append(ToolMessage(
                content=str(result),
                tool_call_id=tool_call["id"],
                name=tool_name,
            ))

        return {"messages": tool_messages}

    def _refresh_tools_after_browser_switch(self):
        """browser_connect/browser_disconnect 执行后刷新 AgentLoop 的工具列表。

        MCPManager 重启 MCP Server 后，ToolRegistry 中的 Chrome 工具已更新，
        此方法重建 LLM 绑定的工具列表，使下一轮 think 能使用新工具。
        """
        try:
            self.tools = self._filter_available_tools(
                self.tool_registry.get_langchain_tools()
            )
            self.llm_with_tools = self.llm.bind_tools(self.tools)
            self._tool_map = {t.name: t for t in self.tools}
            logger.info("浏览器模式切换后工具列表已刷新，当前 %d 个工具可用", len(self.tools))
        except Exception as e:
            logger.error("刷新工具列表失败: %s", e)

    def _plan(self, state: AgentState) -> dict:
        """生成执行计划。

        如果 LLM 已给出 tool_calls，直接用它们构建计划，避免 Planner 重新生成时丢失参数。
        """
        self._check_cancelled()
        user_messages = [
            m for m in state["messages"] if isinstance(m, HumanMessage)
        ]
        goal = user_messages[-1].content if user_messages else ""

        if self.hook_manager:
            self.hook_manager.emit("PlanStart", {
                "goal": goal,
            })

        last_message = state["messages"][-1]
        if isinstance(last_message, AIMessage) and last_message.tool_calls:
            # LLM 已给出完整 tool_calls → 直接构建计划，不重新生成
            plan = self._build_plan_from_tool_calls(goal, last_message.tool_calls)
            logger.info(f"从 tool_calls 直接构建计划: {plan.id}, 共 {len(plan.tasks)} 个任务")
        else:
            # 无 tool_calls → 调用 Planner 生成
            try:
                plan = self.planner.create_plan(goal)
            except Exception as e:
                logger.error(f"计划生成失败: {e}")
                if self.hook_manager:
                    self.hook_manager.emit("PlanError", {"error": str(e)})
                return {
                    "messages": [AIMessage(content=f"计划生成失败: {e}")],
                    "plan": None,
                }

        plan_dict = plan.model_dump()
        if self.hook_manager:
            self.hook_manager.emit("PlanCreated", {
                "plan_id": plan.id,
                "task_count": len(plan.tasks),
            })
        logger.info(f"计划生成完成: {plan.id}, 共 {len(plan.tasks)} 个任务")
        return {"plan": plan_dict}

    def _build_plan_from_tool_calls(self, goal: str, tool_calls: list) -> Plan:
        """将 LLM 产生的 tool_calls 直接转换为 Plan，保留原始参数。"""
        from core.plan_models import Task
        tasks = []
        for i, tc in enumerate(tool_calls, 1):
            tasks.append(Task(
                id=f"task_{i}",
                description=f"执行 {tc['name']}({tc['args']})",
                tool_name=tc["name"],
                tool_args=tc["args"],
                dependencies=[],
            ))
        return Plan(goal=goal, tasks=tasks)

    def _execute_plan(self, state: AgentState) -> dict:
        """执行已生成的计划。"""
        self._check_cancelled()
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
        self._check_cancelled()
        initial_state = {
            "messages": [HumanMessage(content=user_input)],
            "plan": None,
        }

        config = {"recursion_limit": 100}
        for event in self.graph.stream(initial_state, config=config):
            self._check_cancelled()
            yield event

    def stream_with_history(self, conversation: list):
        """流式执行 Agent 循环，传入完整对话历史。"""
        self._reset_runtime_state()
        self._check_cancelled()

        # 压缩检查（对话历史过长时自动压缩）
        if self.compactor.should_compact(conversation):
            conversation = self.compactor.compact(conversation)

        initial_state = {
            "messages": list(conversation),
            "plan": None,
        }

        config = {"recursion_limit": 100}
        for event in self.graph.stream(initial_state, config=config):
            self._check_cancelled()
            yield event

    def invoke(self, user_input: str) -> dict:
        """同步执行 Agent 循环，返回最终状态。"""
        self._reset_runtime_state()
        self._check_cancelled()
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
