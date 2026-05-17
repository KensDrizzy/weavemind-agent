"""SkillIndexFormatter — 格式化 Skill 索引给 LLM 看。"""
from skills.models import Skill

MAX_DESC_LEN = 500
MAX_INDEX_SKILLS = 20


class SkillIndexFormatter:
    @staticmethod
    def format(skills: list[Skill]) -> str:
        if not skills:
            return ""
        skills = skills[:MAX_INDEX_SKILLS]
        lines = ["## 可用 Skills（按需调用 load_skill 加载完整指引）\n"]
        for s in skills:
            desc = s.description[:MAX_DESC_LEN] + "..." if len(s.description) > MAX_DESC_LEN else s.description
            lines.append(f"- **{s.name}**：{desc}")
        lines.append(
            "\n判断准则：当任务匹配某个 Skill 的触发场景时，调用 load_skill(name) 加载完整指引。"
            "已加载的 Skill 会在下一轮 user message 中出现。同一会话内不要重复加载同一 Skill。"
        )
        return "\n".join(lines)
