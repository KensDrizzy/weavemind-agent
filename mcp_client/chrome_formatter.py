"""Chrome DevTools MCP 工具专用结果格式化器。

Chrome DevTools MCP 返回的结果有特殊格式：
  - 截图: base64 图片数据，需要保存为文件而非直接输出
  - DOM 快照: 可能非常长，需要截断
  - 控制台日志: 结构化列表
  - 网络请求: 结构化列表
  - 性能分析: JSON 对象
  - Lighthouse: 大型报告

本模块根据工具名称选择合适的格式化策略，
让 Agent 和用户看到简洁有用的信息，而非原始数据洪水。
"""

import base64
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── 截图保存目录 ──────────────────────────────────────────────

_SCREENSHOT_DIR = Path(".weavemind/chrome_screenshots")


def _ensure_screenshot_dir() -> Path:
    """确保截图保存目录存在，返回路径。"""
    _SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    return _SCREENSHOT_DIR


# ── Chrome 工具识别 ────────────────────────────────────────────

# 已知的 Chrome DevTools MCP 工具名
_CHROME_TOOL_NAMES = frozenset({
    "click", "close_page", "drag", "emulate", "evaluate_script",
    "fill", "fill_form", "get_console_message", "get_network_request",
    "handle_dialog", "hover", "lighthouse_audit", "list_console_messages",
    "list_network_requests", "list_pages", "navigate_page", "new_page",
    "performance_analyze_insight", "performance_start_trace",
    "performance_stop_trace", "press_key", "resize_page", "select_page",
    "take_memory_snapshot", "take_screenshot", "take_snapshot",
    "type_text", "upload_file", "wait_for",
})


def is_chrome_tool(tool_name: str) -> bool:
    """判断工具名是否属于 Chrome DevTools MCP。"""
    return tool_name in _CHROME_TOOL_NAMES


# ── 结果内容提取 ────────────────────────────────────────────────

def _extract_text_items(result: Any) -> list[dict]:
    """从 MCP 结果中提取所有内容项。"""
    content = getattr(result, "content", [])
    items = []
    for item in content:
        item_type = getattr(item, "type", None)
        if item_type == "text":
            items.append({"type": "text", "text": getattr(item, "text", "")})
        elif item_type == "image":
            items.append({
                "type": "image",
                "data": getattr(item, "data", ""),
                "mime_type": getattr(item, "mimeType", "image/png"),
            })
        elif item_type == "resource":
            resource = getattr(item, "resource", None)
            items.append({
                "type": "resource",
                "uri": getattr(resource, "uri", "unknown") if resource else "unknown",
            })
    return items


# ── 截图格式化 ─────────────────────────────────────────────────

def _format_screenshot(items: list[dict]) -> str:
    """格式化截图结果：base64 保存为文件，返回文件路径。"""
    for item in items:
        if item["type"] == "image":
            data = item["data"]
            mime = item.get("mime_type", "image/png")
            ext = "png" if "png" in mime else "jpg"

            # 保存到文件
            out_dir = _ensure_screenshot_dir()
            ts = time.strftime("%Y%m%d_%H%M%S")
            filename = f"screenshot_{ts}.{ext}"
            filepath = out_dir / filename

            try:
                raw = base64.b64decode(data)
                filepath.write_bytes(raw)
                return f"[截图已保存: {filepath}] ({len(raw)} bytes, {mime})"
            except Exception as e:
                logger.warning("截图保存失败: %s", e)
                return f"[截图数据: {mime}, {len(data)} chars base64, 保存失败: {e}]"

    # 没有图片数据，可能是文本描述
    texts = [i["text"] for i in items if i["type"] == "text"]
    if texts:
        return "\n".join(texts)
    return "(截图工具执行完成，但无图片数据返回)"


# ── 长文本截断 ────────────────────────────────────────────────

_MAX_TEXT_LENGTH = 8000
_TRUNCATE_SUFFIX = "\n...[已截断，完整内容过长]"


def _truncate_text(text: str, max_len: int = _MAX_TEXT_LENGTH) -> str:
    """截断过长的文本。"""
    if len(text) <= max_len:
        return text
    return text[:max_len] + _TRUNCATE_SUFFIX


# ── 工具专用格式化 ────────────────────────────────────────────

def _format_take_snapshot(items: list[dict]) -> str:
    """格式化 DOM 快照：截断超长的 HTML/ARIA 树。"""
    texts = [i["text"] for i in items if i["type"] == "text"]
    combined = "\n".join(texts)
    return _truncate_text(combined)


def _format_evaluate_script(items: list[dict]) -> str:
    """格式化 JS 执行结果：截断超长输出。"""
    texts = [i["text"] for i in items if i["type"] == "text"]
    combined = "\n".join(texts)
    return _truncate_text(combined, max_len=4000)


def _format_lighthouse(items: list[dict]) -> str:
    """格式化 Lighthouse 审计报告：截断超长 JSON。"""
    texts = [i["text"] for i in items if i["type"] == "text"]
    combined = "\n".join(texts)
    return _truncate_text(combined, max_len=6000)


def _format_performance(items: list[dict]) -> str:
    """格式化性能分析结果。"""
    texts = [i["text"] for i in items if i["type"] == "text"]
    combined = "\n".join(texts)
    return _truncate_text(combined, max_len=4000)


def _format_generic_chrome(items: list[dict]) -> str:
    """通用 Chrome 工具结果格式化。"""
    parts = []
    for item in items:
        if item["type"] == "text":
            parts.append(item["text"])
        elif item["type"] == "image":
            parts.append(f"[图片数据: {item.get('mime_type', 'unknown')}]")
        elif item["type"] == "resource":
            parts.append(f"[资源: {item.get('uri', 'unknown')}]")

    combined = "\n".join(parts)
    return _truncate_text(combined)


# ── 工具名 → 格式化策略映射 ──────────────────────────────────

_FORMATTERS = {
    "take_screenshot": _format_screenshot,
    "take_snapshot": _format_take_snapshot,
    "evaluate_script": _format_evaluate_script,
    "lighthouse_audit": _format_lighthouse,
    "performance_analyze_insight": _format_performance,
    "performance_start_trace": _format_performance,
    "performance_stop_trace": _format_performance,
    "take_memory_snapshot": _format_performance,
}


# ── 公共入口 ──────────────────────────────────────────────────

def format_chrome_result(tool_name: str, result: Any) -> str:
    """格式化 Chrome DevTools MCP 工具的返回结果。

    Args:
        tool_name: 工具名（如 take_screenshot, navigate_page 等）
        result: MCP 工具返回的 CallToolResult 对象

    Returns:
        str: 格式化后的结果字符串
    """
    # 检查错误
    is_error = getattr(result, "isError", False)
    items = _extract_text_items(result)

    if is_error:
        error_texts = [i["text"] for i in items if i["type"] == "text"]
        error_msg = "\n".join(error_texts) if error_texts else "未知错误"
        return f"[Chrome错误] {error_msg}"

    # 选择格式化器
    formatter = _FORMATTERS.get(tool_name, _format_generic_chrome)
    return formatter(items)
