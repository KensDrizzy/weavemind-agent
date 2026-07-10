"""多模态消息构造器 — 把用户文本与若干图片对象组装成 HumanMessage。"""

from __future__ import annotations

from langchain_core.messages import HumanMessage

from core.multimodal.content_part import ContentPart, TextPart, parts_to_openai


def build_multimodal_message(text: str, image_parts: list[ContentPart] | None = None) -> HumanMessage:
    """构造一条可能包含图片的 HumanMessage。

    如果没有图片，退化为纯文本消息（保持对旧代码的兼容）。
    如果有图片，content 为 OpenAI 兼容的 content block 列表。
    """
    images = image_parts or []
    if not images:
        return HumanMessage(content=text)

    parts: list[ContentPart] = []
    if text:
        parts.append(TextPart(text=text))
    parts.extend(images)
    return HumanMessage(content=parts_to_openai(parts))


def is_multimodal_message(msg: HumanMessage) -> bool:
    """判断 HumanMessage 是否包含多模态 content block。"""
    content = msg.content
    return isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") == "image_url"
        for block in content
    )
