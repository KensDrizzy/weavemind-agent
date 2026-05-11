# 给 Agent CLI 加上 Plan-and-Execute，让 Agent 先规划后执行，支持 DAG

> 来源：https://paicoding.com/column/17/2
> 原文为 Java 版 PaiCLI 实现，本文档整理核心设计思路，供 WeaveMindAgent（Python）参考。
> **注意：原文后半部分被付费墙截断，仅保留可见内容。**

---

## 背景

PaiCLI 第 1 期实现了基础 ReAct Agent，能一步一步执行任务，一边思考一边行动。

但 ReAct 模式的问题：**复杂任务需要很多轮对话**，每一步都需要调用 LLM。

例如"创建一个 Spring Boot 项目，写个 REST API，然后打包运行"这个任务，一共要调用 5 次 LLM，每次都要等网络往返。

第 2 期实现 **Plan-and-Execute** 模式：先让 LLM 制定完整计划，然后按步骤执行，中间不再反复询问 LLM。

---

## 01、Plan-and-Execute 的核心思想

来自论文《Plan-and-Solve Prompting》。

核心思想是 **规划和执行分离**。

好处：

1. **减少 LLM 调用次数**：规划一次，执行多次
2. **可预测性更强**：提前知道整个执行流程
3. **支持并行执行**：识别无依赖的任务并行处理
4. **失败可重试**：某步失败可以单独重试，不用从头来

代价：灵活性降低，如果执行过程中发现计划有问题，需要重新规划。

---

## 02、任务建模

### 为什么需要任务建模

在 ReAct 模式中，任务隐含在对话历史中。LLM 通过阅读历史消息知道当前该做什么。但这种方式有两个问题：

1. **上下文膨胀**：复杂任务需要很多轮对话，随着历史消息越来越长，Token 消耗剧增。
2. **状态不清晰**：对话历史里混杂了思考过程、工具调用、执行结果，很难一眼看出任务执行到哪一步。

任务建模把"做什么"和"怎么做"分离开来。计划阶段确定 **"做什么"**（将目标分解为清单的各个子任务），执行阶段完成 **"怎么做"**（按顺序执行计划）。

### 任务建模详解

#### 任务类设计

```java
public class Task {
    private final String id;              // 任务标识
    private final String description;     // 任务描述
    private final TaskType type;          // 任务类型
    private TaskStatus status;            // 执行状态
    private String result;                // 执行结果
    private String error;                 // 错误信息
    private final List<String> dependencies;    // 依赖的任务ID
    private final List<String> dependents;      // 依赖此任务的ID
    private long startTime;               // 开始时间
    private long endTime;                 // 结束时间
}
```

#### 任务类型（6 种）

- **PLANNING**：规划任务，用于分析和决策
- **FILE_READ**：读取文件，获取信息
- **FILE_WRITE**：写入文件，输出结果
- **COMMAND**：执行命令，如运行脚本
- **ANALYSIS**：分析结果，中间决策
- **VERIFICATION**：验证结果，检查正确性

#### 任务状态（5 种）

- **PENDING**：等待执行
- **RUNNING**：执行中
- **COMPLETED**：已完成
- **FAILED**：执行失败
- **SKIPPED**：被跳过（依赖失败）

#### 任务生命周期

一个任务从创建到完成，完整的生命周期如下所示：

```
PENDING → RUNNING → COMPLETED/FAILED/SKIPPED
```

每个状态转换都有对应的方法：

```java
public void markStarted() {
    this.status = TaskStatus.RUNNING;
    this.startTime = System.currentTimeMillis();
}

public void markCompleted(String result) {
    this.status = TaskStatus.COMPLETED;
    this.result = result;
    this.endTime = System.currentTimeMillis();
}

public void markFailed(String error) {
    this.status = TaskStatus.FAILED;
    this.error = error;
    this.endTime = System.currentTimeMillis();
}
```

记录时间戳有两个用途：一是统计执行耗时，二是分析任务瓶颈，如果某个步骤执行时间特别长，可能需要优化或拆分。

### 依赖关系

复杂任务有先后依赖，比如"写代码"的前置条件是"创建项目"。我们用 DAG（有向无环图）表示依赖关系：

```
A（需求分析）
├── B（UI设计）
│   └── D（前端开发）
├── C（数据库设计）
│   └── E（后端开发）
└── F（测试用例）
    └── G（测试执行）
```

执行计划按这个顺序：A → B、C、F（可并行）→ D、E（可并行）→ G

每个任务可以明确自己的依赖（dependencies），系统会自动计算执行顺序。

### 03、规划器实现

规划器负责责把用户目标分解为复杂任务的计划。

```java
public class Planner {
    private final GLMClient llmClient;

    public ExecutionPlan createPlan(String goal) throws IOException {
        // 1. 构建规划提示
        List<Message> messages = Arrays.asList(
            Message.system(PLANNING_PROMPT),
            Message.user("请为以下任务分步骤制定完整计划，然后按步骤执行，中间不再反复询问。\n" + goal)
        );

        // 2. 调用 LLM 生成计划
        ChatResponse response = llmClient.chat(messages, null);

        // 3. 解析 JSON 计划
        return parsePlan(goal, response.content());
    }
}
```

**规划提示词工程**

关键是给 LLM 一个清晰的提示，让它必须输出 JSON，并且给出完整示例。

### 04、拓扑排序算法

核心方法 `computeExecutionOrder()`，使用拓扑排序把 DAG 转换成线性执行序列。

拓扑排序的基本思想是：

- 找所有入度为 0 的节点（没有依赖的任务）
- 把这些节点加到输出列表
- 移除这些节点及其边
- 重复 1-3、直到所有节点都被处理

我们用 DFS 算法来实现：

```java
public boolean computeExecutionOrder() {
    executionOrder.clear();
    Set<String> visited = new HashSet<>();
    Set<String> visiting = new HashSet<>();

    for (Task task : tasks.values()) {
        if (!visited.contains(task.getId())) {
            if (!topologicalSort(task, visited, visiting)) {
                return false; // 存在环
            }
        }
    }

    Collections.reverse(executionOrder);
    return true;
}

private boolean topologicalSort(Task task, Set<String> visited, Set<String> visiting) {
    String id = task.getId();

    if (visiting.contains(id)) {
        return false; // 存在环，拓扑排序失败
    }
    if (visited.contains(id)) {
        return true;  // 已处理过
    }

    visiting.add(id);

    // 递归处理依赖项
    for (String depId : task.getDependencies()) {
        Task dep = tasks.get(depId);
        if (dep != null) {
            if (!topologicalSort(dep, visited, visiting)) {
                return false;
            }
        }
    }

    visiting.remove(id);
    visited.add(id);
    executionOrder.add(id);
    return true;
}
```

算法的两个集合作用：visiting 是当前递归栈中的节点，用于检测环；visited 是已处理完的节点。

如果检测到环（visiting.contains(id)），说明任务依赖关系有问题，比如 A 依赖 B、B 依赖 C、C 又依赖 A，这样的设计需要报错提醒。

### 05、计划状态管理

执行计划本身也有状态：

- **CREATED**：刚创建，还没开始执行
- **RUNNING**：正在执行中
- **COMPLETED**：所有任务都完成
- **FAILED**：有任务失败
- **CANCELLED**：被取消

状态转换由执行结果决定

---

## 对 WeaveMindAgent 的启发（Python 实现思路）

基于可见内容，WeaveMindAgent 可参考以下方向：

### 数据模型

```python
from enum import Enum
from typing import Optional
from pydantic import BaseModel


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class Task(BaseModel):
    """单个任务节点"""
    id: str
    description: str
    tool_name: Optional[str] = None
    tool_args: Optional[dict] = None
    dependencies: list[str] = []  # 依赖的 task id 列表
    status: TaskStatus = TaskStatus.PENDING
    result: Optional[str] = None


class Plan(BaseModel):
    """执行计划 — DAG 结构"""
    tasks: list[Task]
    goal: str  # 原始用户目标
```

### DAG 执行引擎核心逻辑

```python
from collections import deque


def topological_sort(tasks: list[Task]) -> list[Task]:
    """拓扑排序，返回可执行顺序"""
    in_degree = {t.id: len(t.dependencies) for t in tasks}
    task_map = {t.id: t for t in tasks}
    queue = deque(tid for tid, deg in in_degree.items() if deg == 0)
    order = []

    while queue:
        tid = queue.popleft()
        order.append(task_map[tid])
        for t in tasks:
            if tid in t.dependencies:
                in_degree[t.id] -= 1
                if in_degree[t.id] == 0:
                    queue.append(t.id)

    if len(order) != len(tasks):
        raise ValueError("DAG 中存在循环依赖")
    return order


def get_ready_tasks(tasks: list[Task]) -> list[Task]:
    """获取当前可并行执行的任务（依赖已完成）"""
    completed_ids = {t.id for t in tasks if t.status == TaskStatus.COMPLETED}
    return [
        t for t in tasks
        if t.status == TaskStatus.PENDING
        and all(dep in completed_ids for dep in t.dependencies)
    ]
```

### 与现有 AgentLoop 的集成点

WeaveMindAgent 当前是 ReAct 模式（`core/agent_loop.py` 的 think → route → act 循环）。集成 Plan-and-Execute 的思路：

1. **路由层**：在 `AgentLoop` 的 route 阶段判断任务复杂度，简单任务走 ReAct，复杂任务走 Plan-Execute
2. **Plan 节点**：新增 LangGraph 节点，调用 LLM 生成 `Plan` 对象
3. **Execute 节点**：按 DAG 拓扑序执行任务，无依赖的任务可并行
4. **Replan 节点**：执行失败时重新调用 LLM 调整计划
5. **状态扩展**：`AgentState` 增加 `plan: Optional[Plan]` 字段

---

### 06、运行演进

随着计划的执行，任务状态不断更新。系统首先通过拓扑排序确定执行顺序，然后按顺序执行每个任务。

当所有依赖完成后，一个任务才能被执行。无依赖的任务可以并行执行。

---

## 07、和 ReAct 的对比

### ReAct 模式工作流

```
LLM 调用 → 创建项目 → LLM 调用 → 写代码 → LLM 调用 → 写配置 → 
LLM 调用 → 打包 → LLM 调用 → 执行
```

**特点**：
- 涉及 LLM 调用次数：规划一次，执行次次
- 可预测性更强：提前知道执行流程
- 支持并行执行：无依赖的任务并行处理
- 失败可重试：某步失败可单独重试，不用从头来

**代价：灵活性降低。** 如果执行过程中发现计划有问题，需要重新规划。

### Plan-and-Execute 模式工作流

```
LLM一次性规划 → 创建项目 → 写代码 → 写配置 → 打包 → 执行
```

**优势**：
- **深度 LLM 调用次数**：规划一次，执行多次，可以减少 40% 左右的步骤
- 可预测性更强：提前知道整个执行流程
- 支持并行执行：识别无依赖的任务并行处理
- 失败可重试：某步失败可以单独重试，不用从头来

**代价：灵活性降低**：如果执行过程中发现计划有问题，需要重新规划。

---

## 08、迭代：并行执行

### 并行执行的问题

计划中可能有多个任务可以同时执行（入度为 0 的节点）。比如"创建项目、写 UI 代码、写后端代码"这三个任务可能没有依赖关系，可以并行。

### 执行策略

多个任务可以组成一个执行计划，系统会自动计算任务之间的依赖关系。

#### 拓扑排序：确定执行序列

使用拓扑排序把 DAG 转换成线性执行序列。每个任务可以明确自己的依赖（dependencies），系统会自动计算出执行顺序。

#### 并行执行：找到可执行任务

系统维护任务执行状态，在每个时刻都能找到可执行的任务（所有依赖都已完成）。

```python
def get_executable_tasks(tasks: list[Task]) -> list[Task]:
    """获取当前可执行的任务"""
    completed = {t.id for t in tasks if t.status == TaskStatus.COMPLETED}
    return [
        t for t in tasks
        if t.status == TaskStatus.PENDING
        and all(dep in completed for dep in t.dependencies)
    ]
```

无依赖的任务在起始阶段就可以执行；当某个任务完成后，其他依赖该任务的任务就可以继续执行。

#### 并行执行逻辑

**执行算法**：

1. 初始化：所有任务状态为 PENDING
2. 循环：
   - 找出所有状态为 PENDING 且依赖已满足的任务
   - 这些任务可以并行执行
   - 等待任意任务完成，更新其状态
   - 任务完成后重复寻找下一批可执行任务
3. 终止：所有任务都不再是 PENDING（全部完成、失败或跳过）

### 失败恢复

如果某个任务执行失败：

1. 该任务标记为 FAILED
2. 所有依赖该任务的后续任务标记为 SKIPPED
3. 用户可以选择：
   - 重试失败的任务
   - 忽略失败继续执行
   - 重新规划整个计划

---

## 09、PaiCLI 如何实现

### 核心模块架构

1. **Planner（规划器）**
   - 接收用户目标
   - 调用 LLM 生成执行计划
   - 返回 ExecutionPlan（包含 DAG 结构）

2. **ExecutionEngine（执行引擎）**
   - 接收 ExecutionPlan
   - 使用拓扑排序确定执行顺序
   - 管理任务状态转换
   - 支持并行执行

3. **TaskExecutor（任务执行器）**
   - 根据任务类型调用对应工具
   - 捕获执行结果或错误
   - 更新任务状态

4. **ReplanManager（重规划管理器）**
   - 监测执行失败
   - 决定是否需要重新规划
   - 调用 Planner 生成新计划

### Python 实现思路

在 WeaveMindAgent 中集成 Plan-and-Execute：

```python
# 数据模型
class ExecutionPlan(BaseModel):
    id: str
    goal: str
    tasks: list[Task]
    status: PlanStatus = PlanStatus.CREATED

# 执行引擎
class PlanExecutor:
    def execute(self, plan: ExecutionPlan) -> dict:
        """执行计划，返回结果"""
        plan.status = PlanStatus.RUNNING
        
        while any(t.status == TaskStatus.PENDING for t in plan.tasks):
            # 获取当前可执行任务
            ready_tasks = self.get_ready_tasks(plan.tasks)
            
            # 并行执行
            for task in ready_tasks:
                self.execute_task(task)
        
        plan.status = PlanStatus.COMPLETED
        return {"tasks": plan.tasks, "status": plan.status}

    def execute_task(self, task: Task):
        """执行单个任务"""
        task.mark_started()
        try:
            result = self.call_tool(task.tool_name, task.tool_args)
            task.mark_completed(result)
        except Exception as e:
            task.mark_failed(str(e))
```

---

## 总结

Plan-and-Execute 模式通过**规划与执行分离**，显著降低 LLM 调用次数，提升效率。核心是：

1. **任务建模**：将复杂目标分解为原子任务
2. **DAG 依赖管理**：使用拓扑排序确定执行顺序
3. **并行执行**：充分利用无依赖任务的并行机会
4. **状态管理**：清晰追踪每个任务的执行进度
5. **失败恢复**：支持单任务重试和计划重新规划

WeaveMindAgent 可以在现有 ReAct 框架基础上，为复杂任务启用 Plan-and-Execute 模式，显著提升大规模任务的执行效率。
