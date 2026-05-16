"""权限策略 — 控制工具调用的访问权限。"""

from permissions.modes import PermissionMode, EDIT_TOOLS, DANGEROUS_TOOLS, TOOLS_NEED_CONFIRMATION, CHROME_MODIFY_TOOLS, CHROME_DANGEROUS_TOOLS
import settings


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
            # 自动接受编辑，但 Bash 仍需确认
            return tool_name in DANGEROUS_TOOLS
        # DEFAULT 模式：所有危险工具都需要确认
        return tool_name in TOOLS_NEED_CONFIRMATION

    def needs_chrome_confirmation(self, tool_name: str, url: str = "") -> tuple:
        """
        检查 Chrome 工具是否需要额外确认（敏感页面保护）。

        Returns:
            (是否需要确认, 确认提示信息)
        """
        if not self._browser_guard:
            return False, None

        # 只对 Chrome 写型工具和危险工具做敏感页面检查
        if tool_name not in (CHROME_MODIFY_TOOLS | CHROME_DANGEROUS_TOOLS):
            return False, None

        return self._browser_guard.needs_confirmation(tool_name, url)