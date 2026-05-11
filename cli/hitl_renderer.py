"""HITL 审批面板 — 用 Rich 渲染审批信息和交互提示。"""

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from core.hitl_models import ApprovalRequest


def render_approval_panel(request: ApprovalRequest) -> Panel:
    """渲染审批请求面板。"""
    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column(style="bold", width=10)
    table.add_column()

    table.add_row("工具:", Text(request.tool_name, style="cyan"))
    table.add_row("危险:", Text(request.danger_level))
    table.add_row("风险:", Text(request.risk_description, style="yellow"))

    if request.suggestion:
        table.add_row("建议:", Text(request.suggestion, style="dim"))

    # 参数展示（截断过长值）
    args_text = _format_args(request.arguments)
    table.add_row("参数:", args_text)

    return Panel(table, title="[bold]🔐 人工审批请求[/bold]", border_style="yellow")


def render_choice_hint() -> str:
    """返回交互提示文本。"""
    return (
        "[bold]选择操作:[/bold]  "
        "[cyan]y[/] 批准  "
        "[cyan]a[/] 全部放行  "
        "[cyan]m[/] 修改参数  "
        "[cyan]n[/] 拒绝  "
        "[cyan]s[/] 跳过"
    )


def _format_args(args: dict, max_len: int = 80) -> Text:
    """格式化参数字典，截断过长值。"""
    parts = []
    for k, v in args.items():
        val = str(v)
        if len(val) > max_len:
            val = val[:max_len] + "..."
        parts.append(f"{k}={val}")
    return Text(", ".join(parts), style="dim")
