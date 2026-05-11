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

## 测试

```bash
pytest tests/                    # 全部测试
pytest tests/test_tools.py       # 单文件
pytest tests/test_tools.py::test_write_and_read -v  # 单测试函数
```

测试框架：pytest（3 个测试文件：`test_tools.py`、`test_permissions.py`、`test_subagents.py`）。`conftest.py` 将项目根目录加入 `sys.path`。

## 架构

基于 LangGraph 的状态机 Agent，入口在 `cli/app.py` 的 `WeaveMindCLI`。

```
WeaveMindCLI (cli/app.py) — 顶层编排器 + REPL
  ├── AgentLoop (core/agent_loop.py) — LangGraph StateGraph
  │     循环：think → route → [check_permissions → act → think] 或 [END]
  │     最大迭代 50 次，recursion_limit=100
  ├── ToolRegistry (tools/registry.py) — 注册 9 个内置工具 + SubAgentTool
  ├── PermissionPolicy (permissions/policy.py) — 按模式控制工具调用
  ├── HookManager (hooks/manager.py) — PreToolUse/PostToolUse 事件总线
  └── WeaveMindMemory (core/memory.py) — 启动时注入 CLAUDE.md + .weavemind/MEMORY.md
```

**数据流：** 用户输入 → `HumanMessage` → `AgentLoop.stream()` → LangGraph 循环至 LLM 无 tool_calls → `renderer.stream_response()` 输出。

### 核心模块

| 模块 | 关键类 | 职责 |
|------|--------|------|
| `core/agent_loop.py` | `AgentLoop`, `AgentState` | LangGraph 状态机，think/check/act 循环 |
| `core/llm_factory.py` | — | LLM 实例化（当前始终返回 `ChatAnthropic`） |
| `core/memory.py` | `WeaveMindMemory` | 加载 CLAUDE.md + MEMORY.md 为 SystemMessage |
| `core/session.py` | `SessionManager` | 会话创建/保存/恢复（JSON） |
| `core/compaction.py` | `ContextCompactor` | token 计数 + 上下文压缩（已定义但未调用） |
| `tools/base.py` | `WeaveMindTool` | 抽象基类，继承 `langchain_core.tools.BaseTool` |
| `tools/registry.py` | `ToolRegistry` | 自动注册内置工具 + 手动注册 SubAgentTool |
| `permissions/modes.py` | `PermissionMode` | 枚举：DEFAULT, ACCEPT_EDITS, BYPASS, PERMIT |
| `agents/subagent.py` | `SubAgentTool` | 工具名 "Task"，生成独立 LLM 调用 |
| `agents/loader.py` | — | 从 `.weavemind/agents/*.md` 加载 YAML frontmatter 定义 |
| `cli/renderer.py` | — | Rich 流式输出渲染 |
| `cli/commands.py` | — | /help, /memory, /sessions, /mode, /clear, /exit |
| `settings.py` | — | 单例 YAML 配置加载器，`settings.get("dotted.key")` |

### 内置工具（10 个）

`Read`, `Glob`, `Grep`, `WebFetch`, `WebSearch`(Tavily), `AskUser`, `Edit`, `Write`, `Bash`, `Task`(SubAgentTool)

### 子 Agent 定义（`.weavemind/agents/`）

- **explore** — `claude-haiku-4-5-20251001`，只读工具 [Read, Glob, Grep]
- **general** — 继承主模型，全部工具
- **plan** — 继承主模型，只读工具 [Read, Glob, Grep]

### 权限模式

- `default` — 除非在 `disallowed_tools` 中，否则允许
- `acceptEdits` — Edit/Bash 类工具免确认
- `bypassPermissions` — 跳过所有检查
- `permit` — 仅白名单工具可用

`DANGEROUS_TOOLS = {"Bash"}`, `EDIT_TOOLS = {"Write", "Edit"}`

## 已知限制

- `llm_factory.py` 始终返回 `ChatAnthropic`，`config.yaml` 中的 `provider` 设置无效。仅支持 Anthropic 兼容端点（通过 `anthropic_api_url`）。
- `SubAgentTool` 硬编码 `ChatAnthropic` + `claude-haiku-4-5-20251001`，忽略配置的 provider。
- `mcp/` 和 `rag/` 模块已定义但**未接入** CLI，仅可扩展。
- `session_manager.save()` 从未被调用，会话不持久化。
- Memory 在启动时注入一次，运行中修改 CLAUDE.md 不会生效。
- `ContextCompactor` 已定义但未在主循环中调用。

## 运行时文件

- `.weavemind/MEMORY.md` — 作为系统提示注入的项目记忆
- `.weavemind/sessions/` — 会话 JSON（当前仅写入）
- `.weavemind/agents/*.md` — 子 Agent 定义（YAML frontmatter: `name`, `model`, `system_prompt`）
- `.weavemind/chroma/` — RAG 向量存储
- `config.yaml` — 运行时配置（LLM provider/model、内存路径、会话、权限、RAG）
- `.weavemind/settings.json` — 项目级权限和 MCP 配置

## 开发规范

- 新增工具必须继承 `WeaveMindTool`（`tools/base.py`），并在 `ToolRegistry._register_builtins()` 中注册
- 子 Agent 通过在 `.weavemind/agents/` 下添加 YAML frontmatter 的 `.md` 文件定义
- 配置通过 `settings.get("dotted.key")` 访问，不要直接读取 `config.yaml`
- 所有代码文件使用 UTF-8 无 BOM 编码
- 注释描述意图和约束，不重复代码逻辑
- 禁止 MVP/占位符实现，提交前必须完成全量功能
