# WeaveMindAgent 设计 Plan

> 用 LangChain/LangGraph 实现，架构模仿 Claude Code 的 Agent SDK。

## 一、架构总览

```
                          ┌──────────────────────┐
                          │     CLI (Rich UI)     │
                          │  app.py  renderer.py  │
                          │       commands.py     │
                          └──────────┬───────────┘
                                     │
                          ┌──────────▼───────────┐
                          │     Agent Loop        │  ← 核心 ReAct 循环
                          │  core/agent_loop.py   │    (类 Claude Code agent loop)
                          └──────────┬───────────┘
                                     │
        ┌──────────────┬─────────────┼─────────────┬──────────────┐
        ▼              ▼             ▼             ▼              ▼
   ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐
   │  Tools   │ │ Permissions│ │ SubAgents│ │  Hooks   │ │  Memory  │
   │ Read/Write│ │ policy.py │ │subagent.py│ │manager.py│ │memory.py │
   │ Bash/Glob │ │ modes.py  │ │builtin/   │ │          │ │          │
   │ Grep/Search│ │           │ │loader.py  │ │          │ │          │
   └──────────┘ └──────────┘ └──────────┘ └──────────┘ └──────────┘
                                     │
                          ┌──────────▼───────────┐
                          │   MCP + RAG + Sandbox │
                          │  mcp/manager.py       │
                          │  rag/pipeline.py      │
                          └──────────────────────┘
```

## 二、参考 Claude Code 的设计对照

| Claude Code | WeaveMindAgent | 实现方式 |
|---|---|---|
| `query()` API | `agent_loop.py` | LangGraph StateGraph + asyncio |
| Built-in Tools (Read,Write,Edit,Bash...) | `tools/builtin/` | LangChain BaseTool |
| Subagents (Explore,Plan,General) | `agents/subagent.py` | 独立 Agent 包装为 Tool |
| `allowedTools` / `disallowedTools` | `permissions/policy.py` | 工具过滤 + 运行时拦截 |
| `permissionMode` (default/acceptEdits...) | `permissions/modes.py` | 策略模式 |
| Hooks (PreToolUse,PostToolUse...) | `hooks/manager.py` | 事件回调 |
| MCP Servers | `mcp/manager.py` | MCP SDK |
| `CLAUDE.md` / Memory | `core/memory.py` | 文件读取 + 向量检索 |
| Sessions / Compaction | `core/session.py` | Token 计数 + 摘要压缩 |
| Skills / Commands | `.weavemind/` 目录 | Markdown 文件加载 |

## 三、目录结构

```
weavemind/
│
├── main.py                          # CLI 入口 (claude 命令)
├── config.yaml                      # 全局配置
├── settings.py                      # 配置加载
│ 
├── core/                            # 核心引擎
│   ├── agent_loop.py                # 主 ReAct 循环 (类 Claude Code agent loop)
│   ├── memory.py                    # MEMORY.md + auto-memory + 长期记忆
│   ├── session.py                   # 会话管理 (创建、恢复、持久化)
│   └── compaction.py                # 上下文压缩 (token 超限自动压缩)
│
├── tools/                           # 工具层 (对应 Claude Code built-in tools)
│   ├── registry.py                  # 工具注册中心
│   ├── base.py                      # 工具基类 (继承 LangChain BaseTool)
│   ├── builtin/
│   │   ├── read.py                  # Read - 读文件
│   │   ├── write.py                 # Write - 创建新文件
│   │   ├── edit.py                  # Edit - 精确编辑文件
│   │   ├── bash.py                  # Bash - 执行终端命令
│   │   ├── monitor.py               # Monitor - 监控后台脚本
│   │   ├── glob.py                  # Glob - 按模式查找文件
│   │   ├── grep.py                  # Grep - 正则搜索文件内容
│   │   ├── web_search.py            # WebSearch - 联网搜索
│   │   ├── web_fetch.py             # WebFetch - 抓取网页内容
│   │   ├── ask_user.py              # AskUserQuestion - 向用户提问
│   │   └── task.py                  # Task/Agent - 调用子 Agent
│   └── openapi.py                   # OpenAPI Schema → LangChain Tool
│
├── agents/                          # 子 Agent 系统
│   ├── subagent.py                  # 子 Agent 核心 (SubAgent 类)
│   ├── builtin/
│   │   ├── explore.py               # Explore Agent (只读、Haiku、快速探索)
│   │   ├── plan.py                  # Plan Agent (只读、用于规划模式)
│   │   └── general.py               # General-purpose Agent (全工具)
│   └── loader.py                    # YAML Frontmatter 加载器
│
├── permissions/                     # 权限系统
│   ├── policy.py                    # 权限策略 (allowedTools / disallowedTools)
│   └── modes.py                     # 权限模式 (default/acceptEdits/bypass/permit)
│
├── hooks/                           # Hooks 系统
│   └── manager.py                   # Hook 管理器 (PreToolUse/PostToolUse/Stop)
│
├── mcp/                             # MCP (Model Context Protocol)
│   ├── client.py                    # MCP Client 封装 (stdio/sse/http)
│   └── manager.py                   # MCP Manager (多服务器管理、工具发现)
│
├── rag/                             # RAG 系统
│   ├── loader.py                    # 文档加载 (PDF/MD/TXT -> LangChain Documents)
│   ├── splitter.py                  # 文本分块 (RecursiveCharacterTextSplitter)
│   ├── embedder.py                  # Embedding (OpenAI 兼容 API)
│   ├── milvus_store.py              # Milvus VectorStore
│   └── pipeline.py                  # RAG 流水线 (load → split → embed → store → search)
│
├── sandbox/                         # 代码执行沙箱
│   └── executor.py                  # 安全 Python 执行环境
│
├── cli/                             # CLI 交互层
│   ├── app.py                       # 主交互循环 (prompt_toolkit + Rich)
│   ├── renderer.py                  # 流式渲染 (Markdown、工具调用展示、权限确认)
│   └── commands.py                  # 命令系统 (/plan /explore /agents /memory ...)
│
├── .weavemind/                      # 项目配置目录 (对应 .claude/)
│   ├── settings.json                # 项目级设置
│   ├── MEMORY.md                    # 项目记忆文件
│   └── agents/                      # 自定义子 Agent (YAML frontmatter + Markdown)
│       └── code-reviewer.md
│
└── tests/
    ├── test_agent_loop.py
    ├── test_tools.py
    ├── test_subagents.py
    ├── test_permissions.py
    └── test_rag.py
```

## 四、核心模块设计

### 4.1 Agent Loop (core/agent_loop.py) — 最核心

LangGraph StateGraph 实现，对应 Claude Code 的 agent loop：

```python
class AgentState(TypedDict):
    messages: Annotated[list, add_messages]
    session_id: str
    permission_mode: str          # default | acceptEdits | bypassPermissions
    allowed_tools: list[str]      # 当前允许的工具名列表
    tool_call_count: int
    model_call_count: int

class AgentLoop:
    def __init__(self, llm, tool_registry, permission_policy, hook_manager):
        self.graph = self._build_graph()

    def _build_graph(self):
        graph = StateGraph(AgentState)
        graph.add_node("think", self._think)           # LLM 推理
        graph.add_node("check_permissions", self._check) # 权限检查
        graph.add_node("act", self._act)               # 执行工具
        graph.add_edge(START, "think")
        graph.add_conditional_edges("think", self._route)
        # 有 tool_call → check_permissions → act → think
        # 无 tool_call → END
        graph.add_edge("check_permissions", "act")
        graph.add_edge("act", "think")
        return graph.compile()
```

### 4.2 工具系统 — 模仿 Claude Code 的 10+ 内置工具

每个工具独立文件，继承 LangChain BaseTool：

| 工具 | 功能 | Claude Code 对应 |
|------|------|-----------------|
| `read.py` | 读文件、读目录 | Read |
| `write.py` | 创建新文件 | Write |
| `edit.py` | 精确字符串替换编辑 | Edit |
| `bash.py` | 执行终端命令 | Bash |
| `glob.py` | 文件模式匹配 | Glob |
| `grep.py` | 正则搜索文件内容 | Grep |
| `web_search.py` | Tavily 联网搜索 | WebSearch |
| `web_fetch.py` | 抓取网页内容 | WebFetch |
| `ask_user.py` | 多选问题向用户确认 | AskUserQuestion |
| `task.py` | 调用子 Agent | Task/Agent |

### 4.3 子 Agent 系统 (agents/)

对应 Claude Code 的 Subagents，每个子 Agent 是 YAML frontmatter 定义 + Markdown 系统提示词：

```yaml
# .weavemind/agents/code-reviewer.md
---
name: code-reviewer
description: Expert code reviewer. Use after code changes.
tools: Read, Glob, Grep
model: inherit
permissionMode: acceptEdits
---
你是一个资深代码审查专家。分析代码并给出质量、安全、最佳实践的建议。
```

内置三个子 Agent：

| Agent | 模型 | 工具 | 用途 |
|-------|------|------|------|
| Explore | Haiku (只读) | Read, Glob, Grep | 快速代码探索 |
| Plan | 继承主对话 | Read, Glob, Grep | 规划模式下的研究 |
| General | 继承主对话 | 所有工具 | 复杂多步操作 |

子 Agent 包装为工具供主 Agent 调用：

```python
# agents/subagent.py
class SubAgentTool(BaseTool):
    name: str = "Task"  # Claude Code 叫 Task/Agent
    description: str = "Launch a new agent to handle complex tasks"

    def _run(self, description, subagent_type, prompt):
        agent = self._load_agent(subagent_type)
        return agent.invoke(prompt)
```

### 4.4 权限系统 (permissions/)

三种权限模式：

```python
class PermissionMode(Enum):
    DEFAULT = "default"             # 标准权限检查，每个工具操作都确认
    ACCEPT_EDITS = "acceptEdits"    # 自动接受文件编辑
    BYPASS = "bypassPermissions"    # 跳过所有权限检查
    PERMIT = "permit"               # 只允许 explicitly allowed 的工具
```

权限策略：

```python
class PermissionPolicy:
    def __init__(self, allowed_tools: list[str], disallowed_tools: list[str]):
        self.allowed = set(allowed_tools)
        self.disallowed = set(disallowed_tools)

    def can_use(self, tool_name: str) -> bool:
        if tool_name in self.disallowed:
            return False
        if self.allowed and tool_name not in self.allowed:
            return False
        return True
```

### 4.5 Memory 系统 (core/memory.py) — 模仿 CLAUDE.md

```python
class WeaveMindMemory:
    """CLAUDE.md + auto-memory + 长期向量记忆"""

    def __init__(self, project_root: str):
        self.project_root = project_root
        self.vector_store = MilvusStore()  # 长期记忆

    def load_project_memory(self) -> str:
        """读取 .weavemind/MEMORY.md 和项目根 CLAUDE.md"""
        memories = []
        for path in [f"{self.project_root}/CLAUDE.md",
                     f"{self.project_root}/.weavemind/MEMORY.md"]:
            if os.path.exists(path):
                memories.append(open(path).read())
        return "\n".join(memories)

    async def search_long_term(self, query: str, k=5):
        """从 Milvus 检索相似历史"""
        return await self.vector_store.similarity_search(query, k)
```

### 4.6 会话管理 + 上下文压缩 (core/session.py + compaction.py)

```python
class SessionManager:
    def create(self) -> str: ...
    def resume(self, session_id: str) -> AgentState: ...
    def save(self, session_id: str, state: AgentState): ...

class ContextCompactor:
    """当 token 超过阈值时自动压缩上下文"""
    def should_compact(self, messages: list) -> bool: ...
    def compact(self, messages: list) -> list:
        """保留最近 N 轮 + 摘要旧对话"""
        ...
```

### 4.7 Hooks 系统 (hooks/manager.py)

```python
class HookManager:
    """PreToolUse / PostToolUse / Stop / SubagentStart / SubagentStop"""

    def register(self, event: str, matcher: str, callback): ...
    async def fire(self, event: str, tool_name: str, context: dict):
        """触发匹配的 hooks"""
        ...
```

### 4.8 MCP 集成 (mcp/)

```python
class MCPManager:
    """管理多个 MCP 服务器的连接和工具发现"""

    def __init__(self, servers: dict[str, MCPConfig]):
        # servers = {"playwright": {"command": "npx", "args": [...]}, ...}
        ...

    async def connect_all(self): ...
    async def get_tools(self) -> list[BaseTool]:
        """返回所有 MCP 服务器的工具"""
        ...
```

### 4.9 RAG 系统 (rag/)

```python
class RAGPipeline:
    def __init__(self):
        self.splitter = RecursiveCharacterTextSplitter(chunk_size=500, overlap=100)
        self.embedder = OpenAIEmbeddings(model="text-embedding-v3")
        self.vector_store = Milvus(...)

    def add_document(self, file_path: str):
        docs = UnstructuredFileLoader(file_path).load()
        chunks = self.splitter.split_documents(docs)
        self.vector_store.add_documents(chunks)

    def search(self, query: str, k=5):
        return self.vector_store.similarity_search(query, k=k)
```

### 4.10 CLI 交互 (cli/)

```python
# cli/app.py
class WeaveMindCLI:
    def run(self):
        """主交互循环"""
        while True:
            cmd = PromptSession().prompt("🧠 > ")

            if cmd.startswith("/"):
                self._handle_command(cmd)  # /plan /explore /agents /memory ...
            else:
                self._handle_chat(cmd)

    def _handle_chat(self, prompt: str):
        """流式对话"""
        with Live(Markdown(""), refresh_per_second=10) as live:
            for event in self.agent.stream({"messages": [HumanMessage(prompt)]}):
                if event["type"] == "response_chunk":
                    accumulated += event["chunk"]
                    live.update(Markdown(accumulated))
                elif event["type"] == "tool_use":
                    live.console.print(f"[yellow]🔧 {event['tool_name']}[/yellow]")
                elif event["type"] == "permission_check":
                    # 需要用户确认
                    approved = Confirm.ask(f"Allow {event['tool_name']}?")
                    ...
```

## 五、3 周开发计划

| 周期 | 内容 | 目标 |
|------|------|------|
| **W1** | LLM接入 + AgentLoop + 工具系统 + CLI | 能对话、能读文件、能执行命令 |
| **W2** | 子Agent + 权限 + Memory + RAG | 子Agent调用、权限控制、记忆系统、知识库检索 |
| **W3** | MCP + Hooks + 会话管理 + 打磨 | 完整的类 Claude Code 体验 |

### Week 1：核心引擎 + 工具

| 天 | 内容 |
|----|------|
| Day 1-2 | 项目骨架、config.yaml、LLM 接入、LangGraph AgentLoop |
| Day 3-4 | Read/Write/Edit/Bash 四个核心工具 |
| Day 5-6 | Glob/Grep/WebSearch/WebFetch 四个搜索工具 |
| Day 7 | CLI 交互 (prompt_toolkit + Rich 流式渲染) |

### Week 2：权限 + 子Agent + Memory + RAG

| 天 | 内容 |
|----|------|
| Day 8-9 | 权限系统 (allowedTools/disallowedTools/permissionMode) |
| Day 10-11 | 子Agent 系统 (SubAgent 类、Explore/Plan/General、YAML加载) |
| Day 12-13 | Memory (CLAUDE.md + auto-memory + Milvus长期记忆) |
| Day 14 | RAG Pipeline (文档加载 → 分块 → Embedding → Milvus) |

### Week 3：MCP + Hooks + 会话 + 打磨

| 天 | 内容 |
|----|------|
| Day 15-16 | MCP 集成 (stdio/sse/http 协议、多服务器管理) |
| Day 17 | Hooks 系统 (PreToolUse/PostToolUse/Stop) |
| Day 18-19 | 会话管理 + 上下文压缩 |
| Day 20-21 | 测试 + README + 示例 .weavemind/ 配置 |

## 六、与 OmniAgent 的对比

| | WeaveMindAgent | OmniAgent |
|---|---|---|
| **定位** | Claude Code 风格的 CLI 工具 | ChatGPT 风格的 Web 平台 |
| **交互** | 终端 + Rich | Vue3 + SSE 流式 |
| **工具系统** | 10+ 内置(Read/Write/Edit/Bash...) | 11 个(天气/搜索/邮件...) |
| **子Agent** | Explore/Plan/General + 自定义 | MCPAgent/SkillAgent 包装为工具 |
| **权限** | ✅ 四种权限模式 | ❌ JWT RBAC |
| **Hooks** | ✅ PreToolUse/PostToolUse | ❌ |
| **MCP** | ✅ Stdio/SSE/HTTP 三种协议 | ✅ 同样支持 |
| **Memory** | CLAUDE.md + 向量检索 | 三种记忆(完整Mem0架构) |
| **会话** | ✅ 持久化 + 压缩 + 恢复 | ❌ 依赖 MySQL |
| **RAG** | 标准 LangChain 流程 | 完整Pipeline(混合检索+Rerank) |
| **代码执行** | 安全沙箱 | Pyodide 沙箱 |
