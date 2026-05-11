# Plan-and-Execute 技术说明

## 概述

Plan-and-Execute 是 WeaveMind Agent 的核心能力升级，将原有的单步 ReAct 循环扩展为「规划 → 执行」两阶段架构。对于复杂多步骤任务，系统先通过 LLM 生成 DAG 结构的执行计划，再由执行引擎按拓扑序并行执行，显著提升了任务完成率和执行效率。

## 架构设计

### 整体流程

```
用户输入 → think → route → plan_or_react
                              ├─ 简单任务 → check_permissions → act → think (ReAct)
                              └─ 复杂任务 → plan → execute_plan → think (Plan-Execute)
```

- **ReAct 模式**：单工具调用，适合简单查询和单步操作
- **Plan-Execute 模式**：多步骤 DAG 执行，适合复杂分析、代码重构、批量操作等场景
- **路由策略**：LLM 产生多个独立 tool_calls 时自动走 Plan-Execute；也可通过 `/plan` 命令强制启用

### 核心模块

| 模块 | 职责 |
|------|------|
| `core/plan_models.py` | Task/Plan 数据模型，DAG 依赖管理，状态机转换 |
| `core/planner.py` | 调用 LLM 生成结构化 Plan，JSON 提取，DAG 环检测 |
| `core/plan_executor.py` | DAG 执行引擎，拓扑排序 + 并行执行，失败传播 |
| `core/agent_loop.py` | LangGraph 状态图集成，新增 plan/execute_plan 节点 |

### 数据模型

```python
Task:
  id: str              # 唯一标识 task_1, task_2...
  description: str     # 任务描述
  tool_name: str?      # 指定工具（可选）
  tool_args: dict?     # 工具参数（可选）
  dependencies: list   # 依赖的 task_id 列表
  status: Enum         # pending → running → completed/failed/skipped

Plan:
  id: str              # 计划唯一标识
  goal: str            # 用户原始目标
  tasks: list[Task]    # DAG 任务集合
  status: Enum         # created → running → completed/failed
```

### 执行引擎

**并行策略**：每轮取所有 `PENDING` 且依赖已满足的任务，最多 `max_parallel`（默认 4）个并行执行。

**单任务执行流程**：
1. 权限检查 → 拒绝则标记 FAILED
2. PreToolUse Hook 通知
3. 工具调用
4. PostToolUse Hook 通知
5. 状态更新（COMPLETED / FAILED）

**失败传播**：任务失败后，所有直接/间接依赖该任务的后继任务自动标记 SKIPPED，避免无意义执行。

**DAG 校验**：规划器在生成计划后通过 DFS 检测循环依赖，确保执行不会死锁。

### 与现有系统的集成

- **ToolRegistry**：执行引擎直接复用，通过 `registry.get(tool_name)` 获取工具实例
- **PermissionPolicy**：每个任务执行前调用 `is_allowed()` 检查权限
- **HookManager**：PreToolUse/PostToolUse 事件在任务执行前后触发
- **Renderer**：新增 `print_plan_created`、`print_plan_progress`、`print_plan_result` 三个渲染方法
- **CLI**：新增 `/plan` 命令切换 Plan-Execute 模式

## 关键设计决策

### 为什么选择 DAG 而非线性列表

线性列表无法表达并行性。DAG 结构允许执行引擎自动识别无依赖的任务并并行执行，在 I/O 密集型场景（如同时读取多个文件、并发 Web 请求）中可显著减少总执行时间。

### 为什么路由层而非独立模式

用户不应需要手动判断任务复杂度。路由层根据 LLM 输出自动选择执行路径：单工具调用走 ReAct（低延迟），多工具调用走 Plan-Execute（高可靠性）。同时保留 `/plan` 命令供用户强制使用。

### 为什么先规划后执行而非边想边做

ReAct 模式下 LLM 每步只看当前状态，容易在长链任务中丢失全局目标。Plan-Execute 模式下 LLM 先完整规划再逐步执行，确保任务间的逻辑一致性和依赖正确性。

## 性能特征

- **并行度**：默认最多 4 个任务并行执行，可配置
- **DAG 校验**：O(V+E) DFS，对典型 10-20 个任务的计划几乎无开销
- **内存占用**：Plan 对象轻量，单个任务约 200-500 bytes

## 已知限制与后续迭代

| 优先级 | 功能 | 说明 |
|--------|------|------|
| P2 | 重规划（Replan） | 执行失败时允许 LLM 重新生成部分计划 |
| P2 | 动态参数注入 | 后继任务使用前序任务的结果作为参数 |
| P3 | 失败重试 | 对可重试的失败任务自动重试（如网络超时） |
| P3 | 计划缓存 | 相似目标的计划可复用，减少 LLM 调用 |
