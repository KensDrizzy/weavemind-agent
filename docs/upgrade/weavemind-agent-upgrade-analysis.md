# WeaveMindAgent 升级改造分析

> 分析时间：2026-07-09
> 分析范围：当前代码库（`core/agent_loop.py`、`agents/orchestrator.py`、`tools/registry.py`、`permissions/policy.py`、`core/compaction.py`、`cli/app.py`、`core/plan_executor.py`、`agents/worker.py` 等）+ `/Users/liquanfeng/lqf/projects/obsidian/tech-wiki/wiki` 中的改进想法
> 目标：识别从"能用"到"好用"再到"可讲出亮点"的升级空间，为后续改造排优先级

---

## 一、整体结论

WeaveMindAgent 已经具备比较完整的基础能力栈：ReAct 循环、Plan-Execute、Multi-Agent Supervisor、子 Agent 委托（Task/BatchDelegate）、MCP 集成、HITL 审批、Code RAG、Knowledge RAG、三层记忆、Skill 渐进式披露、微信通道等。

但对照 wiki 中汇总的 DeepAgents / Hermes / Claude Code MCP / A2A 等改进方向，当前代码在**入口体验、Agent 循环架构、Multi-Agent 编排确定性、Plan-Execute 自动触发、工具上下文成本、MCP 安全、上下文压缩梯度、安全边界**等维度仍有明显差距。

| 维度 | 当前状态 | 目标状态 | 差距等级 |
|------|---------|---------|---------|
| 入口设计 | `WeaveMindCLI` 多步初始化，无编程式 API | `create_weave_agent()` 一行创建 | 🔴 大 |
| Agent 循环 | `_act()`/`_think()` 中散落权限/HITL/循环检测/压缩逻辑 | 中间件链可组装 | 🔴 大 |
| Multi-Agent | Supervisor 仍以 LLM 路由为主，Worker 串行 | 硬编码流程 + Worker 并行 | 🔴 大 |
| Plan-Execute | `_choose_path()` 永远返回 react，除非手动 `/plan` | 自动路由 + 步骤内 ReAct | 🔴 大 |
| 子 Agent | 已实现 Task/BatchDelegate/心跳，但缺少深度限制、结果精炼 | 纵深防御 + 可控并行 | 🟡 中 |
| 工具系统 | `ToolRegistry` 全量加载所有 schema | 延迟加载 + 工具级权限路由 | 🟡 中 |
| MCP 安全 | 基础 HITL + 权限模式 | 提示注入/数据外泄/工具投毒专项防御 | 🟡 中 |
| 上下文管理 | 单阈值 80k + 固定 retain 3 轮 | 模型感知 + 三级压缩 | 🟡 中 |
| 安全边界 | Bash 直接本地执行，文件操作无路径围栏 | PathGuard + CommandGuard + SandboxExecutor | 🟡 中 |
| 分布式 | 单进程架构 | A2A JSON-RPC HTTP 端点（规划中 P0） | 🔴 大 |

---

## 二、当前代码关键发现

### 2.1 AgentLoop（`core/agent_loop.py`）

- `_act()` 长达 230+ 行，集中了：工具可用性检查、失败禁用、权限检查、HITL 审批、工具执行、浏览器工具刷新、Hook 事件。
- `_think()` 长达 210+ 行，集中了：system prompt 组装、Skill buffer 注入、上下文压缩、强制首跳 SearchCode/AskKnowledge、浏览器模式指令注入、MiMo reasoning_content 处理、空响应重试、流式/invoke 双路径。
- `_choose_path()`（第 818–832 行）当前策略：**只要 `force_plan_mode` 为 false，永远返回 `"react"`**。这意味着：
  - `team.auto_detect: true` 的复杂任务会被提前分流到 Multi-Agent；
  - 但 Plan-Execute 模式实际上只有用户手动 `/plan` 才会触发；
  - 早期基于 tool_calls 数量或复杂度的自动路由已被注释为"不再根据 tool_calls 数量判断"。
- `_should_continue()` 里只有浏览器工具有专门的 `_detect_browser_loop`，通用工具循环（如 `Read → Glob → Read` 反复）没有统一检测。
- Token 消耗只在每次 LLM 调用时通过 Hook 发出，**没有累计预算和硬上限**。

### 2.2 Multi-Agent 编排（`agents/orchestrator.py`）

- `make_supervisor_node()` 虽然有"硬性规则兜底"（基于已工作 Agent 状态直接路由），但**兜底只在流程前半段生效**；一旦进入 reviewer 未通过后的重试路径，仍依赖 LLM 路由。
- Worker 节点（`agents/worker.py`）使用 `create_react_agent` 内嵌完整 ReAct，但**两个 Worker 之间是串行执行**的——Supervisor 一次只路由到一个 Worker。
- Reviewer 审查不通过时，代码中有 `retry_count` 状态，但**没有将 reviewer 的具体反馈作为 context 回传给 Worker**。
- Planner 节点只做一次 LLM 调用输出文本计划，**没有结构化 `Plan` 对象**，后续 Worker 需要再次理解自然语言计划。

### 2.3 Plan-Execute（`core/planner.py` + `core/plan_executor.py`）

- `PlanExecutor` 已实现 DAG 拓扑序执行和失败传播（`core/plan_executor.py:255–262`）。
- 但每个 task 的执行只是**单次工具调用**（`tool.invoke(tool_input)`），复杂步骤无法完成。
- **没有步骤间结果传递**：task 的 `tool_args` 是静态的，无法引用前置 task 的结果（如 `{{task_1.result}}`）。
- **没有失败重规划（Replanner）**：任务失败后直接标记 FAILED/SKIPPED，不会重新生成计划。
- `PlanExecutor.execute()` 使用 `asyncio.run()`（第 64 行），在已有事件循环环境（如 Jupyter）会回退到串行。

### 2.4 工具注册表（`tools/registry.py`）

- `ToolRegistry.__init__()` 中一次性调用：`_register_builtins()`、`_register_browser_tools()`、`_register_mcp_tools()`，**所有工具 schema 全量加载**。
- MCP 工具若接多个 server，schema 会一次性塞进 LLM 上下文，没有延迟加载机制。
- 只有全局的 `allowed/disallowed` 集合，**不能对同一 MCP server 内的不同工具做细粒度控制**。
- 没有工具结果缓存和工具使用统计。

### 2.5 权限与安全（`permissions/policy.py`）

- `PermissionPolicy` 只有四种模式 + 全局 allow/deny 列表。
- `needs_confirmation()` 把 `EDIT_TOOLS` 和 `DANGEROUS_TOOLS` 混在一起，**缺少按工具名/通配符的二级路由**。
- 没有路径围栏（PathGuard）：`Read`/`Write`/`Edit` 可以操作任意路径。
- 没有命令黑名单（CommandGuard）：`Bash` 可执行任意命令。
- 没有审计日志。

### 2.6 上下文管理（`core/compaction.py`）

- 单阈值 `session.compaction_threshold`（默认 80k），超过后保留最近 3 轮，旧消息摘要替换。
- 阈值不感知模型窗口大小：Claude 200k 和 MiMo 可能用同一阈值。
- 只有"摘要"一种压缩方式，没有梯度策略。
- `_extract_facts()` 在压缩前**同步执行**，阻塞主流程。

### 2.7 入口与开发者体验（`cli/app.py`）

- `WeaveMindCLI.__init__()` 手动组装 10+ 个组件，没有编程式 API。
- 组件依赖在 `__init__` 中硬编码，不方便替换或扩展。
- 没有模型级 Profile：不同模型（Claude/MiMo/DeepSeek）需要不同的策略、提示词后缀、工具排除列表。

---

## 三、对照 wiki 改进想法的升级方向

### 3.1 P0 — 高优先级（稳定性 + 架构核心）

#### 3.1.1 `create_weave_agent()` 简化入口

**来源**：`weavemind-agent-improve.md` 2.1

**目标**：提供一行代码启动的编程式 API：

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

**当前差距**：`WeaveMindCLI` 是 REPL 专用，没有独立的 Agent 工厂。

**涉及文件**：新增 `weavemind/__init__.py` 或 `create_agent.py`，重构 `cli/app.py` 复用工厂。

**面试关键词**：Batteries-included、渐进式复杂性、工厂方法 + 依赖注入。

#### 3.1.2 AgentLoop 中间件链重构

**来源**：`weavemind-agent-improve.md` 2.6、`upgrade_analysis.md` 2.1

**目标**：把 `_act()`/`_think()` 中的横切关注点拆成可配置中间件：

```python
agent_loop = AgentLoop(middleware=[
    LoopDetectionMiddleware(),      # 通用停滞检测
    HitlMiddleware(),               # HITL 审批
    ToolResultCachingMiddleware(),  # 工具结果缓存
    BudgetMiddleware(),             # Token 预算
])
```

**当前差距**：权限、审批、循环检测、缓存、日志全部耦合在 `_act()` 中，单测困难。

**涉及文件**：新增 `core/middleware/` 目录，重构 `core/agent_loop.py`。

**面试关键词**：管道过滤器模式、AOP 思想、横切关注点分离。

#### 3.1.3 Multi-Agent Supervisor 硬编码流程 + Worker 并行

**来源**：`upgrade_analysis.md` 2.2

**目标**：
- Supervisor 不再依赖 LLM 路由，改为确定性状态机：`planner → worker(s) → reviewer → FINISH`。
- 独立 Worker 步骤并行执行（`asyncio.gather`）。
- Reviewer 不通过时，把具体反馈传回 Worker 重试。

**当前差距**：`make_supervisor_node()` 仍以 LLM 路由为主；Worker 串行；Reviewer 反馈未回传。

**涉及文件**：`agents/orchestrator.py`、`agents/worker.py`、`agents/reviewer.py`。

**面试关键词**：确定性编排、LLM 脆弱性治理、可观测性。

#### 3.1.4 Plan-Execute 自动路由 + 步骤内 ReAct

**来源**：`upgrade_analysis.md` 2.3

**目标**：
- 恢复自动路由：根据任务复杂度或 LLM 自我判断决定是否走 Plan。
- 每个 Plan task 不再只是单次工具调用，而是启动一个子 ReAct 循环。
- 支持步骤间结果传递（如 `{{task_1.result}}` 模板注入）。
- 执行失败且进度 < 50% 时触发 Replanner。

**当前差距**：`_choose_path()` 永远返回 react；task 单次调用；无参数传递；无 Replanner。

**涉及文件**：`core/agent_loop.py`、`core/planner.py`、`core/plan_executor.py`、`core/plan_models.py`。

**面试关键词**：DAG 调度、并行执行、任务编排、失败恢复。

#### 3.1.5 AgentBudget + 通用停滞检测

**来源**：`upgrade_analysis.md` 2.1

**目标**：
- 累计 token 消耗，设置三级保险阀：token 预算、停滞检测、硬轮数上限。
- 通用停滞检测：记录每轮工具调用签名（tool_name + args hash），连续 3 轮相同则强制退出。

**当前差距**：只有 `MAX_ITERATIONS = 50` 硬轮数上限；无累计 token 预算；只有浏览器循环检测。

**涉及文件**：新增 `core/budget.py`，修改 `core/agent_loop.py`。

**面试关键词**：资源预算管理、防死循环、成本可控。

### 3.2 P1 — 中优先级（安全 + MCP 深度 + 上下文优化）

#### 3.2.1 工具级权限路由

**来源**：`weavemind-agent-improve.md` 3.1、`mcp-protocol-deep.md`

**目标**：
```yaml
permissions:
  allow: ["mcp__github__*", "Read", "Glob"]
  deny: ["mcp__db__delete", "Bash"]
```

执行链路：
```
工具执行 → MCP 权限路由 → 匹配 allow? → 直接执行
                         → 匹配 deny?   → 拦截 + 审计
                         → 不明确       → HITL 弹窗
```

**当前差距**：`PermissionPolicy` 只有全局 allow/deny，不能按 server/tool 精确控制。

**涉及文件**：`permissions/policy.py`、`permissions/modes.py`、`tools/hitl_registry.py`。

**面试关键词**：最小权限原则、多级权限路由、审计日志。

#### 3.2.2 Tool Schema 延迟加载

**来源**：`weavemind-agent-improve.md` 3.2、`mcp-protocol-deep.md`

**目标**：
- 启动时只加载工具名称 + 一行描述；schema 在 LLM 判断可能用到时才动态获取。
- 策略可配置：`"lazy"` / `"auto"`（schema 总量 > 上下文 10% 时延迟）/ `"eager"`。

**当前差距**：`ToolRegistry.__init__()` 全量加载所有 schema。

**涉及文件**：`tools/registry.py`、`core/agent_loop.py`。

**面试关键词**：延迟加载模式、Token 预算优化、上下文窗口管理。

#### 3.2.3 MCP 安全三防体系

**来源**：`weavemind-agent-improve.md` 3.3、`claude-code-mcp.md`

**目标**：
- 新增 `mcp_client/security.py`：
  - `MCPSecurityGuard.audit_server()`：审计 server 工具列表和权限范围。
  - `MCPSecurityGuard.analyze_response_risk()`：检测提示注入（分隔符混淆、隐藏指令）。
  - `MCPAuditLog`：记录每个 MCP 工具调用来源、结果、风险判定。
- 社区 server 默认低信任，所有工具需审批；可信 server 支持白名单放行。

**当前差距**：只有通用 HITL，没有针对 MCP 的专项防御。

**涉及文件**：新增 `mcp_client/security.py`，修改 `mcp_client/manager.py`、`permissions/policy.py`。

**面试关键词**：纵深防御、信任链模型、生产级 Agent 安全。

#### 3.2.4 PathGuard + CommandGuard + 审计日志

**来源**：`upgrade_analysis.md` 2.6

**目标**：
- `PathGuard`：文件操作限制在项目根目录内，防止路径穿越（`../../etc/passwd`）。
- `CommandGuard`：Bash 黑名单：`sudo`、`rm -rf /`、`mkfs`、`dd of=/dev`、fork bomb、`curl|sh`。
- 审计日志：`.weavemind/audit/audit-YYYY-MM-DD.jsonl` 记录所有危险操作。

**当前差距**：`Read`/`Write`/`Edit`/`Bash` 无路径/命令限制；无审计日志。

**涉及文件**：新增 `permissions/guards.py`，修改 `tools/builtin/read.py`、`write.py`、`edit.py`、`bash.py`。

**面试关键词**：最小权限原则、安全边界、防御性编程。

#### 3.2.5 ContextProfile + 三级压缩

**来源**：`weavemind-agent-improve.md` 4.1、`upgrade_analysis.md` 2.7

**目标**：
- 根据模型窗口大小派生参数：`agentTokenBudget = window * 0.8`、`compressionTriggerRatio = 0.90`。
- 三级压缩：
  - 轻度（60–80%）：替换工具结果为引用路径。
  - 中度（80–90%）：摘要旧对话。
  - 重度（>90%）：全量压缩 + 事实提取。
- 按 user message 边界分割，避免切断 tool_call/tool_result 配对。

**当前差距**：固定 80k 阈值；单级摘要；可能切断工具调用对。

**涉及文件**：`core/compaction.py`、`core/memory.py`。

#### 3.2.6 SandboxExecutor

**来源**：`weavemind-agent-improve.md` 2.4

**目标**：可切换的执行器：`Local → Subprocess（带限制）→ Docker`。

**当前差距**：`BashTool` 直接执行本地 subprocess，无隔离。

**涉及文件**：新增 `tools/sandbox/` 目录，修改 `tools/builtin/bash.py`。

### 3.3 P2 — 低优先级（扩展能力 + 体验优化）

#### 3.3.1 A2A 分布式（JSON-RPC HTTP）

**来源**：`weavemind-upgrade-plan.md`、`entities/a2a-protocol.md`

**目标**：三阶段升级：
1. JSON-RPC HTTP 端点（零新增依赖）
2. Protobuf + gRPC
3. 完整 A2A Agent Mesh

**当前差距**：单进程架构，无 Agent 间通信能力。

**涉及文件**：新增 `a2a/` 目录。

**面试关键词**：Agent-to-Agent Protocol、能力发布、任务委派。

#### 3.3.2 MCP Server Scope 模型

**来源**：`weavemind-agent-improve.md` 3.4、`mcp-protocol-deep.md`

**目标**：支持 local / project（`.mcp.json`）/ user 三层配置，project 配置覆盖 user 配置，敏感字段用 `${ENV_VAR}` 占位。

**当前差距**：MCP server 配置来源单一。

**涉及文件**：`mcp_client/manager.py`、配置加载逻辑。

#### 3.3.3 记忆系统升级

**来源**：`weavemind-agent-improve.md` 4.2–4.4

**目标**：
- 记忆命名空间隔离（项目/会话/角色）。
- 异步记忆整合（`_extract_facts` 拆为独立后台任务）。
- 可插拔 Memory Backend（JSON / SQLite / Chroma）。

**当前差距**：单一 JSON 文件；事实提取同步阻塞。

**涉及文件**：`core/memory.py`、`core/compaction.py`。

#### 3.3.4 提示组装分段化 + 模型级 Profile

**来源**：`weavemind-agent-improve.md` 2.7、2.8

**目标**：
- 四级分段：`USER → BASE（可替换为 CUSTOM）→ SUFFIX`。
- 按模型注册 strategy/suffix/excluded_tools/subagent_model。

**当前差距**：`prompt_assembler.py` 拼接为单 system prompt；无模型级配置层。

**涉及文件**：`core/prompt_assembler.py`、`core/prompt_repository.py`、新增 `core/profile_manager.py`。

#### 3.3.5 子 Agent 深度限制 + 结果精炼 + Fork 共享 Prompt Cache

**来源**：`hermes-subagent-delegation.md`、`weavemind-agent-improve.md` 2.5

**目标**：
- `max_spawn_depth` 限制嵌套委托深度。
- 子 Agent 只回传最终结论 + 输出尾部摘要，不返回完整消息历史。
- Fork 子 Agent 共享父 Agent prompt cache 和上下文。

**当前差距**：`SubAgentTool` 无深度限制；返回完整消息历史；每个子 Agent 独立 LLM 实例。

**涉及文件**：`agents/subagent.py`、`agents/batch_delegate.py`、`agents/monitor.py`。

---

## 四、推荐实施路线

### Phase 1：核心稳定性（1–2 周）

1. **AgentBudget + 通用停滞检测**（2–3 天）
   - 解决死循环、token 失控这两个最直观的痛点。
2. **Multi-Agent Supervisor 硬编码流程 + Worker 并行**（3–4 天）
   - 让 `/team` 模式真正稳定可用。
3. **PathGuard + CommandGuard + 审计日志**（1–2 天）
   - 补齐安全底线，面试高频问题。

### Phase 2：架构升级（2–3 周）

4. **AgentLoop 中间件链重构**（4–6 天）
   - 最大架构改造，收益最高，但风险也最大。
5. **`create_weave_agent()` 简化入口**（2–3 天）
   - 让框架具备"一行代码启动"能力。
6. **Plan-Execute 自动路由 + 步骤内 ReAct + 参数传递**（3–4 天）
   - 让 `/plan` 模式从手动开关变成自动选择。
7. **Tool Schema 延迟加载**（2–3 天）
   - 解决 MCP 工具多时上下文爆炸的问题。

### Phase 3：安全与 MCP 深度（1–2 周）

8. **工具级权限路由**（2–3 天）
9. **MCP 安全三防体系**（3–4 天）
10. **SandboxExecutor**（2–3 天）

### Phase 4：扩展能力（后续）

11. A2A JSON-RPC HTTP 端点
12. Blockchain Tools / MCP Server for Blocface
13. ContextProfile + 三级压缩
14. 记忆命名空间隔离 + 异步整合

---

## 五、面试亮点映射

| 改进项 | 可讲故事 |
|--------|---------|
| `create_weave_agent()` | Batteries-included 设计哲学、渐进式复杂性、工厂方法 |
| AgentLoop 中间件链 | 管道过滤器模式、AOP 思想、横切关注点分离 |
| Multi-Agent 硬编码流程 | 确定性编排、LLM 脆弱性治理、可观测性 |
| Plan-Execute 自动路由 | DAG 调度、失败恢复、任务编排 |
| Tool Schema 延迟加载 | Token 预算优化、延迟加载模式、上下文窗口管理 |
| MCP 安全三防 | 纵深防御、信任链模型、生产级 Agent 安全 |
| AgentBudget | 资源预算管理、防死循环、成本可控 |
| PathGuard + CommandGuard | 最小权限原则、安全边界、防御性编程 |
| A2A 分布式 | Agent-to-Agent Protocol、能力发布、跨 Agent 协作 |

---

## 六、风险与注意事项

1. **中间件链重构范围大**：`core/agent_loop.py` 是当前核心，改造前建议先补全单元测试，避免影响现有 323 个测试。
2. **Plan-Execute 自动路由容易误判**：需要明确 simple/complex 的判定标准，并做好回退到 ReAct 的兜底。
3. **MCP 安全三防需要样本**：提示注入检测规则需要真实 case 迭代，避免误报/漏报。
4. **Tool Schema 延迟加载可能影响模型选择**：启动时 LLM 只能看到工具名和描述，需要验证模型是否仍能正确选择工具。
5. **A2A 和 Blockchain Tools 属于横向扩展**：建议等核心架构稳定后再做，避免同时多战线并行。

---

## 七、参考文档

- `/Users/liquanfeng/lqf/projects/obsidian/tech-wiki/wiki/projects/weavemind-agent-improve.md`
- `/Users/liquanfeng/lqf/projects/obsidian/tech-wiki/wiki/projects/weavemind-upgrade-plan.md`
- `/Users/liquanfeng/lqf/projects/obsidian/tech-wiki/wiki/concepts/subagent-delegation-pattern.md`
- `/Users/liquanfeng/lqf/projects/obsidian/tech-wiki/wiki/concepts/mcp-protocol-deep.md`
- `/Users/liquanfeng/lqf/projects/obsidian/tech-wiki/wiki/entities/a2a-protocol.md`
- `/Users/liquanfeng/lqf/projects/obsidian/tech-wiki/wiki/raw/articles/hermes-subagent-delegation.md`
- `/Users/liquanfeng/lqf/projects/obsidian/tech-wiki/wiki/raw/articles/claude-code-mcp.md`
- `/Users/liquanfeng/lqf/projects/weavemind-agent/upgrade_analysis.md`
