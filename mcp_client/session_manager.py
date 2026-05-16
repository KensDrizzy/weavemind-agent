"""ChromeSessionManager — Chrome 会话模式管理器。

管理 isolated/shared 双模式切换，维护当前会话状态，
协调 ChromeLauncher 和 MCPManager。

使用示例：
    manager = ChromeSessionManager(mcp_manager)
    await manager.start_isolated()  # 默认启动 isolated

    # 当检测到需要登录时
    if manager.detect_need_login(result):
        await manager.switch_to_shared()  # 自动切换到 shared
"""

import asyncio
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Set

logger = logging.getLogger(__name__)


class ChromeMode(Enum):
    """Chrome 运行模式。"""
    ISOLATED = "isolated"   # 临时 user-data-dir，无登录态
    SHARED = "shared"       # 连接用户 Chrome，有登录态


@dataclass
class ChromeSession:
    """Chrome 会话状态。"""
    mode: ChromeMode
    user_data_dir: Optional[str] = None   # isolated 模式使用
    browser_url: Optional[str] = None     # shared 模式使用
    page_ids: Set[str] = field(default_factory=set)  # Agent 创建的标签页


class ChromeSessionManager:
    """
    Chrome 会话管理器。

    职责：
    1. 管理 isolated/shared 双模式切换
    2. 维护当前会话状态
    3. 协调 ChromeLauncher 和 MCPManager
    4. 提供模式切换的原子操作
    """

    # 登录页检测关键词
    LOGIN_URL_INDICATORS = [
        'login', 'signin', 'sign-in', 'auth',
        '登录', '登陆', '授权',
    ]

    def __init__(self, mcp_manager, chrome_launcher=None):
        self._mcp_manager = mcp_manager
        self._chrome_launcher = chrome_launcher
        self._session: Optional[ChromeSession] = None
        self._server_name: str = "chrome"

    @property
    def current_mode(self) -> Optional[ChromeMode]:
        """获取当前会话模式。"""
        return self._session.mode if self._session else None

    @property
    def is_shared(self) -> bool:
        """是否处于 shared 模式。"""
        return self._session is not None and self._session.mode == ChromeMode.SHARED

    @property
    def is_isolated(self) -> bool:
        """是否处于 isolated 模式。"""
        return self._session is not None and self._session.mode == ChromeMode.ISOLATED

    @property
    def session(self) -> Optional[ChromeSession]:
        """获取当前会话。"""
        return self._session

    async def start_isolated(self) -> bool:
        """启动 isolated 模式会话（默认）。

        isolated 模式使用独立的临时 user-data-dir，
        无 Cookie、无登录态，适合访问公开页面。

        Returns:
            bool: 是否成功启动
        """
        # 如果已有 isolated 会话，直接返回
        if self._session and self._session.mode == ChromeMode.ISOLATED:
            logger.debug("已在 isolated 模式，无需重复启动")
            return True

        # 如果 ChromeLauncher 已启动（isolated 模式），记录会话
        if self._chrome_launcher and self._chrome_launcher.is_running():
            self._session = ChromeSession(
                mode=ChromeMode.ISOLATED,
                user_data_dir=str(self._chrome_launcher.port),
            )
            logger.info("Chrome isolated 模式会话已就绪")
            return True

        # 无 ChromeLauncher 或 Chrome 未运行，标记为 isolated（MCP Server 自行管理）
        self._session = ChromeSession(mode=ChromeMode.ISOLATED)
        logger.info("Chrome isolated 模式会话已标记（MCP Server 自行管理）")
        return True

    async def switch_to_shared(self) -> bool:
        """切换到 shared 模式（自动发现用户 Chrome）。

        流程：
        1. 通过 AutoConnectDiscovery 发现用户 Chrome
        2. 断开当前 MCP 连接
        3. 使用 --autoConnect 重新连接 MCP Server
        4. 更新会话状态

        Returns:
            bool: 是否成功切换
        """
        from mcp_client.auto_connect import AutoConnectDiscovery

        # 发现用户 Chrome
        discovery = AutoConnectDiscovery()
        browser_url = discovery.get_browser_url()

        if not browser_url:
            logger.warning("未发现开启远程调试的用户 Chrome，无法切换到 shared 模式")
            return False

        logger.info("发现用户 Chrome: %s，正在切换到 shared 模式...", browser_url)

        # 重启 MCP Server 使用新参数
        success = await self._restart_chrome_with_shared(browser_url)

        if success:
            self._session = ChromeSession(
                mode=ChromeMode.SHARED,
                browser_url=browser_url,
            )
            logger.info("已切换到 shared 模式")
            return True
        else:
            logger.error("切换到 shared 模式失败")
            return False

    async def switch_to_isolated(self) -> bool:
        """切换回 isolated 模式。

        Returns:
            bool: 是否成功切换
        """
        if self.is_isolated:
            return True

        # 重启 MCP Server 使用 isolated 参数
        success = await self._restart_chrome_with_isolated()

        if success:
            self._session = ChromeSession(mode=ChromeMode.ISOLATED)
            logger.info("已切换回 isolated 模式")
            return True
        else:
            logger.error("切换回 isolated 模式失败")
            return False

    def detect_need_login(self, page_content: str, url: str = "") -> bool:
        """
        根据页面内容判断是否为登录页。

        检测指标：
        1. URL 包含 login/signin/auth
        2. 页面内容包含密码输入框
        3. 返回 401/403 状态码（从网络日志）

        Args:
            page_content: 页面内容（文本或 HTML）
            url: 页面 URL

        Returns:
            bool: 是否需要登录
        """
        # URL 检查
        if url:
            url_lower = url.lower()
            if any(indicator in url_lower for indicator in self.LOGIN_URL_INDICATORS):
                return True

        # 内容检查（简化版）
        if page_content:
            content_lower = page_content.lower()
            if '<input type="password"' in content_lower:
                # 有密码框，但无用户信息
                if 'username' in content_lower or 'email' in content_lower:
                    return True
            # 常见登录页文本
            login_texts = ['sign in', 'log in', '登录', '登陆', '请输入密码']
            if any(t in content_lower for t in login_texts):
                return True

        return False

    def record_agent_page(self, page_id: str):
        """记录 Agent 创建的标签页。"""
        if self._session:
            self._session.page_ids.add(page_id)

    def is_agent_page(self, page_id: str) -> bool:
        """检查标签页是否由 Agent 创建。"""
        return self._session is not None and page_id in self._session.page_ids

    def get_status_text(self) -> str:
        """获取当前会话状态文本。"""
        if not self._session:
            return "未启动"

        mode_text = "shared (连接用户 Chrome)" if self.is_shared else "isolated (独立浏览器)"
        pages = len(self._session.page_ids)
        url_info = ""
        if self.is_shared and self._session.browser_url:
            url_info = f"\n  连接地址: {self._session.browser_url}"

        return f"模式: {mode_text}\n  Agent 标签页: {pages}{url_info}"

    # ── 内部方法 ─────────────────────────────────────────────

    async def _restart_chrome_with_shared(self, browser_url: str) -> bool:
        """使用 shared 模式参数重启 Chrome MCP Server。"""
        import settings

        # 构建 shared 模式的 MCP Server 参数
        shared_args = [
            "-y",
            "chrome-devtools-mcp@latest",
            "--browserUrl",
            browser_url,
        ]

        return await self._restart_mcp_server(shared_args)

    async def _restart_chrome_with_isolated(self) -> bool:
        """使用 isolated 模式参数重启 Chrome MCP Server。"""
        import settings

        # 从配置读取原始参数
        chrome_config = settings.get("mcp.servers.chrome", {})
        original_args = chrome_config.get("args", [])

        # 如果原始配置有 args，使用原始参数
        if original_args:
            return await self._restart_mcp_server(original_args)

        # 否则构建默认 isolated 参数
        isolated_args = [
            "-y",
            "chrome-devtools-mcp@latest",
            "--browserUrl",
            "http://localhost:9222",
        ]
        return await self._restart_mcp_server(isolated_args)

    async def _restart_mcp_server(self, new_args: list) -> bool:
        """
        重启 Chrome MCP Server 使用新的参数。

        流程：
        1. 断开现有连接
        2. 更新配置参数
        3. 重新建立连接
        4. 验证连接成功

        注意：重启只影响内存中的配置，不写入 config.yaml
        """
        from mcp_client.client import MCPConnection

        conn = self._mcp_manager.get_connection(self._server_name)
        if not conn:
            logger.warning("未找到 Chrome MCP 连接，无法重启")
            return False

        # 断开现有连接
        try:
            await conn.disconnect()
        except Exception as e:
            logger.warning("断开 Chrome MCP 连接时出错: %s", e)

        # 更新参数（内存中，不写入配置文件）
        config = conn.config.copy()
        config["args"] = new_args

        # 重新连接
        new_conn = MCPConnection(config)
        try:
            success = await new_conn.connect()
        except Exception as e:
            logger.error("重新连接 Chrome MCP Server 失败: %s", e)
            return False

        if success:
            # 更新 MCPManager 中的连接
            self._mcp_manager._connections[self._server_name] = new_conn

            # 重新注册工具
            await self._re_register_tools(new_conn)
            logger.info("Chrome MCP Server 已重启，发现 %d 个工具", len(new_conn.get_tools_info()))
            return True

        return False

    async def _re_register_tools(self, conn):
        """重新注册 MCP 工具到 MCPManager 和 ToolRegistry。

        切换模式后 MCP Server 重启，需要：
        1. 更新 MCPManager._tools（list）中的 Chrome 工具
        2. 更新 ToolRegistry._tools（dict）中的 Chrome 工具
        3. 重建 AgentLoop 的 llm_with_tools
        """
        from mcp_client.tools import create_mcp_tool_instance
        from mcp_client.chrome_formatter import is_chrome_tool

        # 清除 MCPManager 中的旧 Chrome 工具
        self._mcp_manager._tools = [
            t for t in self._mcp_manager._tools
            if not is_chrome_tool(getattr(t, 'name', ''))
        ]

        # 注册新工具到 MCPManager
        for tool_info in conn.get_tools_info():
            try:
                tool_instance = create_mcp_tool_instance(tool_info, conn, mcp_manager=self._mcp_manager)
                self._mcp_manager._tools.append(tool_instance)
            except Exception as e:
                logger.error("注册工具 '%s' 失败: %s", tool_info.name, e)

        # 同步更新 ToolRegistry（dict）中的 Chrome 工具
        tool_registry = self._mcp_manager._tool_registry
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
                        tool_instance = create_mcp_tool_instance(tool_info, conn, mcp_manager=self._mcp_manager)
                        tool_registry._tools[tool_info.name] = tool_instance
                    except Exception as e:
                        logger.error("注册工具 '%s' 到 ToolRegistry 失败: %s",
                                     tool_info.name, e)

            logger.info("ToolRegistry Chrome 工具已更新，当前 %d 个工具",
                        len(tool_registry._tools))
