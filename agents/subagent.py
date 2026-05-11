"""SubAgentTool — 基于 create_react_agent 的子 Agent 工具。

重写自原有的单次 LLM 调用版本，现在支持完整的 ReAct 循环。
"""

from tools.base import WeaveMindTool
from langgraph.prebuilt import create_react_agent
from langchain_core.messages import HumanMessage, SystemMessage
import settings


class SubAgentTool(WeaveMindTool):
    name: str = "Task"
    description: str = (
        "Launch a sub-agent with full ReAct loop for complex isolated tasks. "
        "Do not use for simple lookups or one-line answers. "
        "Args: description, subagent_type, prompt"
    )

    agent_defs: dict = {}

    def _run(self, description: str, subagent_type: str, prompt: str) -> str:
        """启动子 Agent 执行任务。"""
        agent_def = self.agent_defs.get(subagent_type, {})
        model = agent_def.get("model", None)
        system = agent_def.get("system_prompt", f"You are a {subagent_type} agent.")
        tool_names = agent_def.get("tools", [])

        from core.llm_factory import create_llm

        if model == "inherit" or model is None:
            llm = create_llm()
        else:
            provider = _infer_provider(model)
            llm = create_llm(provider=provider, model=model)

        # 获取可用工具
        tools = []
        if tool_names and hasattr(self, '_tool_registry') and self._tool_registry:
            for tn in tool_names:
                tool = self._tool_registry.get(tn)
                if tool:
                    tools.append(tool)

        if tools:
            # 有工具时：使用 create_react_agent 启用完整 ReAct 循环
            agent = create_react_agent(llm, tools=tools, prompt=system)
            result = agent.invoke({"messages": [HumanMessage(content=prompt)]})
            return result["messages"][-1].content
        else:
            # 无工具时：回退到简单 LLM 调用
            messages = [SystemMessage(content=system), HumanMessage(content=prompt)]
            response = llm.invoke(messages)
            return response.content


def _infer_provider(model_name: str) -> str:
    """根据模型名推断 provider。"""
    model_lower = model_name.lower()
    if "deepseek" in model_lower:
        return "deepseek"
    if any(k in model_lower for k in ("claude", "anthropic")):
        return "anthropic"
    if any(k in model_lower for k in ("gpt", "o1", "o3", "o4")):
        return "openai"
    if "mimo" in model_lower:
        return "mimo"
    return "openai"
