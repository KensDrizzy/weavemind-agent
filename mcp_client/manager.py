"""MCPManager — 多 MCP Server 管理器，负责连接初始化、工具聚合、生命周期管理和模式切换。"""

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

    支持 Chrome DevTools MCP Server 的 isolated/shared 双模式切换：
    - isolated: --isolated 参数，MCP Server 自行管理临时 Chrome 实例
    - shared: --autoConnect 参数，连接用户已登录的 Chrome（Chrome 144+）
    切换时重启 MCP Server 并重新注册工具。
    """

    # Chrome DevTools MCP Server 启动参数模板
    ISOLATED_ARGS = ["-y", "chrome-devtools-mcp@latest", "--isolated"]
    SHARED_AUTOCONNECT_ARGS = ["-y", "chrome-devtools-mcp@latest", "--autoConnect"]
    SHARED_BROWSER_URL_ARGS = ["-y", "chrome-devtools-mcp@latest", "--browserUrl", "http://localhost:9222"]

    @staticmethod
    def _get_chrome_user_data_dir() -> Optional[str]:
        """获取 Chrome 默认 user-data-dir 路径（平台相关）。"""
        import platform
        from pathlib import Path
        system = platform.system()
        if system == "Darwin":
            p = Path.home() / "Library/Application Support/Google/Chrome"
        elif system == "Linux":
            p = Path.home() / ".config/google-chrome"
        elif system == "Windows":
            import os
            local_app = os.environ.get("LOCALAPPDATA", "")
            p = Path(local_app) / "Google/Chrome/User Data" if local_app else None
        else:
            return None
        if p and p.exists():
            return str(p)
        return None

    @staticmethod
    def _read_devtools_active_port() -> Optional[tuple]:
        """读取 DevToolsActivePort 文件，返回 (port, ws_path) 或 None。"""
        import platform
        from pathlib import Path
        system = platform.system()
        if system == "Darwin":
            port_file = Path.home() / "Library/Application Support/Google/Chrome/DevToolsActivePort"
        elif system == "Linux":
            port_file = Path.home() / ".config/google-chrome/DevToolsActivePort"
        elif system == "Windows":
            import os
            local_app = os.environ.get("LOCALAPPDATA", "")
            port_file = Path(local_app) / "Google/Chrome/User Data/DevToolsActivePort" if local_app else None
        else:
            return None
        if not port_file or not port_file.exists():
            return None
        try:
            content = port_file.read_text().strip()
            lines = content.split("\n")
            if len(lines) >= 2:
                port = int(lines[0].strip())
                ws_path = lines[1].strip()
                return (port, ws_path)
        except Exception:
            pass
        return None

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
        self._tool_registry = None  # 由 app.py 在初始化后设置

        # Chrome DevTools 模式状态
        self._chrome_mode: str = "isolated"  # isolated | shared
        self._chrome_server_name: Optional[str] = None  # 配置中的 Chrome server 名称
        self._chrome_original_args: Optional[list] = None  # 原始启动参数（用于回滚）
        self._browser_guard = None
        self._mcp_loop: Optional[asyncio.AbstractEventLoop] = None  # 持久后台事件循环（由 app.py 注入）
        self._last_restart_error: Optional[str] = None  # 最后一次重启错误信息

        self._init_browser_guard()

    def set_mcp_loop(self, loop: asyncio.AbstractEventLoop):
        """设置持久后台事件循环（由 app.py 在 _init_mcp_sync 中注入）。

        browser_connect/browser_disconnect 的 sync_func 需要通过
        run_coroutine_threadsafe 在此循环上执行异步操作，
        避免 asyncio.run() 创建新循环破坏 MCP stdio 连接。
        """
        self._mcp_loop = loop

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

            # 检查总开关
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

                try:
                    conn = MCPConnection(config)
                    success = await conn.connect()

                    if success:
                        self._connections[name] = conn
                        success_count += 1

                        # 检测 Chrome DevTools MCP Server
                        if conn.server_type == "chrome":
                            self._chrome_server_name = name
                            self._chrome_original_args = config.get("args", self.ISOLATED_ARGS)
                            # 根据启动参数判断初始模式
                            if "--isolated" in self._chrome_original_args:
                                self._chrome_mode = "isolated"
                            elif "--autoConnect" in self._chrome_original_args:
                                self._chrome_mode = "shared"
                            else:
                                self._chrome_mode = "isolated"
                            logger.info("检测到 Chrome DevTools MCP Server '%s'，初始模式: %s",
                                        name, self._chrome_mode)

                        for tool_info in conn.get_tools_info():
                            try:
                                tool_instance = create_mcp_tool_instance(
                                    tool_info, conn, mcp_manager=self
                                )
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

    # ── Chrome DevTools 模式切换 ──────────────────────────────────

    def _init_browser_guard(self):
        """初始化 BrowserGuard（敏感页面保护）。"""
        import settings
        guard_enabled = settings.get("browser_guard.enabled", True)
        if guard_enabled:
            custom_patterns_file = settings.get(
                "browser_guard.custom_patterns_file",
                ".weavemind/sensitive_patterns.txt",
            )
            from mcp_client.browser_guard import BrowserGuard
            self._browser_guard = BrowserGuard(custom_patterns_file=custom_patterns_file)
            logger.info("BrowserGuard 已初始化（敏感页面保护）")

    def get_browser_guard(self):
        """获取 BrowserGuard 实例。"""
        return self._browser_guard

    def get_chrome_mode(self) -> str:
        """获取当前 Chrome DevTools 模式。"""
        return self._chrome_mode

    def is_shared_mode(self) -> bool:
        """是否处于 shared 模式。"""
        return self._chrome_mode == "shared"

    def is_isolated_mode(self) -> bool:
        """是否处于 isolated 模式。"""
        return self._chrome_mode == "isolated"

    async def switch_to_shared(self) -> bool:
        """切换到 shared 模式（连接用户 Chrome，继承登录态）。

        流程：
        1. 读取 DevToolsActivePort 文件获取端口和 WebSocket 路径
        2. 用 --wsEndpoint 参数通过 WebSocket 连接用户 Chrome
        3. Chrome 会弹出"允许远程调试"确认对话框，用户需点击"允许"
        4. 连接建立后重新注册工具
        5. 失败时回滚到原参数

        Returns:
            bool: 是否成功切换
        """
        if not self._chrome_server_name:
            logger.warning("未检测到 Chrome DevTools MCP Server，无法切换")
            return False

        if self._chrome_mode == "shared":
            logger.debug("已在 shared 模式，无需切换")
            return True

        # 读取 DevToolsActivePort 文件
        port_info = self._read_devtools_active_port()
        if port_info:
            port, ws_path = port_info
            ws_endpoint = f"ws://127.0.0.1:{port}{ws_path}"
            logger.info("从 DevToolsActivePort 读取到 WebSocket: %s", ws_endpoint)
            args = ["-y", "chrome-devtools-mcp@latest", "--wsEndpoint", ws_endpoint]
        else:
            # DevToolsActivePort 不存在，尝试用 --autoConnect + --userDataDir
            user_data_dir = self._get_chrome_user_data_dir()
            if user_data_dir:
                logger.info("DevToolsActivePort 不存在，尝试 autoConnect + userDataDir: %s", user_data_dir)
                args = ["-y", "chrome-devtools-mcp@latest", "--autoConnect", "--userDataDir", user_data_dir]
            else:
                # 最后回退到纯 autoConnect
                logger.info("尝试纯 autoConnect 模式...")
                args = self.SHARED_AUTOCONNECT_ARGS

        logger.info("正在切换 Chrome DevTools MCP Server 到 shared 模式...")
        return await self._restart_chrome_server(args, "shared")

    async def switch_to_isolated(self) -> bool:
        """切换回 isolated 模式（独立浏览器，无登录态）。

        Returns:
            bool: 是否成功切换
        """
        if not self._chrome_server_name:
            logger.warning("未检测到 Chrome DevTools MCP Server，无法切换")
            return False

        if self._chrome_mode == "isolated":
            logger.debug("已在 isolated 模式，无需切换")
            return True

        logger.info("正在切换 Chrome DevTools MCP Server 回 isolated 模式...")
        return await self._restart_chrome_server(self.ISOLATED_ARGS, "isolated")

    async def _restart_chrome_server(self, new_args: list, new_mode: str) -> bool:
        """重启 Chrome DevTools MCP Server 使用新的参数。

        流程：
        1. 保存当前参数（用于回滚）
        2. 断开现有连接
        3. 以新参数重新连接
        4. 重新注册工具到 MCPManager 和 ToolRegistry
        5. 失败时回滚

        Args:
            new_args: 新的启动参数列表
            new_mode: 新的模式标识（isolated/shared）

        Returns:
            bool: 是否成功重启
        """
        server_name = self._chrome_server_name
        old_conn = self._connections.get(server_name)
        if not old_conn:
            logger.warning("未找到 Chrome MCP 连接，无法重启")
            return False

        # 保存回滚参数
        rollback_args = old_conn.config.get("args", self.ISOLATED_ARGS)
        rollback_mode = self._chrome_mode

        # 断开现有连接
        try:
            await old_conn.disconnect()
        except Exception as e:
            logger.warning("断开 Chrome MCP 连接时出错: %s", e)

        # 构建新配置
        new_config = old_conn.config.copy()
        new_config["args"] = new_args

        # 重新连接
        new_conn = MCPConnection(new_config)
        error_detail = None
        try:
            success = await new_conn.connect()
            if not success:
                error_detail = new_conn.get_last_error()
        except Exception as e:
            logger.error("重新连接 Chrome MCP Server 失败: %s", e)
            error_detail = str(e)
            success = False
        
        if not success:
            # 回滚
            await self._rollback_chrome_server(rollback_args, rollback_mode)
            if error_detail:
                logger.error("Chrome MCP Server 重启失败: %s", error_detail)
                self._last_restart_error = error_detail
            return False
        
        # 成功时清除错误信息
        self._last_restart_error = None
        
        # 更新连接
        self._connections[server_name] = new_conn
        self._chrome_mode = new_mode

        # 重新注册工具
        await self._re_register_chrome_tools(new_conn)
        logger.info("Chrome MCP Server 已重启为 %s 模式，发现 %d 个工具",
                    new_mode, len(new_conn.get_tools_info()))
        return True

    async def _rollback_chrome_server(self, rollback_args: list, rollback_mode: str) -> bool:
        """回滚 Chrome MCP Server 到之前的参数。"""
        server_name = self._chrome_server_name
        conn = self._connections.get(server_name)
        if not conn:
            return False

        try:
            await conn.disconnect()
        except Exception:
            pass

        rollback_config = conn.config.copy()
        rollback_config["args"] = rollback_args

        new_conn = MCPConnection(rollback_config)
        try:
            success = await new_conn.connect()
        except Exception as e:
            logger.error("回滚 Chrome MCP Server 失败: %s", e)
            return False

        if success:
            self._connections[server_name] = new_conn
            self._chrome_mode = rollback_mode
            await self._re_register_chrome_tools(new_conn)
            logger.info("已回滚 Chrome MCP Server 到 %s 模式", rollback_mode)
            return True

        return False

    async def _re_register_chrome_tools(self, conn: MCPConnection):
        """重新注册 Chrome 工具到 MCPManager 和 ToolRegistry。

        切换模式后 MCP Server 重启，需要：
        1. 更新 MCPManager._tools 中的 Chrome 工具
        2. 更新 ToolRegistry._tools 中的 Chrome 工具
        """
        from mcp_client.chrome_formatter import is_chrome_tool

        # 清除 MCPManager 中的旧 Chrome 工具
        self._tools = [
            t for t in self._tools
            if not is_chrome_tool(getattr(t, 'name', ''))
        ]

        # 注册新工具到 MCPManager
        for tool_info in conn.get_tools_info():
            try:
                tool_instance = create_mcp_tool_instance(
                    tool_info, conn, mcp_manager=self
                )
                self._tools.append(tool_instance)
            except Exception as e:
                logger.error("注册工具 '%s' 失败: %s", tool_info.name, e)

        # 同步更新 ToolRegistry
        tool_registry = self._tool_registry
        if tool_registry:
            # 移除旧 Chrome 工具
            chrome_names = [name for name in tool_registry._tools
                           if is_chrome_tool(name)]
            for name in chrome_names:
                del tool_registry._tools[name]

            # 注册新 Chrome 工具
            for tool_info in conn.get_tools_info():
                if is_chrome_tool(tool_info.name):
                    try:
                        tool_instance = create_mcp_tool_instance(
                            tool_info, conn, mcp_manager=self
                        )
                        tool_registry._tools[tool_info.name] = tool_instance
                    except Exception as e:
                        logger.error("注册工具 '%s' 到 ToolRegistry 失败: %s",
                                     tool_info.name, e)

            logger.info("ToolRegistry Chrome 工具已更新，当前 %d 个工具",
                        len(tool_registry._tools))

    def check_browser_tool(
        self,
        tool_name: str,
        url: str = "",
        page_id: str = "",
    ) -> tuple:
        """检查浏览器工具调用是否被允许。"""
        if not self._browser_guard:
            return True, None

        is_agent_page = False  # 由 apply_after_execution 维护
        return self._browser_guard.check_tool_use(tool_name, url, is_agent_page)

    def needs_browser_confirmation(self, tool_name: str, url: str) -> tuple:
        """检查浏览器工具是否需要用户确认。"""
        if not self._browser_guard:
            return False, None
        return self._browser_guard.needs_confirmation(tool_name, url)

    def apply_browser_after_execution(self, tool_name: str, args: dict, result: str):
        """浏览器工具执行后更新状态。"""
        if self._browser_guard:
            self._browser_guard.apply_after_execution(tool_name, args, result)

    def detect_need_login(self, page_content: str, url: str = "") -> bool:
        """检测页面是否需要登录。"""
        if not self._browser_guard:
            return False
        return self._browser_guard.detect_login_page(page_content, url)

    def get_browser_status_text(self) -> str:
        """获取浏览器状态文本（供 browser_status 工具和 /browser 命令使用）。"""
        mode_label = {
            "isolated": "isolated (独立浏览器，无登录态)",
            "shared": "shared (连接用户 Chrome，有登录态)",
        }.get(self._chrome_mode, self._chrome_mode)

        lines = [f"当前模式: {mode_label}"]

        # MCP Server 状态
        conn = self._connections.get(self._chrome_server_name or "")
        if conn and conn.is_connected():
            tool_count = len(conn.get_tools_info())
            lines.append(f"chrome-devtools server: ● ready ({tool_count} tools)")
        else:
            lines.append("chrome-devtools server: ✗ 未连接")

        # Chrome 调试端口探活
        from mcp_client.chrome_launcher import ChromeLauncher
        launcher = ChromeLauncher()
        if launcher.is_running():
            lines.append(f"旧式 /json/version 探活: ✅ http://localhost:9222")
        else:
            lines.append("旧式 /json/version 探活: ⚠️ 未检测到")

        # BrowserGuard 信息
        if self._browser_guard:
            pattern_count = len(self._browser_guard._patterns)
            lines.append(f"敏感页面规则: {pattern_count} 条")
            last_url = self._browser_guard.last_navigated_url
            if last_url:
                lines.append(f"最近导航: {last_url}")

        lines.append("自动连接: Chrome 144+ 可在 chrome://inspect/#remote-debugging 勾选后使用 browser_connect")

        return "\n".join(lines)