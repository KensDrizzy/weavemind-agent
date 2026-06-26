"""SkillRegistry — 三层目录扫描与 Skill 管理。"""
import logging
import re
from pathlib import Path
from typing import Optional

from skills.models import Skill, SkillSource
from skills.parser import SkillFrontmatterParser
from skills.state_store import SkillStateStore

logger = logging.getLogger(__name__)

# ── Anthropic Agent Skills 官方规范约束 ──
# 文档：https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices
SPEC_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")
SPEC_NAME_MAX_LEN = 64
SPEC_DESC_MAX_LEN = 1024
SPEC_RESERVED_WORDS = {"anthropic", "claude"}


class SkillRegistry:
    """扫描顺序: builtin → user → project（同名后者覆盖前者）。"""

    def __init__(self, builtin_dir: Path, user_dir: Path, project_dir: Path, state_store: Optional[SkillStateStore] = None):
        self.builtin_dir = builtin_dir
        self.user_dir = user_dir
        self.project_dir = project_dir
        self.state_store = state_store
        self._skills: dict[str, Skill] = {}
        self._warnings: list[str] = []

    def reload(self) -> None:
        self._skills.clear()
        self._warnings.clear()
        self._scan(self.builtin_dir, SkillSource.BUILTIN)
        self._scan(self.user_dir, SkillSource.USER)
        self._scan(self.project_dir, SkillSource.PROJECT)
        logger.info("Skills 加载完成: %d 个（启用 %d）", len(self._skills), len(self.enabled_skills()))

    def all_skills(self) -> list[Skill]:
        return sorted(self._skills.values(), key=lambda s: s.name)

    def enabled_skills(self) -> list[Skill]:
        disabled = self.state_store.disabled() if self.state_store else set()
        return [s for s in self.all_skills() if s.name not in disabled]

    def find_skill(self, name: str) -> Optional[Skill]:
        """查找已启用的 Skill。"""
        skill = self._skills.get(name)
        if not skill:
            return None
        disabled = self.state_store.disabled() if self.state_store else set()
        return None if skill.name in disabled else skill

    def find_any(self, name: str) -> Optional[Skill]:
        """查找 Skill（忽略 disabled 状态）。"""
        return self._skills.get(name)

    @property
    def warnings(self) -> list[str]:
        return list(self._warnings)

    def _scan(self, dir_path: Path, source: SkillSource) -> None:
        if not dir_path.is_dir():
            return
        for entry in sorted(dir_path.iterdir()):
            if not entry.is_dir():
                continue
            skill_md = entry / "SKILL.md"
            if not skill_md.is_file():
                continue
            skill = self._parse(entry, skill_md, source)
            if skill:
                self._skills[skill.name] = skill

    def _parse(self, skill_dir: Path, skill_md: Path, source: SkillSource) -> Optional[Skill]:
        try:
            content = skill_md.read_text(encoding="utf-8")
        except Exception as e:
            self._warnings.append(f"读取失败 {skill_md}: {e}")
            return None
        parsed = SkillFrontmatterParser.parse(content)
        self._warnings.extend(f"{skill_md}: {w}" for w in parsed.warnings)
        fm = parsed.frontmatter
        name = fm.get("name", "").strip() or skill_dir.name
        description = fm.get("description", "").strip() if isinstance(fm.get("description"), str) else ""

        # Anthropic Agent Skills 规范校验：违规只发警告，不拒绝加载，
        # 避免内置/老旧 Skill 因规范升级而集体下线。
        self._validate_frontmatter(name, description, skill_md)

        refs = skill_dir / "references"
        return Skill(
            name=name,
            description=description,
            version=fm.get("version"),
            author=fm.get("author"),
            tags=fm.get("tags", []) if isinstance(fm.get("tags"), list) else [],
            source=source,
            body=parsed.body,
            skill_md_path=skill_md,
            references_dir=refs if refs.is_dir() else None,
        )

    def _validate_frontmatter(self, name: str, description: str, skill_md: Path) -> None:
        """对照 Anthropic Agent Skills 规范校验 frontmatter，违规只产生警告。"""
        ctx = f"{skill_md}"

        if not name:
            self._warnings.append(f"{ctx}: name 为空，已用目录名兜底（违反 Anthropic Skills 规范）")
        else:
            if len(name) > SPEC_NAME_MAX_LEN:
                self._warnings.append(
                    f"{ctx}: name 长度 {len(name)} > {SPEC_NAME_MAX_LEN}，违反 Anthropic Skills 规范"
                )
            if not SPEC_NAME_PATTERN.match(name):
                self._warnings.append(
                    f"{ctx}: name='{name}' 含非法字符，规范要求 小写字母/数字/连字符 且以字母数字开头"
                )
            if name.lower() in SPEC_RESERVED_WORDS:
                self._warnings.append(
                    f"{ctx}: name='{name}' 属保留字，Anthropic 规范禁止使用 {SPEC_RESERVED_WORDS}"
                )
            if "<" in name or ">" in name:
                self._warnings.append(f"{ctx}: name 包含 XML 标签字符，违反规范")

        if not description:
            self._warnings.append(f"{ctx}: description 为空，违反 Anthropic Skills 规范")
        else:
            if len(description) > SPEC_DESC_MAX_LEN:
                self._warnings.append(
                    f"{ctx}: description 长度 {len(description)} > {SPEC_DESC_MAX_LEN}，建议拆分到正文"
                )
            if "<" in description or ">" in description:
                self._warnings.append(f"{ctx}: description 包含 XML 标签字符，违反规范")
