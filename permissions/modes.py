from enum import Enum


class PermissionMode(str, Enum):
    DEFAULT = "default"           # 默认模式，自动判断危险操作并询问用户
    ACCEPT_EDITS = "acceptEdits"  # 自动接受编辑，Bash 仍需确认
    BYPASS = "bypassPermissions"  # 跳过所有检查（危险）
    PERMIT = "permit"             # 仅白名单工具可用


EDIT_TOOLS = {"Write", "Edit"}
DANGEROUS_TOOLS = {"Bash"}

# 需要询问用户的工具（默认模式下）
TOOLS_NEED_CONFIRMATION = DANGEROUS_TOOLS | EDIT_TOOLS
