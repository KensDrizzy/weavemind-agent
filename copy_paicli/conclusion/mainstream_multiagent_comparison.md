# 主流 Multi-Agent 框架对比与 WeaveMindAgent 选型分析

> 综合分析时间：2026-05-09
> 数据来源：LangGraph 官方教程/GitHub 源码、CrewAI 文档、AutoGen 0.4 文档、OpenAI Swarm README、Magentic-One 论文

---

## 一、五大框架核心实现模式

### 1. LangGraph Supervisor 模式（推荐方案）

**核心代码模式（来源：LangGraph 官方 hierarchical_agent_teams 教程）**

```python
from langgraph.graph import StateGraph, MessagesState, START, END
from langgraph.types import Command
from langgraph.prebuilt import create_react_agent

# 1) 定义共享状态
class State(MessagesState):
    next: str  # Supervisor 路由到的下一个 Agent

# 2) Supervisor 节点：LLM 结构化输出决定路由
def make_supervisor_node(llm, members: list[str]):
    options = ["FINISH"] + members

    class Router(TypedDict):
        next: Literal[*options]

    def supervisor_node(state: State) -> Command:
        messages = [{"role": "system", "content": system_prompt}] + state["messages"]
        response = llm.with_structured_output(Router).invoke(messages)
        goto = response["next"]
        if goto == "FINISH":
            goto = END
        return Command(goto=goto, update={"next": goto})

    return supervisor_node

# 3) Worker 节点：create_react_agent 生成完整 ReAct Agent
search_agent = create_react_agent(llm, tools=[tavily_tool])

def search_node(state: State) -> Command[Literal["supervisor"]]:
    result = search_agent.invoke(state)
    return Command(
        update={"messages": [HumanMessage(content=result["messages"][-1].content, name="search")]},
        goto="supervisor",  # Worker 完成后始终回到 Supervisor
    )

# 4) 组装图
builder = StateGraph(State)
builder.add_node("supervisor", supervisor_node)
builder.add_node("search", search_node)
builder.add_node("web_scraper", web_scraper_node)
builder.add_edge(START, "supervisor")  # 入口 → Supervisor
graph = builder.compile()
```

**关键设计模式：**
- **Command 对象**：Worker 完成后通过 `Command(goto="supervisor")` 回到 Supervisor，形成循环
- **结构化路由**：Supervisor 用 `with_structured_output(Router)` 决定下一个 Agent，而不是解析自然语言
- **create_react_agent**：每个 Worker 是一个完整的 ReAct Agent，可以多步推理和调用工具
- **层级嵌套**：Supervisor 下的子图本身也是一个 StateGraph，可实现多层嵌套

---

### 2. LangGraph Swarm/Handoff 模式

**核心代码模式（来源：LangGraph 官方 multi-agent-collaboration 教程）**

```python
# Agent 之间直接 handoff，没有中央 Supervisor
def research_node(state: MessagesState) -> Command[Literal["chart_generator"]]:
    result = research_agent.invoke(state)
    return Command(
        update={"messages": [HumanMessage(content=result["messages"][-1].content, name="researcher")]},
        goto="chart_generator",  # 直接跳到另一个 Agent
    )

def chart_node(state: MessagesState) -> Command[Literal["researcher", END]]:
    result = chart_agent.invoke(state)
    goto = get_next_node(result["messages"][-1], "researcher")
    return Command(
        update={"messages": [HumanMessage(content=result["messages"][-1].content, name="chart_generator")]},
        goto=goto,
    )

# 组装：Agent 之间直接跳转
workflow = StateGraph(MessagesState)
workflow.add_node("researcher", research_node)
workflow.add_node("chart_generator", chart_node)
workflow.add_edge(START, "researcher")
```

**关键设计模式：**
- **无中央编排器**：Agent 通过 Command(goto=...) 直接跳到下一个 Agent
- **FINAL ANSWER 约定**：当某个 Agent 输出含 "FINAL ANSWER" 时结束
- **对称通信**：Agent 地位平等，不需要 Manager

---

### 3. CrewAI Hierarchical 模式

```
Crew（编排器）
├── ManagerAgent（LLM 驱动，动态分配任务）
│   ├── Agent A（角色定义 + backstory）
│   ├── Agent B（角色定义 + backstory）
│   └── Agent C（角色定义 + backstory）
├── Task 1 → Agent A
├── Task 2 → Agent B（context: [Task 1]）
└── Task 3 → Agent C（context: [Task 1, Task 2]）
```

**关键设计模式：**
- Agent 通过 `delegation` 能力将任务委托给其他 Agent
- Task 的 `context` 参数指定依赖的其他 Task 输出
- Hierarchical 模式下 ManagerAgent 做 LLM 路由决策

---

### 4. AutoGen 0.4 事件驱动

```
Runtime（事件总线）
├── BaseAgent（订阅消息事件）
│   ├── on_message() → 处理 → 发布新事件
├── BaseAgent
│   ├── on_message() → 处理 → 发布新事件
└── GroupChat Manager（路由事件）
```

**关键设计模式：**
- Agent 通过 `on_message()` 订阅特定类型的消息
- Runtime 负责消息路由和传递
- 天然支持异步和并发

---

### 5. Magentic-One Orchestrator

```
Orchestrator（维护两个账本）
├── Task Ledger（任务进度）
├── Fact Ledger（已知事实）
├── WebSurfer Agent（网页交互）
├── FileSurfer Agent（文件读取）
├── Coder Agent（代码编写）
└── ComputerTerminal Agent（命令执行）

编排回合：
1. 读取 Task Ledger + Fact Ledger
2. 要求 Orchestrator LLM 制定下一步计划
3. 将指令发送给选定的 Worker
4. 接收 Worker 报告，更新 Fact Ledger
5. 回到步骤 1（循环直到完成）
```

---

## 二、框架对比总表

| 维度 | LangGraph Supervisor | LangGraph Swarm | CrewAI Hierarchical | AutoGen 0.4 | Magentic-One |
|------|---------------------|-----------------|---------------------|-------------|--------------|
| **架构** | 中央 Supervisor 路由 | 对等 handoff | Manager 分配 | 事件驱动 | Orchestrator 回合制 |
| **Worker 能力** | 完整 ReAct Agent | 完整 ReAct Agent | ReAct + 委托 | 对话/代码执行 | 专用工具 Agent |
| **通信** | Command + 共享 State | Command + 共享 State | Task 输出传递 | 发布-订阅 | Orchestrator 中转 |
| **路由** | LLM 结构化输出 | Agent 自决 | LLM Manager 决策 | 事件路由 | LLM 回合决策 |
| **并行** | Fan-out/Fan-in | 不支持 | Flows | 异步事件 | 有限（串行回合） |
| **嵌套** | 子图嵌套 ✅ | 单层 | 单层 | 嵌套对话 | 单层 |
| **检查点** | 原生持久化 ✅ | 原生持久化 ✅ | 无 | 无 | 无 |
| **审查** | 需自定义 | 需自定义 | 需自定义 | 需自定义 | 需自定义 |
| **LangChain 集成** | 完全原生 | 完全原生 | Tool 桥接 | 间接适配 | 间接（AutoGen） |
| **学习曲线** | 中 | 低 | 低 | 中 | 中 |

---

## 三、对 WeaveMindAgent 的选型决策

### 3.1 为什么选 LangGraph Supervisor

1. **WeaveMindAgent 已经基于 LangGraph**：现有 AgentLoop 就是用 StateGraph 实现的，零迁移成本
2. **create_react_agent 可复用现有工具**：ToolRegistry、PermissionPolicy、HookManager 都可以直接用
3. **子图嵌套天然支持**：Supervisor 模式下每个 Worker 是独立的 StateGraph，可以嵌套多层
4. **Command + 共享 State**：比 PaiCLI 的 AgentMessage 更优雅，不需要自己写消息解析
5. **检查点持久化**：LangGraph 原生支持，PaiCLI 没有这个能力

### 3.2 PaiCLI 三角色的取舍

| PaiCLI 角色 | 是否保留 | 改造方式 |
|-------------|---------|---------|
| Planner | 保留 | 作为 Supervisor 的第一个路由目标，用结构化输出生成执行计划 |
| Worker | 保留 | 用 create_react_agent 包装，支持完整的 ReAct 循环 |
| Reviewer | 保留 | 作为 Worker 执行后的审查节点，保守策略 + 重试 |

### 3.3 PaiCLI 的启示 + LangGraph 的增强

| 能力 | PaiCLI 做法 | LangGraph 增强 |
|------|------------|----------------|
| 规划 | Planner 一次性输出 JSON 计划 | Supervisor + 结构化路由，支持动态重新规划 |
| 执行 | Worker 一次性 LLM 调用 | create_react_agent，完整 ReAct 循环 |
| 审查 | Reviewer 审批 + 保守策略 | 保留保守策略，新增 Command 路由 |
| 并行 | ExecutorService + BlockingQueue | asyncio.gather + Fan-out/Fan-in |
| 消息 | AgentMessage 6 种类型 | Command 对象 + 共享 State |
| 历史 | 每步清空 | 子图独立状态，不需要手动清空 |

---

*分析基于截至 2025 年中期的公开信息和 LangGraph 官方教程源码*
