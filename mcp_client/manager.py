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
        self._tool_registry = None  # 由 app.py 在初始化后设置

        # CDP 双模式支持
        self._session_manager = None
        self._browser_guard = None
        self._init_cdp_components()

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
                                tool_instance = create_mcp_tool_instance(tool_info, conn, mcp_manager=self)
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

                # 初始化 ChromeSessionManager（需要 MCP 连接已建立）
                if "chrome" in self._connections:
                    self._create_session_manager()
                    # 启动 isolated 模式会话
                    if self._session_manager:
                        await self._session_manager.start_isolated()
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

    # ── CDP 双模式支持 ────────────────────────────────────────

    def _init_cdp_components(self):
        """初始化 CDP 双模式组件（BrowserGuard + ChromeSessionManager）。"""
        import settings

        # BrowserGuard：敏感页面保护
        guard_enabled = settings.get("browser_guard.enabled", True)
        if guard_enabled:
            custom_patterns_file = settings.get(
                "browser_guard.custom_patterns_file",
                ".weavemind/sensitive_patterns.txt",
            )
            from mcp_client.browser_guard import BrowserGuard
            self._browser_guard = BrowserGuard(custom_patterns_file=custom_patterns_file)
            logger.info("BrowserGuard 已初始化（敏感页面保护）")

        # ChromeSessionManager：在 initialize() 完成后创建（需要 MCP 连接）

    def get_browser_guard(self):
        """获取 BrowserGuard 实例。"""
        return self._browser_guard

    def get_session_manager(self):
        """获取 ChromeSessionManager 实例。"""
        return self._session_manager

    def _create_session_manager(self):
        """创建 ChromeSessionManager（在 MCP 初始化完成后调用）。"""
        if self._session_manager:
            return

        from mcp_client.session_manager import ChromeSessionManager
        self._session_manager = ChromeSessionManager(
            mcp_manager=self,
            chrome_launcher=self._chrome_launcher,
        )
        logger.info("ChromeSessionManager 已创建")

    async def switch_to_shared(self) -> bool:
        """切换到 shared 模式（连接用户 Chrome，继承登录态）。"""
        if not self._session_manager:
            self._create_session_manager()
        return await self._session_manager.switch_to_shared()

    async def switch_to_isolated(self) -> bool:
        """切换回 isolated 模式（独立浏览器，无登录态）。"""
        if not self._session_manager:
            self._create_session_manager()
        return await self._session_manager.switch_to_isolated()

    def get_chrome_mode(self) -> Optional[str]:
        """获取当前 Chrome 运行模式。"""
        if self._session_manager:
            mode = self._session_manager.current_mode
            return mode.value if mode else None
        return "isolated"  # 默认

    def is_shared_mode(self) -> bool:
        """是否处于 shared 模式。"""
        return bool(self._session_manager and self._session_manager.is_shared)

    def check_browser_tool(
        self,
        tool_name: str,
        url: str = "",
        page_id: str = "",
    ) -> tuple:
        """
        检查浏览器工具调用是否被允许。

        Args:
            tool_name: 工具名称
            url: 当前页面 URL
            page_id: 标签页 ID

        Returns:
            (是否允许, 阻止原因)
        """
        if not self._browser_guard:
            return True, None

        is_agent_page = (
            self._session_manager and
            self._session_manager.is_agent_page(page_id)
        )

        return self._browser_guard.check_tool_use(tool_name, url, is_agent_page)

    def needs_browser_confirmation(self, tool_name: str, url: str) -> tuple:
        """检查浏览器工具是否需要用户确认。"""
        if not self._browser_guard:
            return False, None
        return self._browser_guard.needs_confirmation(tool_name, url)

    def record_agent_page(self, page_id: str):
        """记录 Agent 创建的标签页。"""
        if self._session_manager:
            self._session_manager.record_agent_page(page_id)

    def detect_need_login(self, page_content: str, url: str = "") -> bool:
        """检测页面是否需要登录。"""
        if not self._session_manager:
            return False
        return self._session_manager.detect_need_login(page_content, url)