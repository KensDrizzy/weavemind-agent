"""ChromeSessionManager — 已废弃。

会话状态管理已合并到 BrowserGuard（last_navigated_url、agent_opened_tabs），
模式切换由 MCPManager.switch_to_shared() / switch_to_isolated() 处理。
保留此文件仅为向后兼容。
"""

import logging
import warnings
from enum import Enum

logger = logging.getLogger(__name__)


class ChromeMode(Enum):
    ISOLATED = "isolated"
    SHARED = "shared"


class ChromeSessionManager:
    """已废弃：会话状态已合并到 BrowserGuard，模式切换由 MCPManager 处理。"""

    def __init__(self, mcp_manager=None, chrome_launcher=None):
        warnings.warn(
            "ChromeSessionManager 已废弃，请使用 MCPManager 和 BrowserGuard",
            DeprecationWarning,
            stacklevel=2,
        )
        self._mcp_manager = mcp_manager
        self._session = None

    @property
    def current_mode(self):
        if self._mcp_manager:
            mode = self._mcp_manager.get_chrome_mode()
            return ChromeMode(mode) if mode else None
        return None

    @property
    def is_shared(self):
        return self._mcp_manager and self._mcp_manager.is_shared_mode()

    @property
    def is_isolated(self):
        return self._mcp_manager and self._mcp_manager.is_isolated_mode()

    async def start_isolated(self):
        return True

    async def switch_to_shared(self):
        if self._mcp_manager:
            return await self._mcp_manager.switch_to_shared()
        return False

    async def switch_to_isolated(self):
        if self._mcp_manager:
            return await self._mcp_manager.switch_to_isolated()
        return False

    def detect_need_login(self, page_content, url=""):
        if self._mcp_manager:
            return self._mcp_manager.detect_need_login(page_content, url)
        return False

    def record_agent_page(self, page_id):
        pass

    def is_agent_page(self, page_id):
        return False

    def get_status_text(self):
        if self._mcp_manager:
            return self._mcp_manager.get_browser_status_text()
        return "未启动"