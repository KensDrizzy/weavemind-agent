"""HITL 危险操作策略 — 静态规则判断工具是否需要人工审批。"""

import re
from permissions.modes import DANGEROUS_TOOLS, EDIT_TOOLS

# 需要审批的工具集合
TOOLS_REQUIRING_APPROVAL = DANGEROUS_TOOLS | EDIT_TOOLS  # {"Bash", "Write", "Edit"}

# MCP 工具危险等级关键词映射
# 工具名或描述中包含这些关键词时，判定为需要审批
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
# 这些工具直接放行，不走 MCP 关键词判断
_BUILTIN_SAFE_TOOLS = {
    "Read", "Glob", "Grep", "WebSearch", "WebFetch", "AskUser",
    "MemoryAdd", "MemorySearch", "CoreMemoryEdit",
    "SearchCode", "IndexWorkspace", "Task",
}

# 危险等级定义：(等级标签, 风险描述)
DANGER_LEVELS: dict[str, tuple[str, str]] = {
    "Write": ("🟡 中危", "将写入或覆盖文件内容，原有内容将丢失"),
    "Edit": ("🟡 中危", "将编辑文件内容，可能修改关键代码"),
}

# Bash 命令危险等级分类
# 高危命令模式：删除、格式化、覆盖写入、权限修改、网络下载执行
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

# 安全命令模式：只读/查询/创建类操作，不需要审批
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


def requires_approval(tool_name: str, tool_args: dict = None) -> bool:
    """检查工具是否需要审批。

    对于 Bash，根据命令内容判断：安全命令不需要审批。
    对于 MCP 工具（非内置工具），根据工具名关键词判断。
    """
    # 内置工具：静态规则
    if tool_name in TOOLS_REQUIRING_APPROVAL:
        # Bash 命令细粒度判断
        if tool_name == "Bash" and tool_args:
            command = tool_args.get("command", "")
            if _BASH_SAFE_PATTERNS.match(command):
                return False
        return True

    # 已知的内置安全工具：直接放行
    if tool_name in _BUILTIN_SAFE_TOOLS:
        return False

    # MCP 工具：基于关键词判断
    return _mcp_requires_approval(tool_name)


def get_danger_info(tool_name: str, tool_args: dict = None) -> tuple[str, str]:
    """获取危险等级和风险描述。

    对于 Bash，根据命令内容动态判断危险等级。
    对于 MCP 工具，根据工具名关键词判断。

    Returns:
        (等级标签, 风险描述) — 未配置的工具默认为安全。
    """
    if tool_name == "Bash":
        return _get_bash_danger_info(tool_args or {})

    # 内置工具的静态等级
    if tool_name in DANGER_LEVELS:
        return DANGER_LEVELS[tool_name]

    # 已知的内置安全工具
    if tool_name in _BUILTIN_SAFE_TOOLS:
        return ("🟢 安全", "安全的只读操作")

    # MCP 工具的关键词等级
    return _get_mcp_danger_info(tool_name)


def _get_bash_danger_info(tool_args: dict) -> tuple[str, str]:
    """根据 Bash 命令内容判断危险等级。"""
    command = tool_args.get("command", "")

    if _BASH_SAFE_PATTERNS.match(command):
        return ("🟢 安全", "只读或创建类操作，风险较低")

    if _BASH_HIGH_RISK_PATTERNS.search(command):
        return ("🔴 高危", "涉及删除、权限修改或不可逆操作")

    # 其他命令默认中危
    return ("🟡 中危", "将执行 Shell 命令，可能修改文件或系统状态")


def _mcp_requires_approval(tool_name: str) -> bool:
    """根据工具名关键词判断 MCP 工具是否需要审批。

    判断逻辑：
    1. 工具名中包含高危关键词 → 需要审批
    2. 工具名中包含中危关键词 → 需要审批
    3. 工具名中只包含安全关键词 → 不需要审批
    4. 无法判断 → 默认需要审批（保守策略，MCP 工具来自外部）
    """
    name_lower = tool_name.lower()

    # 先检查安全关键词（优先级最高，如 read_file 即使包含 file 也是安全的）
    for kw in _MCP_SAFE_KEYWORDS:
        if kw in name_lower:
            return False

    # 检查高危关键词
    for kw in _MCP_HIGH_RISK_KEYWORDS:
        if kw in name_lower:
            return True

    # 检查中危关键词
    for kw in _MCP_MEDIUM_RISK_KEYWORDS:
        if kw in name_lower:
            return True

    # 无法判断 → 保守策略：MCP 工具来自外部，默认需要审批
    return True


def _get_mcp_danger_info(tool_name: str) -> tuple[str, str]:
    """根据工具名关键词判断 MCP 工具的危险等级。

    Returns:
        (等级标签, 风险描述)
    """
    name_lower = tool_name.lower()

    # 先检查安全关键词
    for kw in _MCP_SAFE_KEYWORDS:
        if kw in name_lower:
            return ("🟢 安全", "MCP 只读/查询操作，风险较低")

    # 检查高危关键词
    for kw in _MCP_HIGH_RISK_KEYWORDS:
        if kw in name_lower:
            return ("🔴 高危", f"MCP 工具 '{tool_name}' 可能涉及删除或不可逆操作")

    # 检查中危关键词
    for kw in _MCP_MEDIUM_RISK_KEYWORDS:
        if kw in name_lower:
            return ("🟡 中危", f"MCP 工具 '{tool_name}' 可能修改数据或系统状态")

    # 无法判断 → 保守标记为中危
    return ("🟡 中危", f"MCP 外部工具 '{tool_name}'，建议确认后执行")
