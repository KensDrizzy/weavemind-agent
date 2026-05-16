"""MCP 客户端模块 — Model Context Protocol 客户端框架。

提供 MCP Server 连接管理、工具动态封装和多 Server 聚合能力，
以及 Chrome DevTools MCP Server 的 isolated/shared 双模式切换。

使用方式:
    from mcp_client import MCPManager, MCPConnection

    manager = MCPManager()
    await manager.initialize()
    tools = manager.get_tools()  # List[WeaveMindTool]
"""

from mcp_client.manager import MCPManager
from mcp_client.client import MCPConnection
from mcp_client.chrome_launcher import ChromeLauncher
from mcp_client.browser_guard import BrowserGuard
from mcp_client.browser_tools import (
    create_browser_connect_tool,
    create_browser_disconnect_tool,
    create_browser_status_tool,
    create_all_browser_tools,
)

__all__ = [
    "MCPManager", "MCPConnection", "ChromeLauncher",
    "BrowserGuard",
    "create_browser_connect_tool", "create_browser_disconnect_tool",
    "create_browser_status_tool", "create_all_browser_tools",
]