"""HITL 危险操作策略 — 静态规则判断工具是否需要人工审批。

支持 Chrome DevTools 工具的细粒度风险判断和 URL 黑名单检查。
"""

import fnmatch
import re
from permissions.modes import (
    DANGEROUS_TOOLS, EDIT_TOOLS,
    CHROME_SAFE_TOOLS, CHROME_MODIFY_TOOLS, CHROME_DANGEROUS_TOOLS,
)

# 需要审批的工具集合
TOOLS_REQUIRING_APPROVAL = DANGEROUS_TOOLS | EDIT_TOOLS

# MCP 工具危险等级关键词映射
_MCP_HIGH_RISK_KEYWORDS = {
    "delete", "remove", "rm", "drop", "destroy", "erase", "wipe",
    "execute", "exec", "run_command", "shell", "bash",
    "format", "reset", "purge", "truncate",
}

_MCP_MEDIUM_RISK_KEYWORDS = {
    "write", "create", "update", "modify", "edit", "save", "upload",
    "insert", "add", "append", "replace", "move", "rename",
    "install", "deploy", "push", "publish", "send",
}

_MCP_SAFE_KEYWORDS = {
    "read", "get", "list", "search", "find", "query", "fetch",
    "describe", "info", "status", "health", "check", "test",
    "count", "exists", "validate", "ping", "version",
}

# 已知的内置安全工具（不在 TOOLS_REQUIRING_APPROVAL 中，且不是 MCP 工具）
_BUILTIN_SAFE_TOOLS = {
    "Read", "Glob", "Grep", "WebSearch", "WebFetch", "AskUser",
    "MemoryAdd", "MemorySearch", "CoreMemoryEdit",
    "SearchCode", "IndexWorkspace", "Task",
}

# 危险等级定义
DANGER_LEVELS: dict[str, tuple[str, str]] = {
    "Write": ("🟡 中危", "将写入或覆盖文件内容，原有内容将丢失"),
    "Edit": ("🟡 中危", "将编辑文件内容，可能修改关键代码"),
}

# Bash 命令危险等级分类
_BASH_HIGH_RISK_PATTERNS = re.compile(
    r"\b(rm\s|rmdir\s|del\s|format\s|mkfs\s|"
    r"chmod\s|chown\s|"
    r"curl\s.*\|\s*sh|wget\s.*\|\s*sh|"
    r"dd\s|"
    r">\s*/etc/|"
    r"pip\s+install\s+--user|"
    r"npm\s+publish|"
    r"git\s+push\s+--force|git\s+reset\s+--hard)"
)

_BASH_SAFE_PATTERNS = re.compile(
    r"^\s*(ls|dir|cat|head|tail|less|more|"
    r"pwd|whoami|hostname|uname|date|echo|env|which|whereis|"
    r"mkdir|touch|cp|mv|"
    r"git\s+(status|log|diff|branch|remote|stash\s+list|tag\s+-l)|"
    r"find\b|grep\b|rg\b|ag\b|"
    r"python\s+-c\s+print|node\s+-e\s|"
    r"pip\s+(list|show|freeze)|npm\s+(list|view)|"
    r"docker\s+(ps|images|logs|inspect)|"
    r"stat\b|file\b|wc\b|du\b|df\b)(\s|$)"
)

# Chrome DevTools URL 黑名单模式
_CHROME_URL_BLACKLIST = [
    "*/banking/*", "*/payment/*", "*/checkout/*",
    "*.bank.*", "*.pay.*",
]


def requires_approval(tool_name: str, tool_args: dict = None) -> bool:
    """检查工具是否需要审批。

    对于 Bash，根据命令内容判断。
    对于 Chrome DevTools，根据工具分类和 URL 黑名单判断。
    对于其他 MCP 工具，根据工具名关键词判断。
    """
    # 内置工具：静态规则
    if tool_name in TOOLS_REQUIRING_APPROVAL:
        if tool_name == "Bash" and tool_args:
            command = tool_args.get("command", "")
            if _BASH_SAFE_PATTERNS.match(command):
                return False
        return True

    # 已知的内置安全工具：直接放行
    if tool_name in _BUILTIN_SAFE_TOOLS:
        return False

    # Chrome DevTools 工具：细粒度判断
    if tool_name in CHROME_SAFE_TOOLS:
        return False
    if tool_name in CHROME_DANGEROUS_TOOLS:
        return True
    if tool_name in CHROME_MODIFY_TOOLS:
        if tool_name == "navigate_page" and tool_args:
            url = tool_args.get("url", "")
            if _is_chrome_url_blacklisted(url):
                return True
        return True

    # MCP 工具：基于关键词判断
    return _mcp_requires_approval(tool_name)


def get_danger_info(tool_name: str, tool_args: dict = None) -> tuple[str, str]:
    """获取危险等级和风险描述。

    Returns:
        (等级标签, 风险描述)
    """
    if tool_name == "Bash":
        return _get_bash_danger_info(tool_args or {})

    if tool_name in DANGER_LEVELS:
        return DANGER_LEVELS[tool_name]

    if tool_name in _BUILTIN_SAFE_TOOLS:
        return ("🟢 安全", "安全的只读操作")

    # Chrome DevTools 工具等级
    if tool_name in CHROME_SAFE_TOOLS:
        return ("🟢 安全", "Chrome 只读操作（查看页面/日志/网络）")
    if tool_name in CHROME_DANGEROUS_TOOLS:
        return ("🔴 高危", f"Chrome 工具 '{tool_name}' 可执行脚本或修改浏览器/扩展配置")
    if tool_name in CHROME_MODIFY_TOOLS:
        if tool_name == "navigate_page" and tool_args:
            url = tool_args.get("url", "")
            if _is_chrome_url_blacklisted(url):
                return ("🔴 高危", f"导航到黑名单 URL: {url}")
        return ("🟡 中危", f"Chrome 工具 '{tool_name}' 将操作浏览器页面")

    # MCP 工具的关键词等级
    return _get_mcp_danger_info(tool_name)


def _get_bash_danger_info(tool_args: dict) -> tuple[str, str]:
    """根据 Bash 命令内容判断危险等级。"""
    command = tool_args.get("command", "")

    if _BASH_SAFE_PATTERNS.match(command):
        return ("🟢 安全", "只读或创建类操作，风险较低")

    if _BASH_HIGH_RISK_PATTERNS.search(command):
        return ("🔴 高危", "涉及删除、权限修改或不可逆操作")

    return ("🟡 中危", "将执行 Shell 命令，可能修改文件或系统状态")


def _is_chrome_url_blacklisted(url: str) -> bool:
    """检查 URL 是否匹配 Chrome 黑名单模式。"""
    if not url:
        return False
    for pattern in _CHROME_URL_BLACKLIST:
        if fnmatch.fnmatch(url, pattern):
            return True
    return False


def _mcp_requires_approval(tool_name: str) -> bool:
    """根据工具名关键词判断 MCP 工具是否需要审批。"""
    name_lower = tool_name.lower()

    for kw in _MCP_SAFE_KEYWORDS:
        if kw in name_lower:
            return False

    for kw in _MCP_HIGH_RISK_KEYWORDS:
        if kw in name_lower:
            return True

    for kw in _MCP_MEDIUM_RISK_KEYWORDS:
        if kw in name_lower:
            return True

    return True


def _get_mcp_danger_info(tool_name: str) -> tuple[str, str]:
    """根据工具名关键词判断 MCP 工具的危险等级。"""
    name_lower = tool_name.lower()

    for kw in _MCP_SAFE_KEYWORDS:
        if kw in name_lower:
            return ("🟢 安全", "MCP 只读/查询操作，风险较低")

    for kw in _MCP_HIGH_RISK_KEYWORDS:
        if kw in name_lower:
            return ("🔴 高危", f"MCP 工具 '{tool_name}' 可能涉及删除或不可逆操作")

    for kw in _MCP_MEDIUM_RISK_KEYWORDS:
        if kw in name_lower:
            return ("🟡 中危", f"MCP 工具 '{tool_name}' 可能修改数据或系统状态")

    return ("🟡 中危", f"MCP 外部工具 '{tool_name}'，建议确认后执行")