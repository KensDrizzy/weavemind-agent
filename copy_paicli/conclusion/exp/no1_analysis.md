# PaiCLI Agent 核心架构面试题详解

## 文档信息
- **来源**: AI Agent 面试题第一弹：ReAct、Plan-and-Execute、Multi-Agent 核心架构 13 题
- **项目**: PaiCLI — Java Agent CLI（对标 Claude Code）
- **原文地址**: https://paicoding.com/column/17/15
- **分析日期**: 2026年5月14日

---

## 一、项目背景与技术栈

### 1.1 项目简介
PaiCLI 是一个从零开始用 Java 实现的终端 AI Agent，历经 21 期迭代，从 400 行代码演进到完整产品形态。对标 Claude Code，覆盖 Agent 从原理到产品的全链路。

### 1.2 技术栈
| 组件 | 技术 |
|------|------|
| 语言 | Java 17 |
| 构建 | Maven |
| LLM 接口 | GLM-5.1 / DeepSeek V4 / Kimi K2.6 多模型 |
| HTTP 客户端 | OkHttp + SSE 流式解析 |
| 终端交互 | JLine3 |
| 向量存储 | SQLite |
| 版本控制 | JGit 快照管理 |
| 测试 | JUnit 5 + Mockito |

### 1.3 三种核心架构模式
1. **ReAct**: 简单问答、单文件修改（默认模式，80%场景）
2. **Plan-and-Execute**: 创建项目、多文件重构（有依赖关系的多步骤任务）
3. **Multi-Agent**: 大规模任务、需要质量保障（分工协作 + 审查机制）

---

## 二、13道面试题详细解析

### 01、什么是 ReAct 模式？它和 Chain-of-Thought 有什么区别？

#### 核心概念
ReAct（Reasoning + Acting）是将推理（Reasoning）和行动（Acting）交错进行的模式。

#### ReAct 循环流程
```
用户输入
    ↓
[思考 Thought] LLM 分析当前状态，决定下一步行动
    ↓
[行动 Action] 调用具体工具（如文件读写、命令执行）
    ↓
[观察 Observation] 获取工具执行结果
    ↓
循环直到 LLM 决定结束（输出 final answer）
```

#### ReAct vs Chain-of-Thought
| 维度 | Chain-of-Thought | ReAct |
|------|------------------|-------|
| 本质 | 纯文本推理 | 推理 + 工具调用 |
| 输出 | 一步一步思考过程 | Thought + Action + Observation |
| 能力 | 只能生成文本 | 可以与外部环境交互 |
| 用途 | 解决纯推理问题 | 需要外部信息的实际任务 |

#### PaiCLI 实现
- 入口：`Agent.java`
- 核心：while 循环 + 迭代计数器
- 最大循环：50 轮
- 每轮 LLM 返回：思考内容 + 工具调用指令

---

### 02、Agent 怎么知道该调用哪个工具？如果 LLM 返回了不存在的工具怎么办？

#### 工具选择机制
通过 **LLM Function Calling** 协议：
1. 系统把可用工具列表插入到 `tools` 参数中
2. 每个工具定义包含：`name`（工具名）、`description`（功能描述）、`parameters`（参数 schema）
3. LLM 根据当前任务和工具描述，决定调用哪个工具
4. 返回格式：`tool_calls` 数组，包含 `name` 和具体参数值

#### 工具注册表（ToolRegistry）
```java
// PaiCLI 内置 9 个工具 + 60+ MCP 外部工具
public class ToolRegistry {
    private final Map<String, Tool> tools = new HashMap<>();
    
    public void register(Tool tool) {
        tools.put(tool.getName(), tool);
    }
    
    public ToolResult execute(String toolName, Map<String, Object> args) {
        Tool tool = tools.get(toolName);
        if (tool == null) {
            return ToolResult.error("Tool not found: " + toolName);
        }
        return tool.execute(args);
    }
}
```

#### 不存在工具的处理
- **请求层校验**：LLM 返回的工具名在 `ToolRegistry` 中找不到时，返回错误信息给 LLM
- **错误信息格式**：`Tool not found: xxx`
- **LLM 纠错**：大多数情况下，LLM 收到错误后会重新选择正确的工具
- **兜底**：如果持续出错，达到最大迭代次数后终止循环，向用户报告问题

---

### 03、ReAct 循环会死循环吗？常见的死循环场景有哪些？PaiCLI 怎么处理死循环的？

#### 死循环的常见场景

| 场景 | 原因 | 例子 |
|------|------|------|
| 工具调用失败无限重试 | 工具执行一直报错，LLM 不断尝试 | 文件不存在却反复读取 |
| 观察结果无变化 | LLM 陷入重复思考，没有进展 | 一直说"我需要更多信息" |
| 目标定义不清 | LLM 不知道何时算完成 | 查询任务但判断标准模糊 |
| 工具选择摇摆 | 两个工具间来回切换 | 先 Read A，再 Read B，再 Read A |

#### PaiCLI 的死循环防护

**第一层：循环次数上限**
```java
// Agent.java
int maxIterations = 50;
int iteration = 0;
while (iteration < maxIterations) {
    // ReAct 循环体
    iteration++;
}
if (iteration >= maxIterations) {
    throw new AgentException("Max iterations reached, possible infinite loop");
}
```

**第二层：AgentBudget 停滞检测**
- 跟踪连续 N 轮是否有"有意义的进展"
- 如果 token 消耗增加但任务状态无变化，主动触发干预

**第三层：系统提示词引导**
- 在 `base.md` 中明确告诉 LLM：
  - 不要重复调用相同的工具获取相同信息
  - 如果工具调用失败超过 2 次，向用户求助
  - 明确结束条件

---

### 04、什么是 Plan-and-Execute 模式？它比 ReAct 好在哪里？

#### 核心思想
先制定完整计划（Plan），再执行（Execute）。把"边想边做"变成"先规划后执行"。

#### 与 ReAct 的对比

| 维度 | ReAct | Plan-and-Execute |
|------|-------|------------------|
| 决策方式 | 每步重新决策 | 一次性规划，按计划执行 |
| 适合任务 | 简单、步骤少 | 复杂、多步骤、有依赖 |
| 可预测性 | 低 | 高 |
| 全局优化 | 局部最优 | 可以全局规划资源分配 |
| 失败恢复 | 可能绕路 | 可以基于计划重新执行 |

#### 适用场景
- ✅ 创建新项目的多文件生成
- ✅ 涉及多个模块的重构
- ✅ 步骤之间有明确依赖关系
- ❌ 简单的单文件修改（规划开销 > 收益）

#### PaiCLI 实现
两个核心类：
1. **ExecutionPlan.java**: 持有任务列表和 DAG 关系
2. **PlanExecuteAgent.java**: 根据 DAG 拓扑排序执行任务

---

### 05、Plan-and-Execute 里的 DAG 是怎么实现的？某个任务失败了怎么办？

#### DAG（有向无环图）实现

**任务结构**：
```java
public class Task {
    private String id;
    private String description;
    private List<String> dependencies; // 依赖的其他 task id
    private TaskStatus status;         // PENDING, RUNNING, COMPLETED, FAILED, SKIPPED
}
```

**拓扑排序执行**：
```
批次1: task_1, task_2（无依赖，可并行）
批次2: task_3（依赖 task_1）, task_4（依赖 task_2）
批次3: task_5（依赖 task_3 和 task_4）
```

**PaiCLI 执行逻辑**：
```java
// 拓扑排序后按批次执行
List<List<Task>> batches = topologicalSort(tasks);
for (List<Task> batch : batches) {
    // 同批次任务并行执行
    List<Future<TaskResult>> futures = batch.stream()
        .map(task -> executor.submit(() -> executeTask(task)))
        .collect(Collectors.toList());
    
    // 等待全部完成
    for (Future<TaskResult> future : futures) {
        TaskResult result = future.get();
        if (result.isFailed()) {
            // 标记失败，下游任务自动 SKIP
            markDownstreamSkipped(task);
        }
    }
}
```

#### 失败处理策略（参考 CI/CD 流水线）

| 情况 | 处理 |
|------|------|
| 任务失败 | 标记为 FAILED |
| 下游依赖 | 所有直接或间接依赖它的任务自动标记为 SKIPPED |
| 独立任务 | 不受影响，继续执行 |
| 重试机制 | 当前 **没有** 任务级重试（强调可预测性） |

---

### 06、Multi-Agent 协作是怎么实现的？各角色的 system prompt 有什么不同？

#### 三角色架构

| 角色 | 职责 | Java 类 |
|------|------|---------|
| **Planner（规划者）** | 拆解任务，输出结构化任务列表 | SubAgent |
| **Worker（执行者）** | 实际执行子任务 | SubAgent |
| **Reviewer（检查者）** | 审查执行结果，给出反馈 | SubAgent |

#### 编排器流程
```
用户输入 "/team 重构登录模块"
    ↓
Planner 拆解:
  task_1: 分析现有登录代码
  task_2: 重构 LoginService（依赖 task_1）
  task_3: 更新单元测试（依赖 task_2）
    ↓
Worker 执行 task_1 → Reviewer 审查
    ↓
  ├─ 通过 → Worker 执行 task_2
  └─ 不通过 → Worker 重做（带反馈，最多 2 次）
```

#### System Prompt 差异

**Planner**（`modes/team-planner.md`）：
- 侧重：任务拆解和依赖分析
- 要求：输出结构化的 JSON 任务列表
- 关注点：任务粒度、依赖关系识别

**Worker**（`modes/team-worker.md`）：
- 侧重：工具使用和执行
- 要求：完整的工具使用指导
- 关注点：如何正确调用工具、处理工具结果

**Reviewer**（`modes/team-reviewer.md`）：
- 侧重：质量标准和反馈格式
- 要求：给出"通过/不通过 + 具体原因"
- 关注点：代码规范、逻辑正确性、边界情况

#### Prompt 分层架构（第19期）
所有 prompt 都拆成独立的 Markdown 文件，位于 `src/main/resources/prompts/` 目录：
- `modes/team-planner.md`
- `modes/team-worker.md`
- `modes/team-reviewer.md`

**好处**：改 prompt 不用改 Java 代码，支持热更新。

---

### 07、Reviewer 审查不通过怎么处理？这个模式和 Code Review 有什么关系？

#### 重试机制

**流程**：
```
Reviewer 给出 "不通过 + 反馈"
    ↓
AgentOrchestrator 把反馈拼接到原始任务
    ↓
交给 Worker 重做
    ↓
Reviewer 再次审查
    ↓
最多 2 次重试
```

**成本考虑**：
- Worker 执行一次 + Reviewer 审查一次 = 至少 2 次 LLM 调用
- 重试 2 次 = 额外 4 次调用
- **限制重试次数的主因**：成本控制

**追问**：为什么不把 Reviewer 的反馈直接塞给 LLM 让它一次改对？
- **答案**：就是这么做的！反馈作为上下文传给 Worker
- **但**：LLM 不是确定性系统，看到反馈也不保证一次改对

#### 与 Code Review 的关系

| CI/CD Code Review | Multi-Agent Reviewer |
|-------------------|---------------------|
| Tech Lead 分任务 | Planner 拆解任务 |
| 开发写代码 | Worker 执行子任务 |
| 审查者提 comment | Reviewer 输出反馈 |
| 不通过打回重写 | 不通过触发重试 |

**区别**：AI Reviewer 的审查标准是 prompt 里定义的，不是人类主观判断。

---

### 08、同一轮 LLM 返回多个 tool_calls 时怎么处理？并行执行的性能提升有多大？

#### 并行工具调用实现

**代码路径**：`Agent.java` 第 7 期实现

```java
// 从 LLM 响应解析所有 tool_calls
List<ToolCall> toolCalls = parseToolCalls(response);

// 提交到线程池并行执行
List<Future<ToolResult>> futures = new ArrayList<>();
for (ToolCall call : toolCalls) {
    futures.add(executor.submit(() -> 
        toolRegistry.executeTool(call.name(), call.arguments())
    ));
}

// 等待全部完成（有统一超时兜底）
for (int i = 0; i < futures.size(); i++) {
    results.add(futures.get(i).get(timeout, TimeUnit.SECONDS));
}

// 按原始顺序拼装结果
// 注意：tool_call_id 必须严格匹配！
```

**关键约束**：LLM 的 API 协议要求每个 tool message 的 `tool_call_id` 和对应的 `tool_call` 严格匹配，**乱序会导致模型理解错误**。

#### 性能提升

**I/O 密集型操作**：
- 3 个文件读取各 100ms
- 串行：300ms
- 并行：约 100ms

**耗时操作**：
- `execute_command` 可能要几秒
- 多个命令并行更能体现优势

**复用**：ReAct、Plan-and-Execute、Multi-Agent Worker 三条路径复用同一套并行工具执行机制，代码不重复。

---

### 09、并行工具调用会有冲突吗？

#### 冲突场景

| 冲突类型 | 例子 |
|----------|------|
| 双写冲突 | 两个工具同时写同一个文件 |
| 读写冲突 | 一个读文件一个改同一个文件 |

#### PaiCLI 的处理策略

**策略**：不做细粒度锁，靠 **LLM 不犯错 + 工程兜底**

**Prompt 层**：
在 `base.md` 里引导 LLM：
```
如果工具之间有依赖关系，模型应分多轮调用
```

**工程兜底**：
1. 每个工具有独立超时，单个卡死不阻塞其他
2. 某个工具执行失败只返回该工具的错误给 LLM，不影响同批次其他工具

**为什么不加文件锁？**
- 做文件级锁成本高：要分析工具参数里的文件路径再做锁管理
- 收益有限：LLM 同轮写冲突的概率本身不高
- **业界共识**：Claude Code、Cursor 等产品也是同样思路

---

### 10、Token 预算是怎么管理的？

#### 背景
- GLM-5.1: 200k token 上下文窗口
- DeepSeek V4: 1M token
- Agent 必须在窗口限制内工作

#### AgentBudget 计算

**预算公式**：
```
可用预算 = maxContextWindow × 80%
// 剩下 20% 留给 LLM 的输出
```

**单次请求可用空间**：
```
available = 预算 - system_prompt_tokens 
                  - tools_definition_tokens 
                  - 当前对话历史 tokens
```

#### TokenBudget + ContextCompressor

```java
// TokenBudget 实时跟踪
tokenBudget.track(message);

// 接近阈值时触发 Map-Reduce 摘要压缩
if (tokenBudget.getUsage() > threshold) {
    // Map 阶段：长对话分段摘要
    List<String> chunks = splitConversation(history);
    List<String> summaries = chunks.stream()
        .map(chunk -> llm.summarize(chunk))
        .collect(Collectors.toList());
    
    // Reduce 阶段：合并成总摘要
    String finalSummary = llm.summarize(String.join("\n", summaries));
    
    // 用摘要替代原始历史
    history.replaceWithSummary(finalSummary);
}
```

#### 长上下文模式（第12期升级）

**条件**：窗口 ≥ 100k 的模型

**策略**：直接 **跳过摘要压缩**

**原因**：
- 200k 窗口的模型，80% 预算就是 160k
- 日常开发的对话很难用到这么多
- 不压缩体验更好（没有信息损失）

---

### 11、ReAct、Plan-and-Execute、Multi-Agent 三种模式怎么选？

#### 决策矩阵

| 场景 | 推荐模式 | 理由 |
|------|----------|------|
| 简单问答、单文件修改 | **ReAct** | 一两步搞定，规划是浪费 |
| 创建项目、多文件重构 | **Plan-and-Execute** | 步骤多、有依赖，需要先规划 |
| 大规模任务、需要质量保障 | **Multi-Agent** | 分工协作 + 审查机制 |

#### PaiCLI 的设计

```
默认 ReAct
    ↓
/plan  → 切换到 Plan-and-Execute
/team  → 切换到 Multi-Agent
    ↓
执行完自动回到 ReAct
```

**数据**：日常使用中 **80%** 的交互 ReAct 就能搞定。

#### 模式路由层（进阶方案）

**追问**：能不能让 Agent 自己判断用哪种模式？

**答案**：可以，但要加控制。

**设计**：
```
用户输入进来
    ↓
特征判断：
  - 是不是简单问答
  - 是否需要工具调用
  - 是否涉及多文件修改
  - 是否有明显步骤依赖
  - 是否适合并行拆分
  - 风险是不是比较高
    ↓
结构化决策输出：
  {
    "mode": "react" | "plan" | "team",
    "confidence": 0.9,
    "reason": "..."
  }
    ↓
规则兜底：
  - 用户显式 /plan 或 /team → 尊重用户
  - 模型判断置信度低 → 默认 ReAct
  - 执行中可升级（ReAct → Plan）
```

---

### 12、如果让你从零设计一个 Agent 架构，你怎么做？

#### 四步递进法

**第一步：最小可用的 ReAct 循环**
- 一个 while 循环
- `LlmClient` 接口
- `ToolRegistry` 注册表
- 跑通"用户输入 → LLM 推理 → 工具调用 → 结果返回 → 继续推理"
- **参考**：PaiCLI 第一期，400 行代码

**第二步：加防护**
必须加的三个防护（不加会失控）：
1. **Token 预算** —— 防止超出上下文窗口
2. **循环次数上限** —— 防止死循环
3. **工具超时** —— 防止卡死
- **参考**：第 3 期加 Token 预算，第 6 期加 HITL 审批

**第三步：按需加复杂度**
- 任务复杂了 → Plan-and-Execute（第 2 期）
- 质量要求高了 → Multi-Agent（第 5 期）
- 工具多了 → 并行调度（第 7 期）

**第四步：抽象与可扩展**
- `LlmClient` 接口不绑死模型（第 8 期）
- `ToolRegistry` 支持动态注册 MCP 工具（第 10 期）
- Prompt 从硬编码拆成 Markdown 文件（第 19 期）

#### 关键原则
> **先跑通再优化，先简单再复杂**
> 
> 一上来就设计完美架构是最大的陷阱。

---

### 13、面试中怎么介绍你的 Agent 项目（1分钟版本）

#### 推荐话术

```
我从零开始用 Java 实现了一个 AI Agent CLI，叫 PaiCLI，
对标 Claude Code，分 21 期从 ReAct 循环做到了完整产品。

核心架构方面，实现了 ReAct、Plan-and-Execute、Multi-Agent 三种模式。
ReAct 是默认的，Plan-and-Execute 加了 DAG 拓扑排序支持任务并行，
Multi-Agent 是 Planner-Worker-Reviewer 三角色协作。

工具系统接入了 MCP 协议，支持 stdio 和 Streamable HTTP 两种传输，
内置了 Chrome DevTools 浏览器操控。

安全层有 HITL 审批、路径围栏、命令黑名单、操作审计。

产品化方面做了 Claude Code 风格的 inline TUI、LSP 诊断注入、
Git Side-History 快照回滚、HTTP Runtime API。

整个项目从第一期的 400 行代码演进到 21 期的完整产品形态，
我最大的收获是理解了 Agent 从原理到产品的全链路——
什么时候该用简单方案，什么时候必须加复杂度。
```

#### 面试技巧

> **面试不是背答案，是带着源码讲故事。**

- 说到 ReAct → 打开 `Agent.java` 指那个 while 循环
- 说到 Plan → 指 `ExecutionPlan.java` 的任务依赖图
- 说到 Multi-Agent → 指 `SubAgent.java` 的角色定义和 prompt 文件
- **代码和回答能对上**，面试官就知道你是真做过的

---

## 三、面试可能被拷打的问题及答案

### 【架构设计类】

#### Q1: 为什么要实现三种模式，而不是一个通用模式走天下？

**答案要点**：
- **复杂度匹配原则**：简单任务用复杂模式是浪费
- **ReAct 优点**：响应快、成本低（80%场景够用）
- **Plan 优点**：复杂任务可预测、全局优化、便于错误恢复
- **Multi-Agent 优点**：分工明确、质量保障、类人协作流程
- **关键认知**：没有银弹，每种模式有其最佳适用场景

**延伸追问**：如何判断用哪种模式？
- 答：可以做模式路由层，基于任务特征判断，但要有规则兜底（尊重用户显式选择）

---

#### Q2: Plan-and-Execute 的 DAG 如果成环了怎么办？

**答案要点**：
- 拓扑排序前先做**环检测**
- 发现环时：报错给用户，或者提示 Planner 重新拆解
- PaiCLI 的设计：Planner 的 prompt 里明确要求"任务之间不能形成循环依赖"

**代码示例**：
```java
// Kahn 算法前检测环
if (hasCycle(graph)) {
    throw new PlanningException("Task dependencies contain cycle");
}
```

---

#### Q3: Multi-Agent 模式下，如果 Reviewer 和 Worker 反复拉扯怎么办？

**答案要点**：
- **硬性限制**：最多重试 2 次
- **成本考虑**：每次重试都是 2 次 LLM 调用（Worker + Reviewer）
- **超时兜底**：整体任务有超时限制
- **人工介入**：超次后标记为"完成但带警告"，提示用户人工检查

---

#### Q4: 为什么并行工具调用要保持原始顺序返回？

**答案要点**：
- **协议要求**：LLM API 要求 `tool_call_id` 严格匹配
- **模型理解**：乱序可能导致模型混淆因果关系
- **实现细节**：使用 `Future.get()` 按原始索引收集结果

---

#### Q5: Token 压缩用 Map-Reduce 会不会丢失关键信息？

**答案要点**：
- **会**，这是压缩的代价
- **缓解**：长上下文模型（≥100k）跳过压缩
- **优化**：分段时按语义边界切分（对话轮次、主题切换）
- **兜底**：关键工具结果被显式保留

---

### 【工程实践类】

#### Q6: 工具执行超时是怎么设计的？单个工具卡住会影响其他工具吗？

**答案要点**：
- **独立超时**：每个工具有自己的超时设置
- **线程池隔离**：I/O 密集型用 CachedThreadPool
- **Future.cancel()**：超时后强制中断
- **不影响其他**：同批次其他工具继续执行

---

#### Q7: MCP 工具和普通内置工具有什么区别？

**答案要点**：
| 维度 | 内置工具 | MCP 工具 |
|------|----------|----------|
| 注册方式 | 代码硬编码 | 动态注册 |
| 传输协议 | 直接调用 | stdio / Streamable HTTP |
| 发现机制 | 编译时确定 | 运行时通过 MCP Server 获取 |
| 沙箱 | 共享进程 | MCP Server 独立进程 |
| 代表工具 | Read, Write, Bash | Chrome DevTools, 数据库工具 |

---

#### Q8: HITL（Human-in-the-Loop）审批在什么场景触发？

**答案要点**：
- **危险操作**：Bash 命令、删除文件
- **敏感路径**：访问用户 home 目录外的文件
- **自定义规则**：用户可配置自己的敏感词列表
- **绕过**：开发模式下可关闭

---

### 【场景分析类】

#### Q9: 用户说"优化这个项目的性能"，应该走什么模式？

**答案要点**：
1. **先分析**：任务特征判断
   - 涉及多文件？是 → 不能用纯 ReAct
   - 需要分步骤？性能分析 → 找瓶颈 → 针对性优化
   - 需要质量保障？修改可能引入 bug
2. **推荐**：Plan-and-Execute 或 Multi-Agent
3. **理由**：性能优化通常是多步骤、需要验证结果、风险较高

---

#### Q10: 如果 LLM 总是选择错误的工具，你会怎么调试？

**答案要点**：
1. **检查工具描述**：是否清晰、准确、区分度高
2. **增加 few-shot 示例**：在 prompt 里给工具选择的例子
3. **工具拆分/合并**：粒度是否合适
4. **日志分析**：看 LLM 的 Thought 过程，理解误解点
5. **降级处理**：错误时给 LLM 明确的纠错提示

---

### 【进阶深挖类】

#### Q11: 你的 Agent 和 LangChain/LangGraph 有什么区别？

**答案要点**：
| 维度 | LangChain/LangGraph | PaiCLI |
|------|---------------------|--------|
| 语言 | Python | Java |
| 定位 | 框架 | 产品 |
| 控制权 | 黑盒抽象 | 全链路自建 |
| 学习价值 | 使用工具 | 理解原理 |
| 对标 | 底层基础设施 | Claude Code |

**核心竞争力**：Java 生态、企业级场景、深度定制

---

#### Q12: 如果要把这个 Agent 做成 SaaS 服务，需要做哪些改造？

**答案要点**：
1. **多租户隔离**：Token 配额、资源限制
2. **状态持久化**：会话跨设备恢复
3. **安全加固**：沙箱执行、命令白名单
4. **可观测性**：详细日志、调用链路追踪
5. **计费系统**：按 token/工具调用计费
6. **并发处理**：WebSocket 连接管理、消息队列

---

#### Q13: 你觉得 Agent 的未来发展方向是什么？

**答案要点**（可选方向）：
- **更长的上下文**：1M+ token 成为标配，减少压缩损失
- **多模态**：图像、音频、视频理解成为基础能力
- **更强的工具生态**：MCP 普及，工具即服务
- **协作模式进化**：Agent 之间可以互相委派的网络
- **推理能力强化**：深度思考、自我纠错
- **端侧部署**：轻量级模型 + 端侧工具执行

---

## 四、核心源码参考

### 4.1 关键文件位置
```
paicli/
├── src/main/java/com/paicli/
│   ├── agent/
│   │   ├── Agent.java              # ReAct 核心循环
│   │   ├── PlanExecuteAgent.java   # Plan-and-Execute
│   │   └── AgentOrchestrator.java  # Multi-Agent 编排
│   ├── plan/
│   │   └── ExecutionPlan.java      # DAG 任务管理
│   ├── tool/
│   │   └── ToolRegistry.java       # 工具注册表
│   └── context/
│       ├── AgentBudget.java        # Token 预算
│       └── ContextCompressor.java  # 压缩逻辑
└── src/main/resources/prompts/
    ├── base.md                     # 基础提示词
    └── modes/
        ├── team-planner.md         # Planner 提示词
        ├── team-worker.md          # Worker 提示词
        └── team-reviewer.md        # Reviewer 提示词
```

### 4.2 一期演进时间线
| 期数 | 内容 | 代码行数 |
|------|------|----------|
| 1 | ReAct 基础循环 | 400 |
| 2 | Plan-and-Execute | +300 |
| 3 | Token 预算管理 | +200 |
| 5 | Multi-Agent | +500 |
| 6 | HITL 审批 | +300 |
| 7 | 并行工具调用 | +200 |
| 8 | 多模型支持 | +400 |
| 10 | MCP 协议 | +600 |
| 12 | 长上下文优化 | +300 |
| 19 | Prompt 分层架构 | +200 |
| 21 | 完整产品形态 | ~4000 |

---

## 五、总结

PaiCLI 项目展示了从零构建一个生产级 AI Agent 的完整路径：

1. **架构演进**：从简单 ReAct 循环到复杂 Multi-Agent 协作
2. **工程实践**：Token 管理、超时控制、并发处理、错误恢复
3. **产品化**：安全层、交互层、可观测性
4. **扩展性**：MCP 工具、Prompt 分层、多模型支持

面试考察的核心能力：
- ✅ 理解 ReAct / Plan-and-Execute / Multi-Agent 的区别和适用场景
- ✅ 知道 DAG 拓扑排序、并行工具调用、Token 压缩的实现原理
- ✅ 能说出为什么做某些设计选择（如无任务级重试、无文件锁）
- ✅ 有源码支撑，能指代码讲故事

---

*本文档基于 PaiCLI 项目技术派教程整理，仅供学习参考。*
