"""模型多模态能力表 — 判断当前模型是否支持 vision。"""

from __future__ import annotations

from langchain_core.messages import BaseMessage, HumanMessage

# 已知明确支持视觉的模型名片段（小写）
_VISION_MARKERS = (
    "claude",
    "gpt-4o",
    "gpt-4-turbo",
    "gemini",
    "glm-5v",
    "qwen-vl",
    "kimi-k2",
)


def supports_vision(model_name: str | None) -> bool:
    """根据模型名启发式判断是否支持 vision。"""
    normalized = (model_name or "").lower()
    return any(marker in normalized for marker in _VISION_MARKERS)


def message_has_image(message: BaseMessage) -> bool:
    """判断单条消息是否包含图片 block。"""
    content = getattr(message, "content", None)
    if not content or isinstance(content, str):
        return False
    if not isinstance(content, list):
        return False
    return any(
        isinstance(block, dict) and block.get("type") == "image_url"
        for block in content
    )


def messages_have_image(messages: list[BaseMessage]) -> bool:
    """判断消息列表中是否存在图片。"""
    return any(message_has_image(m) for m in messages)


def require_vision_model(model_name: str | None) -> None:
    """若当前模型不支持 vision 则抛出明确异常。"""
    if not supports_vision(model_name):
        raise ValueError(
            f"当前模型 {model_name!r} 不支持图片输入。"
            "请在 config.yaml 切换到 vision 模型，例如 kimi-k2.7 / claude-sonnet-4 / gpt-4o。"
        )
