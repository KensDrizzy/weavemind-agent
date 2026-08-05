# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> 本文件会被注入 WeaveMind 自己的 system prompt（core/memory.py），请保持精简；
> 详细架构与机制说明放在 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

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

## 架构速览

基于 LangGraph 的状态机 Agent，入口 `cli/app.py` 的 `WeaveMindCLI`：

```
WeaveMindCLI — REPL + 顶层编排
  ├── AgentLoop (core/agent_loop.py) — LangGraph StateGraph：think → route → act 循环；plan 分支走 Planner/PlanExecutor
  ├── MultiAgentOrchestrator (agents/orchestrator.py) — /team 模式，Supervisor 编排 Planner/Worker/Reviewer
  ├── ToolRegistry (tools/registry.py) — 内置 + MCP 工具注册（注册顺序影响 LLM 选择倾向）
  ├── CheckpointerProvider (core/checkpoint.py) — SQLite checkpoint，中断恢复；执行记录见 core/task_records.py
  ├── MemoryManager (core/memory.py) — 记忆 + system prompt 组装
  ├── MCPManager (mcp_client/manager.py) — MCP 连接、Chrome 双模式
  └── CodeRAGPipeline (rag/pipeline.py) — 代码库索引与混合检索
```

模块职责、执行模式路由、记忆/MCP/Skill/RAG 机制、中断恢复、会话机制、已知限制、运行时文件清单，全部见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 开发规范

- 新增工具必须继承 `WeaveMindTool`（`tools/base.py`），并在 `ToolRegistry._register_builtins()` 中注册；注册顺序影响 LLM 选择倾向，检索类靠前
- MCP 相关异步操作必须投递到持久后台事件循环（`MCPManager.set_mcp_loop`），禁止 `asyncio.run()`
- 子 Agent 角色扩展在 `agents/` 下实现节点工厂函数，通过 `Command(goto=...)` 回到 supervisor
- Skill 通过在三层目录下添加 `<name>/SKILL.md` 定义，frontmatter 至少含 `name`、`description`
- 配置通过 `settings.get("dotted.key")` 访问，不要直接读取 `config.yaml`
- 所有代码文件使用 UTF-8 无 BOM 编码
- 注释描述意图和约束，不重复代码逻辑
- 禁止 MVP/占位符实现，提交前必须完成全量功能
