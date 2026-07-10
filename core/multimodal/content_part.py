"""多模态消息内容块 — 统一 text / image_url / image_base64 的表示。

LangChain HumanMessage 的 content 可以是字符串，也可以是 content block 列表。
本模块把不同来源的图片输入统一转成 OpenAI 兼容的 image_url block，
同时提供文本提取、base64 估算等工具函数。
"""

from __future__ import annotations

import base64
import io
import mimetypes
from dataclasses import dataclass
from typing import Any, Literal

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None  # type: ignore[assignment]


@dataclass
class TextPart:
    """文本内容块。"""

    text: str
    type: Literal["text"] = "text"

    def to_openai(self) -> dict[str, Any]:
        return {"type": "text", "text": self.text}


@dataclass
class ImageUrlPart:
    """图片 URL 内容块（支持 http(s) 或 data URI）。"""

    url: str
    detail: Literal["auto", "low", "high"] = "auto"
    type: Literal["image_url"] = "image_url"

    def to_openai(self) -> dict[str, Any]:
        return {"type": "image_url", "image_url": {"url": self.url, "detail": self.detail}}


@dataclass
class ImageBase64Part:
    """base64 编码的图片内容块。"""

    data: str  # base64 payload，不含 data URI 前缀
    mime_type: str
    detail: Literal["auto", "low", "high"] = "auto"
    type: Literal["image_url"] = "image_url"

    def to_openai(self) -> dict[str, Any]:
        url = f"data:{self.mime_type};base64,{self.data}"
        return {"type": "image_url", "image_url": {"url": url, "detail": self.detail}}


ContentPart = TextPart | ImageUrlPart | ImageBase64Part


def content_to_text(content: str | list[dict[str, Any]] | None) -> str:
    """从字符串或多模态 content 列表中提取纯文本。"""
    if not content:
        return ""
    if isinstance(content, str):
        return content
    texts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            texts.append(block.get("text", ""))
    return "".join(texts)


def extract_plain_text(parts: list[ContentPart]) -> str:
    """从 ContentPart 列表中提取纯文本。"""
    return "".join(p.text for p in parts if isinstance(p, TextPart))


def parts_to_openai(parts: list[ContentPart]) -> list[dict[str, Any]]:
    """把 ContentPart 列表转成 OpenAI 兼容的 content block 列表。"""
    return [p.to_openai() for p in parts]


def base64_from_pil(image: Image.Image, mime_type: str = "image/png", quality: int = 95) -> str:
    """把 PIL Image 编码为 base64 字符串。"""
    if Image is None:
        raise ImportError("图片处理需要 Pillow，请运行 pip install Pillow")

    fmt = "JPEG" if mime_type in {"image/jpeg", "image/jpg"} else "PNG"
    buffer = io.BytesIO()
    if fmt == "JPEG":
        # JPEG 不支持 alpha，先转 RGB
        rgb_image = image.convert("RGB") if image.mode in ("RGBA", "P") else image
        rgb_image.save(buffer, format=fmt, quality=quality, optimize=True)
    else:
        image.save(buffer, format=fmt, optimize=True)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def mime_type_from_path(path: str) -> str:
    """根据文件路径推断 MIME 类型。"""
    mime, _ = mimetypes.guess_type(path)
    return mime or "image/png"


def estimate_base64_size(original_bytes: int) -> int:
    """估算原始字节经 base64 编码后的大小（膨胀比 4/3）。"""
    return (original_bytes * 4 + 2) // 3
