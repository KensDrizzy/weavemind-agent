"""HITL 审批处理器 — 终端交互式人工审批。"""

import json
import logging

from rich.console import Console

from cli.hitl_renderer import render_approval_panel, render_choice_hint
from core.hitl_models import ApprovalRequest, ApprovalResult, ApprovalDecision

console = Console()
logger = logging.getLogger(__name__)


class TerminalHitlHandler:
    """终端 HITL 审批处理器。"""

    def __init__(self):
        self.enabled = False
        self._approved_all_tools: set[str] = set()
        self._approved_all_global: bool = False  # 全局放行标志

    def is_enabled(self) -> bool:
        return self.enabled

    def set_enabled(self, enabled: bool):
        self.enabled = enabled

    def request_approval(self, request: ApprovalRequest) -> ApprovalResult:
        """请求用户审批，返回审批结果。"""

        # 检查全局放行
        if self._approved_all_global:
            return ApprovalResult(decision=ApprovalDecision.APPROVED_ALL)

        # 检查是否已全部放行（单工具）
        if request.tool_name in self._approved_all_tools:
            logger.info("[HITL] %s 已在本次会话中全部放行，自动通过", request.tool_name)
            console.print(f"  [dim][HITL] {request.tool_name} 已全部放行，自动通过[/dim]")
            return ApprovalResult(decision=ApprovalDecision.APPROVED_ALL)

        # 显示审批面板
        console.print()
        console.print(render_approval_panel(request))
        console.print(render_choice_hint())
        console.print()

        # 循环提示直到用户做出有效决策
        return self._prompt_until_decision(request)

    def _prompt_until_decision(self, request: ApprovalRequest) -> ApprovalResult:
        """循环提示直到用户做出有效决策。"""
        for _ in range(5):
            user_input = console.input("[bold]> [/bold]").strip().lower()

            if user_input == "" or user_input == "y":
                return ApprovalResult(decision=ApprovalDecision.APPROVED)

            if user_input == "a":
                self._approved_all_global = True
                return ApprovalResult(decision=ApprovalDecision.APPROVED_ALL)

            if user_input == "n":
                console.print("[dim]请输入拒绝原因（可选，按 Enter 跳过）：[/dim]")
                reason = console.input("> ").strip()
                return ApprovalResult(
                    decision=ApprovalDecision.REJECTED,
                    reason=reason or "用户拒绝了此操作",
                )

            if user_input == "s":
                return ApprovalResult(decision=ApprovalDecision.SKIPPED)

            if user_input == "m":
                return self._handle_modify_args(request)

            console.print("[dim]无法识别的选项，请输入 y/a/n/s/m 之一[/dim]")

        # 连续多次无效输入，保守拒绝
        return ApprovalResult(decision=ApprovalDecision.REJECTED, reason="连续多次无效输入")

    def _handle_modify_args(self, request: ApprovalRequest) -> ApprovalResult:
        """处理参数修改。"""
        console.print("[dim]请输入修改后的参数（JSON 格式）：[/dim]")
        console.print(f"[dim]当前参数：{json.dumps(request.arguments, ensure_ascii=False)}[/dim]")
        modified_input = console.input("> ").strip()

        try:
            modified_args = json.loads(modified_input)
            return ApprovalResult(
                decision=ApprovalDecision.MODIFIED,
                modified_args=modified_args,
            )
        except json.JSONDecodeError:
            console.print("[red]无效的 JSON 格式，使用原始参数[/red]")
            return ApprovalResult(decision=ApprovalDecision.APPROVED)

    def clear_approved_all(self):
        """清除本次会话中积累的全部放行记录。"""
        self._approved_all_tools.clear()
        self._approved_all_global = False

    def approved_all_count(self) -> int:
        """返回已全部放行的工具数量。"""
        if self._approved_all_global:
            return -1  # -1 表示全局放行
        return len(self._approved_all_tools)