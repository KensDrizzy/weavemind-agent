# WeaveMind-Agent SubAgent P0 升级总结

> 本次升级目标：补齐 WeaveMind 多 Agent 机制中子 Agent 委托的 P0 能力，包括工具隔离、批量并行委托、心跳与 stale 检测、非交互审批兜底。

## 背景

升级前，`SubAgentTool` 已经具备独立 ReAct 循环雏形，但存在四个核心短板：

- 子 Agent 工具集缺少强制隔离，存在递归委托、用户交互、长期记忆写入等越权风险。
- 单次只能委托一个子 Agent，无法高效处理互不依赖的批量任务。
- 父 Agent 对子 Agent 运行状态不可见，只能阻塞等待，没有心跳和跑飞检测。
- 子 Agent 在线程池中运行时如果触发 HITL 审批，可能尝试读取终端输入并造成阻塞。

本次 P0 改造围绕“最小权限、故障隔离、运行可观测、默认安全”四个原则落地。

## 核心改造

### 1. 子 Agent 工具隔离

新增 `SUBAGENT_BLOCKED_TOOLS` 黑名单，默认禁止子 Agent 加载以下能力：

- `Task` / `BatchDelegate` / `delegate_task`：禁止递归委托，避免子 Agent 无限生成子任务。
- `AskUser`：禁止子 Agent 直接向用户提问，避免后台线程阻塞交互入口。
- `MemoryAdd` / `MemorySearch` / `CoreMemoryEdit`：隔离共享长期记忆和核心记忆边界。

工具加载策略调整为：

- agent definition 显式声明工具时，逐个过滤黑名单。
- 未声明工具时，默认加载全部注册工具中的非黑名单工具。
- 所有子 Agent 工具统一经过 `_SubAgentApprovalTool` 包装，保留原工具 schema，避免影响 LLM tool calling。

相关实现：

- `agents/subagent.py`
- `SUBAGENT_BLOCKED_TOOLS`
- `_load_subagent_tools()`
- `_SubAgentApprovalTool`

### 2. 非交互审批兜底

新增 `_make_subagent_approval_callback()`，为子 Agent 提供不会读 stdin 的审批策略：

- 默认 `auto_approve=false`：危险工具自动拒绝，并记录审计日志。
- 可配置 `delegation.subagent_auto_approve=true`：自动批准危险工具，适合本地 YOLO 场景，但仍保留审计日志。

这解决了子 Agent 在线程池中触发 Bash/Edit/Write 等危险工具时可能卡住终端审批的问题。

### 3. 批量并行委托

新增 `BatchDelegateTool`，用于执行互不依赖的并行子任务。

能力包括：

- 基于 `ThreadPoolExecutor` 启动多个子 Agent。
- 支持 `max_parallel` 并发上限，默认读取 `delegation.max_concurrent_children`。
- 支持单子任务 `timeout`，默认读取 `delegation.child_timeout_seconds`。
- 单个子任务失败或超时不会影响其他任务。
- 最终结果按“成功完成”和“失败/超时”分区汇总，并按 `delegation.max_result_chars` 截断。

相关实现：

- `agents/batch_delegate.py`
- `BatchDelegateTool._run()`
- `BatchDelegateTool._run_single_task()`
- `BatchDelegateTool._summarize_results()`

### 4. 心跳与 stale 检测

新增 `SubAgentMonitor`，统一管理子 Agent 生命周期状态。

状态模型：

- `RUNNING`
- `THINKING`
- `IN_TOOL`
- `IDLE`
- `STALE`
- `COMPLETED`
- `FAILED`
- `INTERRUPTED`

检测策略：

- idle/thinking 场景：默认 15 个 30s 周期，即 450s 判定 stale。
- in-tool 场景：默认 40 个 30s 周期，即 1200s 判定 stale，避免误杀长工具。
- stale 后可通过 `interrupt()` 请求中断对应 future。

相关实现：

- `agents/monitor.py`
- `SubAgentMonitor.register()`
- `SubAgentMonitor.heartbeat()`
- `SubAgentMonitor.check_stale()`
- `SubAgentMonitor.interrupt()`

## 注册与配置

`ToolRegistry` 现在会注册：

- `Task`
- `BatchDelegate`

两者共享同一个 `SubAgentMonitor`，并注入当前 `ToolRegistry`，保证子 Agent 工具加载可以复用主进程注册表，同时经过隔离过滤。

新增配置示例：

```yaml
delegation:
  max_concurrent_children: 3
  child_timeout_seconds: 600
  max_result_chars: 8000
  subagent_auto_approve: false
  heartbeat_interval_seconds: 30
  stale_cycles_idle: 15
  stale_cycles_in_tool: 40
```

## 测试覆盖

新增/扩展 `tests/test_subagents.py`，覆盖：

- 显式工具列表过滤黑名单。
- 默认工具集排除黑名单。
- 危险工具默认自动拒绝。
- `BatchDelegateTool` 并行执行耗时接近最慢任务。
- 单任务失败不影响其他任务结果。
- 单任务超时能被隔离并进入汇总。
- idle stale 检测。
- in-tool 长任务窗口保护。
- interrupt 只影响目标子 Agent。

验证命令：

```bash
python3 -m pytest tests/test_subagents.py -q
python3 -m py_compile agents/subagent.py agents/monitor.py agents/batch_delegate.py tools/registry.py agents/__init__.py tests/test_subagents.py
```

当前结果：

```text
15 passed
```

## 简历亮点写法

可以提炼为：

> 升级 WeaveMind-Agent 子 Agent 委托系统，设计工具隔离黑名单与非交互审批兜底，禁止递归委托、用户交互和共享记忆越权；新增 BatchDelegate 并行委托工具，基于 ThreadPoolExecutor 实现并发上限、单任务超时和部分失败隔离；实现 SubAgentMonitor 心跳治理，区分 idle 与 in-tool 两类停滞场景，分别以 450s/1200s 阈值识别 stale 任务并支持中断回收，提升多 Agent 执行的安全性、吞吐与可观测性。

面试展开时建议按三层讲：

- 安全边界：最小权限原则、递归委托防护、非交互审批避免线程池死锁。
- 并行调度：ThreadPoolExecutor、max concurrency、timeout isolation、partial failure summary。
- 运行治理：heartbeat、双场景 stale detection、future interrupt、审计日志。
