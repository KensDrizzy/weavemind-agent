"""@image / @clipboard 引用解析。"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from enum import Enum, auto
from typing import Literal


class ImageSourceType(Enum):
    PATH = auto()
    CLIPBOARD = auto()


@dataclass(frozen=True)
class ImageRef:
    """解析后的图片引用。"""

    source: str
    source_type: ImageSourceType
    raw: str

    @property
    def is_clipboard(self) -> bool:
        return self.source_type == ImageSourceType.CLIPBOARD


# 匹配 @image:path 或 @image:<path with spaces>
# 也匹配独立的 @clipboard（后面不能紧跟字母/数字/下划线）
_IMAGE_REF_PATTERN = re.compile(
    r"@image:(<[^>]+>|[^\s<>‐-⁯　-〿＀-￯]+)"
    r"|@clipboard(?!\w)",
    re.UNICODE,
)


def parse_image_refs(text: str) -> list[ImageRef]:
    """从文本中提取所有图片引用。"""
    refs = []
    for match in _IMAGE_REF_PATTERN.finditer(text):
        raw = match.group(0)
        if raw.startswith("@clipboard"):
            refs.append(ImageRef(source="", source_type=ImageSourceType.CLIPBOARD, raw=raw))
            continue

        # @image:... 情况
        path = match.group(1)
        if path.startswith("<") and path.endswith(">"):
            path = path[1:-1]
        path = urllib.parse.unquote(path)
        if path.startswith("file://"):
            path = urllib.parse.urlparse(path).path
        refs.append(ImageRef(source=path, source_type=ImageSourceType.PATH, raw=raw))
    return refs


def strip_image_refs(text: str) -> str:
    """移除文本中的图片引用标记，返回干净文本。"""
    return _IMAGE_REF_PATTERN.sub("", text).strip()


def replace_image_refs(text: str, placeholder: str = "[图片]") -> str:
    """把图片引用替换为占位符，保留文本可读性。"""
    return _IMAGE_REF_PATTERN.sub(placeholder, text).strip()
