"""MCP 工具动态封装 — 将 MCP Server 提供的工具转换为 WeaveMindTool 子类。

核心思路：
  MCP Server 通过 tools/list 返回工具元数据（含 inputSchema），
  本模块根据元数据动态生成 WeaveMindTool 子类，使其能像内置工具一样
  被 ToolRegistry 注册、被 AgentLoop 调用。

实现策略：
  使用 class 语句 + 预定义闭包函数的方式创建工具类，
  避免 Pydantic V2 的字段覆盖问题和抽象方法实例化问题。
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
        return f"[MCP错误] {error_msg}"

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

    return "\n".join(texts) if texts else "(工具执行完成，无返回内容)"


# ── 动态工具类工厂 ─────────────────────────────────────────────────

def create_mcp_tool_instance(
    tool_info: MCPToolInfo,
    connection,  # MCPConnection 实例
) -> WeaveMindTool:
    """
    根据 MCP 工具元数据创建 WeaveMindTool 实例。

    使用 StructuredTool.from_function 将 MCP 工具封装为 LangChain 工具，
    这是 LangChain 推荐的动态工具创建方式，完全兼容 Pydantic V2。

    当 connection.server_type == "chrome" 且工具属于 Chrome DevTools 时，
    使用 Chrome 专用格式化器（截图保存文件、DOM 截断等）。

    Args:
        tool_info: MCP 工具元数据（来自 tools/list）
        connection: MCPConnection 实例

    Returns:
        WeaveMindTool 实例，可直接注册到 ToolRegistry
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

    # 创建同步调用函数（闭包捕获 conn 和 tname）
    def sync_func(**kwargs) -> str:
        filtered_kwargs = {k: v for k, v in kwargs.items() if v is not None}
        try:
            # 优先使用 MCP 连接的事件循环（避免 asyncio.run 关闭循环导致 stdio 连接断开）
            if conn._loop and conn._loop.is_running():
                future = asyncio.run_coroutine_threadsafe(
                    conn.call_tool(tname, filtered_kwargs), conn._loop
                )
                result = future.result(timeout=120)
                return _format(result)

            # 降级：没有持久事件循环时使用 asyncio.run
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    future = pool.submit(
                        asyncio.run, conn.call_tool(tname, filtered_kwargs)
                    )
                    result = future.result(timeout=120)
                    return _format(result)
            else:
                result = asyncio.run(conn.call_tool(tname, filtered_kwargs))
                return _format(result)
        except RuntimeError:
            result = asyncio.run(conn.call_tool(tname, filtered_kwargs))
            return _format(result)
        except Exception as e:
            return f"[MCP调用错误] {e}"

    # 创建异步调用函数
    async def async_func(**kwargs) -> str:
        filtered_kwargs = {k: v for k, v in kwargs.items() if v is not None}
        try:
            result = await conn.call_tool(tname, filtered_kwargs)
            return _format(result)
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
