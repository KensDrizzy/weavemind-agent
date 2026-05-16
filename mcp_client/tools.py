"""MCP 工具动态封装 — 将 MCP Server 提供的工具转换为 WeaveMindTool 子类。

核心思路：
  MCP Server 通过 tools/list 返回工具元数据（含 inputSchema），
  本模块根据元数据动态生成 WeaveMindTool 子类，使其能像内置工具一样
  被 ToolRegistry 注册、被 AgentLoop 调用。
"""

import asyncio
import logging
from typing import Any, Optional, Type

from pydantic import BaseModel, Field, create_model

from mcp.types import Tool as MCPToolInfo
from tools.base import WeaveMindTool

logger = logging.getLogger(__name__)


# ── JSON Schema → Python 类型映射 ──────────────────────────────────

_JSON_SCHEMA_TYPE_MAP = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _json_schema_to_python_type(prop: dict) -> type:
    """将 JSON Schema 属性定义转换为 Python 类型。"""
    json_type = prop.get("type", "string")

    if json_type == "array":
        items = prop.get("items", {})
        item_type = _json_schema_to_python_type(items)
        return list[item_type]  # type: ignore[valid-type]

    if json_type == "object":
        return dict

    return _JSON_SCHEMA_TYPE_MAP.get(json_type, str)


# ── Args Schema 动态生成 ──────────────────────────────────────────

def _create_args_schema(tool_info: MCPToolInfo) -> Type[BaseModel]:
    """根据 MCP 工具的 inputSchema 动态创建 Pydantic 模型。"""
    schema = tool_info.inputSchema

    if not schema or schema.get("type") != "object":
        return create_model("EmptyArgs")

    properties = schema.get("properties", {})
    required = set(schema.get("required", []))

    fields = {}
    for name, prop in properties.items():
        field_type = _json_schema_to_python_type(prop)
        description = prop.get("description", "")
        default = prop.get("default")

        if name in required:
            fields[name] = (field_type, Field(..., description=description))
        elif default is not None:
            fields[name] = (field_type, Field(default=default, description=description))
        else:
            fields[name] = (Optional[field_type], Field(None, description=description))  # type: ignore[assignment]

    if not fields:
        return create_model("EmptyArgs")

    return create_model(
        f"{tool_info.name}Args",
        __doc__=f"Arguments for MCP tool {tool_info.name}",
        **fields,
    )


# ── 工具描述格式化 ─────────────────────────────────────────────────

def _format_description(tool_info: MCPToolInfo) -> str:
    """格式化工具描述，附加参数说明。"""
    desc = tool_info.description or f"MCP tool: {tool_info.name}"

    schema = tool_info.inputSchema
    if schema and schema.get("type") == "object":
        properties = schema.get("properties", {})
        if properties:
            desc += "\n\n参数:"
            for name, prop in properties.items():
                prop_desc = prop.get("description", "无描述")
                prop_type = prop.get("type", "any")
                desc += f"\n- {name} ({prop_type}): {prop_desc}"

    return desc


# ── 工具结果格式化 ─────────────────────────────────────────────────

# 登录/权限问题检测关键词（用于提示 LLM 调用 browser_connect）
_LOGIN_DETECTION_PATTERNS = [
    "login", "sign in", "signin", "log in", "login", "登錄", "登录", "登陆",
    "401", "403", "unauthorized", "未授权", "无权限", "禁止访问",
    "need login", "需要登录", "请先登录", "authentication required",
    "access denied", "拒绝访问",
]


def _detect_login_hint(result_str: str) -> str:
    """检测结果中是否包含登录/权限问题，返回提示信息。"""
    import re
    result_lower = result_str.lower()
    for pattern in _LOGIN_DETECTION_PATTERNS:
        if pattern.lower() in result_lower:
            return (
                "\n\n[系统提示：检测到可能需要登录的页面。"
                "如果当前是 isolated 模式，请调用 browser_connect 切换到 shared 模式，"
                "然后立即用相同参数重试刚才的操作。]"
            )
    return ""


def _format_result(result: Any) -> str:
    """将 MCP 工具返回结果格式化为字符串。"""
    is_error = getattr(result, "isError", False)
    content = getattr(result, "content", [])

    if is_error:
        error_texts = []
        for item in content:
            if getattr(item, "type", None) == "text":
                error_texts.append(getattr(item, "text", ""))
        error_msg = "\n".join(error_texts) if error_texts else "未知错误"
        result_str = f"[MCP错误] {error_msg}"
        # 错误结果也检测登录问题
        return result_str + _detect_login_hint(result_str)

    texts = []
    for item in content:
        item_type = getattr(item, "type", None)
        if item_type == "text":
            texts.append(getattr(item, "text", ""))
        elif item_type == "image":
            mime = getattr(item, "mimeType", "unknown")
            texts.append(f"[图片数据: {mime}]")
        elif item_type == "resource":
            resource = getattr(item, "resource", None)
            if resource:
                texts.append(f"[资源: {getattr(resource, 'uri', 'unknown')}]")

    result_str = "\n".join(texts) if texts else "(工具执行完成，无返回内容)"
    # 检测登录问题并追加提示
    return result_str + _detect_login_hint(result_str)


# ── 动态工具类工厂 ─────────────────────────────────────────────────


def create_mcp_tool_instance(
    tool_info: MCPToolInfo,
    connection,  # MCPConnection 实例
    mcp_manager=None,  # MCPManager 实例（用于 Chrome 模式切换）
) -> WeaveMindTool:
    """根据 MCP 工具元数据创建 WeaveMindTool 实例。

    sync_func 通过 run_coroutine_threadsafe 在持久 MCP 事件循环上执行，
    避免 asyncio.run() 创建新循环破坏 MCP stdio 连接。
    """
    from langchain_core.tools import StructuredTool
    from mcp_client.chrome_formatter import is_chrome_tool, format_chrome_result

    tname = tool_info.name
    desc = _format_description(tool_info)
    args_schema = _create_args_schema(tool_info)
    conn = connection

    # 确定结果格式化器
    use_chrome_formatter = (
        conn.server_type == "chrome" and is_chrome_tool(tname)
    )

    def _format(result: Any) -> str:
        if use_chrome_formatter:
            return format_chrome_result(tname, result)
        return _format_result(result)

    # 创建同步调用函数
    def sync_func(**kwargs) -> str:
        filtered_kwargs = {k: v for k, v in kwargs.items() if v is not None}
        try:
            # 使用持久 MCP 事件循环（由 app.py 注入到 MCPManager）
            loop = mcp_manager._mcp_loop if mcp_manager else None
            if loop and loop.is_running():
                future = asyncio.run_coroutine_threadsafe(
                    conn.call_tool(tname, filtered_kwargs), loop
                )
                result = future.result(timeout=120)
            elif conn._loop and conn._loop.is_running():
                # 降级：使用 MCPConnection 保存的事件循环
                future = asyncio.run_coroutine_threadsafe(
                    conn.call_tool(tname, filtered_kwargs), conn._loop
                )
                result = future.result(timeout=120)
            else:
                # 无持久循环（测试等场景）
                result = asyncio.run(conn.call_tool(tname, filtered_kwargs))

            result_str = _format(result)

            # 执行后更新 BrowserGuard 状态
            if use_chrome_formatter and mcp_manager:
                mcp_manager.apply_browser_after_execution(
                    tname, filtered_kwargs, result_str
                )
            return result_str
        except Exception as e:
            logger.error("MCP 工具 '%s' 调用失败: %s", tname, e)
            return f"[MCP调用错误] {e}"

    # 创建异步调用函数
    async def async_func(**kwargs) -> str:
        filtered_kwargs = {k: v for k, v in kwargs.items() if v is not None}
        try:
            result = await conn.call_tool(tname, filtered_kwargs)
            result_str = _format(result)
            if use_chrome_formatter and mcp_manager:
                mcp_manager.apply_browser_after_execution(
                    tname, filtered_kwargs, result_str
                )
            return result_str
        except Exception as e:
            return f"[MCP调用错误] {e}"

    tool = StructuredTool.from_function(
        func=sync_func,
        coroutine=async_func,
        name=tname,
        description=desc,
        args_schema=args_schema,
    )

    return tool