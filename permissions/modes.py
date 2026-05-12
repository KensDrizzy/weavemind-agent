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

# Chrome DevTools 工具风险分类
CHROME_SAFE_TOOLS = {
    "list_pages", "take_screenshot", "take_snapshot",
    "list_console_messages", "get_console_message",
    "list_network_requests", "get_network_request",
    "performance_analyze_insight", "lighthouse_audit",
    "get_nodes_by_class", "list_extensions",
    "get_memory_snapshot_details",
}
CHROME_MODIFY_TOOLS = {
    "navigate_page", "click", "fill", "fill_form", "type_text",
    "hover", "press_key", "drag", "upload_file", "click_at",
    "handle_dialog", "wait_for",
    "new_page", "close_page", "select_page",
}
CHROME_DANGEROUS_TOOLS = {
    "evaluate_script",
    "emulate", "resize_page",
    "performance_start_trace", "performance_stop_trace",
    "take_memory_snapshot", "load_memory_snapshot",
    "screencast_start", "screencast_stop",
    "install_extension", "uninstall_extension", "trigger_extension_action",
}
