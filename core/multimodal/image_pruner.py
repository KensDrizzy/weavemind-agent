"""历史图片裁剪 — 避免多轮 ReAct 后旧图片 base64 撑爆上下文。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage, ToolMessage


def _replace_image_blocks(content: list[dict[str, Any]], placeholder: str) -> list[dict[str, Any]]:
    """把 content 列表中的 image_url block 替换为文本占位。"""
    new_content = []
    replaced = False
    for block in content:
        if isinstance(block, dict) and block.get("type") == "image_url":
            if not replaced:
                new_content.append({"type": "text", "text": placeholder})
                replaced = True
            # 同一条消息里的多个图片 block 只保留一个占位
        else:
            new_content.append(block)
    return new_content


def prune_historical_image_payloads(
    messages: list[BaseMessage],
    keep_last_n_rounds: int = 1,
    placeholder: str = "[图片已省略，参见上文描述]",
) -> list[BaseMessage]:
    """保留最近 N 轮图片实体，更早的图片替换为占位文本。

    只处理 HumanMessage（用户输入或 MCP 图片注入），ToolMessage 中不会直接含图片 block。
    """
    if keep_last_n_rounds < 0:
        keep_last_n_rounds = 0

    # 找出所有含图片的 HumanMessage 索引
    image_indices = []
    for i, msg in enumerate(messages):
        if not isinstance(msg, HumanMessage):
            continue
        content = getattr(msg, "content", None)
        if isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "image_url" for b in content
        ):
            image_indices.append(i)

    if len(image_indices) <= keep_last_n_rounds:
        return messages

    keep_indices = set(image_indices[-keep_last_n_rounds:]) if keep_last_n_rounds > 0 else set()
    result = list(messages)
    for idx in image_indices:
        if idx in keep_indices:
            continue
        msg = result[idx]
        new_content = _replace_image_blocks(msg.content, placeholder)
        if new_content != msg.content:
            result[idx] = HumanMessage(content=new_content)
    return result
