# WeaveMindAgent Multi-Agent 改造方案

> 基于 PaiCLI 三角色分工 + LangGraph Supervisor 模式
> 改造原则：复用现有基础设施，渐进式改造，不破坏已有功能

---

## 一、现状分析

### 1.1 已有基础

| 模块 | 文件 | 现状 | 可复用度 |
|------|------|------|---------|
| Agent Loop | core/agent_loop.py | LangGraph StateGraph，think→route→act | ✅ 保留 |
| Plan-Execute | core/planner.py + plan_executor.py + plan_models.py | DAG 规划 + asyncio 并行执行 | ⚠️ 改造 |
| SubAgent | agents/subagent.py | 21行，一次性 LLM 调用，无 ReAct | ❌ 重写 |
| Agent 定义 | .weavemind/agents/*.md | YAML frontmatter 加载 | ⚠️ 扩展 |
| 工具注册 | tools/registry.py | 10 个内置工具 + SubAgentTool | ✅ 保留 |
| 权限策略 | permissions/policy.py | 4 种模式 | ✅ 保留 |
| Hook 事件 | hooks/manager.py | PreToolUse/PostToolUse | ✅ 保留 |
| Memory | core/memory.py | CoreMemory + LongTermMemory + Compaction | ✅ 保留 |

### 1.2 核心问题

1. **SubAgentTool 极其简陋**：只做一次 `llm.invoke()`，无工具调用、无 ReAct 循环
2. **Plan-Execute 每个 task 只调单个工具**：不是完整的 Agent 循环
3. **无角色分工**：3 个子 Agent（explore/general/plan）只是提示词不同，没有职责隔离
4. **无审查机制**：执行结果没有质量把关
5. **无并行 Agent 执行**：Plan-Execute 的并行是工具级并行，不是 Agent 级并行

---

## 二、目标架构

### 2.1 整体架构图

```
用户输入
  ↓
WeaveMindCLI（入口）
  ↓
AgentLoop（主 LangGraph）
  ├── 简单任务 → ReAct 循环（现有逻辑不变）
  └── 复杂任务 → /team 命令 → MultiAgentOrchestrator
                                    ↓
                              Supervisor 节点（LLM 路由）
                              ├── Planner Agent（规划，只读工具）
                              ├── Worker Agent-1（执行，全部工具，ReAct 循环）
                              ├── Worker Agent-2（执行，全部工具，ReAct 循环）
                              └── Reviewer Agent（审查，只读工具）
```

### 2.2 核心设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 架构模式 | LangGraph Supervisor | 已有 LangGraph 基础，零迁移成本 |
| Worker 实现 | create_react_agent | 完整 ReAct 循环，可多步推理 |
| 通信机制 | Command + 共享 State | LangGraph 原生，不需要自定义消息类型 |
| 规划方式 | Supervisor 结构化路由 | 比 PaiCLI 的一次性 JSON 计划更灵活 |
| 审查机制 | 保留 PaiCLI 保守策略 | 宁可多重试也不放过错误 |
| 并行执行 | asyncio.gather | 复用现有 PlanExecutor 的并行模式 |

---

## 三、文件改造清单

### 3.1 新增文件

| 文件 | 职责 | 代码量估计 |
|------|------|-----------|
| `agents/orchestrator.py` | Multi-Agent 编排器，Supervisor 模式 | ~200行 |
| `agents/worker.py` | Worker Agent 封装，基于 create_react_agent | ~80行 |
| `agents/reviewer.py` | Reviewer Agent，保守审批策略 | ~100行 |
| `agents/agent_state.py` | Multi-Agent 共享状态定义 | ~40行 |

### 3.2 修改文件

| 文件 | 改动 | 影响范围 |
|------|------|---------|
| `agents/subagent.py` | 重写为基于 create_react_agent 的实现 | SubAgentTool |
| `agents/loader.py` | 扩展 YAML frontmatter 支持 role 字段 | Agent 定义加载 |
| `.weavemind/agents/*.md` | 新增 planner.md / reviewer.md，修改现有 | Agent 定义 |
| `core/agent_loop.py` | 新增 /team 命令入口，调用 orchestrator | 主循环 |
| `cli/commands.py` | 新增 /team 命令处理 | CLI 命令 |

### 3.3 不动文件

`tools/registry.py`、`permissions/policy.py`、`hooks/manager.py`、`core/memory.py`、`core/compaction.py`、`core/plan_models.py` — 全部保留不动

---

## 四、详细实现方案

### 4.1 agents/agent_state.py — 共享状态定义

```python
"""Multi-Agent 共享状态。"""

from typing import Annotated, Literal, Optional
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict


class MultiAgentState(TypedDict):
    """Multi-Agent 编排器的共享状态。"""
    messages: Annotated[list[BaseMessage], add_messages]
    next: str                          # Supervisor 路由目标
    current_task: Optional[str]        # 当前执行的步骤描述
    step_results: dict                 # {step_id: result} 已完成步骤结果
    review_status: Optional[str]       # "approved" / "rejected" / None
    retry_count: int                   # 当前步骤重试次数
```

### 4.2 agents/orchestrator.py — 编排器（核心）

```python
"""Multi-Agent 编排器 — 基于 LangGraph Supervisor 模式。

流程：Supervisor 路由 → Planner/Worker/Reviewer → 回到 Supervisor → 循环
"""

import logging
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import StateGraph, START, END
from langgraph.types import Command

from agents.agent_state import MultiAgentState
from agents.worker import create_worker_node
from agents.reviewer import create_reviewer_node

logger = logging.getLogger(__name__)

MAX_RETRIES_PER_STEP = 2
MAX_SUPERVISOR_ROUNDS = 20


def make_supervisor_node(llm: BaseChatModel, members: list[str]):
    """创建 Supervisor 节点：LLM 结构化输出决定路由。"""

    class Router(dict):
        """路由决策。"""
        next: str  # 下一个 Agent 名称或 "FINISH"

    system_prompt = (
        "你是一个任务编排者，管理以下 Agent 团队：\n"
        f"{members}\n\n"
        "根据用户请求和当前进度，决定下一步由哪个 Agent 执行。\n"
        "- planner: 分析任务，制定执行计划\n"
        "- worker-1 / worker-2: 执行具体操作（读写文件、运行命令等）\n"
        "- reviewer: 审查执行结果的质量\n\n"
        "当所有任务完成时，回复 FINISH。"
    )

    def supervisor_node(state: MultiAgentState) -> Command:
        messages = [SystemMessage(content=system_prompt)] + state["messages"]
        response = llm.with_structured_output(Router).invoke(messages)
        goto = response.get("next", "FINISH")

        if goto == "FINISH":
            return Command(goto=END, update={"next": "__end__"})

        return Command(goto=goto, update={"next": goto})

    return supervisor_node


class MultiAgentOrchestrator:
    """Multi-Agent 编排器。"""

    def __init__(
        self,
        llm: BaseChatModel,
        tool_registry,
        permission_policy,
        hook_manager=None,
        memory=None,
        num_workers: int = 2,
    ):
        self.llm = llm
        self.tool_registry = tool_registry
        self.permission_policy = permission_policy
        self.hook_manager = hook_manager
        self.memory = memory
        self.num_workers = num_workers
        self.graph = self._build_graph()

    def _build_graph(self) -> StateGraph:
        """构建 Multi-Agent StateGraph。"""
        members = ["planner"]
        members += [f"worker-{i+1}" for i in range(self.num_workers)]
        members.append("reviewer")

        builder = StateGraph(MultiAgentState)

        # Supervisor 节点
        supervisor_node = make_supervisor_node(self.llm, members)
        builder.add_node("supervisor", supervisor_node)

        # Planner 节点（只读工具）
        builder.add_node("planner", self._make_planner_node())

        # Worker 节点（全部工具，完整 ReAct）
        for i in range(self.num_workers):
            worker_name = f"worker-{i+1}"
            worker_node = create_worker_node(
                llm=self.llm,
                tool_registry=self.tool_registry,
                permission_policy=self.permission_policy,
                hook_manager=self.hook_manager,
                name=worker_name,
            )
            builder.add_node(worker_name, worker_node)

        # Reviewer 节点
        reviewer_node = create_reviewer_node(self.llm)
        builder.add_node("reviewer", reviewer_node)

        # 入口 → Supervisor
        builder.add_edge(START, "supervisor")

        return builder.compile()

    def _make_planner_node(self):
        """创建 Planner 节点：分析任务并输出执行计划。"""
        planner_prompt = (
            "你是一个任务规划专家。分析用户需求，制定清晰的执行步骤。\n"
            "输出格式：\n"
            "1. [步骤1描述]\n"
            "2. [步骤2描述]\n"
            "...\n\n"
            "注意：只做规划，不执行任何操作。"
        )

        def planner_node(state: MultiAgentState) -> Command[Literal["supervisor"]]:
            messages = [
                SystemMessage(content=planner_prompt),
                HumanMessage(content=state["messages"][-1].content),
            ]
            response = self.llm.invoke(messages)
            return Command(
                update={
                    "messages": [HumanMessage(content=response.content, name="planner")],
                    "current_task": response.content,
                },
                goto="supervisor",
            )

        return planner_node

    def run(self, user_input: str) -> dict:
        """执行 Multi-Agent 协作。"""
        initial_state = {
            "messages": [HumanMessage(content=user_input)],
            "next": "",
            "current_task": None,
            "step_results": {},
            "review_status": None,
            "retry_count": 0,
        }
        config = {"recursion_limit": MAX_SUPERVISOR_ROUNDS * 5}
        result = self.graph.invoke(initial_state, config=config)
        return result
```

### 4.3 agents/worker.py — Worker Agent

```python
"""Worker Agent — 基于 create_react_agent 的完整 ReAct 循环。"""

import logging
from typing import Literal

from langchain_core.messages import HumanMessage
from langgraph.prebuilt import create_react_agent
from langgraph.types import Command

from agents.agent_state import MultiAgentState

logger = logging.getLogger(__name__)

WORKER_SYSTEM_PROMPT = (
    "你是一个任务执行专家。根据给定的任务步骤，调用工具完成具体操作。\n"
    "可用工具：Read, Write, Edit, Bash, Glob, Grep, WebFetch, WebSearch\n\n"
    "规则：\n"
    "- 涉及代码理解时优先使用 Grep/Glob\n"
    "- 每步只做一件事\n"
    "- 完成后简要报告结果"
)


def create_worker_node(
    llm,
    tool_registry,
    permission_policy,
    hook_manager=None,
    name: str = "worker",
):
    """创建一个 Worker Agent 节点函数。"""

    # 从 ToolRegistry 获取 LangChain 工具列表
    tools = tool_registry.get_langchain_tools()

    # 用 create_react_agent 创建完整的 ReAct Agent
    agent = create_react_agent(llm, tools=tools, prompt=WORKER_SYSTEM_PROMPT)

    def worker_node(state: MultiAgentState) -> Command[Literal["supervisor"]]:
        result = agent.invoke({"messages": state["messages"]})
        last_msg = result["messages"][-1].content

        return Command(
            update={
                "messages": [HumanMessage(content=last_msg, name=name)],
                "step_results": {**state.get("step_results", {}), name: last_msg},
            },
            goto="supervisor",
        )

    return worker_node
```

### 4.4 agents/reviewer.py — Reviewer Agent

```python
"""Reviewer Agent — 保守审批策略，借鉴 PaiCLI。"""

import json
import logging
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.types import Command

from agents.agent_state import MultiAgentState

logger = logging.getLogger(__name__)

MAX_RETRIES = 2

REVIEWER_SYSTEM_PROMPT = (
    "你是一个质量检查专家。检查执行结果是否正确、完整和高质量。\n"
    "请以 JSON 格式输出：\n"
    '{"approved": true/false, "summary": "检查摘要", '
    '"issues": ["问题1"], "suggestions": ["建议1"]}\n\n'
    "注意：只有确信结果正确时才批准，有疑问时一律拒绝。"
)


def parse_review_approval(content: str) -> tuple[bool, list[str]]:
    """解析审查结果，保守策略：解析失败默认不通过。"""
    if not content or not content.strip():
        return False, ["审查结果为空"]

    try:
        # 尝试提取 JSON
        start = content.find("{")
        end = content.rfind("}") + 1
        if start >= 0 and end > start:
            data = json.loads(content[start:end])
            approved = data.get("approved", False)
            issues = data.get("issues", [])
            return bool(approved), issues
    except (json.JSONDecodeError, KeyError):
        pass

    # JSON 解析失败：保守判不通过
    return False, ["审查结果无法解析，保守判定不通过"]


def create_reviewer_node(llm):
    """创建 Reviewer 节点。"""

    def reviewer_node(state: MultiAgentState) -> Command[Literal["supervisor"]]:
        messages = [
            SystemMessage(content=REVIEWER_SYSTEM_PROMPT),
            HumanMessage(content=f"请审查以下执行结果：\n{state['messages'][-1].content}"),
        ]
        response = llm.invoke(messages)
        approved, issues = parse_review_approval(response.content)

        retry_count = state.get("retry_count", 0)

        if not approved and retry_count < MAX_RETRIES:
            # 不通过 + 未超重试上限 → 回到 Worker 重做
            feedback = f"审查未通过，原因：{issues}\n请修正后重新执行。"
            return Command(
                update={
                    "messages": [HumanMessage(content=feedback, name="reviewer")],
                    "review_status": "rejected",
                    "retry_count": retry_count + 1,
                },
                goto="supervisor",
            )

        # 通过 或 超过重试上限 → 记录结果，继续
        status = "approved" if approved else "max_retries_exceeded"
        return Command(
            update={
                "messages": [HumanMessage(
                    content=f"审查{'通过' if approved else '超过重试上限，保留当前结果'}",
                    name="reviewer",
                )],
                "review_status": status,
                "retry_count": 0,
            },
            goto="supervisor",
        )

    return reviewer_node
```

### 4.5 agents/subagent.py — 重写

```python
"""SubAgentTool — 重写为基于 create_react_agent 的实现。"""

from tools.base import WeaveMindTool
from langgraph.prebuilt import create_react_agent
from langchain_core.messages import HumanMessage, SystemMessage
import settings


class SubAgentTool(WeaveMindTool):
    name: str = "Task"
    description: str = (
        "Launch a sub-agent with full ReAct loop for complex isolated tasks. "
        "Args: description, subagent_type, prompt"
    )

    agent_defs: dict = {}

    def _run(self, description: str, subagent_type: str, prompt: str) -> str:
        agent_def = self.agent_defs.get(subagent_type, {})
        model = agent_def.get("model", settings.get("llm.model", "claude-haiku-4-5-20251001"))
        system = agent_def.get("system_prompt", f"You are a {subagent_type} agent.")
        tool_names = agent_def.get("tools", [])

        from core.llm_factory import create_llm
        llm = create_llm(model=model)

        # 获取可用工具
        tools = []
        if tool_names and hasattr(self, '_tool_registry'):
            for name in tool_names:
                tool = self._tool_registry.get(name)
                if tool:
                    tools.append(tool)

        if tools:
            agent = create_react_agent(llm, tools=tools, prompt=system)
            result = agent.invoke({"messages": [HumanMessage(content=prompt)]})
            return result["messages"][-1].content
        else:
            # 无工具时回退到简单 LLM 调用
            messages = [SystemMessage(content=system), HumanMessage(content=prompt)]
            response = llm.invoke(messages)
            return response.content
```

### 4.6 Agent 定义文件

**新增 .weavemind/agents/planner.md：**

```yaml
---
name: planner
description: Task planning agent that breaks down complex goals into executable steps.
model: inherit
tools: [Read, Glob, Grep]
role: planner
---
You are a task planning expert. Analyze user requirements and create clear execution plans.

Rules:
- Output numbered steps with descriptions
- Mark dependencies between steps
- Do NOT execute any operations, only plan
- Keep plans concise and actionable
```

**新增 .weavemind/agents/reviewer.md：**

```yaml
---
name: reviewer
description: Quality review agent that checks execution results with conservative approval.
model: inherit
tools: [Read, Glob, Grep]
role: reviewer
---
You are a quality review expert. Check execution results for correctness and completeness.

Rules:
- Only approve when you are confident the result is correct
- When in doubt, reject with specific issues
- Output structured JSON: {"approved": bool, "issues": [...], "suggestions": [...]}
- Never execute operations, only review
```

### 4.7 core/agent_loop.py — 新增 /team 入口

在 AgentLoop 类中新增方法：

```python
def run_multi_agent(self, user_input: str) -> dict:
    """以 Multi-Agent 模式执行任务。"""
    from agents.orchestrator import MultiAgentOrchestrator

    orchestrator = MultiAgentOrchestrator(
        llm=self.llm,
        tool_registry=self.tool_registry,
        permission_policy=self.permission_policy,
        hook_manager=self.hook_manager,
        memory=self.memory,
    )
    return orchestrator.run(user_input)
```

### 4.8 cli/commands.py — 新增 /team 命令

```python
elif cmd == "/team":
    self.force_team_mode = True
    # 下一条用户输入将走 Multi-Agent 模式
```

---

## 五、实施步骤（按优先级排序）

### Phase 1：基础框架（1-2天）

1. 创建 `agents/agent_state.py` — 共享状态定义
2. 创建 `agents/orchestrator.py` — Supervisor 编排器
3. 创建 `agents/worker.py` — Worker Agent
4. 创建 `agents/reviewer.py` — Reviewer Agent
5. 编写单元测试 `tests/test_multiagent.py`

### Phase 2：集成改造（1天）

6. 重写 `agents/subagent.py` — 基于 create_react_agent
7. 扩展 `agents/loader.py` — 支持 role 字段
8. 新增 Agent 定义文件 planner.md / reviewer.md
9. 在 `core/agent_loop.py` 中新增 /team 入口

### Phase 3：CLI 集成 + 端到端测试（1天）

10. 在 `cli/commands.py` 中新增 /team 命令
11. 端到端测试：/team 模式执行多步任务
12. 验证 ReAct / Plan / Team 三种模式切换正常

### Phase 4：增强功能（后续迭代）

13. Worker 并行执行（asyncio.gather）
14. 动态规划调整（Supervisor 重新规划）
15. 流式输出支持
16. Memory 在 Multi-Agent 中的共享

---

## 六、测试方案

### 6.1 单元测试

```python
# tests/test_multiagent.py

def test_supervisor_routes_to_planner():
    """Supervisor 应该将新任务路由到 Planner。"""

def test_worker_executes_with_tools():
    """Worker 应该能调用工具完成任务。"""

def test_reviewer_approves_correct_result():
    """Reviewer 应该批准正确的执行结果。"""

def test_reviewer_rejects_bad_result():
    """Reviewer 应该拒绝错误的执行结果。"""

def test_reviewer_conservative_on_parse_failure():
    """审查结果解析失败时应该保守判定不通过。"""

def test_retry_mechanism():
    """审查不通过时应该重试，最多 MAX_RETRIES 次。"""

def test_orchestrator_full_flow():
    """完整流程：规划 → 执行 → 审查 → 完成。"""
```

### 6.2 集成测试

```bash
# 启动 WeaveMindAgent，手动测试
/team 创建一个 Python 项目，写一个 Hello World，然后验证项目结构
```

---

## 七、风险与缓解

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| create_react_agent 与现有 ToolRegistry 不兼容 | Worker 无法调用工具 | 先做兼容性验证，必要时写适配层 |
| Supervisor 路由死循环 | token 消耗失控 | MAX_SUPERVISOR_ROUNDS 限制 + 递归限制 |
| LLM 结构化输出不稳定 | 路由决策错误 | 回退到关键词匹配路由 |
| Worker 执行超时 | 长时间阻塞 | 超时控制 + 取消机制 |
| Reviewer 过于严格 | 所有结果被拒绝 | 可配置审批策略（严格/宽松） |

---

*改造方案 v1.0 — 待审核后开始编码*
