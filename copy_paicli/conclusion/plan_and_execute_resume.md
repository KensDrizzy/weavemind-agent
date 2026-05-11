# Plan-and-Execute 简历写法

## 项目描述（中文）

### WeaveMind Agent — AI 编程助手

基于 LangGraph 的智能编程助手，支持 ReAct 和 Plan-Execute 双模式执行。

**核心贡献：**

- 设计并实现 **Plan-and-Execute 执行引擎**，将复杂任务分解为 DAG 结构的原子任务，通过拓扑排序确定执行顺序，支持无依赖任务并行执行，相比纯 ReAct 模式减少约 40% 的 LLM 调用次数
- 构建 **DAG 任务调度器**，基于 DFS 拓扑排序 + 并行执行策略（asyncio），实现任务级并行度和失败传播机制，单任务失败自动跳过所有依赖链上的后继任务
- 实现 **智能路由层**，在 LangGraph 状态图中根据 LLM 输出自动选择 ReAct（单步低延迟）或 Plan-Execute（多步高可靠）执行路径，用户无需手动判断任务复杂度
- 设计 **结构化规划提示词**，引导 LLM 输出 JSON 格式的 DAG 执行计划，包含 JSON 提取、环检测、依赖校验等容错机制

**技术栈：** Python, LangGraph, LangChain, Pydantic, asyncio, Rich

---

## 项目描述（英文）

### WeaveMind Agent — AI Coding Assistant

An intelligent coding assistant built on LangGraph, supporting both ReAct and Plan-and-Execute execution modes.

**Key Contributions:**

- Designed and implemented a **Plan-and-Execute execution engine** that decomposes complex tasks into DAG-structured atomic tasks, determines execution order via topological sorting, and supports parallel execution of independent tasks — reducing LLM calls by ~40% compared to pure ReAct mode
- Built a **DAG task scheduler** with DFS-based topological sorting and asyncio parallel execution, implementing task-level parallelism and failure propagation — automatically skipping all dependent tasks when a predecessor fails
- Implemented an **intelligent routing layer** within the LangGraph state machine that automatically selects between ReAct (single-step, low-latency) and Plan-Execute (multi-step, high-reliability) based on LLM output, eliminating the need for manual mode selection
- Designed **structured planning prompts** that guide the LLM to output JSON-formatted DAG execution plans, with robust error handling including JSON extraction, cycle detection, and dependency validation

**Tech Stack:** Python, LangGraph, LangChain, Pydantic, asyncio, Rich

---

## 面试要点

1. **DAG vs 线性列表**：DAG 能表达并行性，线性列表不行。实际场景中"读文件 A + 读文件 B"可以并行，DAG 让执行引擎自动识别
2. **拓扑排序选择**：用 DFS 而非 Kahn 算法，因为 DFS 天然支持环检测（visiting 集合），且代码更简洁
3. **失败传播**：不只在直接依赖上传播，而是递归传播到整个依赖链。这避免了"任务 B 依赖 A，A 失败后 B 还在等"的死锁
4. **路由策略**：不强制用户选择模式，而是根据 LLM 输出自动判断。单工具调用走 ReAct 快速响应，多工具调用走 Plan-Execute 确保可靠性
5. **与 ReAct 的关系**：Plan-Execute 不是替代 ReAct，而是补充。简单查询用 ReAct 更快，复杂任务用 Plan-Execute 更可靠
