"""Reviewer Agent — 保守审批策略，借鉴 PaiCLI。

审查流程：
1. LLM 审查执行结果，输出结构化 JSON
2. 代码层保守策略：解析失败默认不通过
3. 不通过时带上反馈回到 Supervisor，最多重试 MAX_RETRIES 次
"""

import json
import logging
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.types import Command

from agents.agent_state import MultiAgentState

logger = logging.getLogger(__name__)

MAX_RETRIES = 2

REVIEWER_SYSTEM_PROMPT = (
    "你是一个质量检查专家。你的职责是检查执行结果是否正确、完整和高质量。\n"
    "请以 JSON 格式输出检查结果：\n"
    '{"approved": true或false, "summary": "检查摘要", '
    '"issues": ["问题1"], "suggestions": ["建议1"]}\n\n'
    "重要规则：\n"
    "- 只有确信结果正确时才批准（approved: true）\n"
    "- 有任何疑问时一律拒绝（approved: false），并在 issues 中说明原因\n"
    "- 审查标准：结果是否正确、是否完整、是否符合原始任务要求"
)


def parse_review_approval(content: str) -> tuple[bool, list[str]]:
    """解析审查结果，保守策略：任何解析失败都判定不通过。

    Returns:
        (approved, issues): 是否通过，问题列表
    """
    if not content or not content.strip():
        return False, ["审查结果为空"]

    try:
        start = content.find("{")
        end = content.rfind("}") + 1
        if start >= 0 and end > start:
            data = json.loads(content[start:end])
            approved = data.get("approved", False)
            issues = data.get("issues", [])
            if not isinstance(issues, list):
                issues = [str(issues)]
            return bool(approved), issues
    except (json.JSONDecodeError, KeyError, TypeError):
        pass

    # JSON 解析失败：保守判不通过
    return False, ["审查结果无法解析，保守判定不通过"]


def create_reviewer_node(llm):
    """创建 Reviewer 节点函数。

    Args:
        llm: LangChain LLM 实例

    Returns:
        reviewer_node: 可添加到 StateGraph 的节点函数
    """

    def reviewer_node(state: MultiAgentState) -> Command[Literal["supervisor"]]:
        """审查上一步的执行结果。"""
        # 获取最后一条消息（Worker 的执行结果）
        last_msg = state["messages"][-1].content if state["messages"] else ""

        messages = [
            SystemMessage(content=REVIEWER_SYSTEM_PROMPT),
            HumanMessage(content=f"请审查以下执行结果：\n{last_msg}"),
        ]

        response = llm.invoke(messages)
        approved, issues = parse_review_approval(response.content)

        retry_count = state.get("retry_count", 0)

        if not approved and retry_count < MAX_RETRIES:
            # 不通过 + 未超重试上限 → 带反馈回到 Supervisor
            feedback = f"审查未通过，原因：{issues}\n请修正后重新执行。"
            logger.info(f"审查未通过 (重试 {retry_count + 1}/{MAX_RETRIES}): {issues}")
            return Command(
                update={
                    "messages": [HumanMessage(content=feedback, name="reviewer")],
                    "review_status": "rejected",
                    "retry_count": retry_count + 1,
                },
                goto="supervisor",
            )

        # 通过或超过重试上限
        if approved:
            status_msg = "审查通过"
            status = "approved"
        else:
            status_msg = f"超过最大重试次数({MAX_RETRIES})，保留当前结果"
            status = "max_retries_exceeded"

        logger.info(f"审查结果: {status}")
        return Command(
            update={
                "messages": [HumanMessage(content=status_msg, name="reviewer")],
                "review_status": status,
                "retry_count": 0,
            },
            goto="supervisor",
        )

    return reviewer_node
