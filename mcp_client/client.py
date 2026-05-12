"""MCPConnection — 保持长连接的 MCP 会话管理器，支持 stdio 和 HTTP(SSE) 两种传输。"""

import asyncio
import logging
import os
from contextlib import AsyncExitStack
from typing import Any, Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import Tool as MCPToolInfo

logger = logging.getLogger(__name__)


class MCPConnection:
    """
    MCP Server 连接管理器。

    保持与单个 MCP Server 的长连接，支持工具调用和资源读取。
    使用 AsyncExitStack 管理 session 生命周期，确保连接不会提前关闭。
    """

    def __init__(self, server_config: dict):
        self.name: str = server_config["name"]
        self.transport: str = server_config.get("transport", "stdio")
        self.config: dict = server_config

        # Runtime state
        self._session: Optional[ClientSession] = None
        self._exit_stack: Optional[AsyncExitStack] = None
        self._tools_info: list[MCPToolInfo] = []
        self._connected: bool = False

    async def connect(self) -> bool:
        """
        建立与 MCP Server 的长连接。

        使用 AsyncExitStack 保持 session 生命周期，
        连接成功后缓存工具列表，后续调用无需重新获取。

        Returns:
            bool: 连接是否成功
        """
        self._exit_stack = AsyncExitStack()

        try:
            if self.transport == "stdio":
                success = await self._connect_stdio()
            elif self.transport in ("http", "sse"):
                success = await self._connect_http()
            else:
                raise ValueError(f"不支持的传输方式: {self.transport}")

            if success:
                # 获取并缓存工具列表
                tools_response = await self._session.list_tools()
                self._tools_info = tools_response.tools
                self._connected = True

                logger.info(
                    "MCP Server '%s' 连接成功，发现 %d 个工具: %s",
                    self.name,
                    len(self._tools_info),
                    [t.name for t in self._tools_info],
                )
                return True

        except Exception as e:
            logger.error("连接 MCP Server '%s' 失败: %s", self.name, e)
            await self.disconnect()
            return False

    async def _connect_stdio(self) -> bool:
        """建立 stdio 传输连接（本地子进程）。"""
        params = StdioServerParameters(
            command=self.config["command"],
            args=self.config.get("args", []),
            env=self._merge_env(),
        )

        read, write = await self._exit_stack.enter_async_context(
            stdio_client(params)
        )
        self._session = await self._exit_stack.enter_async_context(
            ClientSession(read, write)
        )
        await self._session.initialize()
        return True

    async def _connect_http(self) -> bool:
        """建立 HTTP/SSE 传输连接（远程服务）。"""
        try:
            from mcp.client.sse import sse_client
        except ImportError:
            raise ImportError(
                "HTTP 传输需要 mcp[cli] 依赖，请运行: pip install 'mcp[cli]'"
            )

        url = self.config["url"]
        headers = self.config.get("headers", {})
        timeout = self.config.get("timeout", 30)

        read, write = await self._exit_stack.enter_async_context(
            sse_client(url, headers=headers, timeout=timeout)
        )
        self._session = await self._exit_stack.enter_async_context(
            ClientSession(read, write)
        )
        await self._session.initialize()
        return True

    def _merge_env(self) -> Optional[dict[str, str]]:
        """合并环境变量：系统环境 + 配置中指定的 env。"""
        env = self.config.get("env", {})
        if env:
            merged = dict(os.environ)
            for key, value in env.items():
                if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
                    var_name = value[2:-1]
                    merged[key] = os.environ.get(var_name, "")
                else:
                    merged[key] = str(value)
            return merged
        return None

    async def disconnect(self):
        """断开连接并清理所有资源。"""
        if self._exit_stack:
            try:
                await self._exit_stack.aclose()
            except Exception as e:
                logger.warning("断开 MCP Server '%s' 连接时出错: %s", self.name, e)

        self._session = None
        self._exit_stack = None
        self._connected = False
        self._tools_info = []

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """调用 MCP Server 上的工具。"""
        if not self._connected or not self._session:
            raise RuntimeError(f"MCP Server '{self.name}' 未连接")

        logger.debug("调用工具 '%s' 参数: %s", tool_name, arguments)

        try:
            result = await self._session.call_tool(tool_name, arguments)
            return result
        except Exception as e:
            logger.error("调用工具 '%s' 失败: %s", tool_name, e)
            raise

    def get_tools_info(self) -> list[MCPToolInfo]:
        """获取工具元数据列表（缓存在连接时获取的结果）。"""
        return self._tools_info.copy()

    def is_connected(self) -> bool:
        """检查连接状态。"""
        return self._connected