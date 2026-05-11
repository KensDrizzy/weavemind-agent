"""HITL 审批模型 — 定义人工审批的请求、结果和决策枚举。"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ApprovalDecision(str, Enum):
    """审批决策枚举。"""
    APPROVED = "approved"           # 批准
    APPROVED_ALL = "approved_all"   # 本次会话全部放行
    MODIFIED = "modified"           # 修改参数后执行
    REJECTED = "rejected"           # 拒绝（带原因）
    SKIPPED = "skipped"             # 跳过本步骤


@dataclass
class ApprovalRequest:
    """审批请求。"""
    tool_name: str
    arguments: dict
    danger_level: str       # "🔴 高危" / "🟡 中危" / "🟢 安全"
    risk_description: str   # 风险描述
    suggestion: Optional[str] = None
    caller_context: Optional[str] = None


@dataclass
class ApprovalResult:
    """审批结果。"""
    decision: ApprovalDecision
    reason: Optional[str] = None
    modified_args: Optional[dict] = None
