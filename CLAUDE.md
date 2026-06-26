# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 语言规范

所有 AI 回复、文档、注释、日志、提交信息必须使用**简体中文**。唯一例外：代码标识符遵循项目既有命名约定（英文）。

## 构建与运行

```bash
source .venv/bin/activate && pip install -r requirements.txt   # 安装依赖
./weavemind                                                     # 启动（自动激活 .venv）
python main.py                                                  # 或直接运行
```

配置参照 `config.yaml.example` 复制为 `config.yaml`，通过 `settings.get("dotted.key")` 访问。

## 测试

```bash
pytest tests/                    # 全部测试
pytest tests/test_tools.py       # 单文件
pytest tests/test_tools.py::test_write_and_read -v  # 单测试函数
```

测试框架：pytest，13 个测试文件，覆盖工具、权限、记忆、Plan-Execute、Multi-Agent、HITL、RAG（含增量同步）、MCP/CDP 双模式、Web 搜索。`conftest.py` 将项目根目录加入 `sys.path`。

## 架构

基于 LangGraph 的状态机 Agent，入口在 `cli/app.py` 的 `WeaveMindCLI`。

```
WeaveMindCLI (cli/app.py) — 顶层编排器 + REPL
  ├── AgentLoop (core/agent_loop.py) — LangGraph StateGraph
  │     think → route → plan_or_react ─ react: act → think（循环，上限 50 次迭代）
  │                                   └ plan:  plan → execute_plan → END
  ├── Planner / PlanExecutor (core/planner.py, plan_executor.py) — DAG 规划与并行执行
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
4. **ReAct（默认）**：think/act 循环。护栏：同一工具连续失败 2 次本轮禁用、浏览器工具 3-gram 序列循环检测、空响应（thinking-only）自动重试。

### 核心模块

| 模块 | 关键类 | 职责 |
|------|--------|------|
| `core/agent_loop.py` | `AgentLoop`, `AgentState` | LangGraph 状态机；强制首跳 SearchCode、浏览器模式指令注入、循环检测 |
| `core/planner.py` / `plan_executor.py` / `plan_models.py` | `Planner`, `PlanExecutor`, `Plan`, `Task` | DAG 规划、校验、拓扑序并行执行、失败传播、参数别名归一化 |
| `agents/orchestrator.py` / `worker.py` / `reviewer.py` / `agent_state.py` | `MultiAgentOrchestrator`, `MultiAgentState` | Supervisor 模式多角色协作；Worker 用 `create_react_agent` 内嵌完整 ReAct |
| `core/llm_factory.py` | `create_llm`, `MiMoChatOpenAI` | 多 provider：Anthropic 原生 / OpenAI 兼容端点（MiMo/DeepSeek/OpenAI）；MiMo reasoning_content 捕获与回传 |
| `core/memory.py` | `MemoryManager`, `LongTermMemory`, `CoreMemory` | 三层记忆（详见下文） |
| `core/compaction.py` | `ContextCompactor` | 超阈值压缩 + 压缩前事实沉淀 |
| `core/session.py` | `SessionManager` | 会话元数据 JSON 读写 |
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

### 记忆系统

三层结构（`core/memory.py`）：

- **短期**：LangGraph `add_messages` 管理的消息状态，不单独建类。
- **长期**：`LongTermMemory`，JSON 持久化（`.weavemind/memory/long_term.json`）。写入：MD5 完全去重 → bigram 相似度 > 0.85 原地更新（保留 `updated_from` 元数据）→ 否则新增。检索打分：子串命中 +2.0、字符 bigram Jaccard 相似度、时间衰减（7 天半衰期，保底 30% 权重）。
- **核心**：`CoreMemory`，`user/project/persona` 三块，Agent 通过 `CoreMemoryEdit` 工具修改，始终注入 system prompt（借鉴 Letta Memory Block）。

每轮 `_think` 都重建 system prompt：CLAUDE.md + MEMORY.md + CoreMemory + 按当前问题检索的相关长期记忆 + Skill 索引，经 `PromptAssembler` 拼接 `prompts/` 下的 base/personality/mode 等片段。**运行中修改 CLAUDE.md 即时生效**。

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

### 权限与 HITL

- `PermissionMode`：`default`（危险操作询问确认）/ `acceptEdits`（Edit/Write 免确认，Bash 仍确认）/ `bypassPermissions` / `permit`（仅白名单）。`EDIT_TOOLS = {"Write", "Edit"}`, `DANGEROUS_TOOLS = {"Bash"}`，另有 `CHROME_DANGEROUS_TOOLS`。
- HITL 默认启用（`/hitl off` 或 `hitl.enabled: false` 关闭），在 `AgentLoop._act` 中统一拦截，支持 approve / reject / skip / 修改参数后执行 / 全部放行。

### 斜杠命令（cli/commands.py）

`/help` `/memory` `/save <事实>` `/sessions` `/mode [default|acceptEdits|bypassPermissions]` `/plan` `/team` `/hitl [on|off|status]` `/mcp [status|tools|health]` `/browser [status|connect|disconnect]` `/skill [list|show|on|off|reload]` `/index [目录] [source]` `/search <关键词> [--source 名]` `/clear` `/exit`

## 已知限制

- `SubAgentTool`（agents/subagent.py，工具名 "Task"）和 `.weavemind/agents/*.md` 子 Agent 定义（agents/loader.py）已实现但**未注册到 ToolRegistry**，当前 LLM 无法调用；Multi-Agent 协作走的是 `agents/orchestrator.py`。
- 会话持久化仅写入 `{"message_count": N}` 元数据（cli/app.py `_run_agent` finally 块），对话内容不落盘，`SessionManager.resume()` 无实际恢复能力。
- `team.auto_detect` 开启时每次输入都会额外调用一次分类 LLM，有延迟与成本开销。
- RAG 仅 Python 有 AST 分块器，其他语言走行级 FallbackChunker。
- MiMo provider 禁用了 streaming（`MiMoChatOpenAI._stream` 为空生成器，保证 reasoning_content 不丢失），走 invoke 路径。
- Embedding 默认走 OpenAI 兼容端点（DashScope 等），需配置 `rag.embedding.*` 与对应 API Key。

## 运行时文件

- `config.yaml` — 运行时配置（参照 `config.yaml.example`：llm/providers/memory/session/team/tools/rag）
- `prompts/` — system prompt 片段（base.md、personality.md、modes/、context.md、handoff.md）
- `.weavemind/MEMORY.md` — 注入 system prompt 的项目记忆
- `.weavemind/memory/long_term.json` / `core.json` — 长期记忆与核心记忆块
- `.weavemind/sessions/` — 会话元数据 JSON
- `.weavemind/agents/*.md` — 子 Agent 定义（YAML frontmatter，当前未接入）
- `.weavemind/skills/` — 项目级 Skill；`~/.weavemind/skills/` 用户级；`~/.weavemind/skills.json` 启用状态
- `.weavemind/chroma/` — Chroma 向量库；`.weavemind/rag/` — FTS5 库与索引元数据
- `.weavemind/settings.json` — 项目级权限和 MCP 配置
- `.weavemind/cmd_history` — REPL 输入历史

## 开发规范

- 新增工具必须继承 `WeaveMindTool`（`tools/base.py`），并在 `ToolRegistry._register_builtins()` 中注册；注册顺序影响 LLM 选择倾向，检索类靠前
- MCP 相关异步操作必须投递到持久后台事件循环，禁止 `asyncio.run()`
- 子 Agent 角色扩展在 `agents/` 下实现节点工厂函数，通过 `Command(goto=...)` 回到 supervisor
- Skill 通过在三层目录下添加 `<name>/SKILL.md` 定义，frontmatter 至少含 `name`、`description`
- 配置通过 `settings.get("dotted.key")` 访问，不要直接读取 `config.yaml`
- 所有代码文件使用 UTF-8 无 BOM 编码
- 注释描述意图和约束，不重复代码逻辑
- 禁止 MVP/占位符实现，提交前必须完成全量功能
