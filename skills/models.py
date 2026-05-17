"""Skill 数据模型。"""
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Optional


class SkillSource(Enum):
    BUILTIN = auto()
    USER = auto()
    PROJECT = auto()


@dataclass(frozen=True)
class Skill:
    name: str
    description: str = ""
    version: Optional[str] = None
    author: Optional[str] = None
    tags: list[str] = field(default_factory=list)
    source: SkillSource = SkillSource.BUILTIN
    body: str = ""
    skill_md_path: Optional[Path] = None
    references_dir: Optional[Path] = None

    def display_source(self) -> str:
        return {SkillSource.BUILTIN: "builtin", SkillSource.USER: "user", SkillSource.PROJECT: "project"}[self.source]
