"""SubAgentTool — 基于 ReAct 循环的隔离子 Agent 工具。"""

import logging
import uuid
from typing import Any, Callable

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import PrivateAttr

from agents.monitor import SubAgentMonitor, SubAgentStatus
from tools.base import WeaveMindTool

# LangGraph V1 起，create_react_agent 迁移到 langchain.agents.create_agent。
try:
    from langchain.agents import create_agent as _create_agent  # type: ignore

    def _make_react_agent(llm, tools, system_prompt):
        return _create_agent(llm, tools, system_prompt=system_prompt)

except ImportError:  # pragma: no cover - 老版本环境的回退路径
    from langgraph.prebuilt import create_react_agent as _legacy_create  # type: ignore

    def _make_react_agent(llm, tools, system_prompt):
        return _legacy_create(llm, tools=tools, prompt=system_prompt)


logger = logging.getLogger(__name__)

SUBAGENT_BLOCKED_TOOLS = frozenset({
    "delegate_task",
    "Task",
    "BatchDelegate",
    "AskUser",
    "MemoryAdd",
    "MemorySearch",
    "CoreMemoryEdit",
})

_APPROVED_DECISIONS = {"approve", "approved", "allow", "once", "always", True}


class SubAgentTool(WeaveMindTool):
    name: str = "Task"
    description: str = (
        "Launch a sub-agent with full ReAct loop for complex isolated tasks. "
        "Do not use for simple lookups or one-line answers. "
        "Args: description, subagent_type, prompt"
    )

    agent_defs: dict = {}
    subagent_monitor: SubAgentMonitor | None = None
    blocked_tools: frozenset[str] = SUBAGENT_BLOCKED_TOOLS
    auto_approve: bool | None = None
    _tool_registry: Any = PrivateAttr(default=None)

    def _run(self, description: str, subagent_type: str, prompt: str) -> str:
        """启动子 Agent 执行任务。"""
        subagent_id = f"{subagent_type}-{uuid.uuid4().hex[:8]}"
        monitor = self.subagent_monitor
        if monitor:
            if monitor.is_paused:
                return "[拒绝] 子 Agent 委托已暂停，未创建新任务。"
            monitor.register(subagent_id)
            monitor.heartbeat(subagent_id, SubAgentStatus.THINKING)

        status = SubAgentStatus.COMPLETED
        try:
            return run_subagent(
                agent_defs=self.agent_defs,
                tool_registry=getattr(self, "_tool_registry", None),
                subagent_type=subagent_type,
                prompt=prompt,
                monitor=monitor,
                subagent_id=subagent_id,
                blocked_tools=self.blocked_tools,
                auto_approve=self.auto_approve,
            )
        except Exception:
            status = SubAgentStatus.FAILED
            if monitor:
                monitor.heartbeat(subagent_id, SubAgentStatus.FAILED)
            raise
        finally:
            if monitor:
                monitor.heartbeat(subagent_id, status)
                monitor.unregister(subagent_id)


def run_subagent(
    agent_defs: dict,
    tool_registry,
    subagent_type: str,
    prompt: str,
    monitor: SubAgentMonitor | None = None,
    subagent_id: str | None = None,
    blocked_tools: frozenset[str] = SUBAGENT_BLOCKED_TOOLS,
    auto_approve: bool | None = None,
) -> str:
    """执行一个子 Agent，供 Task 和 BatchDelegate 复用。"""
    agent_def = agent_defs.get(subagent_type, {})
    model = agent_def.get("model", None)
    system = agent_def.get("system_prompt", f"You are a {subagent_type} agent.")
    tool_names = agent_def.get("tools", [])

    from core.llm_factory import create_llm

    if model == "inherit" or model is None:
        llm = create_llm()
    else:
        provider = _infer_provider(model)
        llm = create_llm(provider=provider, model=model)

    tools = _load_subagent_tools(
        tool_registry=tool_registry,
        tool_names=tool_names,
        blocked_tools=blocked_tools,
        auto_approve=auto_approve,
    )

    if tools:
        agent = _make_react_agent(llm, tools, system_prompt=system)
        callbacks = []
        if monitor and subagent_id:
            callbacks.append(HeartbeatCallback(monitor, subagent_id))
        config = {"callbacks": callbacks} if callbacks else None
        if monitor and subagent_id:
            monitor.heartbeat(subagent_id, SubAgentStatus.THINKING)
        if config:
            result = agent.invoke({"messages": [HumanMessage(content=prompt)]}, config=config)
        else:
            result = agent.invoke({"messages": [HumanMessage(content=prompt)]})
        if monitor and subagent_id:
            monitor.heartbeat(subagent_id, SubAgentStatus.IDLE)
        return result["messages"][-1].content

    messages = [SystemMessage(content=system), HumanMessage(content=prompt)]
    response = llm.invoke(messages)
    return response.content


def _load_subagent_tools(
    tool_registry,
    tool_names: list[str] | None,
    blocked_tools: frozenset[str] = SUBAGENT_BLOCKED_TOOLS,
    auto_approve: bool | None = None,
) -> list:
    """加载子 Agent 工具集，并强制应用黑名单和非交互审批。"""
    if not tool_registry:
        return []

    selected_tools = []
    requested = list(tool_names or [])

    if requested:
        for tool_name in requested:
            if tool_name in blocked_tools:
                logger.warning("子 Agent 拒绝加载被禁工具: %s", tool_name)
                continue
            tool = tool_registry.get(tool_name)
            if tool:
                selected_tools.append(tool)
            else:
                logger.warning("子 Agent 请求的工具不存在: %s", tool_name)
    else:
        selected_tools = [
            tool for tool in tool_registry.get_langchain_tools()
            if tool.name not in blocked_tools
        ]

    approval = _make_subagent_approval_callback(
        auto_approve=(
            _settings_get("delegation.subagent_auto_approve", False)
            if auto_approve is None
            else auto_approve
        )
    )
    return [_SubAgentApprovalTool(tool, approval) for tool in selected_tools]


def _make_subagent_approval_callback(auto_approve: bool = False) -> Callable:
    """创建不会阻塞 TUI 的子 Agent 审批回调。"""
    def callback(tool_name: str, tool_args: dict | None = None, **kwargs):
        detail = tool_args or kwargs
        if auto_approve:
            logger.warning("[子Agent审计] 自动批准危险工具: %s | %s", tool_name, detail)
            return "once"
        logger.warning("[子Agent审计] 自动拒绝危险工具: %s | %s", tool_name, detail)
        return "deny"

    return callback


class _SubAgentApprovalTool(WeaveMindTool):
    """对子 Agent 工具调用加一层非交互审批保护。"""

    name: str
    description: str
    args_schema: Any = None
    _wrapped_tool: Any = PrivateAttr()
    _approval_callback: Callable = PrivateAttr()

    def __init__(self, wrapped_tool, approval_callback: Callable):
        super().__init__(
            name=wrapped_tool.name,
            description=getattr(wrapped_tool, "description", ""),
            args_schema=getattr(wrapped_tool, "args_schema", None),
        )
        self._wrapped_tool = wrapped_tool
        self._approval_callback = approval_callback

    def _run(self, **kwargs):
        if _requires_subagent_approval(self.name, kwargs):
            decision = self._approval_callback(self.name, kwargs)
            if decision not in _APPROVED_DECISIONS:
                return f"[子Agent审批] 已拒绝执行需要审批的工具: {self.name}"
        return self._wrapped_tool._run(**kwargs)

    @property
    def args(self) -> dict:
        return getattr(self._wrapped_tool, "args", {})

    @property
    def is_single_input(self) -> bool:
        return getattr(self._wrapped_tool, "is_single_input", False)


class HeartbeatCallback(BaseCallbackHandler):
    """将 LangChain 回调转换成 SubAgentMonitor 心跳。"""

    def __init__(self, monitor: SubAgentMonitor, subagent_id: str):
        self.monitor = monitor
        self.subagent_id = subagent_id

    def on_llm_start(self, *args, **kwargs):
        self.monitor.heartbeat(self.subagent_id, SubAgentStatus.THINKING)

    def on_llm_end(self, *args, **kwargs):
        self.monitor.heartbeat(self.subagent_id, SubAgentStatus.IDLE)

    def on_tool_start(self, serialized, input_str, **kwargs):
        tool_name = ""
        if isinstance(serialized, dict):
            tool_name = serialized.get("name", "")
        self.monitor.heartbeat(self.subagent_id, SubAgentStatus.IN_TOOL, tool=tool_name)

    def on_tool_end(self, *args, **kwargs):
        self.monitor.heartbeat(self.subagent_id, SubAgentStatus.IDLE)

    def on_tool_error(self, *args, **kwargs):
        self.monitor.heartbeat(self.subagent_id, SubAgentStatus.IDLE)


def _settings_get(key: str, default=None):
    try:
        import settings

        return settings.get(key, default)
    except Exception:
        return default


def _requires_subagent_approval(tool_name: str, tool_args: dict | None = None) -> bool:
    """Lightweight approval check that avoids importing the full AgentLoop."""
    from permissions.modes import DANGEROUS_TOOLS, EDIT_TOOLS

    if tool_name in EDIT_TOOLS:
        return True

    if tool_name in DANGEROUS_TOOLS:
        command = (tool_args or {}).get("command", "")
        if tool_name == "Bash" and _is_safe_bash_command(command):
            return False
        return True

    lowered = tool_name.lower()
    safe_keywords = ("read", "get", "list", "search", "find", "query", "fetch", "status")
    risky_keywords = (
        "delete", "remove", "drop", "destroy", "execute", "exec", "shell", "bash",
        "write", "create", "update", "modify", "edit", "save", "upload", "insert",
        "add", "append", "replace", "move", "rename", "install", "deploy", "push",
    )
    if any(keyword in lowered for keyword in safe_keywords):
        return False
    return any(keyword in lowered for keyword in risky_keywords)


def _is_safe_bash_command(command: str) -> bool:
    command = command.strip()
    safe_prefixes = (
        "ls", "dir", "cat", "head", "tail", "pwd", "whoami", "hostname", "uname",
        "date", "echo", "env", "which", "find", "grep", "rg", "python -c print",
        "node -e", "pip list", "pip show", "pip freeze", "npm list", "npm view",
        "git status", "git log", "git diff", "git branch",
    )
    return any(command == prefix or command.startswith(prefix + " ") for prefix in safe_prefixes)


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
