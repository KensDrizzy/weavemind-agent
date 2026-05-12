"""MCP 客户端模块 — Model Context Protocol 客户端框架。

提供 MCP Server 连接管理、工具动态封装和多 Server 聚合能力。

使用方式:
    from mcp_client import MCPManager, MCPConnection

    manager = MCPManager()
    await manager.initialize()
    tools = manager.get_tools()  # List[WeaveMindTool]
"""

from mcp_client.manager import MCPManager
from mcp_client.client import MCPConnection

__all__ = ["MCPManager", "MCPConnection"]