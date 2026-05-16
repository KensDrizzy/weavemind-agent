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


def _try_auto_switch_to_shared(
    result_str: str,
    tool_args: dict,
    tool_name: str,
    mcp_manager,
    format_fn,
    conn=None,  # 当前 MCPConnection，用于获取事件循环
) -> str:
    """检测 Chrome 工具结果是否需要登录，自动切换到 shared 模式并重试。

    如果结果表明需要登录且当前是 isolated 模式，会：
    1. 通过 session_manager.switch_to_shared() 切换模式
    2. 获取新的 MCP 连接
    3. 重新执行工具调用

    只重试一次，避免无限循环。
    """
    session_manager = mcp_manager.get_session_manager()
    if not session_manager or session_manager.is_shared:
        return result_str

    # 检测是否需要登录
    url = tool_args.get("url", "")
    if not session_manager.detect_need_login(result_str, url):
        return result_str

    logger.info("Chrome 工具 '%s' 检测到需要登录，尝试自动切换到 shared 模式...", tool_name)

    try:
        # 使用连接的事件循环（与 sync_func 保持一致）
        loop = getattr(conn, "_loop", None)
        if loop and loop.is_running():
            switched = asyncio.run_coroutine_threadsafe(
                session_manager.switch_to_shared(), loop
            ).result(timeout=30)
        else:
            # 没有持久事件循环时，创建新的
            loop = asyncio.new_event_loop()
            try:
                switched = loop.run_until_complete(
                    session_manager.switch_to_shared()
                )
            finally:
                loop.close()

        if not switched:
            logger.warning("自动切换到 shared 模式失败，返回原始结果")
            return result_str

        # 获取新的连接并重试
        new_conn = mcp_manager.get_connection("chrome")
        if not new_conn:
            logger.warning("切换后未找到新的 Chrome 连接")
            return result_str

        logger.info("已切换到 shared 模式，重新执行 %s...", tool_name)

        new_loop = getattr(new_conn, "_loop", None)
        if new_loop and new_loop.is_running():
            retry_result = asyncio.run_coroutine_threadsafe(
                new_conn.call_tool(tool_name, tool_args), new_loop
            ).result(timeout=120)
        else:
            loop = asyncio.new_event_loop()
            try:
                retry_result = loop.run_until_complete(
                    new_conn.call_tool(tool_name, tool_args)
                )
            finally:
                loop.close()

        retry_str = format_fn(retry_result)
        return f"[已自动切换到 shared 模式（连接用户 Chrome）]\n{retry_str}"

    except Exception as e:
        logger.error("自动切换到 shared 模式出错: %s", e)
        return result_str


def create_mcp_tool_instance(
    tool_info: MCPToolInfo,
    connection,  # MCPConnection 实例
    mcp_manager=None,  # MCPManager 实例（用于 Chrome 自动切换）
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

    # 创建同步调用函数（闭包捕获 conn、tname、mcp_manager）
    def sync_func(**kwargs) -> str:
        filtered_kwargs = {k: v for k, v in kwargs.items() if v is not None}
        try:
            # 优先使用 MCP 连接的事件循环（避免 asyncio.run 关闭循环导致 stdio 连接断开）
            if conn._loop and conn._loop.is_running():
                future = asyncio.run_coroutine_threadsafe(
                    conn.call_tool(tname, filtered_kwargs), conn._loop
                )
                result = future.result(timeout=120)
                result_str = _format(result)

                # Chrome 工具自动检测登录页 → 切换 shared 模式重试
                if use_chrome_formatter and mcp_manager:
                    result_str = _try_auto_switch_to_shared(
                        result_str, filtered_kwargs, tname, mcp_manager, _format, conn=conn
                    )
                return result_str

            # 降级：没有持久事件循环时使用 asyncio.run
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    future = pool.submit(
                        asyncio.run, conn.call_tool(tname, filtered_kwargs)
                    )
                    result = future.result(timeout=120)
                    result_str = _format(result)
                    if use_chrome_formatter and mcp_manager:
                        result_str = _try_auto_switch_to_shared(
                            result_str, filtered_kwargs, tname, mcp_manager, _format, conn=conn
                        )
                    return result_str
            else:
                result = asyncio.run(conn.call_tool(tname, filtered_kwargs))
                result_str = _format(result)
                if use_chrome_formatter and mcp_manager:
                    result_str = _try_auto_switch_to_shared(
                        result_str, filtered_kwargs, tname, mcp_manager, _format, conn=conn
                    )
                return result_str
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
