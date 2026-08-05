# WeaveMindAgent 架构详解

> 本文档是 CLAUDE.md 的详细版。CLAUDE.md 只保留每轮开发必需的核心约定，
> 以控制其被注入 system prompt 的 token 成本（详见"中断恢复与幂等"上方说明）。

## 架构

```
WeaveMindCLI (cli/app.py) — 顶层编排器 + REPL
  ├── AgentLoop (core/agent_loop.py) — LangGraph StateGraph
  │     think → route → plan_or_react ─ react: act → think（循环，上限 50 次迭代）
  │                                   └ plan:  plan → execute_plan → END
  ├── Planner / PlanExecutor (core/planner.py, plan_executor.py) — DAG 规划与并行执行
  ├── CheckpointerProvider (core/checkpoint.py) — LangGraph checkpoint 持久化 + thread 标记
  ├── TaskRecordStore (core/task_records.py) — Plan 任务级执行记录（中断重入幂等）
  ├── MultiAgentOrchestrator (agents/orchestrator.py) — Supervisor 编排 Planner/Worker×2/Reviewer
  ├── HitlToolRegistry (tools/hitl_registry.py) — 工具注册 + 人工审批拦截
  ├── PermissionPolicy (permissions/policy.py) — 按模式控制工具调用
  ├── HookManager (hooks/manager.py) — LLM/工具/Plan 事件总线，流式渲染经由 Hook 驱动
  ├── MemoryManager (core/memory.py) — 长期记忆 + 核心记忆 + system prompt 组装
  ├── ContextCompactor (core/compaction.py) — token 计数 + Map-Reduce 摘要压缩
  ├── MCPManager (mcp_client/manager.py) — MCP 连接管理、工具聚合、Chrome 双模式切换
  ├── SkillRegistry + SkillContextBuffer (skills/) — 三层 Skill 渐进式披露
  └── CodeRAGPipeline (rag/pipeline.py) — AST 分块 + Chroma + SQLite FTS5 混合检索
```

### 执行模式与路由

用户输入在 `cli/app.py:_run_agent` 分流，优先级从高到低：

1. **Multi-Agent**（`/team` 手动开启）：Supervisor LLM 路由 + 基于"已工作 Agent"的硬性规则兜底；Reviewer 输出结构化 JSON 审查（解析失败保守判不通过），不通过带反馈重派 Worker，最多重试 2 次。
2. **自动 Team 检测**（`team.auto_detect: true`，默认开）：`AgentLoop.classify_complexity()` 用 `team.classifier_*` 配置的小模型判 simple/complex，complex 自动走 Multi-Agent。手动 `/plan` 优先于自动检测。
3. **Plan-Execute**（`/plan` 显式开启，设置 `force_plan_mode`）：`Planner.create_plan()` 让 LLM 产出 JSON 格式 DAG（Pydantic `Plan`/`Task` 校验 + DFS 循环依赖检测）；`PlanExecutor` 每轮取依赖就绪任务并行执行（`max_parallel=4`），失败沿依赖链递归传播为 SKIPPED。
4. **ReAct（默认）**：think/act 循环。护栏：同一工具连续失败 2 次本轮禁用、浏览器工具 3-gram 序列循环检测、通用停滞检测（同一 (工具+参数) 签名连续重复：写类 3 次 / 读类 5 次触发，`_detect_stagnation`）、空响应（thinking-only）自动重试。

### LLM 调用重试（core/llm_retry.py）

- **错误分类**：可重试（429 限流、500/502/503/504、超时、连接重置）vs 不可重试（401/400/SSL/解析错误，立即失败）。
- **退避策略**：500ms 起步翻倍、上限 30s、±20% 抖动；服务端 `Retry-After` 优先。统一入口 `call_with_retry()`，已接入 `_think`、Planner、ContextCompactor、复杂度分类器。
- **流式特例**：SSE 已开始输出后断连**不重放整个流**（避免用户看到重复内容），回退非流式 invoke 后把完整回答补发出来；未输出过内容时流式失败可安全退避重试一次。

### 核心模块

| 模块 | 关键类 | 职责 |
|------|--------|------|
| `core/agent_loop.py` | `AgentLoop`, `AgentState` | LangGraph 状态机；强制首跳 SearchCode、浏览器模式指令注入、循环检测、浏览器工具按需挂载 |
| `core/planner.py` / `plan_executor.py` / `plan_models.py` | `Planner`, `PlanExecutor`, `Plan`, `Task` | DAG 规划、校验、拓扑序并行执行、失败传播、参数别名归一化 |
| `agents/orchestrator.py` / `worker.py` / `reviewer.py` / `agent_state.py` | `MultiAgentOrchestrator`, `MultiAgentState` | Supervisor 模式多角色协作；Worker 用 `create_react_agent` 内嵌完整 ReAct |
| `core/llm_factory.py` | `create_llm`, `MiMoChatOpenAI` | 多 provider：Anthropic 原生 / OpenAI 兼容端点（MiMo/DeepSeek/OpenAI）；MiMo reasoning_content 捕获与回传 |
| `core/memory.py` | `MemoryManager`, `LongTermMemory`, `CoreMemory` | 三层记忆（详见下文） |
| `core/compaction.py` | `ContextCompactor` | 超阈值压缩 + 压缩前事实沉淀 |
| `core/session.py` | `SessionManager` | 完整会话持久化（消息+token）、列出与切换 |
| `core/prompt_assembler.py` / `prompt_repository.py` | `PromptAssembler` | 从 `prompts/*.md` 组装 system prompt |
| `core/hitl_models.py` / `hitl_policy.py` | `ApprovalRequest/Result` | HITL 审批数据模型与危险级别判定 |
| `tools/base.py` / `registry.py` / `hitl_registry.py` | `WeaveMindTool`, `ToolRegistry`, `HitlToolRegistry` | 工具基类、注册（顺序影响 LLM 选择倾向）、审批拦截 |
| `permissions/modes.py` / `policy.py` | `PermissionMode` | DEFAULT / ACCEPT_EDITS / BYPASS / PERMIT |
| `mcp_client/` | `MCPConnection`, `MCPManager` | 见"MCP 与浏览器" |
| `skills/` | `SkillRegistry`, `SkillContextBuffer` | 见"Skill 系统" |
| `rag/` | `CodeRAGPipeline`, `KeywordIndex`, `QueryRewriter`, `ResultReranker` | 见"RAG" |
| `cli/renderer.py` / `commands.py` / `direct_intent.py` / `hitl_handler.py` | — | 流式渲染、斜杠命令、高频操作绕过 LLM 直达、终端审批 |
| `settings.py` | — | 单例 YAML 配置加载器 |

### 工具注册

`ToolRegistry._register_builtins()` 按顺序注册（**注册顺序影响 LLM 选择倾向，SearchCode 最优先**）：

- RAG（`rag.enabled: true` 时）：`SearchCode`, `IndexWorkspace`
- 读取/检索：`Read`, `Glob`, `Grep`, `WebFetch`, `WebSearch`, `AskUser`
- 修改：`Edit`, `Write`, `Bash`
- 记忆：`MemoryAdd`, `MemorySearch`, `CoreMemoryEdit`
- Skill（app.py 注册）：`load_skill`
- 浏览器控制（MCP 初始化后）：`browser_connect`, `browser_disconnect`, `browser_status`
- MCP 动态工具：各 Server 的工具经 JSON Schema → Pydantic 动态转换（`mcp_client/tools.py`）后与内置工具同等注册

`WebSearch` 支持 Tavily / SearXNG / DuckDuckGo 多后端，`web/providers/factory.py` 自动探测，无可用后端时整个工具在本轮被过滤。

**浏览器工具按需挂载**（`mcp.lazy_browser_tools: true`，默认开）：chrome-devtools 的 29 个页面操作工具（click/navigate_page/take_snapshot 等，`mcp_client/chrome_formatter.py:is_chrome_tool`）默认不进 LLM 绑定列表，只在 `AgentLoop._think` 检测到浏览器意图（URL、"网页/浏览器/截图"等关键词）或 shared 模式激活时才挂载（挂载后本进程不再卸下）。`browser_connect/disconnect/status` 三个控制工具始终可用；`_act` 执行走 ToolRegistry 不受绑定过滤影响。

### 记忆系统

三层结构（`core/memory.py`）：

- **短期**：LangGraph `add_messages` 管理的消息状态，不单独建类。
- **长期**：`LongTermMemory`，JSON 持久化（`.weavemind/memory/long_term.json`）。写入：MD5 完全去重 → bigram 相似度 > 0.85 原地更新（保留 `updated_from` 元数据）→ 否则新增。检索打分：子串命中 +2.0、字符 bigram Jaccard 相似度、时间衰减（7 天半衰期，保底 30% 权重）。
- **核心**：`CoreMemory`，`user/project/persona` 三块，Agent 通过 `CoreMemoryEdit` 工具修改，始终注入 system prompt（借鉴 Letta Memory Block）。

每轮 `_think` 都重建 system prompt：CLAUDE.md + MEMORY.md + CoreMemory + 按当前问题检索的相关长期记忆 + Skill 索引，经 `PromptAssembler` 拼接 `prompts/` 下的 base/personality/mode 等片段。**运行中修改 CLAUDE.md 即时生效**。CLAUDE.md 保持精简（详细架构在本文档），以控制每轮注入的 token 成本。

压缩（`core/compaction.py`）：tiktoken 计数超 `session.compaction_threshold`（默认 80000）触发。保留最近 N 轮（默认 3），旧消息先经 `_extract_facts` 提取跨会话事实存入长期记忆，再压缩：< 20 条一次性摘要，≥ 20 条 Map-Reduce（每 5 条分片摘要后合并）。`cli/app.py` 另有 40 条消息滑动窗口兜底裁剪。

### MCP 与浏览器（mcp_client/）

- `MCPConnection`：用 `AsyncExitStack` 维持 stdio / SSE 长连接，连接时缓存 `list_tools()` 结果。
- **事件循环约束**：MCP 工具的同步调用必须经 `asyncio.run_coroutine_threadsafe` 投递到 app.py 启动的持久后台事件循环（`MCPManager.set_mcp_loop`）。**不要用 `asyncio.run()`**——新建事件循环会破坏 stdio 长连接（anyio cancel scope 跨 task 报错）。
- **Chrome DevTools 双模式**：isolated（`--isolated` 临时浏览器，无登录态）/ shared（复用用户 Chrome 登录态）。切到 shared 的三级回退：读 `DevToolsActivePort` 拼 `--wsEndpoint` → `--autoConnect --userDataDir` → 纯 `--autoConnect`。切换 = 重启 MCP Server + 重注册工具（MCPManager 与 ToolRegistry 同步更新）+ 失败回滚；`AgentLoop` 检测到 `browser_connect/disconnect` 执行后重建 LLM 工具绑定。
- 工具结果中检测到登录页/401/403 时自动附加"切 shared 模式重试"提示（`tools.py:_detect_login_hint`）；`_think` 按当前模式注入浏览器操作指令。`BrowserGuard` 提供敏感页面保护。

### Skill 系统（skills/）

三层目录扫描，同名后者覆盖前者：builtin（`skills/builtin/`）→ user（`~/.weavemind/skills/`）→ project（`.weavemind/skills/`）。每个 Skill 是 `<目录>/SKILL.md`，frontmatter 用内置极简 YAML 子集解析器（单行 kv、`|` 多行、行内数组）。

渐进式披露两段流程：① system prompt 只放一行式索引（`SkillIndexFormatter`，description 截断 500 字）；② LLM 按任务语义调 `load_skill`，body 推入 `SkillContextBuffer`（同名替换 + LRU 上限 3），下一轮 `_think` 时 `drain()` 一次性 prepend 到最新 user message，防止跨轮重复注入。

### RAG（rag/）

- **索引**：`PythonASTChunker` 按 import 区 / 类概览（类声明+前 5 行，方法单独成块）/ 方法 / 函数分块，提取 signature 与 docstring；语法错误回退行级 `FallbackChunker`。双索引写入：Chroma 向量（分批 ≤10 条，逐条重试容错）+ SQLite FTS5（`unicode61` 分词，`bm25()` 排序）。
- **增量同步**：索引时按 MD5 跳过未变更文件；检索前 `sync_before_search` 做 mtime 快筛 + MD5 精确确认两级检测，处理变更重索引/删除清理/新增补索引。
- **检索链路**：`QueryRewriter`（规则路：标识符拆分 + 中文意图词→英文代码词同义表；auto 模式对指代型问题升级 LLM 改写，并用 FTS 真实符号提示约束防编造）→ 双路召回 → 混合融合（`semantic×0.5 + keyword×0.3 + 类型加成 + 双命中奖励 0.1`，同文件 ≤2 条）→ `ResultReranker`（heuristic / cross_encoder / llm 可配，失败回退 heuristic）→ `SearchCache`（TTL+LRU，缓存键含索引指纹，索引变更自动失效）。
- **接入主循环**：`rag.force_search_code: true` 时，`AgentLoop._maybe_force_search_code` 对代码库实现类问题在首次 LLM 调用前强制构造 SearchCode tool_call；`_act` 自动为 SearchCode 补 `chat_history` 以支持指代消解。

### 中断恢复与幂等（checkpoint）

- **状态保存粒度**：`AgentLoop` / `MultiAgentOrchestrator` 编译时接入 `SqliteSaver`（`core/checkpoint.py`，`checkpoint.enabled` 默认开，缺依赖回退 `InMemorySaver`），LangGraph 在每个 super-step 边界自动把图状态（messages + plan）写入 `.weavemind/checkpoints.sqlite3`。Plan 节点内部另有 `TaskRecordStore`（`.weavemind/task_records.sqlite3`）按 `(plan_id, task_id)` 记录任务结果，补齐节点内的粒度。
- **恢复定位方式**：每轮用户请求生成独立 thread_id（避免与"整段历史重放"冲突，跨轮行为不变），运行期间写 `.weavemind/active_thread.json` 标记，正常结束/用户主动中断清除，意外异常保留。CLI 启动时检测到标记会询问是否恢复；`AgentLoop.resume()` 以 `graph.stream(None, config)` 从中断前最近 super-step 续跑，恢复后把该轮消息合并回会话历史。
- **副作用幂等**：① `PlanExecutor._execute_task` 先查 TaskRecordStore，已完成任务回填结果不重复调工具；`AgentLoop._act` 按 `(act:{thread_id}, tool_call_id)` 记录每个工具调用结果，节点重跑时回放而不重复执行（HITL 拒绝/跳过等终态也会被记录，恢复时不重复弹审批）；② `Edit` 重跑时若目标内容已就位视为成功（不重复替换），`Write` 内容相同跳过写入；③ `Bash`/MCP 等不可通用幂等的工具靠执行记录保证"完成不重跑"。**已知边界**：工具执行成功到记录落盘之间的极小崩溃窗口会导致该操作重复一次（at-least-once 固有边界），不可幂等工具需业务侧自备幂等键。

### 会话与 token 统计

- 每次启动 CLI 自动开新会话（uuid id）；每轮结束把完整 conversation + 累计 token 落盘到 `.weavemind/sessions/<id>.json`（图片 base64 剥离为占位文本，原子写入）。
- `/sessions` 按更新时间倒序列出历史会话，`/sessions <序号或id前缀>` 切换（保存当前再加载目标），`/new` 显式开新会话，`/clear` 清屏并开新会话。
- token 统计语义：每轮结束行 `(2.0s · 输入 16.6k / 输出 90 · 会话 输入 50.7k / 输出 383)` —— 输入包含 system prompt + 工具 schema + 对话历史（每次调用全量重发，多轮 ReAct 会重复计入），输出是模型实际生成。

### 权限与 HITL

- `PermissionMode`：`default`（危险操作询问确认）/ `acceptEdits`（Edit/Write 免确认，Bash 仍确认）/ `bypassPermissions` / `permit`（仅白名单）。`EDIT_TOOLS = {"Write", "Edit"}`, `DANGEROUS_TOOLS = {"Bash"}`，另有 `CHROME_DANGEROUS_TOOLS`。
- HITL 默认启用（`/hitl off` 或 `hitl.enabled: false` 关闭），在 `AgentLoop._act` 中统一拦截，支持 approve / reject / skip / 修改参数后执行 / 全部放行。

### 斜杠命令（cli/commands.py）

`/help` `/memory` `/save <事实>` `/sessions [序号|id前缀]` `/new` `/mode [default|acceptEdits|bypassPermissions]` `/plan` `/team` `/hitl [on|off|status]` `/mcp [status|tools|health]` `/browser [status|connect|disconnect]` `/skill [list|show|on|off|reload]` `/index [目录] [source]` `/search <关键词> [--source 名]` `/clear` `/exit`

## 已知限制

- `SubAgentTool`（agents/subagent.py，工具名 "Task"）和 `.weavemind/agents/*.md` 子 Agent 定义（agents/loader.py）已实现但**未注册到 ToolRegistry**，当前 LLM 无法调用；Multi-Agent 协作走的是 `agents/orchestrator.py`。
- `team.auto_detect` 开启时每次输入都会额外调用一次分类 LLM，有延迟与成本开销。
- RAG 仅 Python 有 AST 分块器，其他语言走行级 FallbackChunker。
- MiMo provider 禁用了 streaming（`MiMoChatOpenAI._stream` 为空生成器，保证 reasoning_content 不丢失），走 invoke 路径。
- Embedding 默认走 OpenAI 兼容端点（DashScope 等），需配置 `rag.embedding.*` 与对应 API Key。

## 运行时文件

- `config.yaml` — 运行时配置（参照 `config.yaml.example`：llm/providers/memory/session/team/tools/rag）
- `prompts/` — system prompt 片段（base.md、personality.md、modes/、context.md、handoff.md）
- `.weavemind/MEMORY.md` — 注入 system prompt 的项目记忆
- `.weavemind/memory/long_term.json` / `core.json` — 长期记忆与核心记忆块
- `.weavemind/sessions/` — 完整会话 JSON（消息 + 元数据 + token 统计，供 /sessions 切换）
- `.weavemind/checkpoints.sqlite3` — LangGraph checkpoint（图状态，super-step 粒度）
- `.weavemind/active_thread.json` — 运行中 thread 标记（崩溃恢复定位）
- `.weavemind/task_records.sqlite3` — Plan 任务级执行记录（中断重入幂等）
- `.weavemind/agents/*.md` — 子 Agent 定义（YAML frontmatter，当前未接入）
- `.weavemind/skills/` — 项目级 Skill；`~/.weavemind/skills/` 用户级；`~/.weavemind/skills.json` 启用状态
- `.weavemind/chroma/` — Chroma 向量库；`.weavemind/rag/` — FTS5 库与索引元数据
- `.weavemind/settings.json` — 项目级权限和 MCP 配置
- `.weavemind/cmd_history` — REPL 输入历史
