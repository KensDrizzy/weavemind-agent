"""Build a UI-free, remote-safe WeaveMind Agent runtime for WeChat."""

from __future__ import annotations

from pathlib import Path

import settings
from channels.wechat.safety import (
    DEFAULT_SAFE_TOOLS,
    FilteredToolRegistry,
    ScopedGlobTool,
    ScopedGrepTool,
    ScopedReadTool,
)
from core.agent_loop import AgentLoop
from core.agent_session import AgentSession
from core.cancellation import CancellationToken
from core.memory import MemoryManager
from hooks.manager import HookManager
from permissions.policy import PermissionPolicy
from tools.registry import ToolRegistry


def create_wechat_agent_session(workspace: str | Path) -> AgentSession:
    """Create a WeChat session without terminal input or dangerous tools."""
    workspace_path = Path(workspace).expanduser().resolve()
    cancellation_token = CancellationToken()
    memory = MemoryManager()
    hook_manager = HookManager()

    rag_pipeline = None
    if settings.get("rag.enabled", False):
        try:
            from rag.pipeline import CodeRAGPipeline

            rag_pipeline = CodeRAGPipeline()
        except Exception:
            rag_pipeline = None

    base_registry = ToolRegistry(
        memory_manager=memory,
        rag_pipeline=rag_pipeline,
        mcp_manager=None,
    )
    base_registry.register(ScopedReadTool(workspace=str(workspace_path)))
    base_registry.register(ScopedGlobTool(workspace=str(workspace_path)))
    base_registry.register(ScopedGrepTool(workspace=str(workspace_path)))

    skill_registry = None
    skill_buffer = None
    try:
        from skills.buffer import SkillContextBuffer
        from skills.registry import SkillRegistry
        from skills.state_store import SkillStateStore
        from tools.builtin.skill_tools import LoadSkillTool

        project_root = Path(__file__).resolve().parents[2]
        builtin_dir = project_root / "skills" / "builtin"
        user_dir = Path.home() / ".weavemind" / "skills"
        project_dir = workspace_path / ".weavemind" / "skills"
        state_file = Path.home() / ".weavemind" / "skills.json"
        skill_registry = SkillRegistry(
            builtin_dir,
            user_dir,
            project_dir,
            SkillStateStore(state_file),
        )
        skill_registry.reload()
        skill_buffer = SkillContextBuffer()
        base_registry.register(
            LoadSkillTool(
                skill_registry=skill_registry,
                skill_buffer=skill_buffer,
            )
        )
    except Exception:
        skill_registry = None
        skill_buffer = None

    configured_safe = settings.get(
        "wechat.security.allowed_tools",
        list(DEFAULT_SAFE_TOOLS),
    )
    allowed_tools = frozenset(
        name for name in configured_safe if name in DEFAULT_SAFE_TOOLS
    )
    registry = FilteredToolRegistry(base_registry, allowed_tools)
    permission_policy = PermissionPolicy(allowed=list(allowed_tools))

    agent_loop = AgentLoop(
        tool_registry=registry,
        permission_policy=permission_policy,
        hook_manager=hook_manager,
        memory=memory,
        skill_registry=skill_registry,
        skill_buffer=skill_buffer,
        cancellation_token=cancellation_token,
    )
    return AgentSession(
        agent_loop,
        cancellation_token=cancellation_token,
        max_messages=int(settings.get("wechat.max_conversation_messages", 40)),
    )
