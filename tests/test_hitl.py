"""HITL 人工审批功能测试。"""

import pytest
from unittest.mock import MagicMock, patch

from core.hitl_models import ApprovalDecision, ApprovalRequest, ApprovalResult
from core.hitl_policy import requires_approval, get_danger_info, TOOLS_REQUIRING_APPROVAL
from cli.hitl_handler import TerminalHitlHandler
from tools.hitl_registry import HitlToolRegistry


# ── Phase 1.1: 审批模型测试 ──

class TestApprovalModels:
    def test_approval_decision_values(self):
        assert ApprovalDecision.APPROVED == "approved"
        assert ApprovalDecision.APPROVED_ALL == "approved_all"
        assert ApprovalDecision.MODIFIED == "modified"
        assert ApprovalDecision.REJECTED == "rejected"
        assert ApprovalDecision.SKIPPED == "skipped"

    def test_approval_request_fields(self):
        req = ApprovalRequest(
            tool_name="Bash",
            arguments={"command": "rm -rf /"},
            danger_level="🔴 高危",
            risk_description="将在系统上执行 Shell 命令",
        )
        assert req.tool_name == "Bash"
        assert req.arguments == {"command": "rm -rf /"}
        assert req.danger_level == "🔴 高危"
        assert req.suggestion is None
        assert req.caller_context is None

    def test_approval_result_with_reason(self):
        result = ApprovalResult(
            decision=ApprovalDecision.REJECTED,
            reason="危险操作",
        )
        assert result.decision == ApprovalDecision.REJECTED
        assert result.reason == "危险操作"
        assert result.modified_args is None

    def test_approval_result_modified(self):
        result = ApprovalResult(
            decision=ApprovalDecision.MODIFIED,
            modified_args={"command": "ls"},
        )
        assert result.decision == ApprovalDecision.MODIFIED
        assert result.modified_args == {"command": "ls"}


# ── Phase 1.2: 危险操作策略测试 ──

class TestHitlPolicy:
    def test_dangerous_tools_require_approval(self):
        """Write/Edit 仍需审批，Bash 视命令内容而定。"""
        assert requires_approval("Write") is True
        assert requires_approval("Edit") is True

    def test_bash_safe_commands_no_approval(self):
        """Bash 安全命令不需要审批。"""
        assert requires_approval("Bash", {"command": "mkdir e"}) is False
        assert requires_approval("Bash", {"command": "ls"}) is False
        assert requires_approval("Bash", {"command": "cat README.md"}) is False
        assert requires_approval("Bash", {"command": "git status"}) is False
        assert requires_approval("Bash", {"command": "pwd"}) is False
        assert requires_approval("Bash", {"command": "find . -name '*.py'"}) is False
        assert requires_approval("Bash", {"command": "touch test.txt"}) is False
        assert requires_approval("Bash", {"command": "cp a.txt b.txt"}) is False

    def test_bash_dangerous_commands_require_approval(self):
        """Bash 高危命令需要审批。"""
        assert requires_approval("Bash", {"command": "rm -rf /tmp/test"}) is True
        assert requires_approval("Bash", {"command": "chmod 777 /etc/passwd"}) is True
        assert requires_approval("Bash", {"command": "git push --force"}) is True
        assert requires_approval("Bash", {"command": "dd if=/dev/zero of=/dev/sda"}) is True

    def test_bash_medium_risk_commands_require_approval(self):
        """Bash 中危命令（不在安全列表也不在高危列表）需要审批。"""
        assert requires_approval("Bash", {"command": "pip install numpy"}) is True
        assert requires_approval("Bash", {"command": "python script.py"}) is True
        assert requires_approval("Bash", {"command": "docker run ubuntu"}) is True

    def test_bash_no_args_defaults_to_approval(self):
        """Bash 无参数时默认需要审批。"""
        assert requires_approval("Bash") is True

    def test_safe_tools_no_approval(self):
        assert requires_approval("Read") is False
        assert requires_approval("Glob") is False
        assert requires_approval("Grep") is False
        assert requires_approval("WebFetch") is False
        assert requires_approval("WebSearch") is False
        assert requires_approval("AskUser") is False
        assert requires_approval("Task") is False

    def test_bash_danger_info_safe(self):
        """mkdir 等安全命令应为 🟢 安全。"""
        level, desc = get_danger_info("Bash", {"command": "mkdir e"})
        assert level == "🟢 安全"

    def test_bash_danger_info_high_risk(self):
        """rm 等高危命令应为 🔴 高危。"""
        level, desc = get_danger_info("Bash", {"command": "rm -rf /tmp"})
        assert level == "🔴 高危"

    def test_bash_danger_info_medium_risk(self):
        """pip install 等中危命令应为 🟡 中危。"""
        level, desc = get_danger_info("Bash", {"command": "pip install numpy"})
        assert level == "🟡 中危"

    def test_danger_info_for_write(self):
        level, desc = get_danger_info("Write")
        assert level == "🟡 中危"

    def test_danger_info_for_edit(self):
        level, desc = get_danger_info("Edit")
        assert level == "🟡 中危"

    def test_danger_info_for_safe_tool(self):
        level, desc = get_danger_info("Read")
        assert level == "🟢 安全"

    def test_tools_requiring_approval_set(self):
        assert "Bash" in TOOLS_REQUIRING_APPROVAL
        assert "Write" in TOOLS_REQUIRING_APPROVAL
        assert "Edit" in TOOLS_REQUIRING_APPROVAL
        assert len(TOOLS_REQUIRING_APPROVAL) == 3


# ── Phase 2.1: HITL 处理器测试 ──

class TestTerminalHitlHandler:
    def test_default_disabled(self):
        handler = TerminalHitlHandler()
        assert handler.is_enabled() is False

    def test_enable_disable(self):
        handler = TerminalHitlHandler()
        handler.set_enabled(True)
        assert handler.is_enabled() is True
        handler.set_enabled(False)
        assert handler.is_enabled() is False

    def test_approved_all_auto_pass(self):
        """已全部放行的工具应自动通过。"""
        handler = TerminalHitlHandler()
        handler.set_enabled(True)
        handler._approved_all_tools.add("Bash")

        req = ApprovalRequest(
            tool_name="Bash",
            arguments={"command": "rm -rf /tmp"},
            danger_level="🔴 高危",
            risk_description="执行 Shell 命令",
        )
        result = handler.request_approval(req)
        assert result.decision == ApprovalDecision.APPROVED_ALL

    def test_clear_approved_all(self):
        handler = TerminalHitlHandler()
        handler._approved_all_tools.add("Bash")
        handler._approved_all_tools.add("Write")
        assert handler.approved_all_count() == 2

        handler.clear_approved_all()
        assert handler.approved_all_count() == 0

    @patch("cli.hitl_handler.console")
    def test_approval_approved(self, mock_console):
        """输入 y 应返回 APPROVED。"""
        handler = TerminalHitlHandler()
        handler.set_enabled(True)
        mock_console.input.side_effect = ["y"]
        mock_console.print = MagicMock()

        req = ApprovalRequest(
            tool_name="Bash",
            arguments={"command": "pip install numpy"},
            danger_level="🟡 中危",
            risk_description="执行 Shell 命令",
        )
        result = handler.request_approval(req)
        assert result.decision == ApprovalDecision.APPROVED

    @patch("cli.hitl_handler.console")
    def test_approval_rejected(self, mock_console):
        """输入 n 应返回 REJECTED。"""
        handler = TerminalHitlHandler()
        handler.set_enabled(True)
        mock_console.input.side_effect = ["n", ""]  # n + 空原因
        mock_console.print = MagicMock()

        req = ApprovalRequest(
            tool_name="Bash",
            arguments={"command": "rm -rf /"},
            danger_level="🔴 高危",
            risk_description="执行 Shell 命令",
        )
        result = handler.request_approval(req)
        assert result.decision == ApprovalDecision.REJECTED

    @patch("cli.hitl_handler.console")
    def test_approval_approved_all(self, mock_console):
        """输入 a 应返回 APPROVED_ALL 并设置全局放行。"""
        handler = TerminalHitlHandler()
        handler.set_enabled(True)
        mock_console.input.side_effect = ["a"]
        mock_console.print = MagicMock()

        req = ApprovalRequest(
            tool_name="Bash",
            arguments={"command": "pip install numpy"},
            danger_level="🟡 中危",
            risk_description="执行 Shell 命令",
        )
        result = handler.request_approval(req)
        assert result.decision == ApprovalDecision.APPROVED_ALL
        assert handler._approved_all_global is True

    @patch("cli.hitl_handler.console")
    def test_approval_skipped(self, mock_console):
        """输入 s 应返回 SKIPPED。"""
        handler = TerminalHitlHandler()
        handler.set_enabled(True)
        mock_console.input.side_effect = ["s"]
        mock_console.print = MagicMock()

        req = ApprovalRequest(
            tool_name="Bash",
            arguments={"command": "pip install numpy"},
            danger_level="🟡 中危",
            risk_description="执行 Shell 命令",
        )
        result = handler.request_approval(req)
        assert result.decision == ApprovalDecision.SKIPPED

    @patch("cli.hitl_handler.console")
    def test_approval_modified(self, mock_console):
        """输入 m + 有效 JSON 应返回 MODIFIED。"""
        handler = TerminalHitlHandler()
        handler.set_enabled(True)
        mock_console.input.side_effect = ["m", '{"command": "ls -la"}']
        mock_console.print = MagicMock()

        req = ApprovalRequest(
            tool_name="Bash",
            arguments={"command": "rm -rf /"},
            danger_level="🔴 高危",
            risk_description="执行 Shell 命令",
        )
        result = handler.request_approval(req)
        assert result.decision == ApprovalDecision.MODIFIED
        assert result.modified_args == {"command": "ls -la"}

    @patch("cli.hitl_handler.console")
    def test_approval_modified_invalid_json(self, mock_console):
        """输入 m + 无效 JSON 应回退到 APPROVED。"""
        handler = TerminalHitlHandler()
        handler.set_enabled(True)
        mock_console.input.side_effect = ["m", "not json"]
        mock_console.print = MagicMock()

        req = ApprovalRequest(
            tool_name="Bash",
            arguments={"command": "pip install numpy"},
            danger_level="🟡 中危",
            risk_description="执行 Shell 命令",
        )
        result = handler.request_approval(req)
        assert result.decision == ApprovalDecision.APPROVED


# ── Phase 2.2: HitlToolRegistry 测试 ──

class TestHitlToolRegistry:
    def test_check_approval_disabled(self):
        """HITL 禁用时应返回 None。"""
        handler = TerminalHitlHandler()
        handler.set_enabled(False)
        registry = HitlToolRegistry(hitl_handler=handler)
        result = registry.check_approval("Bash", {"command": "rm -rf /tmp"})
        assert result is None

    def test_check_approval_safe_tool(self):
        """安全工具应返回 None。"""
        handler = TerminalHitlHandler()
        handler.set_enabled(True)
        registry = HitlToolRegistry(hitl_handler=handler)
        result = registry.check_approval("Read", {"path": "/tmp/test.txt"})
        assert result is None

    def test_check_approval_bash_safe_command(self):
        """Bash 安全命令（mkdir）不需要审批。"""
        handler = TerminalHitlHandler()
        handler.set_enabled(True)
        registry = HitlToolRegistry(hitl_handler=handler)
        result = registry.check_approval("Bash", {"command": "mkdir e"})
        assert result is None

    def test_check_approval_bash_dangerous_command(self):
        """Bash 高危命令需要审批。"""
        handler = TerminalHitlHandler()
        handler.set_enabled(True)
        registry = HitlToolRegistry(hitl_handler=handler)
        # 需要模拟用户输入来通过审批
        with patch("cli.hitl_handler.console") as mock_console:
            mock_console.input.side_effect = ["y"]
            mock_console.print = MagicMock()
            result = registry.check_approval("Bash", {"command": "rm -rf /tmp"})
            assert result is not None
            assert result.decision == ApprovalDecision.APPROVED

    def test_check_approval_no_handler(self):
        """无处理器时应返回 None。"""
        registry = HitlToolRegistry(hitl_handler=None)
        result = registry.check_approval("Bash", {"command": "rm -rf /tmp"})
        assert result is None

    def test_inherits_tool_registry(self):
        """HitlToolRegistry 应继承 ToolRegistry 的所有功能。"""
        handler = TerminalHitlHandler()
        registry = HitlToolRegistry(hitl_handler=handler)
        assert hasattr(registry, 'get')
        assert hasattr(registry, 'get_all')
        assert hasattr(registry, 'register')
        assert hasattr(registry, 'get_langchain_tools')