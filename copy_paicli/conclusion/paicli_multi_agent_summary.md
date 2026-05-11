# PaiCLI Multi-Agent 实现总结

> 来源：PaiCLI multi-agent.pdf（技术派 - Java技术社区）
> 技术栈：Java 17、GLMClient、ExecutorService + BlockingQueue

---

## 一、核心问题：为什么需要 Multi-Agent

单 Agent 的瓶颈：
- 全能型 Agent 什么都做，但什么都不精
- 复杂任务中规划、执行、检查混在一起，容易顾此失彼
- 没有质量把关，执行结果好坏全靠模型自觉

Multi-Agent 的核心价值：**引入多个专职角色**，规划者专门拆任务，执行者专门干活，检查者专门找茬。每个人只做一件事，做精、做深。

---

## 二、架构选型：主从模式 + 三角色分工

### 2.1 为什么选主从而非对等

| 模式 | 优点 | 缺点 |
|------|------|------|
| 对等模式 | Agent 自主协作，灵活 | 通信复杂，调试困难，容易死锁 |
| **主从模式** | 协调逻辑集中，结构清晰，调试方便 | 编排器成为单点 |

PaiCLI 选择主从架构（Orchestrator-SubAgent）：
- 编排器是"主"，负责任务分发和流程控制
- 子 Agent 是"从"，只管干自己的活
- 子 Agent 之间不直接对话，所有消息经过编排器路由

### 2.2 三角色定义

```
规划者（Planner）—— 拿到用户需求，拆成可执行的步骤列表，标注每一步的类型和依赖关系
执行者（Worker）  —— 拿到步骤描述，调用工具完成具体操作（读文件、写文件、跑命令）
检查者（Reviewer）—— 拿到执行结果，判断是否符合要求。通过放行，不通过打回重干
```

---

## 三、代码结构

### 3.1 核心类

| 类 | 职责 |
|----|------|
| `AgentRole.java` | 角色枚举：PLANNER / WORKER / REVIEWER，每个角色有显示名和描述 |
| `AgentMessage.java` | Agent 间通信消息（Java 17 record），6 种消息类型 |
| `SubAgent.java` | 子 Agent 实现：独立角色 + 提示词 + 对话历史，共享 LLM 和工具 |
| `AgentOrchestrator.java` | 编排器：管理规划→执行→审查→汇总全流程 |

### 3.2 消息类型

```java
public record AgentMessage(
    String fromAgent,
    AgentRole fromRole,
    String content,
    Type type
) {
    public enum Type {
        TASK,       // 任务
        RESULT,     // 结果
        FEEDBACK,   // 反馈
        APPROVAL,   // 通过
        REJECTION,  // 拒绝
        ERROR       // 错误
    }
}
```

6 种消息类型覆盖了 Agent 间协作的完整生命周期：下发任务 → 返回结果 → 审查反馈 → 通过/拒绝/出错。

---

## 四、编排器（AgentOrchestrator）核心流程

### 4.1 构造方法

默认创建 1 个规划者、2 个执行者、1 个检查者。两个 Worker 做轮询分配——当一个 Worker 在干活时，另一个可以接下一个步骤，为并行执行做准备。

```java
public AgentOrchestrator(GLMClient llmClient, ToolRegistry toolRegistry, MemoryManager memoryManager) {
    this.planner = new SubAgent("planner", AgentRole.PLANNER, llmClient, toolRegistry);
    this.workers = List.of(
        new SubAgent("worker-1", AgentRole.WORKER, llmClient, toolRegistry),
        new SubAgent("worker-2", AgentRole.WORKER, llmClient, toolRegistry)
    );
    this.reviewer = new SubAgent("reviewer", AgentRole.REVIEWER, llmClient, toolRegistry);
}
```

### 4.2 run() 六阶段流程

```
第一阶段：规划
  编排器把用户任务交给规划者，规划者输出 JSON 格式的执行计划

第二阶段：解析计划
  编排器把 JSON 解析成 ExecutionStep 列表，建立步骤间的依赖关系

第三阶段：执行
  按依赖顺序，把可执行的步骤分配给 Worker。同一批次内无依赖的步骤可并行

第四阶段：审查
  每个步骤执行完后，交给检查者验收。通过标记完成，不通过带上反馈重新执行

第五阶段：处理残留步骤
  某步失败导致后续依赖步骤无法执行，显式提示用户这些步骤被跳过

第六阶段：汇总结果
  把所有步骤的状态和结果汇总，写入记忆，返回给用户
```

---

## 五、依赖管理与并行执行

### 5.1 依赖判定

每步有 `dependencies` 字段，标注依赖哪些步骤。只有所有依赖步骤都 COMPLETED，当前步骤才能执行：

```java
List<ExecutionStep> getExecutableSteps(List<ExecutionStep> steps) {
    Map<String, StepStatus> statusMap = new HashMap<>();
    for (ExecutionStep step : steps) {
        statusMap.put(step.id(), step.status());
    }
    return steps.stream()
        .filter(step -> step.status() == StepStatus.PENDING)
        .filter(step -> step.dependencies().stream()
            .allMatch(dep -> statusMap.get(dep) == StepStatus.COMPLETED))
        .toList();
}
```

### 5.2 并行执行

同一批次有多个互不依赖的步骤时，编排器让它们并行执行：

```java
private void runBatchParallel(List<ExecutionStep> batch, ...) {
    ExecutorService executor = Executors.newFixedThreadPool(parallelism);
    BlockingQueue<SubAgent> workerPool = new LinkedBlockingQueue<>(workers);
    Map<String, ByteArrayOutputStream> buffers = new ConcurrentHashMap<>();

    for (ExecutionStep step : batch) {
        futures.add(executor.submit(() -> {
            SubAgent worker = workerPool.take();  // 从池子取 Worker
            // 执行 + 审查
            workerPool.offer(worker);              // 用完放回
        }));
    }

    // 按 step_id 顺序 flush，保证用户看到有序输出
    for (ExecutionStep step : batch) {
        System.out.print(buffers.get(step.id()).toString());
    }
}
```

关键设计：
- **BlockingQueue Worker 池**：Worker 用完放回，实现轮询分配
- **ConcurrentHashMap 缓冲输出**：每步一个 buffer，并行写入
- **按 step_id 顺序 flush**：保证用户看到的执行过程是有序的

---

## 六、三套系统提示词

### 6.1 规划者提示词

约束只输出 JSON，每步必须有 id、描述、类型和依赖：

```
你是一个任务规划专家。你的职责是分析用户的需求，将其拆解为清晰的执行步骤。
请按以下 JSON 格式输出执行计划：
{
    "summary": "任务摘要",
    "steps": [
        {
            "id": "step_1",
            "description": "步骤描述，要具体明确",
            "type": "FILE_READ | FILE_WRITE | COMMAND | ANALYSIS | VERIFICATION",
            "dependencies": []
        }
    ]
}
```

### 6.2 执行者提示词

列出所有可用工具，并给出使用优先级——涉及代码理解时先用 search_code：

```
你是一个任务执行专家。你的职责是根据给定的任务步骤，调用工具完成具体操作。
可用工具：
1. read_file - 读取文件内容
2. write_file - 写入文件内容
3. list_dir - 列出目录内容
4. execute_command - 执行命令
5. create_project - 创建项目
6. search_code - 语义检索代码库
如果任务涉及理解代码库，请优先使用 search_code 工具。
```

### 6.3 检查者提示词

约束输出 JSON，approved 为 true 放行，false 打回：

```
你是一个质量检查专家。你的职责是检查执行结果是否正确、完整和高质量。
请以 JSON 格式输出检查结果：
{
    "approved": true 或 false,
    "summary": "检查摘要",
    "issues": ["问题1", "问题2"],
    "suggestions": ["建议1", "建议2"]
}
```

### 6.4 工具调用控制

**只有执行者才会调用工具**，规划者和检查者都只做分析和判断：

```java
private boolean shouldUseTools() {
    return role == AgentRole.WORKER;
}
```

设计理由：规划者一旦调了工具，就不再是"规划者"，变成了"又规划又执行"的混合角色。混合角色容易在规划阶段就陷入执行细节，导致计划不够宏观、不够完整。

---

## 七、检查者机制

### 7.1 审查流程

1. 执行者完成步骤后，编排器把原始任务和执行结果交给检查者
2. 解析检查者的审批结果（approved 字段）
3. 未通过则提取问题列表，带上反馈让执行者重新干

### 7.2 保守策略

当检查者输出无法解析时，**默认为"不通过"**：

```java
boolean parseReviewApproval(String reviewContent) {
    if (reviewContent == null || reviewContent.isEmpty()) {
        return false;  // 空内容，默认不通过
    }
    try {
        JsonNode root = mapper.readTree(cleaned);
        JsonNode approvedNode = root.path("approved");
        if (approvedNode.isMissingNode() || approvedNode.isNull()) {
            return false;  // 缺少 approved 字段，默认不通过
        }
        return approvedNode.asBoolean(false);
    } catch (Exception e) {
        // JSON 解析失败：必须同时有肯定关键词且无否定关键词才视为通过
        String lower = reviewContent.toLowerCase();
        boolean hasNegative = lower.contains("未通过") || lower.contains("不通过");
        boolean hasPositive = lower.contains("通过") || lower.contains("合格");
        if (hasNegative) return false;
        if (!hasPositive) return false;  // 既无肯定也无否定，保守判不通过
        return true;
    }
}
```

**为什么选保守策略？** 让问题结果放行的代价远大于让正确结果重试的代价。一条错误代码被放过去，后续步骤可能全跑错；一条正确结果被多审一次，最多多消耗一些 token。

### 7.3 重试机制

审查未通过时，编排器让执行者带上反馈重新执行，最多重试 2 次：

```java
while (!approved && retries < MAX_RETRIES_PER_STEP) {
    retries++;
    String feedbackContext = context + "\n\n之前的执行结果被审查拒绝，原因：\n" + issues;
    AgentMessage retryResult = worker.executeWithContext(taskMsg, feedbackContext, out);
    AgentMessage retryReview = reviewer.review(step.description(), retryResult.content(), out);
    approved = parseReviewApproval(retryReview.content());
}
```

超过 2 次还不过，保留当前结果，不再死磕。

### 7.4 上下文传递

Worker 执行每一步时，编排器注入"已完成的依赖步骤"的上下文，并对结果做截断（超过 500 字符只取前 500 + 省略号），避免 token 撑爆。

---

## 八、对话历史管理

每个 SubAgent 维护独立的对话历史，但每处理完一个独立任务后**清空历史（保留系统提示词）**：

```java
public void clearHistory() {
    GLMClient.Message systemMsg = conversationHistory.get(0);
    conversationHistory.clear();
    conversationHistory.add(systemMsg);
}
```

原因：每个步骤是独立的任务，上一步的对话上下文对下一步没有帮助，反而会干扰模型判断。保留系统提示词就够了——角色设定不能丢。

---

## 九、记忆和工具的共享

Multi-Agent 模式和 ReAct 模式**共享同一套 Memory 和 ToolRegistry**：
- ReAct 模式下 /save 保存的事实，Worker 也能通过记忆检索找到
- /index 里建立的代码索引，Worker 调用 search_code 也能搜到

---

## 十、模式切换

| 命令 | 模式 | 适用场景 |
|------|------|----------|
| 默认 | ReAct | 一步能搞定的简单任务 |
| /plan | Plan-Execute | 多步有依赖的任务 |
| /team | Multi-Agent | 多步需要分工且每步要验收的任务 |

Multi-Agent 任务执行完后，自动回到默认的 ReAct 模式。

---

## 十一、PaiCLI Multi-Agent 的优缺点

### 优点

1. **职责清晰**：三角色各司其职，分工越清晰出错越少
2. **质量把关**：检查者机制保证每步结果可复核
3. **并行执行**：BlockingQueue Worker 池 + 依赖感知调度
4. **保守策略**：宁可多重试也不放过错误结果
5. **上下文隔离**：每步清空历史，避免上下文污染

### 不足

1. **SubAgent 是一次性 LLM 调用**：没有 ReAct 循环，Worker 不能多步推理
2. **规划者只调一次**：如果规划有问题，无法动态调整
3. **检查者不能调工具**：无法验证执行结果（如跑测试验证代码正确性）
4. **Worker 数量固定**：2 个 Worker 硬编码，不能动态扩缩
5. **无共享状态**：Worker 之间只能通过编排器传递依赖步骤的截断结果
6. **流式输出按 step_id 排序 flush**：并行时用户需等整批完成才能看到输出

---

*总结基于 PaiCLI multi-agent.pdf 原文，PaiCLI v5.0.0，Java 实现*
