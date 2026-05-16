# WeaveMindAgent 改进计划 — 基于 PaiCLI 对标分析

> 分析范围：只看两个项目都已实现的功能，找出 WeaveMindAgent 可参考 PaiCLI 改进的地方。
> 排除：PaiCLI 有但 WeaveMindAgent 没实现的功能（Skill 系统、LSP 诊断、Git 快照等）。

---

## 1. 系统提示词架构 — 从硬编码到分层 .md 文件

### 现状（WeaveMindAgent）

- `core/memory.py` 的 `build_system_message()` 硬编码组装：CLAUDE.md + MEMORY.md + CoreMemory + 相关事实 + 行为规范
- `_behavior_guide()` 方法内嵌 ~130 行 Python 字符串，包含工具使用指导、联网策略、浏览器策略等
- 没有模式区分（ReAct / Plan / Team 用同一个 system prompt）
- 没有用户/项目级覆盖机制
- 没有人格层

### PaiCLI 的做法

- `PromptAssembler` 分层组装：`base.md` + `personalities/calm.md` + `modes/{mode}.md` + `approvals/{mode}.md` + 动态上下文 + `context-management.md` + `handoff.md`
- `PromptRepository` 三级覆盖：内置 → 用户 (`~/.paicli/prompts/`) → 项目 (`.paicli/prompts/`)
- `PromptMode` 枚举：AGENT / PLAN / PLANNER / TEAM_PLANNER / TEAM_WORKER / TEAM_REVIEWER，每个模式有独立 `.md` 文件
- `PromptContext` 携带 approvalMode / memoryContext / externalContext / skillIndex / variables
- 必须包含 `## Language` 段落（验证机制）

### 改进建议

| # | 改进项 | 优先级 | 说明 |
|---|--------|--------|------|
| 1.1 | 提取 prompts 到独立 .md 文件 | **P0** | 将 `_behavior_guide()` 拆为 `prompts/base.md`（身份+语言+工具策略）、`prompts/personalities/default.md`（人格）、`prompts/modes/react.md` / `plan.md` / `team-planner.md` 等 |
| 1.2 | 实现 PromptAssembler + PromptRepository | **P0** | 参考 PaiCLI 的分层组装 + 三级覆盖机制，让提示词可外部化编辑 |
| 1.3 | 为三种模式提供独立提示词 | **P0** | ReAct 模式强调工具决策自主；Plan 模式强调任务拆解和依赖；Team 模式为 Planner/Worker/Reviewer 各写专用提示词 |
| 1.4 | 精简工具使用指导 | **P1** | 当前 `_behavior_guide()` 中 30+ 行工具使用指导应移到 `prompts/tools/` 目录，按需注入（如只有 MCP 工具时才注入浏览器策略） |
| 1.5 | 添加 Handoff 段落 | **P1** | 参考 PaiCLI 的 `handoff.md`：「最终回复要聚焦用户目标：说明完成了什么、验证了什么、还有哪些明确边界。不要虚构未执行的命令或未看到的文件。」 |

---

## 2. ReAct 循环 — 死循环检测 + Token 预算

### 现状（WeaveMindAgent）

- `MAX_ITERATIONS = 50`，`MAX_CONSECUTIVE_TOOL_FAILURES = 2`
- 有工具失败次数追踪 + 自动禁用（`_record_tool_failure`）
- 有 `ContextCompactor.should_compact()` 检查（在 `_think` 中调用）
- **没有**停滞检测（同一工具+参数连续调用 N 次）
- **没有** token 预算追踪（不知道本轮已消耗多少 token）
- **没有** token 统计展示

### PaiCLI 的做法

- `AgentBudget` 三层保险阀：
  1. **Token 预算**：默认 `Integer.MAX_VALUE`（实质不限），显式配置时启用
  2. **停滞检测**：连续 N 轮（默认 3）完全相同的工具名 + 参数 → 判定死循环
  3. **硬轮数上限**：默认 50 轮
- LLM 自决退出（不调工具 = 结束），budget 只做兜底
- 每轮显示 Token 使用统计：`📊 Token: 已用 X / Y (cached: Z) | 耗时: N.Ns`

### 改进建议

| # | 改进项 | 优先级 | 说明 |
|---|--------|--------|------|
| 2.1 | 实现 `AgentBudget` 类 | **P0** | 移植 PaiCLI 的三层保险阀：token 预算 + 停滞检测 + 硬轮数。用 `hashlib` 计算工具调用签名 |
| 2.2 | 在 `_act` 中集成停滞检测 | **P0** | 每次工具调用后记录签名（tool_name + args hash），连续 3 轮相同则强制退出 |
| 2.3 | Token 预算追踪 | **P1** | 累计每轮的 input_tokens + output_tokens，超过阈值时警告或强制退出 |
| 2.4 | 每轮结束显示 Token 统计 | **P1** | 在 `cli/renderer.py` 的 `LLMEnd` hook 中展示 token 使用量和耗时 |
| 2.5 | 对话历史压缩前的图片裁剪 | **P2** | 参考 PaiCLI 的 `pruneHistoricalImagePayloads()`：压缩前把历史消息中的图片 base64 清除，只保留当前轮的图片 |

---

## 3. Plan-Execute — DAG 依赖 + 并行执行 + 重规划

### 现状（WeaveMindAgent）

- `Planner` 生成 `Plan`（steps 列表），`PlanExecutor` 串行执行
- 没有依赖/图结构
- 没有并行执行
- 没有失败重规划
- 没有计划审查（用户确认）
- 每步只做一次 LLM 调用（无多轮工具调用）

### PaiCLI 的做法

- `ExecutionPlan` + `Task` 带依赖关系（DAG）
- `getExecutableTasks()` 找出依赖已全部完成的可执行任务
- 独立任务并行执行（线程池，缓冲输出后按顺序 flush）
- 失败时如果进度 < 50%，自动 `replan()`（带上已完成任务和失败原因重新规划）
- `PlanReviewHandler`：用户可以 approve / supplement / cancel
- 简单目标检测：短文本 + 无多步关键词 → 跳过规划直接执行
- 每个 Task 内部支持多轮工具调用（最多 5 轮）

### 改进建议

| # | 改进项 | 优先级 | 说明 |
|---|--------|--------|------|
| 3.1 | Plan 增加 step 依赖字段 | **P0** | `PlanStep` 新增 `dependencies: list[str]`，Planner 输出 JSON 格式包含依赖 |
| 3.2 | PlanExecutor 支持 DAG 执行 | **P0** | `get_executable_steps()` 找出依赖已完成的步骤，独立步骤并行执行 |
| 3.3 | 步骤内多轮工具调用 | **P0** | 当前每步只做一次 LLM 调用，应改为 ReAct 循环（最多 N 轮），让步骤能完成复杂操作 |
| 3.4 | 失败重规划 | **P1** | 步骤失败且进度 < 50% 时，调用 Planner 重新规划（带上失败原因和已完成步骤） |
| 3.5 | 简单目标检测 | **P1** | 参考 PaiCLI 的 `isSimpleGoal()`：短文本 + 无多步关键词（然后/并且/再/最后）→ 跳过规划直接执行 |
| 3.6 | Plan 审查交互 | **P2** | 用户可以 approve / supplement / cancel 计划再执行 |

---

## 4. Multi-Agent — 修复 Supervisor 路由 + 增强

### 现状（WeaveMindAgent）

- `MultiAgentOrchestrator` 基于 LangGraph Supervisor 模式
- Supervisor 用 LLM 决定路由到哪个 Agent（**脆弱！** LLM 经常输出不合法格式）
- 有硬编码回退规则（planner→worker-1→reviewer→FINISH），但覆盖场景不全
- Worker 用 `create_react_agent`（完整 ReAct）
- 没有并行执行
- 没有重试机制
- 没有工具调用展示
- 没有 per-agent token 追踪

### PaiCLI 的做法

- `AgentOrchestrator` 硬编码流程：planner → worker(s) → reviewer（不用 LLM 路由！）
- Planner 输出 JSON 计划（含依赖）
- Worker 并行执行独立步骤（线程池 + 缓冲输出）
- Reviewer 检查结果（JSON `approved` 格式），不通过则带反馈重试（最多 2 次）
- 工具调用用 emoji 标签展示：`📖 读取 2 个文件`、`✏️ 写入 1 个文件`
- 关键参数提取展示：`└ path/to/file.py`

### 改进建议

| # | 改进项 | 优先级 | 说明 |
|---|--------|--------|------|
| 4.1 | **重构 Supervisor 为硬编码流程** | **P0** | 用 PaiCLI 的硬编码模式替代 LLM 路由：planner → worker(s) → reviewer → FINISH。LLM 路由太脆弱 |
| 4.2 | Planner 输出结构化 JSON | **P0** | 参考 PaiCLI 的 JSON 计划格式（含依赖），而非自由文本 |
| 4.3 | 并行执行独立步骤 | **P1** | 独立步骤分配给不同 Worker 并行执行，缓冲输出后按顺序 flush |
| 4.4 | Reviewer 结果检查 + 重试 | **P1** | Reviewer 输出 JSON `approved`/`issues`，不通过时带反馈让 Worker 重试（最多 2 次） |
| 4.5 | 工具调用 emoji 展示 | **P1** | 参考 PaiCLI 的 `toolLabel()`：`📖 读取`、`✏️ 写入`、`⚡ 执行`、`🔍 搜索`、`🌐 联网`、`🔌 MCP` |
| 4.6 | 关键参数提取展示 | **P2** | 工具调用下方缩进展示关键参数（文件路径、命令、URL 等） |

---

## 5. 上下文管理 — ContextProfile + 压缩改进

### 现状（WeaveMindAgent）

- `ContextCompactor` 有 `should_compact()` + `compact()` + Map-Reduce 策略
- 阈值硬编码：`settings.get("session.compaction_threshold", 80000)`
- 压缩前自动提取事实到长期记忆（`_extract_facts`）
- **没有** ContextProfile（不感知模型 window 大小）
- **没有** per-mode 的 token 预算分配

### PaiCLI 的做法

- `ContextProfile` 从 LLM 客户端获取 `maxContextWindow`，派生所有参数：
  - `agentTokenBudget = window * 0.8`
  - `compressionTriggerRatio = 0.90`
  - `shortTermMemoryBudget = window * 0.45`
  - `memoryContextTokens = min(5000, window / 200)`
- `ConversationHistoryCompactor` 在每次 LLM 调用前检查，按 user message 边界分割（不切断 tool_call/tool_result 对）
- 压缩后重建：`[system] + [user("摘要")] + [assistant("已了解")] + [尾部保留]`

### 改进建议

| # | 改进项 | 优先级 | 说明 |
|---|--------|--------|------|
| 5.1 | 实现 `ContextProfile` | **P1** | 从 LLM 客户端获取 window 大小，派生压缩阈值、记忆预算等参数，替代硬编码 |
| 5.2 | 改进压缩分割策略 | **P1** | 按 user message 边界分割，避免切断 tool_call / tool_result 的成对协议 |
| 5.3 | 压缩后注入上下文提示 | **P2** | 参考 PaiCLI：压缩后插入 `[user("摘要")] + [assistant("已了解")]`，让 LLM 知道有历史上下文 |

---

## 6. 安全策略 — 路径围栏 + 命令审计

### 现状（WeaveMindAgent）

- `PermissionPolicy` + `PermissionMode`（DEFAULT / ACCEPT_EDITS / BYPASS / PERMIT）
- `DANGEROUS_TOOLS = {"Bash"}`，`EDIT_TOOLS = {"Write", "Edit"}`
- HITL 审批支持
- **没有**路径围栏（不限制文件操作范围）
- **没有**命令黑名单
- **没有**操作审计日志

### PaiCLI 的做法

- `PathGuard`：文件操作必须在项目根目录之内
- `CommandGuard`：禁止 `sudo`、`rm -rf` 全盘、`mkfs`、`dd of=/dev`、fork bomb、`curl|sh` 等
- `AuditLog`：所有危险操作记录到 `~/.paicli/audit/audit-YYYY-MM-DD.jsonl`
- 策略拒绝的工具调用返回 `🛡️ 策略拒绝` 前缀，提示模型不要重试

### 改进建议

| # | 改进项 | 优先级 | 说明 |
|---|--------|--------|------|
| 6.1 | 实现 `PathGuard` | **P0** | Write/Edit/Read 工具的路径必须在项目根目录之内，防止路径穿越 |
| 6.2 | 实现 `CommandGuard` | **P0** | Bash 工具的命令黑名单：`sudo`、`rm -rf /`、`mkfs`、`dd of=/dev`、fork bomb 等 |
| 6.3 | 策略拒绝提示模型 | **P1** | 被策略拒绝的工具调用返回 `[策略拒绝]` 前缀 + 原因，提示模型改用更安全的方式 |
| 6.4 | 操作审计日志 | **P2** | 记录所有危险操作到 `.weavemind/audit/audit-YYYY-MM-DD.jsonl` |

---

## 7. CLI 渲染 — Token 统计 + 工具标签

### 现状（WeaveMindAgent）

- Rich-based `InteractionStreamRenderer` 流式输出
- `/mcp` 命令显示 MCP 工具列表
- **没有** token 使用统计展示
- **没有**工具调用 emoji 标签
- **没有**底部状态栏

### PaiCLI 的做法

- 每轮结束显示 Token 统计：`📊 Token: 已用 X / Y (cached: Z) | 耗时: N.Ns`
- 工具调用用 emoji 标签：`📖 读取 2 个文件`、`✏️ 写入 1 个文件`、`⚡ 执行 1 条命令`
- 关键参数缩进展示：`└ path/to/file.py`
- `BottomStatusBar` 实时显示模型、token、HITL 状态
- `/context` 命令显示上下文使用细分（system/tools/conversation 各占多少）

### 改进建议

| # | 改进项 | 优先级 | 说明 |
|---|--------|--------|------|
| 7.1 | Token 统计展示 | **P1** | 每轮结束在 renderer 中展示 token 使用量、cached tokens、耗时 |
| 7.2 | 工具调用 emoji 标签 | **P1** | 参考 PaiCLI 的 `toolLabel()`，为不同工具类型添加 emoji 前缀 |
| 7.3 | `/context` 命令 | **P2** | 显示上下文使用细分：system prompt / tools schema / conversation 各占多少 token |

---

## 实施优先级排序

### P0（核心改进，建议优先实施）

1. **1.1 + 1.2 + 1.3** — 提取 prompts 到 .md 文件 + PromptAssembler + 模式区分
2. **2.1 + 2.2** — AgentBudget + 停滞检测
3. **3.1 + 3.2 + 3.3** — Plan DAG 依赖 + 并行执行 + 步骤内多轮
4. **4.1 + 4.2** — 重构 Multi-Agent Supervisor 为硬编码流程 + Planner JSON 输出
5. **6.1 + 6.2** — PathGuard + CommandGuard

### P1（重要改进）

6. **2.3 + 2.4** — Token 预算追踪 + 统计展示
7. **3.4 + 3.5** — 失败重规划 + 简单目标检测
8. **4.3 + 4.4 + 4.5** — 并行执行 + Reviewer 重试 + 工具 emoji
9. **5.1 + 5.2** — ContextProfile + 压缩分割改进
10. **6.3** — 策略拒绝提示
11. **7.1 + 7.2** — Token 统计 + 工具 emoji

### P2（优化项）

12. **1.4 + 1.5** — 精简工具指导 + Handoff 段落
13. **2.5** — 图片裁剪
14. **3.6** — Plan 审查交互
15. **4.6** — 关键参数展示
16. **5.3** — 压缩后注入提示
17. **6.4** — 操作审计日志
18. **7.3** — `/context` 命令

---

## 预期收益

| 改进项 | 预期效果 |
|--------|----------|
| 提示词分层 | 提示词可外部化编辑，不同模式用不同提示词，效果可控可调 |
| AgentBudget | 杜绝死循环，token 消耗可控，用户体验更好 |
| Plan DAG + 并行 | 复杂任务执行效率提升 2-3x |
| 硬编码 Multi-Agent | 消除 Supervisor LLM 路由的不稳定性 |
| PathGuard + CommandGuard | 安全性大幅提升，防止意外破坏 |
| Token 统计 | 用户知道 token 消耗，成本可控 |
