"""PromptAssembler — 按模式组装完整 system prompt。

组装顺序：base + personality + mode + 动态上下文(memory/skill) + context-management + handoff
"""
from enum import Enum
from typing import Optional

from core.prompt_repository import PromptRepository


class PromptMode(Enum):
    """Agent 运行模式，对应 prompts/modes/ 下的文件。"""
    AGENT = "modes/agent.md"
    PLANNER = "modes/planner.md"
    PLAN_WORKER = "modes/plan_worker.md"
    TEAM_PLANNER = "modes/team_planner.md"
    TEAM_WORKER = "modes/team_worker.md"
    TEAM_REVIEWER = "modes/team_reviewer.md"


class PromptAssembler:
    """从 PromptRepository 加载片段并组装为完整 system prompt。"""

    def __init__(self, repository: Optional[PromptRepository] = None):
        self.repository = repository or PromptRepository.create_default()

    def assemble(
        self,
        mode: PromptMode = PromptMode.AGENT,
        memory_context: Optional[str] = None,
        skill_index: Optional[str] = None,
        variables: Optional[dict] = None,
    ) -> str:
        """组装完整 system prompt。

        Args:
            mode: 当前运行模式
            memory_context: 动态记忆上下文（CLAUDE.md + MEMORY.md + CoreMemory + 相关事实）
            skill_index: Skill 索引段落
            variables: 模板变量（如 {{taskDescription}}）
        """
        parts = []

        # 1. 动态记忆上下文（放最前面，优先级最高）
        if memory_context:
            parts.append(memory_context)

        # 2. base.md（Identity + Language + Tools + Tool Policy + Browser Policy + Safety）
        parts.append(self.repository.load_required("base.md"))

        # 3. personality.md
        personality = self.repository.load("personality.md")
        if personality:
            parts.append(personality)

        # 4. 模式提示词
        mode_content = self.repository.load_required(mode.value)
        if variables:
            for key, value in variables.items():
                mode_content = mode_content.replace("{{" + key + "}}", value)
        parts.append(mode_content)

        # 5. Skill 索引
        if skill_index:
            parts.append(skill_index)

        # 6. context-management.md
        ctx = self.repository.load("context.md")
        if ctx:
            parts.append(ctx)

        # 7. handoff.md
        handoff = self.repository.load("handoff.md")
        if handoff:
            parts.append(handoff)

        return "\n\n".join(p for p in parts if p and p.strip())
