"""HITL 危险操作策略 — 静态规则判断工具是否需要人工审批。"""

import re
from permissions.modes import DANGEROUS_TOOLS, EDIT_TOOLS

# 需要审批的工具集合
TOOLS_REQUIRING_APPROVAL = DANGEROUS_TOOLS | EDIT_TOOLS  # {"Bash", "Write", "Edit"}

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
    """
    if tool_name not in TOOLS_REQUIRING_APPROVAL:
        return False

    # Bash 命令细粒度判断
    if tool_name == "Bash" and tool_args:
        command = tool_args.get("command", "")
        if _BASH_SAFE_PATTERNS.match(command):
            return False

    return True


def get_danger_info(tool_name: str, tool_args: dict = None) -> tuple[str, str]:
    """获取危险等级和风险描述。

    对于 Bash，根据命令内容动态判断危险等级。

    Returns:
        (等级标签, 风险描述) — 未配置的工具默认为安全。
    """
    if tool_name == "Bash":
        return _get_bash_danger_info(tool_args or {})

    return DANGER_LEVELS.get(tool_name, ("🟢 安全", "安全的只读操作"))


def _get_bash_danger_info(tool_args: dict) -> tuple[str, str]:
    """根据 Bash 命令内容判断危险等级。"""
    command = tool_args.get("command", "")

    if _BASH_SAFE_PATTERNS.match(command):
        return ("🟢 安全", "只读或创建类操作，风险较低")

    if _BASH_HIGH_RISK_PATTERNS.search(command):
        return ("🔴 高危", "涉及删除、权限修改或不可逆操作")

    # 其他命令默认中危
    return ("🟡 中危", "将执行 Shell 命令，可能修改文件或系统状态")
