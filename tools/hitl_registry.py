"""HITL 工具注册表 — 带人工审批的工具注册表拦截层。

继承 ToolRegistry，新增审批检查方法供 AgentLoop._act() 调用。
工具执行仍由 AgentLoop._act() 负责，本类只提供审批判断和请求构建。
"""

import logging
from typing import Any, Optional

from core.hitl_models import ApprovalRequest, ApprovalResult, ApprovalDecision
from core.hitl_policy import requires_approval, get_danger_info
from cli.hitl_handler import TerminalHitlHandler
from tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class HitlToolRegistry(ToolRegistry):
    """带 HITL 审批的工具注册表。"""

    def __init__(self, hitl_handler: Optional[TerminalHitlHandler] = None,
                 memory_manager=None, rag_pipeline=None, mcp_manager=None):
        super().__init__(memory_manager=memory_manager, rag_pipeline=rag_pipeline, mcp_manager=mcp_manager)
        self.hitl_handler = hitl_handler

    def check_approval(self, tool_name: str, tool_args: dict) -> Optional[ApprovalResult]:
        """检查工具是否需要审批，如需要则发起审批流程。

        Returns:
            None — 不需要审批，直接执行
            ApprovalResult — 需要审批且已完成审批，根据决策决定后续行为
        """
        if not self.hitl_handler or not self.hitl_handler.is_enabled():
            return None

        if not requires_approval(tool_name, tool_args):
            return None

        # 构建审批请求
        danger_level, risk_description = get_danger_info(tool_name, tool_args)
        request = ApprovalRequest(
            tool_name=tool_name,
            arguments=tool_args,
            danger_level=danger_level,
            risk_description=risk_description,
        )

        result = self.hitl_handler.request_approval(request)
        logger.info("[HITL] %s 审批结果: %s", tool_name, result.decision.value)
        return result