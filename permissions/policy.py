"""权限策略 — 控制工具调用的访问权限。"""

from permissions.modes import PermissionMode, EDIT_TOOLS, DANGEROUS_TOOLS, TOOLS_NEED_CONFIRMATION
import settings


class PermissionPolicy:
    def __init__(self, allowed: list[str] = None, disallowed: list[str] = None):
        self.allowed = set(allowed or settings.get("permissions.allowed_tools", []))
        self.disallowed = set(disallowed or settings.get("permissions.disallowed_tools", []))

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