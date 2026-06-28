# WeaveMindAgent — 子 Agent 委托系统改造计划

> **对应分析文档：** Hermes Agent 子 Agent 委托深度分析（`agent-wiki/general/hermes-subagent-delegation.md`）
> **改造目标：** 将 WeaveMindAgent 现有的 SubAgent/Worker 升级为具备完整隔离+并行+可观测+治理能力的委托系统
> **秋招价值：** ⭐⭐⭐⭐⭐ 安全边界设计 + 并行调度架构 + 纵深防御体系

---

## 一、现状分析

### 1.1 当前系统全景

WeaveMindAgent 目前有三处涉及子 Agent / 多 Agent 的机制，彼此独立：

| 机制 | 模块 | 职责 | 局限 |
|------|------|------|------|
| **SubAgentTool** | `agents/subagent.py` | 单次子 Agent 调用（Tool） | 每次只能启动一个；无工具隔离；无超时心跳 |
| **MultiAgentOrchestrator** | `agents/orchestrator.py` | Supervisor 模式编排 Planner/Worker/Reviewer | Supervisor 硬编码角色；Worker 共享全部工具；无深度限制 |
| **PlanExecutor** | `core/plan_executor.py` | DAG 规划 + 拓扑序并行执行 | 只能执行内置 Task（tool call）；结果直接进上下文 |

### 1.2 当前 SubAgentTool 实现细节

```python
class SubAgentTool(WeaveMindTool):
    name = "Task"
    description = "Launch a sub-agent with full ReAct loop..."
    
    agent_defs: dict = {}  # 外部注入的不同 Agent 类型定义
    
    def _run(self, description, subagent_type, prompt):
        # 1. 从 agent_defs 获取模型和工具定义
        agent_def = self.agent_defs.get(subagent_type, {})
        model = agent_def.get("model", None)
        system = agent_def.get("system_prompt", ...)
        tool_names = agent_def.get("tools", [])
        
        # 2. 创建 LLM（继承父或独立）
        llm = create_llm(provider=provider, model=model)
        
        # 3. 获取工具（全部注册工具，无过滤！）
        tools = []
        for tn in tool_names:
            tool = self._tool_registry.get(tn)
            if tool: tools.append(tool)
        
        # 4. 执行 ReAct 循环
        agent = create_react_agent(llm, tools=tools, prompt=system)
        result = agent.invoke({"messages": [HumanMessage(content=prompt)]})
        return result["messages"][-1].content
```

### 1.3 与 Hermes Agent 的差距总览

| # | 能力 | Hermes Agent | WeaveMindAgent 现状 | 差距等级 |
|---|------|-------------|-------------------|---------|
| 1 | **工具隔离** | DELEGATE_BLOCKED_TOOLS 硬编码黑名单 | 子 Agent 可用全部工具，无限制 | 🚨 P0 |
| 2 | **批量并行** | tasks[] + ThreadPoolExecutor + max_concurrent_children | 单次调用 | 🚨 P0 |
| 3 | **心跳检测** | 30s 心跳 + 双场景停滞（450s/1200s） | 无心跳，依赖全局超时 | 🚨 P0 |
| 4 | **审批安全** | 非交互审批回调（auto-deny/approve） | 继承父 TUI 回调，可能死锁 | 🚨 P0 |
| 5 | **深度限制** | max_spawn_depth + _delegate_depth 追踪 | 无嵌套限制 | 🔶 P1 |
| 6 | **结果控制** | 输出尾部摘要（最后12工具+8000字符） | 返回全部消息列表 | 🔶 P1 |
| 7 | **可观测性** | DelegateEvent 全生命周期事件推送 | 无实时进度 | 🔶 P1 |
| 8 | **凭证独立** | delegation.provider/model 独立配置 | 继承父或 agent_defs 固定模型 | 🔶 P1 |
| 9 | **运行时控制** | 暂停新建 + 按 ID 中断子 Agent | 无暂停/中断 | 🔷 P2 |
| 10 | **Orchestrator 提示** | 动态提示词告知 LLM 委托边界 | Supervisor 硬编码规则 | 🔷 P2 |

---

## 二、改造目标

### 2.1 总体架构

```
                    ┌─────────────────────────────┐
                    │     AgentLoop (主循环)        │
                    │   /team & auto-detect 入口     │
                    └──────────┬──────────────────┘
                               │
                    ┌──────────▼──────────────────┐
                    │  MultiAgentOrchestrator v2   │  ← 增强版 Supervisor
                    │  ┌────────────────────────┐  │
                    │  │ Supervisor (LLM + rule) │  │
                    │  │  + max_spawn_depth      │  │
                    │  │  + DelegationEvent 推送  │  │
                    │  └───┬────┬────┬────┬─────┘  │
                    │      │    │    │    │         │
                    │  ┌───▼┐ ┌▼───┐┌▼───┐┌▼────┐  │
                    │  │Plnt│ │Wk1 ││Wk2 ││Revwr│  │
                    │  └────┘ └────┘└────┘└─────┘  │
                    └─────────────────────────────┘
                               │
          ┌────────────────────┼────────────────────┐
          │                    │                    │
    ┌─────▼──────┐    ┌───────▼────────┐   ┌──────▼──────┐
    │ SubAgent   │    │ BatchDelegate  │   │ SubAgent    │
    │ Tool (v2)  │    │ Tool (new)     │   │ Monitor     │
    │ +隔离+审批   │    │ +并行+超时+汇总  │   │ (new)       │
    └────────────┘    └────────────────┘   │ +心跳+中断   │
                                           └─────────────┘
```

### 2.2 各模块职责

| 模块 | 文件 | 职责 |
|------|------|------|
| **SubAgentTool v2** | `agents/subagent.py` | 单次子 Agent 调用 + 工具隔离 + 审批安全 + 结果控制 |
| **BatchDelegateTool** | `agents/batch_delegate.py` | 批量并行委托 + 超时容错 + 结果汇总 |
| **SubAgentMonitor** | `agents/monitor.py` | 心跳检测 + stale 判定 + 暂停/中断控制 |
| **MultiAgentOrchestrator v2** | `agents/orchestrator.py` | Supervisor + 深度限制 + 委派事件 + 动态提示 |
| **DelegationConfig** | `agents/delegation_config.py` | 凭证独立 + 最大并发 + 超时 + 审批模式配置 |

---

## 三、详细设计方案

### 3.1 P0 — 子 Agent 工具集隔离 + 黑名单机制

#### 现状问题

当前 `SubAgentTool._run()` 从 `agent_defs.get("tools", [])` 获取工具列表，但如果不指定工具名则默认使用全部注册工具。即使指定了工具名，也**没有拒绝列表**——无法禁止子 Agent 调用某些危险工具。

#### 设计

```python
# agents/subagent.py — 新增

SUBAGENT_BLOCKED_TOOLS = frozenset([
    "delegate_task",      # 禁止递归委托
    "Task",               # 禁止调用另一个 SubAgentTool（防止间接递归）
    "AskUser",            # 禁止向用户提问
    "MemoryAdd",          # 禁止写共享长期记忆
    "MemorySearch",       # 禁止搜索共享记忆（读可以但写不行？实际上读也越界了）
    "CoreMemoryEdit",     # 禁止修改核心记忆
])

class SubAgentTool(WeaveMindTool):
    name: str = "Task"
    
    def _run(self, description: str, subagent_type: str, prompt: str) -> str:
        agent_def = self.agent_defs.get(subagent_type, {})
        tool_names = agent_def.get("tools", [])
        
        # 获取工具
        tools = []
        for tn in tool_names:
            if tn in SUBAGENT_BLOCKED_TOOLS:
                logger.warning(f"子 Agent 拒绝加载被禁工具: {tn}")
                continue
            tool = self._tool_registry.get(tn)
            if tool:
                tools.append(tool)
        
        # 如果没有指定工具名，默认加载全部非禁用工具
        if not tool_names:
            all_tools = self._tool_registry.get_langchain_tools()
            tools = [t for t in all_tools if t.name not in SUBAGENT_BLOCKED_TOOLS]
        
        # 继续执行...
```

#### 涉及变更

- `agents/subagent.py`：新增 `SUBAGENT_BLOCKED_TOOLS` 常量 + 加载过滤逻辑
- 配置文件（可选）：`subagent.blocked_tools: []` 允许用户扩展黑名单

#### 测试策略

```python
def test_subagent_tool_filters_blocked_tools():
    """验证 SubAgentTool 过滤掉被禁工具"""
    tool = SubAgentTool(...)
    # 注入包含 AskUser 的工具集
    # 调用 _run 验证 AskUser 不在最终 tools 中
```

#### 面试话术

> "我实现了子 Agent 的最小权限原则：通过 `SUBAGENT_BLOCKED_TOOLS` 硬编码黑名单，禁止子 Agent 递归委托、跨 Agent 通信、修改共享记忆。这样即使 Agent 定义配置了全部工具，实际运行时也会被安全过滤，形成纵深防御的第一道防线。"

---

### 3.2 P0 — 批量并行委托 + 超时控制

#### 现状问题

`SubAgentTool` 一次只能启动一个子 Agent。对于"调研 5 个数据库"这类可并行任务，需要串行执行 5 次，总耗时 = 5 倍单次时间。

#### 设计

```python
# agents/batch_delegate.py — 新增

import concurrent.futures
import logging
from tools.base import WeaveMindTool

logger = logging.getLogger(__name__)

class BatchDelegateTool(WeaveMindTool):
    """批量并行子 Agent 委托工具"""
    
    name: str = "BatchDelegate"
    description: str = (
        "Launch multiple sub-agents in parallel for independent tasks. "
        "Use when tasks are independent of each other. "
        "Returns a summarized result of all sub-agents."
    )
    
    # 可配置参数
    DEFAULT_MAX_PARALLEL = 3
    DEFAULT_CHILD_TIMEOUT = 600  # 10分钟
    DEFAULT_MAX_RESULTS_CHARS = 8000
    
    agent_defs: dict = {}
    subagent_monitor: object = None  # SubAgentMonitor 实例
    
    def _run(
        self,
        tasks: list[dict],
        max_parallel: int = None,
        timeout: int = None,
    ) -> str:
        """批量并行执行子 Agent。
        
        Args:
            tasks: [{"goal": "...", "subagent_type": "...", "toolsets": [...]}, ...]
            max_parallel: 最大并行数（默认 delegation.max_concurrent_children）
            timeout: 每个子 Agent 的超时秒数
        
        Returns:
            汇总后的结果字符串
        """
        max_parallel = max_parallel or self.DEFAULT_MAX_PARALLEL
        timeout = timeout or self.DEFAULT_CHILD_TIMEOUT
        
        results = []
        errors = []
        
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max_parallel,
            thread_name_prefix="subagent"
        ) as executor:
            future_map = {
                executor.submit(
                    self._run_single_task, t, timeout
                ): t for t in tasks
            }
            
            for future in concurrent.futures.as_completed(future_map, timeout=None):
                task = future_map[future]
                try:
                    result = future.result(timeout=timeout)
                    results.append({
                        "task": task.get("goal", ""),
                        "status": "completed",
                        "summary": self._truncate(result, 2000),
                    })
                except concurrent.futures.TimeoutError:
                    errors.append({
                        "task": task.get("goal", ""),
                        "status": "timeout",
                    })
                except Exception as e:
                    errors.append({
                        "task": task.get("goal", ""),
                        "status": "error",
                        "detail": str(e),
                    })
        
        return self._summarize_results(results, errors)
    
    def _run_single_task(self, task: dict, timeout: int) -> str:
        """执行单个子 Agent 任务。"""
        # 复用 SubAgentTool v2 的核心逻辑
        subagent_type = task.get("subagent_type", "default")
        prompt = task.get("goal", "")
        toolsets = task.get("toolsets", [])
        
        agent_def = self.agent_defs.get(subagent_type, {})
        # ... 创建 LLM、过滤工具、创建 ReAct Agent、执行 ...
    
    def _summarize_results(
        self, 
        results: list[dict], 
        errors: list[dict]
    ) -> str:
        """汇总所有子 Agent 的结果。"""
        parts = []
        
        if results:
            parts.append(f"## 成功完成 ({len(results)} 个)")
            for r in results:
                parts.append(f"### {r['task']}\n{r['summary']}")
        
        if errors:
            parts.append(f"## 失败/超时 ({len(errors)} 个)")
            for e in errors:
                parts.append(f"- {e['task']}: {e['status']}")
                if 'detail' in e:
                    parts.append(f"  {e['detail']}")
        
        combined = "\n\n".join(parts)
        return self._truncate(combined, self.DEFAULT_MAX_RESULTS_CHARS)
    
    @staticmethod
    def _truncate(text: str, max_chars: int) -> str:
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + f"\n\n[... 已截断，原文 {len(text)} 字符]"
```

#### 配置支持

```yaml
# config.yaml
delegation:
  max_concurrent_children: 3    # 最大并行数
  child_timeout_seconds: 600    # 子 Agent 超时
  max_result_chars: 8000        # 结果汇总最大字符数
```

#### 测试策略

```python
def test_batch_delegate_parallel_execution():
    """验证任务并行执行（总耗时 ≈ 最慢任务）"""
    
def test_batch_delegate_partial_failure():
    """验证部分失败不影响其他任务"""
    
def test_batch_delegate_timeout_isolation():
    """验证超时的任务被隔离，不影响正常任务"""
```

#### 面试话术

> "我设计了批量并行委托工具 `BatchDelegateTool`，基于 `ThreadPoolExecutor` + `as_completed` 模式。核心设计原则是『故障隔离』：一个子 Agent 超时或失败不影响其他正在执行的子 Agent，最终结果会汇总成功和失败两部分。父 Agent 根据汇总信息决定是否需要重试失败任务。"

---

### 3.3 P0 — 子 Agent 心跳 + stale 检测

#### 现状问题

子 Agent 调用 `create_react_agent.invoke()` 后，父 Agent 完全阻塞等待。如果子 Agent 进入死循环、API 响应卡住、或工具调用挂起（如等待用户输入），父 Agent 无感知，只能依赖全局超时暴力终结。

#### 设计

采用**双场景停滞检测**：区分"轮次间空闲"和"工具内执行"两种不同的停滞情况。

```python
# agents/monitor.py — 新增

import time
import logging
import threading
from enum import Enum

logger = logging.getLogger(__name__)


class SubAgentStatus(Enum):
    RUNNING = "running"
    THINKING = "thinking"      # LLM 推理中
    IN_TOOL = "in_tool"        # 工具执行中
    IDLE = "idle"              # 轮次间等待
    STALE = "stale"            # 判定为跑飞
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class SubAgentHeartbeat:
    """子 Agent 心跳状态"""
    
    def __init__(self, subagent_id: str):
        self.subagent_id = subagent_id
        self.status = SubAgentStatus.RUNNING
        self.last_heartbeat = time.time()
        self.current_tool = ""
        self.cycle_count = 0           # 连续 idle 周期数
        self.in_tool_cycle_count = 0   # 连续 in_tool 周期数
        self.future = None             # concurrent.futures.Future


class SubAgentMonitor:
    """子 Agent 心跳监控器
    
    设计理念（借鉴 Hermes Agent）：
    - 轮次间空闲：子 Agent 可能在等 API 响应，15 个心跳周期没动静标记为 stale
    - 工具内执行：子 Agent 可能在跑长命令（npm install），给 40 个周期
    """
    
    _HEARTBEAT_INTERVAL = 30          # 秒
    _STALE_CYCLES_IDLE = 15           # 15 × 30s = 450s 空闲判跑飞
    _STALE_CYCLES_IN_TOOL = 40        # 40 × 30s = 1200s 工具内判跑飞
    
    def __init__(self):
        self._active: dict[str, SubAgentHeartbeat] = {}
        self._paused = False          # 全局暂停新建委托
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
    
    def register(self, subagent_id: str, future=None) -> SubAgentHeartbeat:
        """注册一个新的子 Agent 到监控"""
        hb = SubAgentHeartbeat(subagent_id)
        hb.future = future
        with self._lock:
            self._active[subagent_id] = hb
        return hb
    
    def heartbeat(self, subagent_id: str, status: SubAgentStatus, tool: str = ""):
        """子 Agent 上报心跳"""
        with self._lock:
            hb = self._active.get(subagent_id)
            if hb:
                hb.last_heartbeat = time.time()
                hb.status = status
                hb.current_tool = tool
                if status == SubAgentStatus.IDLE:
                    hb.cycle_count += 1
                    hb.in_tool_cycle_count = 0
                elif status == SubAgentStatus.IN_TOOL:
                    hb.in_tool_cycle_count += 1
                    hb.cycle_count = 0
                else:
                    hb.cycle_count = 0
                    hb.in_tool_cycle_count = 0
    
    def check_stale(self) -> list[str]:
        """检查所有活跃子 Agent 是否 stale（由心跳线程定期调用）"""
        stale_ids = []
        with self._lock:
            now = time.time()
            for sid, hb in list(self._active.items()):
                if hb.status in (SubAgentStatus.COMPLETED, SubAgentStatus.FAILED):
                    continue
                
                elapsed = now - hb.last_heartbeat
                
                if hb.status == SubAgentStatus.IN_TOOL:
                    if hb.in_tool_cycle_count >= self._STALE_CYCLES_IN_TOOL:
                        stale_ids.append(sid)
                        hb.status = SubAgentStatus.STALE
                        logger.warning(
                            f"子 Agent {sid} 工具内停滞 {elapsed:.0f}s, 标记为 STALE"
                        )
                else:
                    if hb.cycle_count >= self._STALE_CYCLES_IDLE:
                        stale_ids.append(sid)
                        hb.status = SubAgentStatus.STALE
                        logger.warning(
                            f"子 Agent {sid} 空闲 {elapsed:.0f}s, 标记为 STALE"
                        )
        return stale_ids
    
    def unregister(self, subagent_id: str):
        """移除完成的子 Agent"""
        with self._lock:
            self._active.pop(subagent_id, None)
    
    def set_paused(self, paused: bool):
        """暂停/恢复新建委托"""
        self._paused = paused
    
    @property
    def is_paused(self) -> bool:
        return self._paused
    
    def interrupt(self, subagent_id: str) -> bool:
        """中断指定子 Agent"""
        with self._lock:
            hb = self._active.get(subagent_id)
            if hb and hb.future and not hb.future.done():
                hb.future.cancel()
                hb.status = SubAgentStatus.INTERRUPTED
                logger.info(f"子 Agent {subagent_id} 已中断")
                return True
        return False
    
    def start_heartbeat_thread(self):
        """启动后台心跳检查线程"""
        def _loop():
            while not self._stop_event.is_set():
                stale = self.check_stale()
                if stale:
                    for sid in stale:
                        self.interrupt(sid)
                self._stop_event.wait(self._HEARTBEAT_INTERVAL)
        
        thread = threading.Thread(target=_loop, daemon=True, name="subagent-hb")
        thread.start()
        return thread
    
    def stop(self):
        """停止心跳线程"""
        self._stop_event.set()
```

#### 集成方式

在 `SubAgentTool._run()` 和 `BatchDelegateTool._run_single_task()` 中，将 `create_react_agent` 的回调嵌入心跳上报：

```python
# 在 create_react_agent 的回调中注入心跳
class HeartbeatCallback:
    def __init__(self, monitor, subagent_id):
        self.monitor = monitor
        self.subagent_id = subagent_id
    
    def on_llm_start(self): ...  # → THINKING
    def on_tool_start(self, tool): ...  # → IN_TOOL
    def on_tool_end(self): ...    # → IDLE
```

#### 测试策略

```python
def test_monitor_detects_stale_idle():
    """验证 idle 超过 450s 被标记 stale"""

def test_monitor_allows_long_tool():
    """验证工具内执行 1000s 不被误判"""

def test_monitor_interrupt():
    """验证中断一个子 Agent 不影响其他"""
```

#### 面试话术

> "我实现了子 Agent 的双场景心跳检测机制：以 30 秒为间隔，区分『轮次间空闲』和『工具内执行』两种停滞。空闲状态 450 秒无响应判跑飞，工具内执行给到 1200 秒。这个设计避免了一个正在跑 `npm install` 的长工具被误杀，同时又能在真正跑飞时及时回收资源。"

---

### 3.4 P0 — 非交互审批回调

#### 现状问题

子 Agent 在 `ThreadPoolExecutor` 工作线程中执行，TUI 审批回调（`prompt_toolkit`）不继承父线程的 TLS 上下文。如果子 Agent 调用了一个需要审批的危险命令（如 Bash），它会尝试从 stdin 读取——但 stdin 被父 Agent 的 TUI 占着，导致**死锁**。

#### 设计

```python
# agents/subagent.py — 新增

import logging

logger = logging.getLogger(__name__)


def _make_subagent_approval_callback(auto_approve: bool = False):
    """为子 Agent 创建非交互式审批回调。
    
    子 Agent 在 ThreadPoolExecutor 线程中运行，不继承父线程的
    TUI 审批回调。直接调用 input() 会因 stdin 被父 TUI 占用而死锁。
    
    本方法返回一个安全的替代回调：
    - auto_approve=False（默认）：自动拒绝，记录审计日志
    - auto_approve=True（YOLO模式）：自动批准，记录审计日志
    """
    if auto_approve:
        def callback(command, description, **kwargs):
            logger.warning(
                f"[子Agent审计] 自动批准危险命令: {command} | {description}"
            )
            return "once"
    else:
        def callback(command, description, **kwargs):
            logger.warning(
                f"[子Agent审计] 自动拒绝危险命令: {command} | {description}"
            )
            return "deny"
    
    return callback
```

#### 配置集成

```yaml
# config.yaml
delegation:
  subagent_auto_approve: false   # 默认自动拒绝
```

#### 集成到 SubAgentTool

```python
# SubAgentTool._run() 中
from agents.subagent import _make_subagent_approval_callback

# 安装子 Agent 专用的审批回调
subagent_approval = _make_subagent_approval_callback(
    auto_approve=settings.get("delegation.subagent_auto_approve", False)
)

# 将回调注册到 HitlToolRegistry / PermissionPolicy
if hasattr(self, '_permission_policy'):
    self._permission_policy.set_subagent_callback(subagent_approval)
```

#### 面试话术

> "我发现了子 Agent 在 ThreadPoolExecutor 中执行时的审批死锁问题——工作线程尝试从 stdin 读用户输入，但 stdin 被父 Agent 的 TUI 占着。解决方案是为子 Agent 安装『非交互审批回调』：默认 auto-deny 安全第一，同时记录完整审计日志；YOLO 模式可配置 auto-approve。这个设计既防止了死锁，又保留了安全审计能力。"

---

### 3.5 P1 — 深度限制 + 嵌套委托

#### 设计

```python
# agents/subagent.py — 新增深度追踪

class SubAgentTool(WeaveMindTool):
    _delegate_depth: int = 0        # 当前嵌套深度
    MAX_SPAWN_DEPTH: int = 1         # 默认扁平（不可嵌套）
    
    def _run(self, ...):
        if self._delegate_depth >= self.MAX_SPAWN_DEPTH:
            logger.warning(
                f"已达委托深度上限 ({self.MAX_SPAWN_DEPTH})，拒绝创建子 Agent"
            )
            return f"[拒绝] 委托深度已达上限 ({self.MAX_SPAWN_DEPTH})"
        
        # 创建子 Agent 时传递深度 + 1
        child_tool = SubAgentTool(
            _delegate_depth=self._delegate_depth + 1,
            MAX_SPAWN_DEPTH=self.MAX_SPAWN_DEPTH,
            ...
        )
```

#### Orchestrator 提示词动态生成

```python
# agents/orchestrator.py — 新增动态提示词构建

def _build_orchestrator_prompt(max_spawn_depth: int, child_depth: int) -> str:
    """根据当前深度动态生成 Orchestrator 提示词。
    
    告知 LLM 它能委托的深度边界，防止模型幻觉。
    """
    remaining = max_spawn_depth - child_depth
    parts = [
        "### 子 Agent 生成指南",
        f"你有权使用 delegate_task 工具创建自己的子 Agent（剩余深度: {remaining}）。",
        "",
        "何时委托：",
        "- 目标可拆分为 2+ 个独立子任务并行执行",
        "- 子任务推理密集，会撑爆你的上下文",
        "",
        "何时不委托：",
        "- 单步机械操作——直接做",
        "- 简单任务，一两个工具调用就能完成",
    ]
    return "\n".join(parts)
```

---

### 3.6 P1 — 子 Agent 结果尾部摘要

#### 设计

```python
# agents/subagent.py — 新增

def extract_output_tail(
    result_messages: list,
    max_entries: int = 12,
    max_chars: int = 8000,
) -> list[dict]:
    """提取子 Agent 对话的最后 N 个工具调用结果。
    
    设计目的：父 Agent 不需要看到子 Agent 的完整中间步骤，
    只需要最终结果 + 关键工具调用的预览。
    
    Returns:
        [{"tool": tool_name, "preview": preview_text, "is_error": bool}, ...]
    """
    # 构建 tool_call_id → tool_name 映射
    tool_name_map = {}
    for msg in result_messages:
        if hasattr(msg, "tool_calls") and msg.tool_calls:
            for tc in msg.tool_calls:
                tool_name_map[tc["id"]] = tc.get("name", "unknown")
    
    # 反向遍历，提取最新的工具结果
    tail = []
    for msg in reversed(result_messages):
        if len(tail) >= max_entries:
            break
        if hasattr(msg, "type") and msg.type == "tool":
            tool_name = tool_name_map.get(getattr(msg, "tool_call_id", ""), "?")
            content = str(getattr(msg, "content", ""))
            is_error = not getattr(msg, "is_error", False)
            tail.append({
                "tool": tool_name,
                "preview": content[:max_chars // max_entries],
                "is_error": is_error,
            })
    
    return tail
```

---

### 3.7 P1 — 可观测事件推送

#### 设计

```python
# agents/events.py — 新增

from enum import Enum


class DelegationEvent(str, Enum):
    TASK_SPAWNED = "delegate.task_spawned"
    TASK_PROGRESS = "delegate.task_progress"
    TASK_COMPLETED = "delegate.task_completed"
    TASK_FAILED = "delegate.task_failed"
    TASK_THINKING = "delegate.task_thinking"
    TASK_TOOL_STARTED = "delegate.tool_started"
    TASK_TOOL_COMPLETED = "delegate.tool_completed"


class DelegationEventBus:
    """子 Agent 事件总线，对接现有的 HookManager"""
    
    def __init__(self, hook_manager=None):
        self._hook_manager = hook_manager
        self._listeners = {}
    
    def emit(self, event: DelegationEvent, data: dict):
        """发射事件"""
        if self._hook_manager:
            self._hook_manager.emit(event.value, data)
        for listener in self._listeners.get(event, []):
            listener(data)
    
    def on(self, event: DelegationEvent, callback):
        self._listeners.setdefault(event, []).append(callback)
```

---

### 3.8 P1/P2 — 凭证独立配置

#### 设计

```yaml
# config.yaml
delegation:
  # 子 Agent 使用独立的模型和凭证（可选）
  # 不配置时继承父 Agent
  provider: "deepseek"
  model: "deepseek-v4-pro"
  api_key_env: "DEEPSEEK_API_KEY"
  base_url: "https://api.deepseek.com/anthropic"
```

```python
# agents/delegation_config.py — 新增

def resolve_delegation_credentials(cfg, parent_llm):
    """解析委托凭证：优先使用独立配置，否则继承父 Agent"""
    provider_cfg = cfg.get("delegation", {})
    
    if "provider" not in provider_cfg:
        # 继承父 Agent
        return parent_llm
    
    # 使用独立凭证
    from core.llm_factory import create_llm
    return create_llm(
        provider=provider_cfg.get("provider"),
        model=provider_cfg.get("model"),
        api_key_env=provider_cfg.get("api_key_env"),
        base_url=provider_cfg.get("base_url"),
    )
```

---

## 四、文件变更清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `agents/subagent.py` | 重写 | 新增工具隔离、审批安全、深度限制、结果摘要 |
| `agents/batch_delegate.py` | 新增 | 批量并行委托 + 超时容错 + 结果汇总 |
| `agents/monitor.py` | 新增 | 心跳检测 + stale 判定 + 暂停/中断 |
| `agents/delegation_config.py` | 新增 | 凭证独立 + 超时 + 并发 + 审批模式配置 |
| `agents/events.py` | 新增 | DelegationEvent 枚举 + 事件总线 |
| `agents/orchestrator.py` | 增强 | Supervisor 注入深度限制 + 动态提示词 |
| `agents/agent_state.py` | 扩展 | 新增 delegate_depth / subagent_status 等字段 |
| `config.yaml.example` | 扩展 | 新增 delegation.* 配置节 |
| `settings.py` | 无变更 | 已有 settings.get() 支持 |
| `tools/base.py` | 无变更 | WeaveMindTool 基类无需修改 |

---

## 五、实现路线图

### 第一轮：P0 核心能力（7-10 天）

| 步骤 | 内容 | 预估工时 | 前置依赖 |
|------|------|---------|---------|
| 1.1 | `subagent.py` 重写：工具隔离黑名单 + 审批回调 + 深度限制 | 2 天 | 无 |
| 1.2 | `batch_delegate.py` 新增：ThreadPoolExecutor 并行 + 超时 | 2 天 | 1.1 |
| 1.3 | `monitor.py` 新增：心跳检测 + stale 判定 | 2 天 | 1.1 |
| 1.4 | `events.py` 新增：事件枚举 + 总线 | 1 天 | 无 |
| 1.5 | 集成测试 + config 配置 + 回归测试 | 1-2 天 | 1.1-1.4 |

### 第二轮：P1 能力增强（3-5 天）

| 步骤 | 内容 | 预估工时 | 前置依赖 |
|------|------|---------|---------|
| 2.1 | 结果尾部摘要 + orchestrator 动态提示词 | 1.5 天 | 1.1 |
| 2.2 | 凭证独立配置 + 多回退 | 1 天 | 1.1 |
| 2.3 | 可观测事件对接 HookManager + TUI 渲染 | 1.5 天 | 1.4 |

### 第三轮：P2 高级能力（2-3 天）

| 步骤 | 内容 | 预估工时 | 前置依赖 |
|------|------|---------|---------|
| 3.1 | 运行时暂停/中断 | 1.5 天 | 1.3 |
| 3.2 | 端到端打通：AgentLoop 调度 -> 子 Agent -> 结果汇总 | 1-2 天 | 全部 |

---

## 六、测试策略

### 6.1 单元测试

| 测试 | 文件 | 验证点 |
|------|------|--------|
| `test_filter_blocked_tools` | `tests/test_subagents.py` | 黑名单工具被过滤 |
| `test_auto_deny_prevents_deadlock` | `tests/test_subagents.py` | 审批回调不阻塞 |
| `test_max_spawn_depth` | `tests/test_subagents.py` | 超深度委托被拒 |
| `test_batch_parallel` | `tests/test_batch_delegate.py` | 并行执行时间≈最慢 |
| `test_batch_partial_failure` | `tests/test_batch_delegate.py` | 单任务失败不影响其他 |
| `test_monitor_stale_idle` | `tests/test_monitor.py` | 空闲超时标记 stale |
| `test_monitor_tool_not_stale` | `tests/test_monitor.py` | 长工具不被误判 |
| `test_monitor_interrupt` | `tests/test_monitor.py` | 中断精确到位 |
| `test_output_tail_size` | `tests/test_subagents.py` | 尾部摘要不超限 |
| `test_independent_credentials` | `tests/test_delegation_config.py` | 凭证独立配置生效 |

### 6.2 集成测试

| 测试 | 场景 |
|------|------|
| `test_team_with_subagent` | 主 MultiAgent 中调用 SubAgentTool |
| `test_batch_in_plan` | PlanExecutor 中调用 BatchDelegateTool |
| `test_subagent_auto_deny_logging` | 审计日志记录自动拒绝 |
| `test_subagent_chain` | 多层委托（受深度限制）|

### 6.3 压力测试

- 10 个子 Agent 同时执行，验证并发控制和内存
- 子 Agent 内调用长命令（30s+），验证不被误判 stale
- 子 Agent 死循环，验证心跳及时回收

---

## 七、秋招面试准备

### 7.1 项目亮点话术

#### 亮点 1：纵深防御的子 Agent 隔离体系

> "我设计了三层隔离的子 Agent 委托系统：第一层上下文隔离——每个子 Agent 获得独立的 AIAgent 实例，看不到父 Agent 和其他兄弟的历史；第二层工具隔离——通过 `SUBAGENT_BLOCKED_TOOLS` 硬编码黑名单禁止危险操作（递归委托、跨 Agent 通信、修改共享记忆）；第三层深度控制——通过 `max_spawn_depth` 参数限制嵌套层数，防止无限递归。三层组合形成了纵深防御体系，借鉴了 Hermes Agent 的设计思想。"

#### 亮点 2：安全优先的并行执行引擎

> "我实现了批量并行委托工具 `BatchDelegateTool`，基于 `ThreadPoolExecutor` 的故障隔离模式运行。关键设计包括：每个子 Agent 有独立的超时控制（600s），失败的任务被隔离不影响其他；非交互审批回调防止 TUI 线程死锁；输出尾部摘要保护父 Agent 上下文不被中间步骤撑爆（限制 8000 字符）。双场景心跳检测（450s 空闲 / 1200s 工具内执行）确保了既能及时发现跑飞任务，又不会误杀长工具执行。"

#### 亮点 3：成本优化的凭证池设计

> "子 Agent 默认继承父 Agent 的模型和凭证，但也可独立配置特定模型和 Provider。这就允许父 Agent 用 Claude Sonnet 做决策推理，子 Agent 用 DeepSeek 做批量执行——成本可降低 5-10 倍。凭证独立 + 负载均衡的能力让系统可以在"贵但聪明"和"便宜但够用"的模型之间灵活调配。"

### 7.2 面试可能追问

| 问题 | 回答思路 |
|------|---------|
| 为什么不用现成的 LangGraph Supervisor？ | 它只解决了"路由"问题，没解决隔离、超时、心跳、审批等运行时治理问题 |
| 为什么不让子 Agent 共享父 Agent 的模型缓存？ | 子 Agent 有独立的 AIAgent 实例，模型缓存可以复用但上下文缓存必须隔离 |
| 批量委托结果怎么保证顺序？ | 不保证顺序——可并行任务天然无序，父 Agent 根据 goal 字段匹配结果 |
| 心跳间隔为什么选 30 秒？ | 平衡检测延迟和开销：30s 内一个正常子 Agent 通常能完成一轮 think-act 循环 |

---

## 八、风险与规避

| 风险 | 影响 | 规避措施 |
|------|------|---------|
| ThreadPoolExecutor 线程泄漏 | 子 Agent 完成后线程未回收 | 使用 `with` 语句确保自动回收 |
| 审批回调死锁 | TUI 阻塞 | 默认 auto-deny + 审计日志 |
| 子 Agent 写共享文件冲突 | 多个子 Agent 同时写同一文件 | 黑名单过滤 + 建议使用临时目录 |
| 内存溢出（大量子 Agent） | 并行数过高 | `max_concurrent_children` 硬限制 + 配置可调 |
| 凭证泄露（日志中） | API Key 暴露 | 审计日志不记录完整 API Key |

---

## 九、相关链接

- [Hermes 子 Agent 委托深度分析](../hermes-subagent-delegation.md)（详细对比分析）
- [WeaveMindAgent 改进计划](../../weavemind-agent-improve.md)（总改进清单）
- WeaveMindAgent 代码：
  - `agents/subagent.py` — 当前 SubAgentTool 实现
  - `agents/orchestrator.py` — 当前 MultiAgentOrchestrator
  - `agents/worker.py` — 当前 Worker 节点
  - `agents/agent_state.py` — 当前共享状态定义
- [Hermes Agent 文档](https://hermes-agent.nousresearch.com/docs)
