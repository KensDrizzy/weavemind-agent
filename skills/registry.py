"""SkillRegistry — 三层目录扫描与 Skill 管理。"""
import logging
from pathlib import Path
from typing import Optional

from skills.models import Skill, SkillSource
from skills.parser import SkillFrontmatterParser
from skills.state_store import SkillStateStore

logger = logging.getLogger(__name__)


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
        refs = skill_dir / "references"
        return Skill(
            name=name,
            description=fm.get("description", "").strip() if isinstance(fm.get("description"), str) else "",
            version=fm.get("version"),
            author=fm.get("author"),
            tags=fm.get("tags", []) if isinstance(fm.get("tags"), list) else [],
            source=source,
            body=parsed.body,
            skill_md_path=skill_md,
            references_dir=refs if refs.is_dir() else None,
        )
