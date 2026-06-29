# WeaveMindAgent

WeaveMindAgent 是一个面向代码工作的终端 Agent CLI。它基于 LangGraph / LangChain 组织 ReAct 循环，能读取和修改本地文件、执行命令、检索代码库、联网搜索、抓取网页、连接 MCP 工具，并在复杂任务中切换到 Plan-Execute 或 Multi-Agent 协作模式。

项目的核心目标是：把日常开发里的「理解代码、定位实现、修改文件、验证结果、沉淀记忆」串成一个可交互、可扩展、带审批保护的 Agent 工作流。

## 主要功能

- 交互式 CLI：支持历史记录、命令补全、流式输出、工具调用进度和多种运行模式。
- ReAct Agent：模型可按需调用 Read / Write / Edit / Bash / Glob / Grep 等工具完成开发任务。
- Plan-Execute：复杂任务可先生成 DAG 计划，再按依赖执行任务。
- Multi-Agent：Supervisor 路由 Planner、Worker、Reviewer，支持执行后审查和失败重试。
- 子 Agent 委托：`Task` 支持独立 ReAct 子任务，`BatchDelegate` 支持并行委托，并内置工具隔离、非交互审批兜底、心跳和超时治理。
- 记忆系统：长期记忆、核心记忆和上下文压缩共同维护跨会话项目上下文。
- 代码 RAG：基于 AST 分块、Chroma 向量库、SQLite FTS5 关键词索引实现混合检索。
- Web 能力：支持搜索、网页抓取、正文提取、SSRF 防护和多搜索引擎 Provider。
- MCP / 浏览器：可连接多个 MCP Server，并支持 Chrome DevTools isolated/shared 双模式。
- HITL 审批：危险文件操作、Shell、浏览器写操作和外部 MCP 工具可在执行前请求人工确认。
- Skill 系统：按任务场景加载可复用经验指引，支持 builtin / user / project 三层覆盖。
- 微信通道：可通过腾讯 iLink Bot API 将 Agent 接入微信私聊，并以远程只读安全策略运行。
- 会话运行时：支持独立 Agent Session、协作取消信号和运行结果封装，便于 CLI 与远程入口复用。

## 快速开始

```bash
git clone git@github.com:KensDrizzy/weavemind-agent.git
cd weavemind-agent

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp config.yaml.example config.yaml
python main.py
```

也可以安装全局命令：

```bash
bash install.sh
source ~/.zshrc
weavemind
```

常用启动参数：

```bash
python main.py --debug     # 启用调试日志
python main.py --no-hitl   # 禁用人工审批
```

## 微信接入

WeaveMindAgent 可以通过腾讯 iLink Bot API 在微信私聊中运行。微信通道是独立入口，默认不会启动，也不会改变现有终端 CLI。

```bash
# 首次绑定：终端显示二维码，使用微信扫码确认
weavemind wechat setup --workspace /path/to/project

# 前台启动微信通道
weavemind wechat start

# 查看状态或删除本地凭证
weavemind wechat status
weavemind wechat logout
```

首版采用 `remote_safe` 只读策略：仅开放工作区内的 Read / Glob / Grep、代码 RAG 和联网检索，不开放 Write、Edit、Bash、浏览器修改及外部 MCP 写操作。微信凭证保存在 `~/.weavemind/wechat/account.json`，文件权限为 `0600`。

## 配置

配置文件默认读取 `config.yaml`。可以从 `config.yaml.example` 复制后修改。

关键配置项：

```yaml
llm:
  provider: anthropic
  model: claude-sonnet-4-20250514
  max_tokens: 8192
  temperature: 0

providers:
  anthropic:
    api_key_env: ANTHROPIC_API_KEY

  mimo:
    base_url: https://token-plan-cn.xiaomimimo.com/anthropic
    api_key_env: MIMO_API_KEY

permissions:
  default_mode: default

rag:
  enabled: false
  chroma_dir: .weavemind/chroma
  keyword_db: .weavemind/rag/keyword_index.db

team:
  auto_detect: true

delegation:
  max_concurrent_children: 3
  child_timeout_seconds: 600
  max_result_chars: 8000
  subagent_auto_approve: false
  heartbeat_interval_seconds: 30
  stale_cycles_idle: 15
  stale_cycles_in_tool: 40

wechat:
  private_chat_only: true
  security:
    allowed_tools:
      - Read
      - Glob
      - Grep
      - SearchCode
      - WebSearch
      - WebFetch
```

LLM 支持 `anthropic`、`deepseek`、`mimo`、`openai`。Anthropic 走原生接口；MiMo、DeepSeek 和 OpenAI 兼容服务可通过对应 `base_url` 接入。API Key 通常通过环境变量提供，例如：

```bash
export MIMO_API_KEY=...
export ANTHROPIC_API_KEY=...
export DEEPSEEK_API_KEY=...
export OPENAI_API_KEY=...
```

## CLI 命令

| 命令 | 说明 |
| --- | --- |
| `/help` | 查看可用命令 |
| `/memory` | 查看核心记忆和长期记忆状态 |
| `/save <事实>` | 手动保存事实到长期记忆 |
| `/index [目录] [source]` | 索引代码库，启用 RAG 检索 |
| `/search <关键词> [--source 名称]` | 手动检索代码片段 |
| `/mode [default\|acceptEdits\|bypassPermissions]` | 切换权限模式 |
| `/hitl [on\|off\|status]` | 管理人工审批 |
| `/mcp [status\|tools\|health]` | 查看 MCP 状态、工具和健康检查 |
| `/browser [status\|connect\|disconnect]` | 查看或切换 Chrome 浏览器模式 |
| `/skill [list\|show\|on\|off\|reload]` | 管理 Skills |
| `/plan` | 切换 Plan-Execute 模式 |
| `/team` | 切换 Multi-Agent 模式 |
| `/clear` | 清空当前对话历史 |
| `/exit` / `/quit` | 退出 |

## 工作模式

### 默认 ReAct 模式

默认模式由 `core/agent_loop.py` 中的 `AgentLoop` 驱动。流程是：

```text
think -> route -> plan_or_react -> act -> think -> ... -> END
```

模型每轮先读取系统提示、记忆、Skill 索引和对话历史，再决定是否调用工具。工具执行统一经过权限策略、HITL 审批、Hook 事件和失败熔断。

### Plan-Execute 模式

`/plan` 会强制进入规划执行流程。`core/planner.py` 将用户目标拆成 DAG 任务，`core/plan_executor.py` 根据依赖关系并行执行可运行任务，并处理失败传播和不可达任务。

### Multi-Agent 模式

`/team` 或自动复杂度判断会进入 `agents/orchestrator.py`。该模式包含：

- `supervisor`：决定下一步交给哪个 Agent。
- `planner`：只读规划，不执行操作。
- `worker-1` / `worker-2`：基于 ReAct 执行任务。
- `reviewer`：审查结果，失败时最多重试 2 次。

### 子 Agent 委托

子 Agent 能力由 `agents/subagent.py`、`agents/batch_delegate.py` 和 `agents/monitor.py` 提供，并通过 `tools/registry.py` 注册到工具系统：

- `Task`：根据 `.weavemind/agents/*.md` 中的 Agent 定义启动一个隔离的 ReAct 子任务。
- `BatchDelegate`：一次提交多个相互独立的子任务，使用线程池并行执行，并按成功、失败和超时汇总结果。
- 工具隔离：子 Agent 默认禁止加载 `Task`、`BatchDelegate`、`delegate_task`、`AskUser`、`MemoryAdd`、`MemorySearch` 和 `CoreMemoryEdit`，避免递归委托、后台交互阻塞和共享记忆越权。
- 审批兜底：危险工具会被非交互审批 wrapper 包装，默认自动拒绝，避免子 Agent 在线程池中等待终端输入。
- 心跳治理：`SubAgentMonitor` 区分 idle/thinking 和 in-tool 两类停滞场景，默认分别以 450 秒和 1200 秒窗口判定 stale，并支持按子任务中断。

## 模块实现说明

### `main.py`

程序入口。负责解析 `--debug` 和 `--no-hitl` 参数，配置日志，并过滤 MCP 关闭时常见的无害告警，最后启动 `WeaveMindCLI`。

### `cli/`

终端交互层。

- `app.py`：顶层 REPL 编排器，初始化权限、Hook、记忆、RAG、MCP、Skill、AgentLoop，并处理普通输入、斜杠命令和模式切换。
- `commands.py`：实现 `/help`、`/memory`、`/index`、`/search`、`/hitl`、`/mcp`、`/browser`、`/skill` 等命令。
- `renderer.py`：渲染流式响应、工具调用、计划进度和最终结果。
- `hitl_handler.py` / `hitl_renderer.py`：终端人工审批交互，支持批准、全部批准、拒绝、跳过、修改参数。
- `direct_intent.py`：高频确定性本地操作的快速通道，例如列目录、读文件、查文件、grep 内容和统计文件信息。

### `core/`

Agent 核心能力层。

- `agent_loop.py`：LangGraph 状态机，负责 ReAct 循环、工具执行、权限检查、HITL、浏览器循环检测、RAG 上下文注入、上下文压缩和 Multi-Agent 入口。
- `agent_session.py`：可复用的单次 Agent 会话运行器，封装运行结果、错误和取消状态，供 CLI 与微信通道共享。
- `cancellation.py`：轻量级取消令牌，用于跨入口协作停止长任务。
- `llm_factory.py`：根据配置创建 LLM；支持 OpenAI 兼容接口、Anthropic 原生接口，并为 MiMo thinking 模式保留 `reasoning_content`。
- `memory.py`：记忆门面，组合 CLAUDE.md、MEMORY.md、核心记忆、长期记忆和 Skill 索引生成 system prompt。
- `compaction.py`：上下文压缩器，超过 token 阈值时保留最近轮次，旧消息用摘要替换，并抽取关键事实写入长期记忆。
- `planner.py` / `plan_executor.py` / `plan_models.py`：Plan-Execute 的计划生成、DAG 模型和执行引擎。
- `prompt_repository.py` / `prompt_assembler.py`：从 builtin、用户目录、项目目录三层加载提示词，并按运行模式组装。
- `session.py`：把会话状态保存到 `.weavemind/sessions`。
- `hitl_policy.py` / `hitl_models.py`：定义危险操作判断、审批请求和审批结果模型。

### `tools/`

工具系统。

- `registry.py`：注册内置工具、RAG 工具、浏览器控制工具和 MCP 动态工具。
- `hitl_registry.py`：在工具注册表上增加审批检查。
- `builtin/read.py`：读取文件或列目录。
- `builtin/write.py`：创建或覆盖文件。
- `builtin/edit.py`：精确字符串替换。
- `builtin/bash.py`：执行 Shell 命令。
- `builtin/glob.py`：按 glob 模式查找文件。
- `builtin/grep.py`：正则搜索文件内容。
- `builtin/web_search.py`：互联网搜索。
- `builtin/web_fetch.py`：抓取 URL 并提取正文 Markdown。
- `builtin/memory_tools.py`：给 Agent 使用的长期记忆和核心记忆工具。
- `builtin/rag_tools.py`：`SearchCode` 和 `IndexWorkspace`。
- `builtin/skill_tools.py`：`load_skill`，按需把 Skill 正文注入下一轮上下文。

### `permissions/`

权限策略模块。

- `modes.py`：定义 `default`、`acceptEdits`、`bypassPermissions`、`permit` 四类权限模式，以及编辑、危险和 Chrome 工具分类。
- `policy.py`：判断工具是否允许执行、是否需要确认，并接入 BrowserGuard 的敏感页面保护。

### `rag/`

本地代码库检索模块。

- `pipeline.py`：统一索引和检索入口。索引时写入 Chroma 向量库和 SQLite FTS5；检索时支持 semantic、keyword、hybrid。
- `chunkers/python_chunker.py`：用 Python AST 按 import、class、method、function 拆分代码。
- `chunkers/__init__.py`：通用回退分块器、索引文件过滤和目录过滤。
- `keyword_index.py`：SQLite FTS5 关键词索引，适合精确匹配类名、函数名、标识符。
- `retrieval_enhancements.py`：查询改写、启发式 / Cross-Encoder / LLM 重排和 TTL + LRU 缓存。
- `models.py`：代码块、检索结果和索引统计模型。

RAG 启用后，工作流通常是：

```bash
/index .
/search MemoryManager
```

Agent 在代码相关问题中也会优先使用 `SearchCode`。

### `web/`

联网搜索和网页抓取模块。

- `providers/`：搜索引擎抽象层，支持 Tavily、智谱、SerpAPI、SearXNG、DuckDuckGo，并按配置和环境变量自动选择。
- `fetcher/fetcher.py`：HTTP 抓取、SSL 回退、响应截断、标题提取和正文提取。
- `fetcher/extractor.py`：清理噪声 HTML，定位正文容器，并转换为 Markdown。
- `fetcher/policy.py`：SSRF 防护和同域名限流，禁止访问内网、file、ftp、data、javascript 等地址。
- `models.py`：搜索结果数据模型。

### `mcp_client/`

MCP 集成层。

- `manager.py`：管理多个 MCP Server，连接、关闭、健康检查、工具聚合，并支持 Chrome DevTools 模式切换。
- `client.py`：单个 MCP Server 的长连接，支持 stdio 和 HTTP/SSE 传输。
- `tools.py`：把 MCP `tools/list` 返回的 JSON Schema 动态转换成 LangChain StructuredTool。
- `browser_tools.py`：内置 `browser_connect`、`browser_disconnect`、`browser_status`，用于控制 Chrome DevTools MCP 模式。
- `browser_guard.py`：敏感页面保护、登录页检测、导航状态追踪。
- `chrome_formatter.py`：格式化 Chrome MCP 工具结果，减少无用输出。
- `chrome_launcher.py` / `auto_connect.py`：Chrome 调试端口探测和辅助连接逻辑。

Chrome DevTools 支持两种模式：

- `isolated`：MCP 启动独立浏览器，无用户登录态。
- `shared`：连接用户已登录 Chrome，可访问需要登录的页面，敏感操作需要确认。

### `channels/`

远程入口模块。当前包含微信 iLink 通道：

- `wechat/cli.py`：实现 `weavemind wechat setup/start/status/logout`。
- `wechat/engine.py`：轮询消息、调度 Agent Session、处理忙碌状态和回复截断。
- `wechat/ilink_client.py`：封装登录、轮询、发送消息和 typing 状态。
- `wechat/account_store.py`：以 `0600` 权限保存本地微信凭证。
- `wechat/safety.py`：远程入口只读工具白名单和请求过滤。
- `wechat/message_parser.py` / `renderer.py` / `models.py`：消息解析、回复渲染和数据模型。

### `skills/`

经验复用系统。

- `registry.py`：扫描 `skills/builtin`、`~/.weavemind/skills`、`.weavemind/skills` 三层目录，同名 Skill 后者覆盖前者。
- `parser.py`：解析 `SKILL.md` frontmatter 和正文。
- `formatter.py`：把启用的 Skill 列表压缩为索引注入 system prompt。
- `buffer.py`：`load_skill` 后将正文暂存，并在下一轮用户消息前一次性注入。
- `state_store.py`：保存禁用 Skill 列表。
- `builtin/web-access`：内置联网操作决策手册，覆盖 WebSearch、WebFetch、Chrome DevTools 和常见站点经验。

### `agents/`

多 Agent 和子 Agent 模块。

- `orchestrator.py`：Supervisor 模式的多 Agent 编排。
- `worker.py`：基于 `create_react_agent` 的 Worker 节点。
- `reviewer.py`：质量审查节点，JSON 解析失败时保守判定不通过。
- `subagent.py`：`Task` 子 Agent 工具，可根据 `.weavemind/agents/*.md` 定义启动独立 ReAct 子任务，并负责工具隔离和审批兜底。
- `batch_delegate.py`：`BatchDelegate` 批量委托工具，负责并行调度、单任务超时、失败隔离和结果汇总。
- `monitor.py`：子 Agent 心跳监控，负责 stale 检测、暂停和中断。
- `loader.py`：加载带 YAML frontmatter 的 Agent 定义文件。
- `agent_state.py`：Multi-Agent 共享状态结构。

### `hooks/`

事件钩子系统。`HookManager` 支持注册并触发 `LLMStart`、`LLMDelta`、`LLMEnd`、`PreToolUse`、`PostToolUse`、`PlanStart`、`PlanCreated` 等事件，CLI 渲染器依赖这些事件展示流式进度和工具状态。

### `prompts/`

系统提示词模板目录。`base.md`、`personality.md`、`context.md`、`handoff.md` 和 `modes/*.md` 会由 `PromptAssembler` 按模式组合。项目级覆盖路径是 `.weavemind/prompts`，用户级覆盖路径是 `~/.weavemind/prompts`。

### `tests/`

测试覆盖了权限策略、工具、RAG、记忆、HITL、Plan-Execute、Multi-Agent、Web、Chrome MCP 和 CDP 双模式等核心行为。

## 数据目录

运行时数据默认保存在 `.weavemind/`：

| 路径 | 用途 |
| --- | --- |
| `.weavemind/MEMORY.md` | 项目记忆文本 |
| `.weavemind/memory/long_term.json` | 长期记忆 |
| `.weavemind/memory/core.json` | 核心记忆块 |
| `.weavemind/sessions/` | 会话状态 |
| `.weavemind/chroma/` | Chroma 向量库 |
| `.weavemind/rag/keyword_index.db` | SQLite FTS5 关键词索引 |
| `.weavemind/skills/` | 项目级 Skills |
| `.weavemind/prompts/` | 项目级提示词覆盖 |
| `.weavemind/chrome_screenshots/` | Chrome 截图缓存 |
| `~/.weavemind/wechat/account.json` | 微信 iLink 本地登录凭证 |

## 开发与测试

```bash
source .venv/bin/activate
python -m pytest
```

常用单测：

```bash
pytest tests/test_permissions.py
pytest tests/test_rag.py
pytest tests/test_hitl.py
pytest tests/test_multiagent.py
pytest tests/test_subagents.py
pytest tests/test_chrome_mcp.py
```

## 推荐工作流

1. 配置 `config.yaml` 和相关 API Key。
2. 启动 `weavemind`。
3. 对代码库类问题，先启用 RAG 并运行 `/index .`。
4. 简单任务直接描述需求。
5. 多文件改动或复杂功能使用 `/plan` 或 `/team`。
6. 保持 `/hitl on`，让危险操作在执行前经过确认。

## 项目结构

```text
WeaveMindAgent/
├── main.py                 # CLI 入口
├── cli/                    # 终端交互、命令、渲染、HITL
├── core/                   # AgentLoop、LLM、记忆、计划执行、提示词
├── channels/               # 微信等远程入口
├── tools/                  # 内置工具、RAG 工具、HITL 工具注册表
├── permissions/            # 权限模式和策略
├── rag/                    # 代码索引和混合检索
├── web/                    # 搜索 Provider 和网页抓取
├── mcp_client/             # MCP 连接、动态工具、Chrome DevTools
├── skills/                 # Skill 注册、解析、状态和内置 Skill
├── agents/                 # Multi-Agent、Worker、Reviewer、SubAgent
├── hooks/                  # 事件钩子
├── prompts/                # 系统提示词模板
├── tests/                  # 测试
├── config.yaml.example     # 示例配置
├── install.sh              # 安装 weavemind alias
└── weavemind               # CLI 启动脚本
```
