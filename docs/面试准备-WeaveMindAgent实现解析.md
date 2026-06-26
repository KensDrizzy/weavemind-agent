# WeaveMind-Agent 实现解析与面试准备

> 按简历五个亮点逐条对照实际代码的讲解，附面试高频问题与参考回答。
> 代码引用格式为 `文件:行号`，均基于当前 main 分支。

---

## 总体架构图

```
cli/app.py (WeaveMindCLI 顶层编排 + REPL)
 ├─ core/agent_loop.py      ← 亮点① ReAct / Plan-Execute 状态机
 │   ├─ core/planner.py + plan_executor.py + plan_models.py  ← DAG 规划与执行
 │   └─ agents/orchestrator.py + worker.py + reviewer.py     ← Multi-Agent
 ├─ core/memory.py + compaction.py                           ← 亮点② 三层记忆与压缩
 ├─ mcp_client/ (client/manager/tools/browser_tools)         ← 亮点③ MCP + CDP 双模式
 ├─ skills/ (registry/parser/formatter/buffer)               ← 亮点④ 三层 Skill
 └─ rag/ (pipeline/keyword_index/retrieval_enhancements/chunkers) ← 亮点⑤ RAG
```

---

## 亮点① 基于 LangGraph 的 ReAct / Plan-and-Execute / Multi-Agent 混合架构

**总述**：这一条对应四组代码。`core/agent_loop.py` 的 `AgentLoop` 用 LangGraph `StateGraph` 实现 ReAct 主循环和 Plan 分支路由；`core/planner.py` 的 `Planner` 负责把目标拆成 DAG；`core/plan_executor.py` 的 `PlanExecutor` 按拓扑序并行执行；`agents/orchestrator.py` 的 `MultiAgentOrchestrator` 实现 Supervisor 模式的多角色协作（配合 `agents/worker.py`、`agents/reviewer.py`、`agents/agent_state.py`）。

### ReAct 主循环

`_build_graph()`（agent_loop.py:408）构建的图是：

```
think → route → plan_or_react ─ react: act → think（循环）
                              └ plan:  plan → execute_plan → END
```

- `_think`（:443）调用 `llm_with_tools.stream()` 流式产出，失败回退 `invoke()`。
- `_should_continue`（:710）检查最后一条 `AIMessage` 是否带 `tool_calls` 决定继续或结束。
- `_act`（:790）逐个执行 tool_call，每个调用走统一流程：**权限检查 → HITL 审批 → PreToolUse Hook → 工具执行 → PostToolUse Hook**。

安全护栏：
- `MAX_ITERATIONS = 50` 硬上限（:43）。
- `_record_tool_failure`（:206）：同一工具连续失败 2 次本轮禁用，并向 LLM 返回可解释错误阻止重试。
- `_detect_browser_loop`（:226）：取最近 20 个工具调用，三种检测——截图/快照 ≥5 次、`evaluate_script` 无导航连续 ≥6 次、3-gram 工具序列重复 ≥3 次（排除含导航的合法多页提取），命中后注入 SystemMessage 停止指令。
- 空响应（thinking-only）检测与自动重试（:608-644）。

### Plan-and-Execute

由 `/plan` 命令设置 `force_plan_mode` 触发：

- `Planner.create_plan()`（planner.py:104）：规划 System Prompt 让 LLM 输出 JSON 任务列表（每个任务带 `tool_name`、`tool_args`、`dependencies`），解析成 Pydantic 的 `Plan`/`Task` 模型（plan_models.py）。
- `_validate_dag()`（planner.py:159）：**三色标记 DFS 检测循环依赖** + 依赖引用有效性校验。
- `PlanExecutor.execute()`（plan_executor.py:54）：每轮调 `plan.ready_tasks()`（plan_models.py:85，取 PENDING 且依赖全部 COMPLETED 的任务），`asyncio.gather` 并行执行（`max_parallel=4`）。
- 失败处理：`_propagate_failure()`（:240）递归把下游依赖任务标记为 SKIPPED——这就是"失败传播"。
- 工程容错：`_normalize_tool_args()`（:194）按工具 `_run` 签名做参数别名归一化（如 `file_path → path`），容忍 LLM 规划时参数名不规范；`_check_required_args` 用 Pydantic schema 校验必填参数。
- 如果 LLM 在 think 阶段已给出完整 tool_calls，`_plan` 直接从 tool_calls 构建计划（agent_loop.py:1052-1055），避免 Planner 重新生成丢参数。

### Multi-Agent（/team 或自动触发）

`AgentLoop.run_multi_agent()`（agent_loop.py:1195）创建 `MultiAgentOrchestrator`。图结构是 **Supervisor 中心辐射型**：所有节点执行完都通过 LangGraph 的 `Command(goto="supervisor")` 回到 Supervisor，由它再路由到 `planner / worker-1 / worker-2 / reviewer` 或 `FINISH`（orchestrator.py:253）。

关键设计：
- **Supervisor 三层容错路由**（orchestrator.py:71）：先从 LLM 回复提取 JSON 路由字段（兼容 `next/agent/action/goto` 等 7 种字段名）→ 回退纯文本关键词匹配 → 都失败则 FINISH。
- **硬性规则路由**（:195）：检查消息里已出现的 Agent name，若 planner 已工作而 worker 未工作，跳过 LLM 直接路由到 worker-1，防止 LLM 重复派活。
- **Worker**：用 `langgraph.prebuilt.create_react_agent` 包装成完整的内嵌 ReAct Agent（worker.py:70），可多步调工具。
- **Reviewer 重试闭环**（reviewer.py）：LLM 输出 `{"approved": ..., "issues": [...]}`，`parse_review_approval()` 保守策略——JSON 解析失败一律判不通过；不通过带反馈回 Supervisor 重派 worker，最多 `MAX_RETRIES = 2` 次。

### 三种模式的真实路由（cli/app.py:_run_agent）

优先级从高到低：
1. `/team` 手动开启 → Multi-Agent。
2. **自动复杂度检测**（`_should_auto_team`，app.py:268）：`team.auto_detect: true`（默认开）时，`classify_complexity()`（agent_loop.py:1169）用 `team.classifier_*` 配置的小模型按 `COMPLEXITY_PROMPT` 判 simple/complex，**complex 自动切 Multi-Agent**；分类失败保守走 simple。手动 `/plan` 优先于自动检测。
3. `/plan` → Plan-Execute。
4. 默认 → ReAct。

---

## 亮点② 三层记忆与上下文压缩机制

**总述**：对应 `core/memory.py`（`MemoryManager` 门面 + `LongTermMemory` + `CoreMemory`）和 `core/compaction.py`（`ContextCompactor`）。压缩器在 `AgentLoop.__init__`（agent_loop.py:100）创建，每次 `_think` 前检查（:483-484），`cli/app.py:_compact_conversation_history` 另有兜底。

### 三层结构

1. **短期记忆**：LangGraph 的消息状态——`AgentState.messages` 用 `Annotated[list, add_messages]`（agent_loop.py:59），框架自动追加合并，不需要自建会话类。
2. **长期记忆**（memory.py:61 `LongTermMemory`）：JSON 文件持久化（`.weavemind/memory/long_term.json`），每次 `store()` 即时落盘。写入三段逻辑：
   - MD5 哈希完全去重；
   - `_find_similar_entry()` 找相似度 > 0.85 的旧记忆做**原地更新**（记录 `updated_from`、`update_similarity` 元数据）；
   - 否则新增。

   检索 `search()`（:188）打分公式：**子串命中 +2.0 + 字符 bigram Jaccard 相似度 + 时间衰减**。bigram 相似度（`_bigram_similarity`，:219）不依赖 jieba 也能做中文模糊匹配；时间衰减为 7 天半衰期 `0.5 ** (age_hours / 168)`，且 `score *= 0.3 + 0.7 * decay` 保底 30% 权重，防止旧记忆完全消失。
3. **核心记忆**（memory.py:249 `CoreMemory`）：借鉴 Letta 的 Memory Block，固定 `user / project / persona` 三个块，Agent 通过 `CoreMemoryEdit` 工具 `set/append/edit` 修改，`to_prompt()` 始终拼入 system prompt。

每轮 `_think` 都调 `MemoryManager.build_system_message(query)`（memory.py:367）重新组装 system prompt：CLAUDE.md + MEMORY.md + CoreMemory + 用最新用户消息检索出的前 5 条相关长期记忆 + Skill 索引，替换消息列表第一条。

### Map-Reduce 压缩（compaction.py）

- 用 tiktoken `cl100k_base` 数 token，超阈值（`session.compaction_threshold`，默认 80000）触发 `compact()`。
- 流程：分离 system 消息 → 保留最近 N 轮（默认 3 轮）不压缩 → **先调 `_extract_facts()`（:145）让 LLM 从将被压缩的旧消息中提取跨会话事实，沉淀进长期记忆** → 旧消息 < 20 条一次性摘要，≥ 20 条 Map-Reduce（:111）：每 5 条一组各自摘要（Map），再合并去重为最终摘要（Reduce）→ 组装成 `system + [对话历史摘要] + 最近消息`。
- `cli/app.py` 还有 `MAX_CONVERSATION_MESSAGES = 40` 的滑动窗口兜底裁剪（先尝试压缩，压不下去才硬截断）。

---

## 亮点③ MCP 外部工具生态 + Chrome DevTools Protocol 双浏览器模式

**总述**：整个 `mcp_client/` 包。`client.py` 的 `MCPConnection` 封装 stdio/SSE 长连接；`tools.py` 做 Schema 动态转换和统一封装；`manager.py` 的 `MCPManager` 管理多 Server 生命周期和 isolated/shared 模式切换；`browser_tools.py` 提供 `browser_connect/disconnect/status` 三个切换工具。

### 长连接通信

`MCPConnection.connect()`（client.py:58）用 `AsyncExitStack` 持有 session 生命周期，避免 async context 提前退出导致连接关闭。stdio 走 `mcp.client.stdio.stdio_client` 拉起本地子进程（把 server 的 stderr 重定向到 devnull 抑制启动 banner），SSE 走 `mcp.client.sse.sse_client` 连远程服务（:135）。连接成功后 `list_tools()` 拿到工具元数据并缓存。

### Schema 动态转换与统一注册

- `tools.py:_create_args_schema`（:50）：把 MCP 工具的 JSON Schema `inputSchema` 用 `pydantic.create_model` 动态生成 Pydantic 模型（处理 required/default/array 嵌套类型映射）。
- `StructuredTool.from_function`（:238）包装成和内置工具一致的 LangChain 工具，注册进同一个 `ToolRegistry`——LLM 看到的 MCP 工具和内置工具没有任何区别。

### 运行时调用的关键工程点

同步的 `sync_func`（tools.py:192）通过 `asyncio.run_coroutine_threadsafe` 把调用投递到 app.py 注入的**持久后台事件循环**（`MCPManager.set_mcp_loop`，manager.py:108；线程在 app.py:188 `_init_mcp_sync` 中创建）——**不能用 `asyncio.run()`**，因为新建/关闭事件循环会破坏 stdio 长连接（anyio TaskGroup 的 cancel scope 跨 task 退出会报 RuntimeError）。

### CDP 双浏览器模式

`MCPManager` 通过名称/参数特征识别 chrome-devtools-mcp Server（client.py:38 `_detect_server_type`）。

- **isolated**：启动参数 `--isolated`，MCP Server 自管一个无登录态的临时 Chrome。
- **shared**：复用用户本机 Chrome 的登录态。`switch_to_shared()`（manager.py:289）三级回退：
  1. 读 Chrome 的 `DevToolsActivePort` 文件（平台相关路径，:53）拼出 `--wsEndpoint ws://127.0.0.1:{port}{path}` 直连；
  2. 不存在则 `--autoConnect --userDataDir`；
  3. 最后纯 `--autoConnect`。

切换本质是 `_restart_chrome_server()`（:348）：断开旧连接 → 新参数重连 → **失败回滚到旧参数** → 成功后 `_re_register_chrome_tools()` 同步刷新 MCPManager 和 ToolRegistry 里的 Chrome 工具，`AgentLoop._refresh_tools_after_browser_switch()`（agent_loop.py:1020）重建 LLM 绑定的工具列表。

自动化闭环：
- `tools.py:_detect_login_hint`（:113）在工具结果中检测登录页/401/403 关键词，自动附加"请调 browser_connect 切到 shared 模式重试"的系统提示。
- `_think` 里按当前模式注入浏览器操作指令（agent_loop.py:490-532），让 LLM 遇到登录墙自动走切换流程而不是反复询问用户。
- `BrowserGuard`（browser_guard.py）做敏感页面保护，shared 模式下敏感页面写操作需用户确认。

---

## 亮点④ 三层 Skill 渐进式披露系统

**总述**：`skills/` 包五个文件 + `tools/builtin/skill_tools.py` 的 `load_skill` 工具 + `agent_loop.py` 的注入逻辑。"渐进式披露"的含义：常驻 prompt 只放一行式索引，完整 Skill 正文由 LLM 按需加载。

### 三层扫描与覆盖

`SkillRegistry.reload()`（registry.py:24）按 `builtin（skills/builtin/）→ user（~/.weavemind/skills/）→ project（.weavemind/skills/）` 顺序扫描三个目录下的 `<skill>/SKILL.md`，存入同一个 dict，**同名后者覆盖前者**——项目级 Skill 可以覆盖内置 Skill。

`SkillFrontmatterParser`（parser.py）是手写的极简 YAML 子集解析器（单行 kv、`|` 多行、行内数组），不引第三方依赖。`SkillStateStore` 管 enable/disable 状态（持久化在 `~/.weavemind/skills.json`）。

### 渐进式披露的两段流程

1. **索引常驻**：`_think` 里把 `SkillIndexFormatter.format(enabled_skills())` 写进 system prompt（agent_loop.py:450-454）。索引只有名字 + 截断到 500 字的 description + 一条判断准则："任务匹配时调用 `load_skill(name)`"。这就是"降低常驻 Prompt 成本"。
2. **按需加载**：LLM 判断任务匹配后调用 `load_skill` 工具，工具把 Skill 完整 body push 进 `SkillContextBuffer`（buffer.py）；下一轮 `_think` 时 `drain()` 一次性取出并 **prepend 到最新 user message 前**（agent_loop.py:471-479）。

Buffer 约束：同名替换 + 最多缓存 3 个 Skill 的 LRU 淘汰；`drain` 是一次性消费，取完即清空，防止同一 Skill body 跨轮重复注入撑爆上下文。

---

## 亮点⑤ 代码库 RAG 检索

**总述**：`rag/` 包四块——`chunkers/python_chunker.py`（AST 分块）、`pipeline.py` 的 `CodeRAGPipeline`（索引 + 混合检索 + 增量同步）、`keyword_index.py` 的 `KeywordIndex`（SQLite FTS5/BM25）、`retrieval_enhancements.py`（`QueryRewriter` / `ResultReranker` / `SearchCache`）。对 Agent 的出口是 `tools/builtin/rag_tools.py` 的 `SearchCode` / `IndexWorkspace` 工具。

### AST 结构化分块（python_chunker.py）

用标准库 `ast` 解析，按结构产出五类 chunk：
- import 区一块；
- 模块级函数每个一块；
- **类只保留"类声明 + 前 5 行"的概览块**（方法单独成块，避免大类整体进一个向量；类体 ≤10 行时包含完整类）；
- 类方法每个完整一块；
- 同时提取 signature 和 docstring 存进元数据。

语法错误时回退到行级 `FallbackChunker`。

### 双路召回

- **语义路**：`langchain_chroma.Chroma` 持久化向量库（pipeline.py:83），embedding 支持 OpenAI 兼容端点/Ollama；写入时分批（DashScope 限制每批 ≤10 条）并逐条重试容错。
- **关键词路**：`KeywordIndex`（keyword_index.py）建 FTS5 虚拟表（`tokenize='unicode61'` 支持中文）+ metadata 表，查询时对每个词项做前缀匹配 `"token"*`，用 SQLite 内置 `bm25()` 排序，分数归一化到 0-1。负责 `MemoryManager` 这类标识符的精确命中。

### Query Rewrite（retrieval_enhancements.py:25）

`rewrite()` 产出最多 3 个查询变体：
- **规则路**（免费且确定）：驼峰/下划线标识符拆词 + 中文意图词→英文代码词同义表（"检索"→search/retrieve 等 20 余条）。
- **LLM 路**：`auto` 模式下遇到含"它/这个/刚才"等指代词的上下文型问题才升级到 LLM 改写——并先从 FTS 库捞**真实项目符号**作提示（`get_symbol_hints`，keyword_index.py:240），约束 LLM 不要编造类名。
- `AgentLoop._act` 自动给 SearchCode 补 `chat_history` 参数（agent_loop.py:805-809），让"那它在哪里调用"这类追问可被改写。

### 混合融合排序（pipeline.py:411 `_hybrid_search`）

两路各召回 2×top_k 候选，按 `file_path::name::start_line` 合并去重，融合打分：

```
final = semantic×0.5 + keyword×0.3 + type_boost(method/function +0.08, class +0.05) + 双路命中奖励 0.1
```

最后同文件最多保留 2 条防止单文件刷屏。

### Re-ranking（retrieval_enhancements.py:249）

三种方法可配，失败一律回退 heuristic：
- **heuristic**（默认）：查询词与 chunk 的 name/path/content 重叠率加权 0.45/0.15/0.40 + 类型加成；
- **cross_encoder**：sentence-transformers CrossEncoder，召回分 0.3 + CE 分 0.7 融合；
- **llm**：让模型给候选打 0-1 分，0.25/0.75 融合。

配套 `SearchCache`：TTL(300s)+LRU(128) 内存缓存，缓存键含**索引指纹**（文件数 + 最大索引时间戳，pipeline.py:639），索引一变缓存自动失效。

### mtime + MD5 增量同步（pipeline.py:652 `sync_before_search`）

检索前快速保鲜，两步检测：
1. **mtime 快筛**：比对 `os.path.getmtime` 与上次索引时间戳（618 个文件约 1ms），mtime ≤ 索引时间直接跳过；
2. **MD5 精确确认**：mtime 变了才算 MD5（排除 touch/保存未修改的假阳性）。

同时处理三类变更：内容变更重索引、已删除文件清理向量 + FTS 记录、新增文件补索引。索引层面 `index_file` 也按 MD5 做增量跳过（:114）。

### 接入主循环

`_maybe_force_search_code`（agent_loop.py:316）：对"解释/实现/架构 + 项目线索词"的代码库问题，在第一次 LLM 调用前**直接构造一个强制的 SearchCode tool_call**——只靠 prompt 约束模型优先检索不够稳定，这是把检索策略做成代码保证。

---

## 实现现状的两点说明（已核实）

1. **自动复杂度路由是存在的**：`cli/app.py:_should_auto_team`（:268）已把 `classify_complexity` 接入主流程，`team.auto_detect` 默认开启，复杂任务自动切 Multi-Agent。简历"混合架构"的说法完全站得住。
2. **Plan-Execute 是显式触发**（`/plan`），自动路由的目标是 Team 而不是 Plan。原因（写在 `_choose_path` 注释里，agent_loop.py:739）：LLM 一次性产出的 tool_calls 数量/参数质量不稳定，DAG 静态规划对依赖关系的容错不如 Supervisor 动态派活，Plan 对简单任务太重。**这是有意识的工程权衡，面试可以主动讲。**

---

# 面试官可能会问的问题与参考回答

## A. 架构与 LangGraph

**Q1：为什么用 LangGraph 而不是自己写个 while 循环调 LLM？**

A：核心收益有三个。① 状态管理：`AgentState` 用 `Annotated[list, add_messages]` 声明后，消息合并、状态在节点间传递都由框架保证，不用手写消息簿记；② 图结构让控制流显式化：think/route/act/plan 各是独立节点，条件边（`add_conditional_edges`）把"继续还是结束""走 ReAct 还是 Plan"的决策点变成可测试的纯函数；③ Multi-Agent 场景下 `Command(goto=...)` 原语天然支持 Supervisor 中心辐射拓扑，Worker 内部还能复用 `create_react_agent` 嵌套子图。当然它有学习成本，简单场景 while 循环也够——但我们有三种执行模式共存，图抽象的边际收益是正的。

**Q2：ReAct、Plan-Execute、Multi-Agent 三种模式怎么选？为什么不全自动？**

A：默认 ReAct，因为逐步执行最稳——每步工具结果都回到 LLM 重新决策，容错最好。自动路由用一个小模型分类器（`classify_complexity`，可配独立的 classifier 模型）判 simple/complex，complex 自动进 Multi-Agent。Plan-Execute 保留为 `/plan` 显式触发，原因是实践中发现：LLM 一次性产出的 DAG 参数质量不稳定（所以 PlanExecutor 里做了参数别名归一化和必填校验），静态规划遇到中途失败只能传播 SKIPPED，而 Supervisor 可以动态重派；对简单任务 Plan 又太重。这是用稳定性换自动化程度的取舍。

**Q3：Supervisor 怎么防止 LLM 路由抽风（重复派活/死循环）？**

A：三层防御。① 提示词里写明决策规则和"已工作的 Agent"进度信息；② 解析容错：先提取 JSON（兼容 next/agent/goto 等 7 种字段名），再回退文本关键词匹配，都失败 FINISH；③ 最关键的是**硬性规则路由**：代码检查消息流里已出现的 Agent name，planner 干完 worker 没干，就跳过 LLM 直接路由 worker-1。教训是：能用代码保证的控制流不要交给 LLM。另外 recursion_limit 兜底防止无限循环。

**Q4：Reviewer 审查失败怎么办？会不会无限重试？**

A：Reviewer 输出结构化 JSON `{"approved": bool, "issues": []}`，解析采用**保守策略**——内容为空、JSON 坏了、字段缺失，一律判不通过（宁可多审一轮也不放过坏结果）。不通过时把 issues 作为反馈消息回到 Supervisor 重派 Worker，`retry_count` 计数，超过 `MAX_RETRIES=2` 就带"超过重试上限"标记保留当前结果结束，不会无限循环。

**Q5：DAG 并行执行怎么实现的？怎么处理失败？**

A：调度循环每轮调 `ready_tasks()` 取"PENDING 且所有依赖已 COMPLETED"的任务，`asyncio.gather` 并行跑一批（信号量式限流 max_parallel=4，实际工具是同步的，用 `run_in_executor` 丢线程池）。失败时 `_propagate_failure` 沿依赖边递归把下游标 SKIPPED；如果出现"无就绪任务但计划未完成"（说明剩余任务都依赖了失败任务），统一标记不可达。生成侧还有三色 DFS 检测循环依赖，保证调度不会死锁。

## B. 记忆与上下文

**Q6：为什么用字符 bigram 相似度而不是 jieba 分词或向量？**

A：场景是长期记忆条目的去重/更新判断和轻量检索，条目都是一两句话的短文本。bigram Jaccard 零依赖、确定性、对中文友好（中文没有空格分词问题，字符二元组天然捕捉局部搭配），几百条记忆全量扫描也就微秒级。jieba 引入分词器依赖和词典维护成本；向量化每条都要过 embedding API，有延迟和费用，对"判断两句话是不是说同一件事"性价比不高。代码库级的语义检索才交给 RAG 的向量路。

**Q7：长期记忆的"更新"机制为什么需要？阈值 0.85 怎么定的？**

A：没有更新机制时，"用户偏好 JDK 17"和后来的"用户偏好 JDK 21"会变成两条矛盾记忆同时被检索出来。所以 store 时先找相似度 > 0.85 的旧条目做原地替换，并在 metadata 里记 `updated_from` 和相似度，保留可追溯性。0.85 是经验值：太低会把不同主题的记忆错误合并，太高则同一事实的小改写也判成新条目。这个值配合"完全相同 MD5 去重"形成三段式：完全重复丢弃、高度相似更新、其余新增。

**Q8：压缩会丢信息，怎么缓解？**

A：三道防线。① 保留最近 N 轮原文不压缩，近期上下文无损；② **压缩前先做事实提取**——让 LLM 从将被摘要的旧消息里抽取跨会话有价值的事实（偏好、决策、配置）写入长期记忆，之后即使摘要丢了细节，事实还能按需检索回来；③ Map-Reduce 分片摘要而不是一把梭——长对话一次性摘要容易顾此失彼，分片（每 5 条）摘要再合并，单片信息密度可控。摘要提示词也明确"只保留决策和结论，不保留代码细节"，因为代码可以重新读文件，决策丢了就找不回来。

**Q9：每轮重建 system prompt 不浪费 token 吗？**

A：有这个代价，但换来的是：CLAUDE.md/MEMORY.md 运行中修改即时生效、长期记忆按当前问题动态检索（每轮只注入 top-5 相关条目而不是全部）、Skill 索引随启用状态更新。如果要优化，方向是 prompt caching——把稳定前缀（CLAUDE.md、base prompt）和动态后缀（检索记忆）分层，命中提供商的缓存。这也是我知道的 Claude/OpenAI API 的标准做法。

## C. MCP 与浏览器

**Q10：MCP 工具怎么和内置工具统一？**

A：关键是 Schema 动态转换。MCP Server 的 `tools/list` 返回 JSON Schema 格式的 `inputSchema`，我用 `pydantic.create_model` 在运行时把它转成 Pydantic 模型（处理 required/optional/default/array item 类型），再用 LangChain 的 `StructuredTool.from_function` 包成标准工具注册进同一个 ToolRegistry。对 LLM 来说 MCP 工具和内置 Read/Bash 没有任何区别，`bind_tools` 一视同仁。

**Q11：你提到"持久后台事件循环"，为什么不能直接 asyncio.run()？**

A：这是踩过的真实的坑。MCP stdio 连接的生命周期由 anyio TaskGroup 管理，连接建立在哪个事件循环上，后续所有 I/O 都必须在同一个循环上执行。`asyncio.run()` 每次新建循环、结束时关闭循环，会导致连接的异步生成器被跨 task 强制关闭，anyio 的 cancel scope 报 RuntimeError。解法：启动时开一个 daemon 线程跑 `loop.run_forever()`，MCP 初始化和所有工具调用都用 `asyncio.run_coroutine_threadsafe` 投递到这个持久循环，主线程 `future.result(timeout)` 同步等结果。

**Q12：isolated/shared 双模式怎么设计的？切换怎么保证不挂？**

A：isolated 是默认（`--isolated`，MCP Server 自管临时 Chrome，无登录态，安全）；shared 连用户本机 Chrome 复用登录态，用于需要登录的页面。切换是重启 MCP Server 子进程：保存旧参数 → 断开 → 新参数重连 → **失败自动回滚旧参数** → 成功则重新拉工具列表，同步更新 MCPManager、ToolRegistry，并重建 AgentLoop 的 `bind_tools`。连 shared 的参数有三级回退（wsEndpoint → autoConnect+userDataDir → autoConnect）。另外整个流程对 LLM 是自动的：工具结果里检测到登录页特征就注入"切 shared 重试"提示，system prompt 里也按当前模式注入操作规程。

**Q13：shared 模式直接操作用户登录态浏览器，安全问题怎么考虑？**

A：三层。① `BrowserGuard` 维护敏感页面 pattern（支持用户自定义文件），敏感 URL 上的写操作需要用户确认；② HITL 审批体系覆盖浏览器危险工具（`CHROME_DANGEROUS_TOOLS`）；③ 默认就是 isolated，shared 需要显式切换，且 Chrome 侧本身会弹"允许远程调试"确认。设计原则是：读操作放宽、写操作收紧、默认最小权限。

## D. Skill 系统

**Q14：Skill 和 RAG、长期记忆有什么区别？为什么需要三个东西？**

A：解决的问题不同。RAG 检索的是**代码事实**（这段逻辑在哪、怎么写的）；长期记忆存的是**对话中沉淀的事实**（用户偏好、项目决策）；Skill 是**人工编写的过程性知识**（做某类任务的完整方法论，比如怎么抓取小红书）。Skill 的内容是指令而不是事实，需要完整、有序地进入上下文才有用，所以不适合切块检索；又因为体积大，不能常驻 prompt——这就推导出"索引常驻 + 按需全文加载"的渐进式披露设计。

**Q15："渐进式披露"具体怎么省 token？**

A：每个 Skill 在 system prompt 里只占一行（名字 + ≤500 字描述），20 个 Skill 也就一两千 token。完整 body（可能几千 token 一个）只在 LLM 判断任务匹配并调用 `load_skill` 后，经 buffer 在下一轮注入到 user message。buffer 做了两个防爆设计：LRU 上限 3 个（最多同时加载 3 个 Skill 的正文）、drain 一次性消费（注入后立即清空，绝不跨轮重复注入）。等于把"要不要为这个领域知识花 token"的决策交给了 LLM 自己。

**Q16：为什么 Skill body 注入到 user message 而不是 system prompt？**

A：两个原因。① system prompt 每轮重建，如果 body 进 system，要么常驻（违背省 token 初衷）要么管理"什么时候移除"的复杂状态；注入 user message 是一次性的，随对话自然滚动、被压缩机制正常处理。② 指令邻近性：Skill 是针对当前任务的操作指引，紧贴用户输入放置，模型遵循度更好。

## E. RAG

**Q17：为什么自己做 AST 分块，不用固定大小滑窗？**

A：代码的语义边界就是语法边界。固定滑窗会把一个函数切成两半、把两个无关函数拼一起，embedding 出来的向量语义混杂。AST 分块保证每个 chunk 是完整语义单元（函数/方法/类概览），还能附带 signature、docstring、parent_name 等结构化元数据——这些后面在融合排序的 type_boost 和 rerank 的字段加权里都用上了。一个细节：类不整体成块，只留"声明+前几行"概览，方法单独成块——否则大类会变成一个超长低质 chunk。

**Q18：为什么选 SQLite FTS5 而不是 Elasticsearch？Chroma 而不是 Milvus/pgvector？**

A：这是个本地 CLI 工具，部署形态决定技术选型：零运维、零外部服务、嵌入式优先。FTS5 是 SQLite 内置的，自带 BM25，`unicode61` tokenizer 对中文够用，单文件数据库随项目走；ES 的能力（分布式、复杂分析）在单机几百个文件的场景完全用不上。Chroma 同理——persist_directory 指向项目目录即可，LangChain 集成成熟。如果是服务端多租户场景，我会换 pgvector/ES，但选型要匹配场景。

**Q19：混合检索的权重 0.5/0.3 怎么来的？为什么语义比关键词高？**

A：经验调参 + 对场景的判断：用户问题以自然语言为主（"上下文压缩在哪实现"），语义路是主召回源；关键词路主要价值是标识符精确命中和给双命中加置信。双命中奖励 0.1 很重要——语义和 BM25 同时命中的结果几乎不会是误检。type_boost 偏向 method/function 因为用户找的多是"做事的代码"而不是 import 块。坦白说这组权重没做过系统的离线评测，是人工 bad case 驱动调出来的；要严谨化就建标注集跑 nDCG/MRR 网格搜索——这也是我知道的改进方向。

**Q20：Query Rewrite 为什么默认走规则而不是全 LLM？**

A：成本和确定性。规则路（标识符拆分 + 同义词表）零延迟、零费用、结果可预测，覆盖了大部分中文问代码的场景。LLM 改写只在 auto 模式下针对**指代型问题**（含"它/这个/刚才"）启用，因为这类问题必须结合对话历史消解指代，规则做不了。LLM 改写还有个防幻觉设计：先从 FTS 的 metadata 表捞出和查询词相关的**真实项目符号**喂给改写模型，明确要求"优先用这些符号、不要发明类名"。

**Q21：mtime + MD5 为什么要两级？只用 MD5 不行吗？**

A：性能。每次检索前全量算 MD5，几百个文件要读全文件内容做哈希，IO 成本高。mtime 是 stat 调用，~1ms 扫完全部文件，能把"肯定没变"的绝大多数文件先排除（mtime ≤ 上次索引时间则内容必然没变）。MD5 只对 mtime 变了的少数文件做精确确认，排除 touch、保存未修改这类 mtime 变了但内容没变的假阳性。本质是经典的 fast-path/slow-path 分层。

**Q22：检索缓存怎么保证不返回过期结果？**

A：缓存键里编入**索引指纹**（已索引文件数 + 最大索引时间戳）。任何索引变更（增量同步、重新 index）都会改变指纹，旧缓存条目在 get 时指纹不匹配直接失效。再加 TTL 300s 和 LRU 128 条上限兜底。索引写入路径上也主动 `clear()`。

## F. 工程与综合

**Q23：工具调用失败怎么处理？**

A：分层降级。① 启动时 `_filter_available_tools` 过滤环境不可用的工具（如没配搜索 API key 的 WebSearch），不让 LLM 看到注定失败的工具；② 运行时同一工具连续失败 2 次，本轮禁用并短路返回可解释错误，明确告诉 LLM"不要再调，换方案或向用户说明"；③ 失败信息作为 ToolMessage 回流给 LLM 自行调整。核心思想：失败不可怕，可怕的是 LLM 拿着同样的参数无限重试。

**Q24：怎么防止 Agent 死循环？**

A：四道闸。① `MAX_ITERATIONS=50` + LangGraph `recursion_limit=100` 硬上限；② 工具失败计数禁用；③ 浏览器循环检测——从最近 20 个 tool_call 提取序列，检测重复截图、无导航的连续 evaluate_script、3-gram 模式重复，命中注入系统级停止指令（同时排除"多页提取"这种合法模式，避免误杀）；④ Multi-Agent 侧的硬性规则路由和重试上限。

**Q25：权限和 HITL 怎么设计的？**

A：两层。PermissionPolicy 是静态策略：四种模式（default/acceptEdits/bypassPermissions/permit 白名单），`DANGEROUS_TOOLS={"Bash"}`、`EDIT_TOOLS={"Write","Edit"}`。HITL 是动态审批：`HitlToolRegistry` 继承 ToolRegistry 加审批检查，在 `_act` 执行前拦截，支持批准/拒绝/跳过/**修改参数后执行**/对某工具全部放行。审批请求带危险等级和风险描述渲染给用户。两层都在 `_act` 一个入口统一处理，ReAct 和 Plan-Execute 共用。

**Q26：这个项目最难的 bug / 最深的坑是什么？**

A：可以讲两个。① MCP 事件循环问题（见 Q11）——表象是工具第二次调用随机报 RuntimeError，根因是 asyncio.run 销毁了 stdio 连接所在的循环，定位花了很久，最终方案是持久后台循环 + run_coroutine_threadsafe。② MiMo 模型的 reasoning_content：LangChain 的消息转换会把非标字段丢掉，导致 thinking 模式既拿不到推理内容、回传时 API 又报错。解法是继承 ChatOpenAI 重写 `_create_chat_result`（响应侧捕获到 additional_kwargs）和 `_get_request_payload`（请求侧注回消息 dict），并禁用 streaming 路径强制走 invoke。这说明对框架内部序列化机制要有掌控力。

**Q27：如果重新设计/继续迭代，你会改什么？**

A：诚实地列：① 会话持久化目前只存元数据，要把完整对话落盘支持 resume；② SubAgentTool（Task 工具）已实现但未注册，子 Agent 定义文件没真正用起来，要接入；③ RAG 评测体系——现在融合权重是经验值，要建标注集跑 nDCG 做系统调参；④ 多语言 AST 分块（现在只有 Python，可以上 tree-sitter）；⑤ system prompt 分层配合 prompt caching 降成本；⑥ 复杂度分类器每次输入都多一次 LLM 调用，可以加规则前置或缓存。主动说缺陷比被问出来好。

**Q28：（追问简历）"混合架构"到底混合在哪？**

A：三个层面。① 模式层：同一入口根据任务复杂度在 ReAct/Multi-Agent 间自动路由，Plan-Execute 显式触发；② 结构层：Multi-Agent 的 Worker 内部就是一个完整 ReAct Agent（create_react_agent），即"Supervisor 编排 + ReAct 执行"的嵌套；③ 基础设施层：三种模式共享同一套 ToolRegistry、PermissionPolicy、HookManager、Memory——切模式不切基础设施，这是混合架构能落地的前提。

---

## 面试叙事建议

1. **先讲分流再讲细节**：开场用一句话讲清"默认 ReAct，小模型分类器自动把复杂任务路由到 Supervisor 编排的 Multi-Agent，/plan 显式走 DAG"，再按面试官兴趣下钻。
2. **主动讲权衡**：Plan 为什么不默认开（tool_calls 不稳定）、bigram 为什么不用 jieba、FTS5 为什么不用 ES——每个"为什么不用更高级的"都是展示判断力的机会。
3. **准备一两个踩坑故事**：MCP 事件循环、MiMo reasoning_content，有细节、有定位过程、有通用结论。
4. **坦诚已知限制**：会话恢复未完成、RAG 权重未系统评测，配上改进方案，比完美人设可信。
