# WeaveMindAgent 升级改造分析报告

> 分析时间：基于当前代码库 + tech-wiki 改进计划 + PaiCLI 对标分析
> 分析维度：架构设计、Agent 循环、多 Agent 编排、工具系统、安全、上下文管理、记忆、入口体验

---

## 一、整体结论

WeaveMindAgent 已经实现了 **相当完整的基础能力**：ReAct 循环、Plan-Execute、Multi-Agent、子 Agent 委托、MCP 集成、HITL 审批、RAG 检索、记忆系统、Skill 系统、微信通道。但从"能用"到"好用"再到"面试能讲出亮点"，还有以下 **7 个核心差距维度** 需要补齐：

| 维度 | 现状 | 目标状态 | 差距等级 |
|------|------|---------|---------|
| **入口设计** | `WeaveMindCLI` 多步初始化 | `create_weave_agent()` 一行创建 | 🔴 大 |
| **Agent 循环** | `_act()` 中散落权限/HITL/循环检测 | 中间件链可组装 | 🔴 大 |
| **Multi-Agent** | LLM 路由 Supervisor（脆弱） | 硬编码流程 + 并行 Worker | 🔴 大 |
| **Plan-Execute** | 串行执行，每步单次 LLM 调用 | DAG 并行 + 步骤内 ReAct | 🟡 中 |
| **工具系统** | 全量加载所有 schema | 延迟加载 + 权限路由 | 🟡 中 |
| **安全体系** | 基础 HITL + 权限模式 | 纵深防御 + 审计 + 沙箱 | 🟡 中 |
| **上下文管理** | 单阈值压缩 + 固定 retain | 模型感知 + 三级压缩 | 🟡 中 |

---

## 二、逐维度深度分析

### 2.1 Agent 循环（core/agent_loop.py）

#### 现状
- `AgentLoop._act()` 长达 300+ 行，**集中了**：权限检查、HITL 审批、工具不可用处理、工具失败熔断、浏览器循环检测、参数规范化
- `_think()` 也承担了过多职责：system prompt 组装、skill buffer 注入、上下文压缩、强制首跳 SearchCode/AskKnowledge、浏览器模式指令注入、MiMo reasoning_content 处理、空响应重试
- 浏览器循环检测（`_detect_browser_loop`）是特化的硬编码逻辑，和其他循环检测不统一

#### 差距
1. **职责不单一**：一个方法做了太多事，难以单测和维护
2. **横切关注点未分离**：权限、审批、缓存、循环检测都是"横切"逻辑，却和核心业务代码耦合
3. **缺少通用的停滞检测**：只有浏览器有循环检测，通用工具（如 Read→Glob→Read 反复）没有
4. **缺少 Token 预算追踪**：每轮消耗多少 token 没有累计，无法做预算控制

#### 改进方向
- **中间件链重构**：将权限检查、HITL、循环检测、缓存、日志分别抽取为 Middleware，按配置组装链
  ```python
  agent_loop = AgentLoop(middleware=[
      LoopDetectionMiddleware(),      # 通用停滞检测
      HitlMiddleware(),               # HITL 审批
      ToolResultCachingMiddleware(),  # 工具结果缓存
      BudgetMiddleware(),             # Token 预算
  ])
  ```
- **AgentBudget 类**：移植 PaiCLI 的三层保险阀（token 预算 + 停滞检测 + 硬轮数上限）
- **通用停滞检测**：记录每轮工具调用签名（tool_name + args hash），连续 3 轮相同则强制退出

---

### 2.2 Multi-Agent 编排（agents/orchestrator.py）

#### 现状
- Supervisor 用 LLM 结构化输出来决定路由到哪个 Agent
- 虽然有硬编码回退规则（planner→worker-1→reviewer→FINISH），但** Supervisor 仍然是 LLM 驱动的**
- Worker 用 `create_react_agent`，但**没有并行执行**独立步骤
- Reviewer 审查后没有带反馈重试机制

#### 差距
1. **LLM 路由不稳定**：Supervisor 可能输出不合法格式，导致流程卡住或错误路由
2. **Worker 串行执行**：即使步骤无依赖，也串行执行
3. **缺少步骤内多轮**：每个 Worker 只做一次 LLM 调用，无法处理复杂子任务
4. **Reviewer 重试不完整**：审查不通过时没有将具体反馈传给 Worker 修正

#### 改进方向
- **重构为硬编码流程**：参考 PaiCLI 的做法，Supervisor 不用 LLM 路由，而是按固定流程推进：
  `planner → worker(s) → reviewer → FINISH`
  - 只有 planner 和 reviewer 用 LLM，路由是确定性的
- **Worker 并行执行**：独立步骤分配给不同 Worker 并行执行（asyncio.gather）
- **步骤内 ReAct**：每个 Worker 内部走完整的 ReAct 循环（最多 N 轮），而非单次调用
- **Reviewer 反馈重试**：审查不通过时，将 reviewer 的具体意见作为 context 传给 Worker 重试

---

### 2.3 Plan-Execute（core/planner.py + plan_executor.py）

#### 现状
- Planner 生成 DAG 结构计划（支持依赖），`PlanExecutor` 做拓扑排序执行
- 但 **Plan-Execute 当前在 AgentLoop 中的入口有问题**：`_choose_path()` 目前只看 `force_plan_mode`，**不再自动根据复杂度判断**，所有非 `/plan` 的任务都走 ReAct
- 每步只做**一次工具调用**（task.tool_name + tool_args），没有多轮推理
- 没有失败重规划（Replan）
- 没有动态参数注入（后继任务使用前序结果）

#### 差距
1. **Plan-Execute 实际未自动触发**：`_choose_path()` 永远返回 "react"，除非用户手动 `/plan`
2. **步骤执行太简单**：每步只是单次工具调用，复杂步骤无法完成
3. **缺少失败恢复**：任务失败后直接标记 FAILED，没有重试或重规划
4. **缺少参数传递**：步骤间结果不传递，后续步骤无法引用前置结果

#### 改进方向
- **恢复自动路由**：参考早期的复杂度判断，或让 LLM 在 think 阶段决定是否走 plan
- **步骤内 ReAct**：每个 task 的执行不再只是单次 tool invoke，而是启动一个子 ReAct 循环
- **失败重规划**：执行失败且进度 < 50% 时，让 Planner 重新生成计划（带上已完成和失败原因）
- **动态参数注入**：前序任务的结果可通过模板语法（如 `{{task_1.result}}`）注入到后续任务的参数中

---

### 2.4 子 Agent 委托（agents/subagent.py + batch_delegate.py + monitor.py）

#### 现状
- `Task` 和 `BatchDelegate` 已实现
- 工具隔离：`SUBAGENT_BLOCKED_TOOLS` 黑名单
- 心跳监控：`SubAgentMonitor` 区分 idle/thinking 和 in-tool 两种停滞场景
- 非交互审批：`_SubAgentApprovalTool` 包装危险工具

#### 差距
1. **SubAgent 不与外部 LangGraph 互通**：当前子 Agent 是独立 `create_react_agent` 实例，不能复用已有的 `CompiledStateGraph`
2. **缺少 Fork 子 Agent 共享 Prompt Cache**：每个子 Agent 独立创建 LLM 实例，没有共享 prompt cache
3. **结果回传不够精炼**：子 Agent 返回完整消息历史，而非精炼结论
4. **缺少优雅降级**：子 Agent 失败时直接抛异常，没有 fallback 策略

#### 改进方向
- **CompiledStateGraph 作为一等公民**：任何 LangGraph 的 `CompiledStateGraph` 都可以注册为子 Agent
  ```python
  graph = some_langgraph.compile()
  registry.register_subagent("my_graph", graph)
  ```
- **Fork Subagent**：参考 Hermes 的 fork 模式，共享父 Agent 的 prompt cache 和上下文
- **结果只回传结论**：子 Agent 执行完后提取最终结论，而非完整消息历史
- **优雅降级**：子 Agent 超时时返回部分结果 + 超时说明，而非直接失败

---

### 2.5 工具系统（tools/registry.py）

#### 现状
- `ToolRegistry` 在 `__init__` 中**全量加载**所有工具定义和 schema
- MCP 工具、RAG 工具、Knowledge 工具、内置工具全部一次性塞入
- 通过 `get_langchain_tools()` 返回给 LLM `bind_tools`

#### 差距
1. **Schema 全量加载浪费 Token**：如果接了 3 个 MCP server，每个 10+ 工具，工具 schema 会占用大量上下文
2. **没有工具级权限路由**：只有全局的 allowed/disallowed，不能对同一 server 内的不同工具配置不同策略
3. **没有工具结果缓存**：同一参数重复调用时，没有复用上次结果
4. **没有工具使用统计**：不知道哪些工具常用、哪些耗时、哪些失败率高

#### 改进方向
- **LazyToolRegistry（延迟加载）**：
  - 启动时只加载：工具名称 + 一行简短描述
  - 具体 schema 在 LLM 判断可能用到时才动态获取
  - 策略可配置：`"lazy"` / `"auto"`（schema 总量 > 上下文 10% 时延迟）/ `"eager"`
- **工具级权限路由**：
  ```yaml
  permissions:
    allow: ["mcp__github__*", "Read", "Glob"]
    deny: ["mcp__db__delete", "Bash"]
  ```
  匹配 allow → 直接执行；匹配 deny → 拦截 + 审计；不明确 → HITL 弹窗
- **ToolResultCache**：对纯读取类工具（Read、Glob、Grep、SearchCode）做 LRU 缓存
- **工具使用 Metrics**：记录每个工具的调用次数、平均耗时、失败率

---

### 2.6 安全体系（permissions/ + mcp_client/）

#### 现状
- `PermissionPolicy` + `PermissionMode`：DEFAULT / ACCEPT_EDITS / BYPASS / PERMIT
- HITL 审批：危险工具弹窗确认
- `BrowserGuard`：敏感页面保护
- 子 Agent 工具隔离 + 非交互审批

#### 差距
1. **缺少路径围栏（PathGuard）**：Read/Write/Edit 可以操作任意路径，没有限制在项目根目录内
2. **缺少命令黑名单（CommandGuard）**：Bash 可以执行 `sudo rm -rf /` 等危险命令
3. **没有审计日志**：所有危险操作没有被记录到可审计的日志文件中
4. **MCP 专项安全缺失**：没有针对 MCP 的提示注入检测、数据外泄防护、工具投毒审查
5. **没有 SandboxExecutor**：BashTool 直接执行本地 subprocess

#### 改进方向
- **PathGuard**：文件操作必须限制在项目根目录内，防止路径穿越（`../../etc/passwd`）
- **CommandGuard**：Bash 工具的黑名单：`sudo`、`rm -rf /`、`mkfs`、`dd of=/dev`、fork bomb、`curl|sh`
- **审计日志**：`.weavemind/audit/audit-YYYY-MM-DD.jsonl` 记录所有危险操作（工具名、参数、时间、结果）
- **MCP 安全三防体系**：
  - **提示注入检测**：MCP server 返回内容中检测分隔符混淆、隐藏指令
  - **数据外泄防护**：监控 MCP 工具是否尝试上传/外传敏感数据
  - **工具投毒审查**：社区 server 默认标记为"低信任"，所有工具需审批
- **SandboxExecutor**：可切换的执行器：Local → Subprocess（带限制）→ Docker

---

### 2.7 上下文管理（core/compaction.py + core/memory.py）

#### 现状
- `ContextCompactor`：单阈值 80k token，超阈值时保留最近 3 轮，旧消息用 LLM 摘要替换
- 压缩前自动提取事实到长期记忆（`_extract_facts`）
- `MemoryManager`：组装 system prompt（CLAUDE.md + MEMORY.md + CoreMemory + 相关事实）

#### 差距
1. **没有 ContextProfile**：不感知模型窗口大小，阈值是硬编码的 80k
2. **单级压缩策略**：只有"摘要"一种压缩方式，没有梯度策略
3. **压缩可能切断 tool_call/tool_result 对**：按消息数切割，可能切断 LLM 的工具调用和工具结果之间的配对
4. **记忆系统缺少命名空间隔离**：单一 JSON 文件，所有会话共享
5. **事实提取是同步的**：在压缩前同步执行，阻塞主流程

#### 改进方向
- **ContextProfile**：根据模型获取 `maxContextWindow`，派生参数：
  - `agentTokenBudget = window * 0.8`
  - `compressionTriggerRatio = 0.90`
  - `shortTermMemoryBudget = window * 0.45`
- **三级压缩策略**：
  - 轻度（60-80%）：替换工具结果为引用路径
  - 中度（80-90%）：摘要旧对话
  - 重度（>90%）：全量压缩 + 事实提取
- **按 user message 边界分割**：避免切断 tool_call/tool_result 配对
- **记忆命名空间隔离**：按项目/会话/角色隔离记忆存储
- **异步记忆整合**：将 `_extract_facts` 拆出为独立后台任务（cron job）

---

### 2.8 入口与开发者体验（cli/app.py + main.py）

#### 现状
- `WeaveMindCLI.__init__()` 中手动组装：PermissionPolicy、HookManager、MemoryManager、RAG、MCP、Skill、AgentLoop
- 用户需要运行 `python main.py` 进入 REPL，然后交互使用
- 没有编程式 API

#### 差距
1. **没有 Batteries-included 的入口**：不能像 `create_react_agent()` 那样一行代码启动
2. **初始化过程耦合**：组件之间的依赖关系在 `__init__` 中硬编码，不方便替换或扩展
3. **缺少模型级 Profile 配置**：不同模型（Claude/MiMo/DeepSeek）需要不同的策略、提示词后缀、工具排除列表

#### 改进方向
- **`create_weave_agent()` 工厂函数**：
  ```python
  from weavemind import create_weave_agent
  agent = create_weave_agent(
      model="openai:gpt-4o",
      tools=[my_custom_tool],
      system_prompt="你是一个研究助手",
      memory=True,
      mcp_servers=["path/to/server.py"],
      hitl=True,
      skills_dir="./skills",
  )
  result = agent.invoke("分析这个项目")
  ```
- **模型级 Profile 配置**：
  ```python
  profiles = {
      "claude-sonnet": {"strategy": "react", "suffix": "", "excluded_tools": []},
      "mimo": {"strategy": "react", "suffix": "mimo", "supports_reasoning": True},
      "deepseek": {"strategy": "plan_first", "suffix": "deepseek", "excluded_tools": ["Browser"]},
  }
  ```
- **PromptAssembler 分段化**：USER → BASE（可替换为 CUSTOM）→ SUFFIX，支持模式级覆盖

---

## 三、已完成的 vs 规划中的

### 3.1 已完成（从代码中确认）

| 功能 | 完成度 | 说明 |
|------|--------|------|
| SubAgent P0 | ✅ | 工具隔离、BatchDelegate、心跳监测、非交互审批 |
| Knowledge RAG | ✅ | 文档向量索引 + Chroma 检索 + SearchKnowledge |
| Code RAG | ✅ | AST 分块 + 增量同步 + 查询改写 + 重排序 |
| HITL 审批 | ✅ | TerminalHitlHandler + 全部放行 + 修改参数 |
| MCP 集成 | ✅ | stdio/SSE + Chrome DevTools 双模式 + 动态工具注册 |
| Skill 系统 | ✅ | 三层扫描 + 渐进式披露 |
| 微信通道 | ✅ | iLink Bot API + 远程只读安全策略 |

### 3.2 规划中但尚未实现（高优先级）

| 功能 | 优先级 | 来源 |
|------|--------|------|
| `create_weave_agent()` 简化入口 | P0 | tech-wiki |
| AgentLoop 中间件链重构 | P0 | tech-wiki |
| Multi-Agent Supervisor 硬编码流程 | P0 | PaiCLI 对标 |
| Plan-Execute 步骤内 ReAct + 自动路由 | P0 | PaiCLI 对标 |
| Tool Schema 延迟加载 | P1 | Claude Code MCP |
| 工具级权限路由 | P1 | Claude Code MCP |
| MCP 安全三防体系 | P1 | Claude Code MCP |
| AgentBudget（停滞检测 + Token 预算）| P0 | PaiCLI 对标 |
| PathGuard + CommandGuard | P0 | PaiCLI 对标 |
| ContextProfile + 三级压缩 | P1 | PaiCLI 对标 |

### 3.3 规划中但尚未实现（中低优先级）

| 功能 | 优先级 | 来源 |
|------|--------|------|
| A2A 分布式（JSON-RPC HTTP）| P0 | upgrade-plan |
| Blockchain Tools | P1 | upgrade-plan |
| MCP Server for Blocface | P1 | upgrade-plan |
| 会话上下文持久化存储 | P1 | upgrade-plan |
| SandboxExecutor 沙箱 Shell | P1 | tech-wiki |
| 记忆命名空间隔离 | P2 | tech-wiki |
| 异步记忆整合 Agent | P2 | tech-wiki |
| Pluggable Memory Backend | P2 | tech-wiki |
| MCP Server Scope 模型 | P2 | tech-wiki |

---

## 四、推荐实施路线

### Phase 1：核心稳定性（1-2 周）

**目标**：解决当前最影响用户体验的稳定性问题

1. **AgentBudget + 通用停滞检测**（2-3 天）
   - 解决死循环问题、token 失控问题
   - 这是用户最直观的痛点

2. **Multi-Agent Supervisor 硬编码流程**（3-4 天）
   - 解决 LLM 路由不稳定导致的流程错乱
   - 让 Multi-Agent 模式真正可用

3. **PathGuard + CommandGuard**（1-2 天）
   - 解决安全隐患
   - 面试中"安全性"是高频问题

### Phase 2：架构升级（2-3 周）

**目标**：从"功能堆叠"转向"架构清晰"

4. **AgentLoop 中间件链重构**（4-6 天）
   - 将 `_act()` 和 `_think()` 中的横切关注点分离
   - 这是最大的架构改造，但收益最高

5. **`create_weave_agent()` 简化入口**（2-3 天）
   - 让框架有"一行代码启动"的能力
   - 面试亮点

6. **Tool Schema 延迟加载**（2-3 天）
   - 解决 MCP 工具多时上下文爆炸的问题

7. **Plan-Execute 自动路由 + 步骤内 ReAct**（3-4 天）
   - 让 Plan-Execute 真正自动触发且能处理复杂步骤

### Phase 3：安全与 MCP 深度（1-2 周）

**目标**：补齐 MCP 专项能力

8. **工具级权限路由**（2-3 天）
9. **MCP 安全三防体系**（3-4 天）
10. **审计日志 + SandboxExecutor**（2-3 天）

### Phase 4：扩展能力（后续）

11. A2A 分布式
12. Blockchain Tools
13. ContextProfile + 三级压缩
14. 记忆命名空间隔离

---

## 五、面试亮点映射

将改进点映射到可讲的面试故事：

| 改进项 | 面试话术关键词 |
|--------|--------------|
| `create_weave_agent()` | Batteries-included 设计哲学、渐进式复杂性、工厂方法 |
| AgentLoop 中间件链 | 管道过滤器模式、AOP 思想、横切关注点分离 |
| Multi-Agent 硬编码流程 | 确定性编排、LLM 脆弱性治理、可观测性 |
| Tool Schema 延迟加载 | Token 预算优化、延迟加载模式、上下文窗口管理 |
| MCP 安全三防 | 纵深防御、信任链模型、生产级 Agent 安全 |
| AgentBudget | 资源预算管理、防死循环、成本可控 |
| PathGuard + CommandGuard | 最小权限原则、安全边界、防御性编程 |
| Plan-Execute DAG | DAG 调度、并行执行、任务编排 |

