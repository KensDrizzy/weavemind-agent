"""Multi-Agent 编排器 — 基于 LangGraph Supervisor 模式。

架构：
    Supervisor（LLM 路由）→ Planner / Worker-N / Reviewer → 回到 Supervisor → 循环

流程：
    1. Supervisor 根据当前状态决定路由到哪个 Agent
    2. Agent 执行后通过 Command 回到 Supervisor
    3. Supervisor 再次决策，直到输出 FINISH
"""

import json
import logging
import re
from typing import Literal, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import StateGraph, START, END
from langgraph.types import Command
from pydantic import BaseModel, Field

from agents.agent_state import MultiAgentState
from agents.worker import create_worker_node
from agents.reviewer import create_reviewer_node

logger = logging.getLogger(__name__)

MAX_SUPERVISOR_ROUNDS = 20

SUPERVISOR_SYSTEM_PROMPT = (
    "你是一个任务编排者，管理以下 Agent 团队：\n\n"
    "- planner: 分析任务，制定执行计划（只读，不执行操作）\n"
    "- worker-1 / worker-2: 执行具体操作（读写文件、运行命令等）\n"
    "- reviewer: 审查执行结果的质量\n\n"
    "根据用户请求和当前进度，决定下一步由哪个 Agent 执行。\n\n"
    "决策规则（严格遵守）：\n"
    "1. 新任务先交给 planner 制定计划\n"
    "2. planner 已输出规划后，必须交给 worker-1 执行（不要重复交给 planner）\n"
    "3. worker 执行后交给 reviewer 审查\n"
    "4. 审查不通过时重新交给 worker 修正\n"
    "5. 审查通过或所有任务完成时回复 FINISH\n\n"
    "【重要】判断 planner 是否已完成规划：\n"
    "- 如果消息列表中已有 name=planner 的消息，说明规划已完成，下一步必须是 worker-1\n"
    "- 不要重复将任务交给 planner\n\n"
    "【重要】你的回复格式：\n"
    "第一行必须是以下之一，不要加任何前缀、标点或解释：\n"
    "planner\n"
    "worker-1\n"
    "worker-2\n"
    "reviewer\n"
    "FINISH\n\n"
    "示例回复：\n"
    "planner\n"
    "worker-1\n"
    "FINISH"
)

PLANNER_SYSTEM_PROMPT = (
    "你是一个任务规划专家。分析用户需求，制定清晰的执行步骤。\n\n"
    "输出格式：\n"
    "1. [步骤1描述]\n"
    "2. [步骤2描述]\n"
    "...\n\n"
    "规则：\n"
    "- 只做规划，不执行任何操作\n"
    "- 每个步骤要具体明确\n"
    "- 标注步骤间的依赖关系\n"
    "- 简单任务拆 1-3 步，复杂任务拆 3-7 步"
)


def _build_router_model(options: list[str]):
    """根据 options 动态构造 Pydantic Router，约束 LLM 只能返回这些值。

    对标 LangGraph 官方 supervisor 教程的结构化输出做法：
    使用 Literal 把可选路由烧到 schema 里，让 provider 端的 JSON schema 校验
    或函数调用约束帮我们把"幻觉路由"拒在外面。
    """
    literal_type = Literal[tuple(options)]  # type: ignore[valid-type]

    class _Router(BaseModel):
        """Supervisor 路由决策。"""
        next: literal_type = Field(  # type: ignore[valid-type]
            description="下一个执行的成员，或 FINISH 表示任务完成"
        )

    return _Router


def make_supervisor_node(llm, members: list[str]):
    """创建 Supervisor 节点：LLM 路由决策。

    策略（四层回退，优先级从高到低）：
    1. 硬性规则：基于已工作 Agent 直接路由（最稳，无 LLM）
    2. 结构化输出：llm.with_structured_output(Router) — 对标 LangGraph 官方做法
    3. JSON 提取：从 LLM 文本中提取 JSON 对象，兼容多种字段名
    4. 纯文本关键词匹配 → 仍失败则 FINISH

    Args:
        llm: LangChain LLM 实例
        members: 可路由的 Agent 名称列表

    Returns:
        supervisor_node: 可添加到 StateGraph 的节点函数
    """
    options = ["FINISH"] + members

    # 预构造结构化输出版本的 LLM；个别 provider 不支持时在调用处兜底
    try:
        _router_model = _build_router_model(options)
        _structured_llm = llm.with_structured_output(_router_model)
    except Exception as e:  # pragma: no cover - 仅 provider 不支持时触发
        logger.debug(f"Supervisor 结构化路由不可用，回退文本解析: {e}")
        _structured_llm = None

    system_prompt = SUPERVISOR_SYSTEM_PROMPT

    # LLM 可能输出的字段名列表（兼容不同模型习惯）
    ROUTE_FIELD_NAMES = ("next", "agent", "next_agent", "action", "target", "goto", "route")

    def _extract_route_from_json(text: str) -> Optional[str]:
        """从 LLM 回复中提取 JSON 并查找路由字段。

        兼容多种字段名：next / agent / next_agent / action / target / goto / route
        也兼容 {'approved': true} 这种不合法的格式。
        """
        # 尝试提取 JSON 对象
        start = text.find("{")
        end = text.rfind("}") + 1
        if start < 0 or end <= start:
            return None

        try:
            data = json.loads(text[start:end])
        except (json.JSONDecodeError, ValueError):
            return None

        if not isinstance(data, dict):
            return None

        # 按优先级查找路由字段
        for field in ROUTE_FIELD_NAMES:
            val = data.get(field, "")
            if isinstance(val, str) and val.strip():
                candidate = val.strip()
                # 检查是否在合法选项中
                for opt in options:
                    if candidate.lower() == opt.lower():
                        return opt
                # 部分匹配
                for opt in options:
                    if opt.lower() in candidate.lower():
                        return opt

        return None

    def _extract_route_from_text(text: str) -> str:
        """从 LLM 纯文本回复中提取路由目标。"""
        text = text.strip()

        # 精确匹配优先（整行匹配）
        first_line = text.split("\n")[0].strip().rstrip("。，,.！!？?")
        for opt in options:
            if first_line == opt:
                return opt

        # 包含匹配
        text_lower = text.lower()
        # 优先匹配更具体的选项（worker-2 优先于 worker-1）
        for opt in sorted(options, key=lambda x: -len(x)):
            if opt.lower() in text_lower:
                return opt

        return "FINISH"

    def _extract_route(text: str) -> str:
        """综合提取路由目标：JSON 优先，文本回退。"""
        # 1. 尝试 JSON 提取
        route = _extract_route_from_json(text)
        if route:
            return route

        # 2. 文本关键词匹配
        return _extract_route_from_text(text)

    def supervisor_node(state: MultiAgentState) -> Command:
        """Supervisor 路由节点。"""
        # 构建带进度信息的 prompt
        progress_info = ""
        step_results = state.get("step_results", {})
        review_status = state.get("review_status")
        if step_results:
            progress_info += "\n\n已完成步骤：\n"
            for name, result in step_results.items():
                preview = result[:100] + "..." if len(result) > 100 else result
                progress_info += f"- {name}: {preview}\n"
        if review_status:
            progress_info += f"\n审查状态: {review_status}\n"
        if state.get("retry_count", 0) > 0:
            progress_info += f"当前重试次数: {state['retry_count']}\n"

        # 检查哪些 Agent 已经工作过，添加明确提示
        worked_agents = set()
        for msg in state["messages"]:
            msg_name = getattr(msg, "name", None)
            if msg_name and isinstance(msg_name, str):
                worked_agents.add(msg_name)
        if worked_agents:
            progress_info += f"\n已工作的 Agent: {', '.join(sorted(worked_agents))}\n"
            if "planner" in worked_agents and "worker-1" not in worked_agents:
                progress_info += "→ planner 已完成规划，下一步必须交给 worker-1\n"
            if "worker-1" in worked_agents and "reviewer" not in worked_agents:
                progress_info += "→ worker-1 已执行，下一步必须交给 reviewer\n"
            if "reviewer" in worked_agents and review_status == "approved":
                progress_info += "→ 审查已通过，可以回复 FINISH\n"

        full_prompt = system_prompt + progress_info
        messages = [SystemMessage(content=full_prompt)] + state["messages"]

        # 硬性规则：根据已工作 Agent 状态，跳过 LLM 决策直接路由
        # 这避免 LLM 重复路由到已完成的 Agent
        force_route = None
        if "planner" in worked_agents and "worker-1" not in worked_agents:
            force_route = "worker-1"
        elif "worker-1" in worked_agents and "reviewer" not in worked_agents and review_status is None:
            force_route = "reviewer"
        elif review_status == "approved" and "reviewer" in worked_agents:
            force_route = "FINISH"

        if force_route:
            goto = force_route
            logger.info(f"Supervisor 硬性路由: {goto}（基于已工作 Agent 状态）")
        else:
            goto = None
            # ① 优先走结构化输出（对标 LangGraph 官方 supervisor 教程的稳定路径）
            if _structured_llm is not None:
                try:
                    decision = _structured_llm.invoke(messages)
                    candidate = getattr(decision, "next", None)
                    if isinstance(candidate, str) and candidate in options:
                        goto = candidate
                        logger.info(f"Supervisor 结构化路由: {goto}")
                except Exception as e:
                    logger.debug(f"Supervisor 结构化输出失败，退回文本解析: {e}")

            # ② 兜底：保留原有 JSON + 关键词解析路径，确保对不支持 schema 的 provider 仍可用
            if goto is None:
                try:
                    response = llm.invoke(messages)
                    response_text = response.content or ""
                    goto = _extract_route(response_text)
                    logger.info(f"Supervisor 文本路由: {response_text[:150]}, 提取: {goto}")
                except Exception as e:
                    logger.error(f"Supervisor LLM 调用失败: {e}")
                    goto = "FINISH"

        if goto == "FINISH":
            return Command(goto=END, update={"next": "__end__"})

        # 校验 goto 是否在合法列表内
        if goto not in members:
            logger.warning(f"Supervisor 路由目标非法: {goto}，回退到 FINISH")
            return Command(goto=END, update={"next": "__end__"})

        logger.info(f"Supervisor 路由到: {goto}")
        return Command(goto=goto, update={"next": goto})

    return supervisor_node


class MultiAgentOrchestrator:
    """Multi-Agent 编排器。

    基于 LangGraph Supervisor 模式，管理 Planner/Worker/Reviewer 三角色协作。
    """

    def __init__(
        self,
        llm,
        tool_registry,
        permission_policy=None,
        hook_manager=None,
        memory=None,
        num_workers: int = 2,
    ):
        self.llm = llm
        self.tool_registry = tool_registry
        self.permission_policy = permission_policy
        self.hook_manager = hook_manager
        self.memory = memory
        self.num_workers = num_workers
        self.graph = self._build_graph()

    def _build_graph(self) -> StateGraph:
        """构建 Multi-Agent StateGraph。"""
        # 构建成员列表
        members = ["planner"]
        for i in range(self.num_workers):
            members.append(f"worker-{i + 1}")
        members.append("reviewer")

        # 最终 members = ["planner", "worker-1", "worker-2", "reviewer"]

        builder = StateGraph(MultiAgentState)

        # 1. Supervisor 节点
        supervisor_node = make_supervisor_node(self.llm, members)
        builder.add_node("supervisor", supervisor_node)

        # 2. Planner 节点（只读工具）
        builder.add_node("planner", self._make_planner_node())

        # 3. Worker 节点（全部工具，完整 ReAct）
        for i in range(self.num_workers):
            worker_name = f"worker-{i + 1}"
            worker_node = create_worker_node(
                llm=self.llm,
                tool_registry=self.tool_registry,
                permission_policy=self.permission_policy,
                hook_manager=self.hook_manager,
                name=worker_name,
            )
            builder.add_node(worker_name, worker_node)

        # 4. Reviewer 节点
        reviewer_node = create_reviewer_node(self.llm)
        builder.add_node("reviewer", reviewer_node)

        # 5. 入口 → Supervisor
        builder.add_edge(START, "supervisor")

        return builder.compile()

    def _make_planner_node(self):
        """创建 Planner 节点：分析任务并输出执行计划。"""

        def planner_node(state: MultiAgentState) -> Command[Literal["supervisor"]]:
            """Planner 规划节点。"""
            messages = [
                SystemMessage(content=PLANNER_SYSTEM_PROMPT),
                HumanMessage(content=state["messages"][-1].content),
            ]
            response = self.llm.invoke(messages)

            logger.info(f"Planner 输出规划: {response.content[:200]}")

            return Command(
                update={
                    "messages": [HumanMessage(content=response.content, name="planner")],
                    "current_task": response.content,
                },
                goto="supervisor",
            )

        return planner_node

    def run(self, user_input: str) -> dict:
        """执行 Multi-Agent 协作。

        Args:
            user_input: 用户的任务描述

        Returns:
            最终状态字典
        """
        initial_state = {
            "messages": [HumanMessage(content=user_input)],
            "next": "",
            "current_task": None,
            "step_results": {},
            "review_status": None,
            "retry_count": 0,
        }
        config = {"recursion_limit": MAX_SUPERVISOR_ROUNDS * 5}
        result = self.graph.invoke(initial_state, config=config)
        return result

    def stream(self, user_input: str):
        """流式执行 Multi-Agent 协作，yield 每步状态。"""
        initial_state = {
            "messages": [HumanMessage(content=user_input)],
            "next": "",
            "current_task": None,
            "step_results": {},
            "review_status": None,
            "retry_count": 0,
        }
        config = {"recursion_limit": MAX_SUPERVISOR_ROUNDS * 5}
        for event in self.graph.stream(initial_state, config=config):
            yield event
