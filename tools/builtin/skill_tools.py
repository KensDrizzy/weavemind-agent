"""load_skill 工具 — LLM 按需加载 Skill 完整指引。"""
from typing import Type

from pydantic import BaseModel, Field

from tools.base import WeaveMindTool

MAX_BODY_SIZE = 5 * 1024  # 5KB


class LoadSkillInput(BaseModel):
    name: str = Field(description="要加载的 Skill 名称（如 'web-access'）")


class LoadSkillTool(WeaveMindTool):
    name: str = "load_skill"
    description: str = (
        "加载指定 Skill 的完整决策指引。调用后指引将在下一轮上下文中出现。"
        "仅在任务匹配某个 Skill 的触发场景时调用，同一会话内不要重复加载同一 Skill。"
    )
    args_schema: Type[BaseModel] = LoadSkillInput

    def __init__(self, skill_registry=None, skill_buffer=None):
        super().__init__()
        self._registry = skill_registry
        self._buffer = skill_buffer

    def _run(self, name: str) -> str:
        if not self._registry:
            return "错误：Skill 系统未初始化"
        skill = self._registry.find_skill(name)
        if not skill:
            # 区分 "不存在" vs "已禁用"
            any_skill = self._registry.find_any(name)
            if any_skill:
                return f"Skill '{name}' 已被禁用，使用 /skill on {name} 启用"
            available = [s.name for s in self._registry.enabled_skills()]
            return f"未找到 Skill '{name}'。可用: {', '.join(available)}"
        body = skill.body  # 取出 body（SKILL.md 的正文部分，不含 YAML frontmatter）
         # 截断保护：超过 5KB 就截断
        if len(body) > MAX_BODY_SIZE:
            body = body[:MAX_BODY_SIZE] + "\n...(已截断，完整内容请用 /skill show " + name + ")"
        # 关键操作：把 body 塞进 buffer，不是返回给 LLM
        if self._buffer:
            self._buffer.push(name, body)
        size = len(body.encode("utf-8"))
        return f"已加载 Skill '{name}' 的完整指引（{size} bytes），将在下一轮上下文中出现。"
