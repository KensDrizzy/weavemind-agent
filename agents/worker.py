"""Worker Agent — 基于 create_react_agent 的完整 ReAct 循环。

每个 Worker 是一个独立的 ReAct Agent，可以多步推理和多次工具调用。
Worker 完成后通过 Command 回到 Supervisor。
"""

import logging
from typing import Literal

from langchain_core.messages import HumanMessage
from langgraph.types import Command

# LangGraph V1 起，create_react_agent 迁移到 langchain.agents.create_agent，
# 参数 prompt → system_prompt。这里做版本兼容：优先用新 API，回退到老 API。
try:
    from langchain.agents import create_agent as _create_agent  # type: ignore

    def _make_react_agent(llm, tools, system_prompt):
        return _create_agent(llm, tools, system_prompt=system_prompt)

    _USING_NEW_AGENT_API = True
except ImportError:  # pragma: no cover - 老版本环境的回退路径
    from langgraph.prebuilt import create_react_agent as _legacy_create  # type: ignore

    def _make_react_agent(llm, tools, system_prompt):
        return _legacy_create(llm, tools=tools, prompt=system_prompt)

    _USING_NEW_AGENT_API = False

from agents.agent_state import MultiAgentState

logger = logging.getLogger(__name__)

WORKER_SYSTEM_PROMPT = (
    "你是一个任务执行专家。根据给定的任务步骤，调用工具完成具体操作。\n\n"
    "可用工具：Read, Write, Edit, Bash, Glob, Grep, WebFetch, WebSearch, "
    "MemoryAdd, MemorySearch, CoreMemoryEdit, AskUser\n\n"
    "规则：\n"
    "- 涉及代码理解时优先使用 Grep/Glob\n"
    "- 每步只做一件事\n"
    "- 完成后简要报告结果\n"
    "- 不要添加不必要的背景说明"
)


def create_worker_node(
    llm,
    tool_registry,
    permission_policy=None,
    hook_manager=None,
    name: str = "worker",
    system_prompt: str = None,
    tool_names: list[str] = None,
):
    """创建一个 Worker Agent 节点函数。

    Args:
        llm: LangChain LLM 实例
        tool_registry: ToolRegistry 实例，提供可用工具
        permission_policy: PermissionPolicy 实例（可选）
        hook_manager: HookManager 实例（可选）
        name: Worker 名称，用于消息标识
        system_prompt: 自定义系统提示词（可选）
        tool_names: 限制可用工具列表（可选，None 表示全部工具）

    Returns:
        worker_node: 可添加到 StateGraph 的节点函数
    """
    prompt = system_prompt or WORKER_SYSTEM_PROMPT

    # 从 ToolRegistry 获取工具
    if tool_names:
        # 只加载指定工具
        tools = []
        for tn in tool_names:
            tool = tool_registry.get(tn)
            if tool:
                tools.append(tool)
            else:
                logger.warning(f"Worker {name}: 工具 {tn} 不存在，跳过")
    else:
        # 加载全部工具
        tools = tool_registry.get_langchain_tools()

    # 用 LangChain create_agent（V1 推荐）/ langgraph.prebuilt（旧版本兜底）创建完整 ReAct Agent
    agent = _make_react_agent(llm, tools, system_prompt=prompt)
    logger.debug(f"Worker {name}: 使用 {'langchain.agents.create_agent' if _USING_NEW_AGENT_API else 'langgraph.prebuilt.create_react_agent'}")

    def worker_node(state: MultiAgentState) -> Command[Literal["supervisor"]]:
        """Worker 执行节点：调用 ReAct Agent 处理任务。"""
        logger.info(f"Worker {name} 开始执行任务")

        result = agent.invoke({"messages": state["messages"]})
        last_msg = result["messages"][-1].content

        logger.info(f"Worker {name} 执行完成: {last_msg[:100]}")

        # 更新 step_results
        existing_results = state.get("step_results", {})
        updated_results = {**existing_results, name: last_msg}

        return Command(
            update={
                "messages": [HumanMessage(content=last_msg, name=name)],
                "step_results": updated_results,
            },
            goto="supervisor",
        )

    return worker_node