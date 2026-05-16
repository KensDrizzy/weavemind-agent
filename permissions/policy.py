"""权限策略 — 控制工具调用的访问权限。"""

from permissions.modes import PermissionMode, EDIT_TOOLS, DANGEROUS_TOOLS, TOOLS_NEED_CONFIRMATION
import settings

# browser_connect 是敏感操作（切换到用户已登录的 Chrome），需要确认
# 注：虽然这是自动登录态切换的一部分，但涉及用户真实浏览器，保留确认以增加安全性
# 如需完全自动化，可将 browser_connect 从 BROWSER_CONNECT_TOOLS 移除
BROWSER_CONNECT_TOOLS = {"browser_disconnect"}  # browser_connect 需要明确确认


class PermissionPolicy:
    def __init__(self, allowed: list[str] = None, disallowed: list[str] = None):
        self.allowed = set(allowed or settings.get("permissions.allowed_tools", []))
        self.disallowed = set(disallowed or settings.get("permissions.disallowed_tools", []))
        self._browser_guard = None

    def set_browser_guard(self, browser_guard):
        """设置 BrowserGuard 实例（由 MCPManager 初始化后注入）。"""
        self._browser_guard = browser_guard

    def can_use(self, tool_name: str, mode: str = "default") -> bool:
        """检查工具是否可用（旧接口，保留兼容）。"""
        return self.is_allowed(tool_name, mode)

    def is_allowed(self, tool_name: str, mode: str = "default") -> bool:
        """检查工具是否被允许使用。"""
        if mode == PermissionMode.BYPASS:
            return True
        if tool_name in self.disallowed:
            return False
        if mode == PermissionMode.PERMIT:
            return tool_name in self.allowed
        if self.allowed and tool_name not in self.allowed:
            return False
        return True

    def needs_confirmation(self, tool_name: str, mode: str) -> bool:
        """检查工具是否需要用户确认。"""
        if mode == PermissionMode.BYPASS:
            return False
        if mode == PermissionMode.ACCEPT_EDITS:
            return tool_name in DANGEROUS_TOOLS
        # DEFAULT 模式：危险工具 + 浏览器连接工具需要确认
        return tool_name in TOOLS_NEED_CONFIRMATION or tool_name in BROWSER_CONNECT_TOOLS

    def needs_chrome_confirmation(self, tool_name: str, url: str = "") -> tuple:
        """检查 Chrome 工具是否需要额外确认（敏感页面保护）。"""
        if not self._browser_guard:
            return False, None
        return self._browser_guard.needs_confirmation(tool_name, url)

    def check(self, tool_name: str, args: dict = None) -> tuple:
        """统一权限检查接口。

        Returns:
            (allowed, reason): allowed=True 表示允许
        """
        if not self.is_allowed(tool_name):
            return False, f"工具 {tool_name} 被权限策略拒绝"
        return True, None