"""PromptRepository — 三层提示词加载器（builtin → user → project）。

加载优先级：project > user > builtin，后者覆盖前者。
"""
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class PromptRepository:
    """从三层目录加载 .md 提示词文件，支持覆盖。"""

    def __init__(self, builtin_dir: Path, user_dir: Optional[Path] = None, project_dir: Optional[Path] = None):
        self.builtin_dir = builtin_dir
        self.user_dir = user_dir
        self.project_dir = project_dir

    @classmethod
    def create_default(cls) -> "PromptRepository":
        builtin = Path(__file__).parent.parent / "prompts"
        user = Path.home() / ".weavemind" / "prompts"
        project = Path(".weavemind") / "prompts"
        return cls(builtin, user, project)

    def load(self, relative_path: str) -> Optional[str]:
        """加载提示词文件，按 builtin → user → project 顺序，后者覆盖前者。"""
        normalized = relative_path.replace("\\", "/")
        if normalized.startswith("/") or ".." in normalized:
            raise ValueError(f"非法路径: {relative_path}")

        content = self._read_from(self.builtin_dir, normalized)
        content = self._override(self.user_dir, normalized, content)
        content = self._override(self.project_dir, normalized, content)
        return content.strip() if content else None

    def load_required(self, relative_path: str) -> str:
        """加载必需的提示词文件，不存在则抛异常。"""
        content = self.load(relative_path)
        if not content:
            raise FileNotFoundError(f"提示词文件缺失: {relative_path}")
        return content

    def _read_from(self, dir_path: Optional[Path], relative: str) -> Optional[str]:
        if not dir_path:
            return None
        file = dir_path / relative
        if not file.is_file():
            return None
        try:
            return file.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning(f"读取提示词失败 {file}: {e}")
            return None

    def _override(self, dir_path: Optional[Path], relative: str, fallback: Optional[str]) -> Optional[str]:
        content = self._read_from(dir_path, relative)
        return content if content else fallback
