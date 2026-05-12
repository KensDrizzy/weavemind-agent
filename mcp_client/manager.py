"""MCPManager — 多 MCP Server 管理器，负责连接初始化、工具聚合和生命周期管理。"""

import asyncio
import logging
from typing import Dict, List, Optional

from mcp_client.client import MCPConnection
from mcp_client.tools import create_mcp_tool_instance
from tools.base import WeaveMindTool

logger = logging.getLogger(__name__)


class MCPManager:
    """
    MCP Server 管理器。

    管理多个 MCP Server 连接，聚合所有可用工具，
    并将它们转换为 WeaveMindTool 实例供 ToolRegistry 注册。
    """

    def __init__(self, servers_config: Optional[dict] = None):
        if servers_config is None:
            import settings
            servers_config = settings.get("mcp.servers", {})
            self._servers_config_read = True
        else:
            self._servers_config_read = False

        self._servers_config = servers_config

        # Runtime state
        self._connections: Dict[str, MCPConnection] = {}
        self._tools: List[WeaveMindTool] = []
        self._initialized = False
        self._init_lock = asyncio.Lock()
        self._chrome_launcher = None

    async def initialize(self) -> bool:
        """
        初始化所有 MCP Server 连接。

        逐个连接配置中的 Server，连接失败的 Server 不影响其他 Server。
        连接成功后自动注册该 Server 提供的所有工具。

        Returns:
            bool: 是否至少成功连接一个 Server
        """
        async with self._init_lock:
            if self._initialized:
                return True

            if not self._servers_config:
                logger.info("未配置 MCP Server，跳过初始化")
                self._initialized = True
                return True

            # 检查总开关（仅当配置从 settings 读取时才检查，显式传入时跳过检查）
            if self._servers_config_read:
                import settings
                if not settings.get("mcp.enabled", True):
                    logger.info("MCP 功能已禁用（mcp.enabled=false），跳过初始化")
                    self._initialized = True
                    return True

            logger.info("开始初始化 %d 个 MCP Server", len(self._servers_config))

            success_count = 0
            for name, config in self._servers_config.items():
                config["name"] = name

                if not config.get("enabled", True):
                    logger.info("MCP Server '%s' 已禁用，跳过", name)
                    continue

                # Chrome 自动启动：检查 chrome 子配置的 auto_start
                chrome_config = config.get("chrome")
                if isinstance(chrome_config, dict) and chrome_config.get("auto_start", False):
                    self._ensure_chrome_running(chrome_config)

                try:
                    conn = MCPConnection(config)
                    success = await conn.connect()

                    if success:
                        self._connections[name] = conn
                        success_count += 1

                        for tool_info in conn.get_tools_info():
                            try:
                                tool_instance = create_mcp_tool_instance(tool_info, conn)
                                self._tools.append(tool_instance)
                                logger.debug("注册 MCP 工具: %s", tool_info.name)
                            except Exception as e:
                                logger.error("创建工具 '%s' 失败: %s", tool_info.name, e)
                    else:
                        logger.warning("MCP Server '%s' 连接失败", name)

                except Exception as e:
                    logger.error("初始化 MCP Server '%s' 时出错: %s", name, e)

            self._initialized = True

            if success_count > 0:
                logger.info(
                    "MCP 初始化完成: %d/%d Server 已连接，共 %d 个工具可用",
                    success_count,
                    len(self._servers_config),
                    len(self._tools),
                )
            else:
                logger.warning("没有可用的 MCP Server")

            return success_count > 0

    async def shutdown(self):
        """关闭所有 MCP Server 连接并清理资源。"""
        if not self._initialized:
            return

        logger.info("正在关闭 MCP 连接...")

        for name, conn in list(self._connections.items()):
            try:
                await conn.disconnect()
                logger.debug("已断开 MCP Server '%s'", name)
            except Exception as e:
                logger.error("断开 '%s' 时出错: %s", name, e)

        self._connections.clear()
        self._tools.clear()
        self._initialized = False

        # 停止由启动器启动的 Chrome
        if self._chrome_launcher and self._chrome_launcher.launched_by_us:
            logger.info("正在停止由 MCPManager 启动的 Chrome...")
            self._chrome_launcher.stop()
            self._chrome_launcher = None

        logger.info("MCP 连接已清理")

    def get_tools(self) -> List[WeaveMindTool]:
        """获取所有可用的 MCP 工具实例。"""
        return self._tools.copy()

    def get_tools_info(self) -> dict[str, list[str]]:
        """获取工具信息摘要: {server_name: [tool_name1, ...]}"""
        info = {}
        for name, conn in self._connections.items():
            info[name] = [t.name for t in conn.get_tools_info()]
        return info

    def get_connection(self, name: str) -> Optional[MCPConnection]:
        """获取指定名称的 MCP Server 连接。"""
        return self._connections.get(name)

    def is_initialized(self) -> bool:
        """检查是否已完成初始化。"""
        return self._initialized

    def _ensure_chrome_running(self, chrome_config: dict) -> None:
        """确保 Chrome 在调试端口运行，未运行则自动启动。

        幂等操作：Chrome 已运行时直接返回，不会重复启动。
        仅停止由本启动器启动的 Chrome（launched_by_us）。
        """
        from mcp_client.chrome_launcher import ChromeLauncher

        port = chrome_config.get("port", 9222)
        headless = chrome_config.get("headless", False)
        executable = chrome_config.get("executable")

        if self._chrome_launcher is None:
            self._chrome_launcher = ChromeLauncher(
                port=port,
                headless=headless,
                executable=executable,
            )

        if not self._chrome_launcher.is_running():
            logger.info("Chrome 未运行，正在自动启动 (port=%d, headless=%s)", port, headless)
            success = self._chrome_launcher.start()
            if not success:
                logger.warning("Chrome 自动启动失败，MCP Server 可能无法连接")
        else:
            logger.info("Chrome 已在端口 %d 运行，跳过启动", port)

    async def health_check(self) -> dict[str, bool]:
        """检查所有连接的健康状态。"""
        results = {}
        for name, conn in self._connections.items():
            try:
                if conn._session:
                    await conn._session.send_ping()
                    results[name] = True
                else:
                    results[name] = False
            except Exception:
                results[name] = False
        return results