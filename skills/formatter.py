"""SkillIndexFormatter — 格式化 Skill 索引给 LLM 看。"""
from skills.models import Skill

# 索引中 description 的截断长度。Anthropic Skills 规范允许 description 最长 1024 字符；
# 这里取 600 作为权衡：常规简介足够展开，又能压住 system prompt 长度。
MAX_DESC_LEN = 600
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
