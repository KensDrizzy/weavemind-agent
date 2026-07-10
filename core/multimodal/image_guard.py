"""图片安全校验 — 路径围栏、类型白名单、大小上限。"""

from __future__ import annotations

import mimetypes
import os
from pathlib import Path
from typing import Iterable

import settings

DEFAULT_ALLOWED_MIME_TYPES = {"image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"}
DEFAULT_MAX_FILE_SIZE = 20 * 1024 * 1024  # 20MB


class ImageGuardError(Exception):
    """图片校验失败。"""


class ImageGuard:
    """对用户提供的图片路径做安全校验。"""

    def __init__(
        self,
        project_root: Path | str | None = None,
        allowed_mime_types: set[str] | None = None,
        allowed_dirs: list[Path | str] | None = None,
        max_file_size: int | None = None,
    ):
        self.project_root = Path(project_root or os.getcwd()).resolve()
        self.allowed_mime_types = allowed_mime_types or set(
            settings.get("multimodal.allowed_mime_types", list(DEFAULT_ALLOWED_MIME_TYPES))
        )
        self.max_file_size = max_file_size or settings.get(
            "multimodal.max_image_file_size", DEFAULT_MAX_FILE_SIZE
        )
        self.allowed_dirs = self._resolve_allowed_dirs(allowed_dirs)

    def _resolve_allowed_dirs(self, allowed_dirs: list[Path | str] | None) -> set[Path]:
        dirs = {self.project_root}
        configured = allowed_dirs or settings.get("multimodal.allowed_image_dirs", [])
        for d in configured:
            dirs.add(Path(d).expanduser().resolve())
        return dirs

    def validate(self, path: Path | str) -> Path:
        """校验图片路径，返回绝对路径。"""
        p = Path(path).expanduser().resolve()

        if not p.exists():
            raise ImageGuardError(f"图片不存在: {p}")
        if not p.is_file():
            raise ImageGuardError(f"路径不是文件: {p}")

        # 路径围栏：必须在项目根或白名单目录内
        if not self._is_under_allowed_dir(p):
            raise ImageGuardError(
                f"图片路径不在允许范围内: {p}。"
                f"请放入项目目录或 multimodal.allowed_image_dirs 白名单。"
            )

        # MIME 类型白名单
        mime, _ = mimetypes.guess_type(str(p))
        if not mime or mime not in self.allowed_mime_types:
            raise ImageGuardError(
                f"不支持的图片类型: {mime}。允许: {sorted(self.allowed_mime_types)}"
            )

        # 文件大小上限
        size = p.stat().st_size
        if size > self.max_file_size:
            raise ImageGuardError(
                f"图片过大: {size} 字节，上限 {self.max_file_size} 字节。"
            )

        return p

    def _is_under_allowed_dir(self, p: Path) -> bool:
        try:
            return any(p == d or d in p.parents for d in self.allowed_dirs)
        except ValueError:
            # 不同盘符等异常
            return False

    def is_allowed(self, path: Path | str) -> bool:
        try:
            self.validate(path)
            return True
        except ImageGuardError:
            return False
