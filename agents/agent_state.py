"""Multi-Agent 共享状态定义。

Supervisor、Planner、Worker、Reviewer 通过此 State 共享信息。
"""

from typing import Annotated, Optional
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict


class MultiAgentState(TypedDict):
    """Multi-Agent 编排器的共享状态。

    字段说明：
        messages: 对话消息列表，所有 Agent 的输入输出都通过此字段传递
        next: Supervisor 路由目标（Agent 名称或 "__end__"）
        current_task: 当前正在执行的步骤描述
        step_results: 已完成步骤的结果 {agent_name: result}
        review_status: 审查状态 "approved" / "rejected" / None
        retry_count: 当前步骤的重试次数
    """
    messages: Annotated[list[BaseMessage], add_messages]
    next: str
    current_task: Optional[str]
    step_results: dict
    review_status: Optional[str]
    retry_count: int
