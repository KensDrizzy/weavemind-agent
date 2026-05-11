# 人工审批（HITL）实现分析与 WeaveMind 升级方案

> 基于 PaiCLI 的 HITL 实现和主流 Agent 框架的分析

---

## 一、PaiCLI 的 HITL 实现分析

### 1.1 核心架构

PaiCLI 的 HITL 系统采用**拦截层设计**，核心类是 `HitlToolRegistry`，继承自 `ToolRegistry`，只覆写 `executeTool` 一个方法。

```
┌─────────────────────────────────────────────────────────────┐
│                    WeaveMind CLI                            │
│                         │                                   │
│                         ▼                                   │
│                   ToolRegistry                              │
│                         │                                   │
│            ┌────────────┴────────────┐                     │
│            │                         │                     │
│     HitlToolRegistry           普通工具                     │
│            │                         │                     │
│    ┌───────┴───────┐                │                     │
│    │               │                │                     │
│  HITL 启用?    HITL 未启用           │                     │
│    │               │                │                     │
│    ▼               ▼                ▼                     │
│  审批流程      直接执行          直接执行                    │
│    │                                                            │
│    ▼                                                            │
│  用户决策                                                        │
│    │                                                            │
│    ├─ APPROVED ──────────────────────► 执行工具                  │
│    ├─ APPROVED_ALL ─────────────────► 执行 + 记录放行            │
│    ├─ MODIFIED ─────────────────────► 修改参数后执行             │
│    ├─ REJECTED ─────────────────────► 返回拒绝原因给 Agent      │
│    └─ SKIPPED ──────────────────────► 跳过本步骤                │
└─────────────────────────────────────────────────────────────┘
```

### 1.2 关键设计决策

#### 决策 1：静态规则 vs 动态 LLM 判断

**PaiCLI 选择：静态规则**

```java
public class ApprovalPolicy {
    // 需要人工确认的工具集合
    private static final Set<String> DANGEROUS_TOOLS = Set.of(
        "write_file",
        "execute_command", 
        "create_project"
    );
    
    public static boolean requiresApproval(String toolName) {
        return DANGEROUS_TOOLS.contains(toolName);
    }
}
```

**理由**：
- 动态判断意味着每次调用工具前都要问 LLM："这个操作危险吗？"
- LLM 有随机性，用它来判断"是否要人工干预"不可靠
- 简单的 `Set.contains()` 比 LLM 调用更可信

**WeaveMind 现状**：已有类似实现（`permissions/modes.py`）

```python
EDIT_TOOLS = {"Write", "Edit"}
DANGEROUS_TOOLS = {"Bash"}
```

#### 决策 2：审批请求设计

PaiCLI 的 `ApprovalRequest` 包含：

| 字段 | 说明 | 示例 |
|------|------|------|
| `toolName` | 工具名称 | `write_file` |
| `dangerLevel` | 危险等级 | 🔴 高危 / 🟡 中危 |
| `riskDescription` | 风险描述 | "将写入或覆盖文件内容" |
| `arguments` | 工具参数 | `{"path": "...", "content": "..."}` |
| `suggestion` | 建议操作 | 可选 |
| `callerContext` | 调用上下文 | 可选 |

**显示效果**：
```
┌──────────────────────────────────────────────────────────┐
│  ⚠️  需要审批                                             │
├──────────────────────────────────────────────────────────┤
│  工具: write_file                                         │
│  等级: 🟡 中危                                            │
│  风险: 将写入或覆盖文件内容，原有内容将丢失                │
├──────────────────────────────────────────────────────────┤
│  参数:                                                    │
│    path: "/Users/itwanger/project/config.json"            │
│    content: "{"version": "2.0", ...}" (1240 字符)         │
└──────────────────────────────────────────────────────────┘
```

#### 决策 3：五种用户决策

| 决策 | 说明 | 使用场景 |
|------|------|----------|
| **APPROVED** | 批准（用原始参数） | 最普通的确认 |
| **APPROVED_ALL** | 本次会话全部放行同类操作 | 批量文件操作时不想每次确认 |
| **MODIFIED** | 修改参数后执行 | Agent 想写到错误路径时 |
| **REJECTED** | 拒绝（带原因） | 操作不正确，让 Agent 重新规划 |
| **SKIPPED** | 跳过本步骤 | 只是这一步不执行 |

**关键设计**：拒绝原因会回传给 Agent，让其重新规划

```python
# 拒绝时返回
"[HITL] 操作已被拒绝：路径有误，应该写到 ~/project 而不是 /tmp"

# Agent 看到这个结果后，会调整思路重新规划
```

#### 决策 4：默认关闭

**PaiCLI 选择：HITL 默认关闭，手动 `/hitl on` 开启**

**理由**：
- 开发调试阶段，频繁的审批确认会打断节奏
- 用户知道 Agent 要干什么时，每次按 y 会很烦
- HITL 适合"不确定 Agent 会做什么"或"操作结果不可逆"的场景
- 默认关闭让用户主动选择，而不是强迫所有人走审批流程

### 1.3 拦截层实现

```java
public class HitlToolRegistry extends ToolRegistry {
    private final HitlHandler hitlHandler;
    
    @Override
    public String executeTool(String name, String argumentsJson) {
        // HITL 未启用或该工具不需要审批，直接执行
        if (!hitlHandler.isEnabled() || !ApprovalPolicy.requiresApproval(name)) {
            return super.executeTool(name, argumentsJson);
        }
        
        // 构建审批请求并发起审批
        ApprovalRequest request = ApprovalRequest.of(name, argumentsJson, null);
        ApprovalResult result = hitlHandler.requestApproval(request);
        
        if (result.isRejected()) {
            String reason = result.reason() != null ? result.reason() : "用户拒绝了此操作";
            return "[HITL] 操作已被拒绝：" + reason;
        }
        
        if (result.isSkipped()) {
            return "[HITL] 操作已被跳过";
        }
        
        // 批准（含修改参数）
        String effectiveArgs = result.effectiveArguments(argumentsJson);
        return super.executeTool(name, effectiveArgs);
    }
}
```

**运行时开销**：一次 boolean 读取 + 一次 Set 查找，几乎可以忽略。

### 1.4 流式渲染器冲突处理

**问题**：Agent 流式输出时，渲染器有缓冲区。如果审批框紧贴着上游文字输出，视觉上很难分辨。

**解决方案**：在进入 tool-call 迭代前，先调用 `renderer.resetBetweenIterations()`，把缓冲区强制 flush 掉。

```java
// Agent.java：进入工具调用前 flush 渲染缓冲
renderer.resetBetweenIterations();

// 然后再执行工具
String toolResult = toolRegistry.executeTool(toolName, toolArgs);
```

### 1.5 APPROVED_ALL 的作用域

"全部放行"是**按工具名**作为 key 存储的，不是全局的。

- 放行了 `write_file` **不代表**放行 `execute_command`
- 用户可能愿意放行一批文件操作，但对执行命令还是想逐一审核
- `/clear` 命令会清除本次会话中积累的"全部放行"记录

---

## 二、主流 Agent 框架的做法

### 2.1 Claude Code（Anthropic）

**权限架构**：
```
┌─────────────────────────────────────────────────────────────┐
│                    Permission System                        │
├─────────────────────────────────────────────────────────────┤
│  1. 默认只读权限                                            │
│  2. 编辑文件、执行命令需要显式批准                           │
│  3. 用户控制：批准一次 or 自动允许                           │
└─────────────────────────────────────────────────────────────┘
```

**关键特性**：

| 特性 | 说明 |
|------|------|
| **沙箱 Bash 工具** | 文件系统和网络隔离，减少权限提示 |
| **写访问限制** | 只能写入启动目录及其子目录 |
| **Accept Edits 模式** | 自动批准文件编辑和固定集合的 Bash 命令 |
| **命令注入检测** | 可疑的 Bash 命令即使在白名单中也需要手动批准 |
| **失败关闭匹配** | 未匹配的命令默认需要手动批准 |
| **自然语言描述** | 复杂的 Bash 命令包含解释供用户理解 |

**权限模式**：
- `default`：每次危险操作都提示
- `acceptEdits`：自动批准文件编辑
- `bypassPermissions`：跳过所有检查（危险）
- `allowlist`：按用户/代码库/组织配置安全命令白名单

### 2.2 OpenAI Codex

**审批模式**（Approval Mode）：
- 类似 Claude Code 的权限系统
- 危险操作前需要用户确认
- 支持批量批准

### 2.3 LangGraph（LangChain）

**Human-in-the-Loop 实现**：

```python
from langgraph.graph import StateGraph
from langgraph.checkpoint.memory import MemorySaver

# 创建带检查点的图
graph = StateGraph(...)
checkpointer = MemorySaver()
app = graph.compile(
    checkpointer=checkpointer,
    interrupt_before=["action_node"]  # 在 action 节点前中断
)

# 运行到中断点
config = {"configurable": {"thread_id": "1"}}
result = app.invoke(input_data, config)

# 用户审核后继续
result = app.invoke(None, config)  # 继续执行
```

**关键特性**：
- `interrupt_before`：在指定节点前中断
- `interrupt_after`：在指定节点后中断
- 检查点机制：保存状态，用户审核后可继续
- 支持修改状态后继续执行

### 2.4 AutoGPT

**人工监督**：
- 人类代理（Human-in-the-loop）模式
- Agent 需要人类批准才能执行某些操作
- 支持设置批准阈值

---

## 三、WeaveMind 升级方案

### 3.1 现状分析

**WeaveMind 已有的**：

| 组件 | 状态 | 说明 |
|------|------|------|
| `PermissionPolicy` | ✅ 已实现 | 基础权限检查 |
| `PermissionMode` | ✅ 已实现 | DEFAULT/ACCEPT_EDITS/BYPASS/PERMIT |
| `DANGEROUS_TOOLS` | ✅ 已定义 | `{"Bash"}` |
| `EDIT_TOOLS` | ✅ 已定义 | `{"Write", "Edit"}` |
| Hook 系统 | ✅ 已实现 | PreToolUse/PostToolUse 事件 |

**缺少的**：

| 功能 | 说明 |
|------|------|
| **用户审批交互** | 没有终端交互式审批流程 |
| **审批请求格式化** | 没有美观的审批框显示 |
| **APPROVED_ALL** | 没有"本次会话全部放行"功能 |
| **MODIFIED 决策** | 没有"修改参数后执行"功能 |
| **拒绝原因回传** | 没有将拒绝原因返回给 Agent |
| **流式渲染冲突处理** | 没有在工具调用前 flush 渲染缓冲区 |

### 3.2 升级方案

#### 方案 A：最小改动（推荐）

在现有 `_act()` 方法中添加审批逻辑：

```python
# core/agent_loop.py

def _act(self, state: AgentState) -> dict:
    """执行工具调用。"""
    last_message = state["messages"][-1]
    if not isinstance(last_message, AIMessage):
        return {}

    tool_messages = []
    for tool_call in last_message.tool_calls:
        tool_name = tool_call["name"]
        tool_args = tool_call["args"]

        # ✅ 新增：HITL 审批检查
        if self.hitl_enabled and self._needs_approval(tool_name):
            approval_result = self._request_approval(tool_name, tool_args)
            
            if approval_result["decision"] == "rejected":
                reason = approval_result.get("reason", "用户拒绝了此操作")
                tool_messages.append(ToolMessage(
                    content=f"[HITL] 操作已被拒绝：{reason}",
                    tool_call_id=tool_call["id"],
                ))
                continue
            
            if approval_result["decision"] == "skipped":
                tool_messages.append(ToolMessage(
                    content="[HITL] 操作已被跳过",
                    tool_call_id=tool_call["id"],
                ))
                continue
            
            if approval_result["decision"] == "modified":
                tool_args = approval_result["modified_args"]
            
            # approved 或 approved_all 继续执行
        
        # 原有的工具执行逻辑
        tool = self.tool_registry.get(tool_name)
        if tool:
            result = tool.invoke(tool_args)
            # ...
```

#### 方案 B：拦截层设计（类似 PaiCLI）

创建 `HitlToolRegistry` 类，继承 `ToolRegistry`：

```python
# tools/hitl_registry.py

from tools.registry import ToolRegistry
from typing import Any

class HitlToolRegistry(ToolRegistry):
    """带 HITL 审批的工具注册表"""
    
    def __init__(self, hitl_handler=None, **kwargs):
        super().__init__(**kwargs)
        self.hitl_handler = hitl_handler
        self._approved_all_tools = set()  # 本次会话全部放行的工具
    
    def invoke_tool(self, tool_name: str, tool_args: dict) -> Any:
        """执行工具（带审批检查）"""
        
        # HITL 未启用或该工具不需要审批，直接执行
        if not self.hitl_handler or not self.hitl_handler.is_enabled():
            return super().invoke_tool(tool_name, tool_args)
        
        if not self._needs_approval(tool_name):
            return super().invoke_tool(tool_name, tool_args)
        
        # 检查是否已全部放行
        if tool_name in self._approved_all_tools:
            logger.info(f"[HITL] {tool_name} 已在本次会话中全部放行，自动通过")
            return super().invoke_tool(tool_name, tool_args)
        
        # 构建审批请求
        request = self._build_approval_request(tool_name, tool_args)
        result = self.hitl_handler.request_approval(request)
        
        if result["decision"] == "rejected":
            reason = result.get("reason", "用户拒绝了此操作")
            return f"[HITL] 操作已被拒绝：{reason}"
        
        if result["decision"] == "skipped":
            return "[HITL] 操作已被跳过"
        
        if result["decision"] == "approved_all":
            self._approved_all_tools.add(tool_name)
        
        # 批准（含修改参数）
        effective_args = result.get("modified_args", tool_args)
        return super().invoke_tool(tool_name, effective_args)
    
    def _needs_approval(self, tool_name: str) -> bool:
        """检查工具是否需要审批"""
        from permissions.modes import DANGEROUS_TOOLS, EDIT_TOOLS
        return tool_name in DANGEROUS_TOOLS or tool_name in EDIT_TOOLS
    
    def clear_approved_all(self):
        """清除本次会话中积累的全部放行记录"""
        self._approved_all_tools.clear()
```

### 3.3 审批请求设计

```python
# core/hitl_models.py

from dataclasses import dataclass
from typing import Optional

@dataclass
class ApprovalRequest:
    """审批请求"""
    tool_name: str
    arguments: dict
    danger_level: str  # "🔴 高危" / "🟡 中危" / "🟢 安全"
    risk_description: str
    suggestion: Optional[str] = None
    caller_context: Optional[str] = None
    
    def to_display_text(self) -> str:
        """格式化为美观的终端输出"""
        lines = [
            "┌──────────────────────────────────────────────────────────┐",
            "│  ⚠️  需要审批                                             │",
            "├──────────────────────────────────────────────────────────┤",
            f"│  工具: {self.tool_name:<52}│",
            f"│  等级: {self.danger_level:<52}│",
            f"│  风险: {self.risk_description:<52}│",
            "├──────────────────────────────────────────────────────────┤",
            "│  参数:                                                    │",
        ]
        
        # 格式化参数
        for key, value in self.arguments.items():
            value_str = str(value)
            if len(value_str) > 50:
                value_str = value_str[:50] + "..."
            lines.append(f"│    {key}: {value_str:<48}│")
        
        lines.append("└──────────────────────────────────────────────────────────┘")
        return "\n".join(lines)
```

### 3.5 用户交互设计

```python
# cli/hitl_handler.py

import sys
from typing import Optional

class TerminalHitlHandler:
    """终端 HITL 审批处理器"""
    
    def __init__(self):
        self.enabled = False
        self._approved_all_tools = set()
    
    def is_enabled(self) -> bool:
        return self.enabled
    
    def set_enabled(self, enabled: bool):
        self.enabled = enabled
    
    def request_approval(self, request: 'ApprovalRequest') -> dict:
        """请求用户审批"""
        
        # 检查是否已全部放行
        if request.tool_name in self._approved_all_tools:
            print(f"  [HITL] {request.tool_name} 已在本次会话中全部放行，自动通过")
            return {"decision": "approved_all"}
        
        print()
        print("────────── ⚠️  HITL 审批请求 ──────────")
        print(request.to_display_text())
        
        return self._prompt_until_decision(request)
    
    def _prompt_until_decision(self, request: 'ApprovalRequest') -> dict:
        """循环提示直到用户做出有效决策"""
        for attempt in range(5):
            print("请选择操作：[y/Enter] 批准  [a] 全部放行  [n] 拒绝  [s] 跳过  [m] 修改参数")
            print("> ", end="", flush=True)
            
            user_input = input().strip().lower()
            
            if user_input == "" or user_input == "y":
                return {"decision": "approved"}
            
            if user_input == "a":
                self._approved_all_tools.add(request.tool_name)
                return {"decision": "approved_all"}
            
            if user_input == "n":
                print("请输入拒绝原因（可选）：")
                reason = input().strip()
                return {"decision": "rejected", "reason": reason or "用户拒绝了此操作"}
            
            if user_input == "s":
                return {"decision": "skipped"}
            
            if user_input == "m":
                print("请输入修改后的参数（JSON 格式）：")
                modified_args = input().strip()
                return {"decision": "modified", "modified_args": modified_args}
            
            print("  ❓ 无法识别的选项，请输入 y/a/n/s/m 之一")
        
        # 连续多次无效，保守拒绝
        return {"decision": "rejected", "reason": "连续多次无效输入"}
    
    def clear_approved_all(self):
        """清除本次会话中积累的全部放行记录"""
        self._approved_all_tools.clear()
```

### 3.6 集成到 CLI

```python
# cli/app.py

class WeaveMindCLI:
    def __init__(self):
        # ... 现有代码 ...
        
        # ✅ 新增：HITL 处理器
        self.hitl_handler = TerminalHitlHandler()
        
        # 创建工具注册表（带 HITL）
        self.tool_registry = HitlToolRegistry(
            hitl_handler=self.hitl_handler,
            memory_manager=self.memory,
            rag_pipeline=self.rag_pipeline
        )
    
    def _handle_command(self, command: str):
        # ... 现有命令处理 ...
        
        # ✅ 新增：/hitl 命令
        if command == "/hitl":
            if args == "on":
                self.hitl_handler.set_enabled(True)
                print("✅ HITL 已启用，危险操作将在执行前请求确认")
            elif args == "off":
                self.hitl_handler.set_enabled(False)
                print("❌ HITL 已禁用")
            else:
                status = "启用" if self.hitl_handler.is_enabled() else "禁用"
                print(f"HITL 当前状态：{status}")
            return
        
        # ✅ 修改：/clear 命令
        if command == "/clear":
            self.conversation = []
            self.hitl_handler.clear_approved_all()  # 清除全部放行记录
            print("已清除对话历史和审批状态")
            return
```

---

## 四、设计权衡

### 4.1 默认关闭 vs 默认开启

| 选项 | 优点 | 缺点 |
|------|------|------|
| **默认关闭** | 开发调试流畅，不打断节奏 | 新用户可能不知道 HITL 存在 |
| **默认开启** | 安全性高，新手友好 | 频繁确认会打断节奏 |

**建议**：默认关闭，但在首次检测到危险操作时提示用户可以启用 HITL

```python
if not self.hitl_handler.is_enabled() and self._is_first_dangerous_op:
    print("💡 提示：检测到危险操作，你可以使用 /hitl on 启用人工审批")
```

### 4.2 拒绝原因回传

**PaiCLI 做法**：拒绝原因作为工具调用结果返回给 Agent

**优点**：
- Agent 知道为什么被拒绝，可以调整思路重新规划
- 比"默默不执行"更好

**缺点**：
- 用户可能不想解释原因
- 增加交互复杂度

**建议**：拒绝原因可选，用户可以直接按 Enter 跳过

### 4.3 APPROVED_ALL 作用域

**PaiCLI 做法**：按工具名作为 key，不是全局的

**优点**：
- 用户可以放行一批文件操作，但对执行命令逐一审核
- 粒度更细，安全性更高

**缺点**：
- 用户可能想要全局放行

**建议**：保持工具级别，但可以添加"全部放行所有"选项（需要二次确认）

### 4.4 流式渲染冲突处理

**问题**：Agent 流式输出时，渲染器有缓冲区。审批框紧贴着上游文字输出，视觉上很难分辨。

**解决方案**：在进入 tool-call 迭代前，先调用 `renderer.resetBetweenIterations()`

```python
# agent_loop.py

def _act(self, state: AgentState) -> dict:
    # ✅ 新增：flush 渲染缓冲区
    if self.hook_manager:
        self.hook_manager.emit("BeforeToolExecution", {})
    
    # 执行工具
    # ...
```

---

## 五、实施计划（详细版）

### Phase 1：基础模型与 UI（1-2 天）

#### 1.1 创建审批模型

**文件**：`core/hitl_models.py`

```python
from dataclasses import dataclass
from typing import Optional
from enum import Enum

class ApprovalDecision(str, Enum):
    APPROVED = "approved"           # 批准
    APPROVED_ALL = "approved_all"   # 本次会话全部放行
    MODIFIED = "modified"           # 修改参数后执行
    REJECTED = "rejected"           # 拒绝（带原因）
    SKIPPED = "skipped"             # 跳过本步骤

@dataclass
class ApprovalRequest:
    """审批请求"""
    tool_name: str
    arguments: dict
    danger_level: str       # "🔴 高危" / "🟡 中危" / "🟢 安全"
    risk_description: str   # 风险描述
    suggestion: Optional[str] = None
    caller_context: Optional[str] = None

@dataclass
class ApprovalResult:
    """审批结果"""
    decision: ApprovalDecision
    reason: Optional[str] = None
    modified_args: Optional[dict] = None
```

#### 1.2 危险操作策略

**文件**：`core/hitl_policy.py`

```python
from permissions.modes import DANGEROUS_TOOLS, EDIT_TOOLS

# 需要审批的工具集合
TOOLS_REQUIRING_APPROVAL = DANGEROUS_TOOLS | EDIT_TOOLS  # {"Bash", "Write", "Edit"}

# 危险等级定义
DANGER_LEVELS = {
    "Bash": ("🔴 高危", "将在系统上执行 Shell 命令，可能修改文件、安装软件或影响系统状态"),
    "Write": ("🟡 中危", "将写入或覆盖文件内容，原有内容将丢失"),
    "Edit": ("🟡 中危", "将编辑文件内容，可能修改关键代码"),
}

def requires_approval(tool_name: str) -> bool:
    """检查工具是否需要审批"""
    return tool_name in TOOLS_REQUIRING_APPROVAL

def get_danger_info(tool_name: str) -> tuple[str, str]:
    """获取危险等级和风险描述"""
    return DANGER_LEVELS.get(tool_name, ("🟢 安全", "安全的只读操作"))
```

#### 1.3 Rich UI 审批面板

**文件**：`cli/hitl_renderer.py`

```python
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console()

def print_approval_panel(request: 'ApprovalRequest') -> str:
    """显示美观的审批面板"""
    
    # 根据危险等级设置颜色
    danger_colors = {
        "🔴 高危": "red",
        "🟡 中危": "yellow",
        "🟢 安全": "green"
    }
    color = danger_colors.get(request.danger_level, "yellow")
    
    # 创建参数表格
    args_table = Table(show_header=False, box=None, padding=(0, 2))
    args_table.add_column("参数名", style="bold cyan")
    args_table.add_column("值")
    
    for key, value in request.arguments.items():
        value_str = str(value)
        if len(value_str) > 50:
            value_str = value_str[:50] + f"... ({len(str(value))} 字符)"
        args_table.add_row(key, value_str)
    
    # 创建面板内容
    panel_content = f"""[bold]工具:[/bold] {request.tool_name}
[bold]等级:[/bold] [{color}]{request.danger_level}[/{color}]
[bold]风险:[/bold] {request.risk_description}

[bold]参数:[/bold]
{args_table}"""
    
    # 显示面板
    panel = Panel(
        panel_content,
        title="⚠️ 需要审批",
        border_style=color,
        padding=(1, 2)
    )
    console.print(panel)
    
    # 显示操作提示
    console.print()
    console.print("[bold cyan]请选择：[/bold cyan]")
    console.print("  [green]y[/green]/[green]Enter[/green] 批准  [yellow]a[/yellow] 全部放行  [red]n[/red] 拒绝  [blue]s[/blue] 跳过  [magenta]m[/magenta] 修改参数")
    console.print()
    
    return console.input("[bold]> [/bold]").strip().lower()
```

**显示效果**：
```
╭──────────────────────────────────────────────────────────────╮
│                       ⚠️ 需要审批                            │
├──────────────────────────────────────────────────────────────┤
│  工具: Write                                                 │
│  等级: 🟡 中危                                              │
│  风险: 将写入或覆盖文件内容，原有内容将丢失                  │
│                                                              │
│  参数:                                                       │
│    path      /Users/xxx/project/config.json                  │
│    content   {"version": "2.0", ...} (1240 字符)             │
╰──────────────────────────────────────────────────────────────╯

请选择：
  y/Enter 批准  a 全部放行  n 拒绝  s 跳过  m 修改参数

> 
```

---

### Phase 2：处理器与拦截层（1 天）

#### 2.1 HITL 处理器

**文件**：`cli/hitl_handler.py`

```python
class TerminalHitlHandler:
    """终端 HITL 审批处理器"""
    
    def __init__(self):
        self.enabled = False
        self._approved_all_tools = set()
    
    def is_enabled(self) -> bool:
        return self.enabled
    
    def set_enabled(self, enabled: bool):
        self.enabled = enabled
    
    def request_approval(self, request: 'ApprovalRequest') -> dict:
        """请求用户审批"""
        
        # 检查是否已全部放行
        if request.tool_name in self._approved_all_tools:
            print(f"  [HITL] {request.tool_name} 已在本次会话中全部放行，自动通过")
            return {"decision": "approved_all"}
        
        # 显示审批面板
        user_input = print_approval_panel(request)
        
        # 处理用户输入
        return self._process_decision(user_input, request)
    
    def _process_decision(self, user_input: str, request: 'ApprovalRequest') -> dict:
        """处理用户决策"""
        
        if user_input == "" or user_input == "y":
            return {"decision": "approved"}
        
        if user_input == "a":
            self._approved_all_tools.add(request.tool_name)
            return {"decision": "approved_all"}
        
        if user_input == "n":
            console.print("[dim]请输入拒绝原因（可选，按 Enter 跳过）：[/dim]")
            reason = console.input("> ").strip()
            return {"decision": "rejected", "reason": reason or "用户拒绝了此操作"}
        
        if user_input == "s":
            return {"decision": "skipped"}
        
        if user_input == "m":
            return self._handle_modify_args(request)
        
        return {"decision": "rejected", "reason": "无效输入"}
    
    def _handle_modify_args(self, request: 'ApprovalRequest') -> dict:
        """处理参数修改"""
        console.print("[dim]请输入修改后的参数（JSON 格式）：[/dim]")
        console.print(f"[dim]当前参数：{request.arguments}[/dim]")
        modified_input = console.input("> ").strip()
        
        try:
            import json
            modified_args = json.loads(modified_input)
            return {"decision": "modified", "modified_args": modified_args}
        except json.JSONDecodeError:
            console.print("[red]❌ 无效的 JSON 格式，使用原始参数[/red]")
            return {"decision": "approved"}
    
    def clear_approved_all(self):
        """清除本次会话中积累的全部放行记录"""
        self._approved_all_tools.clear()
```

#### 2.2 拦截层设计

**文件**：`tools/hitl_registry.py`

```python
from tools.registry import ToolRegistry
from typing import Any

class HitlToolRegistry(ToolRegistry):
    """带 HITL 审批的工具注册表"""
    
    def __init__(self, hitl_handler=None, **kwargs):
        super().__init__(**kwargs)
        self.hitl_handler = hitl_handler
    
    def invoke_tool(self, tool_name: str, tool_args: dict) -> Any:
        """执行工具（带审批检查）"""
        
        # HITL 未启用或该工具不需要审批，直接执行
        if not self.hitl_handler or not self.hitl_handler.is_enabled():
            return super().invoke_tool(tool_name, tool_args)
        
        from core.hitl_policy import requires_approval
        if not requires_approval(tool_name):
            return super().invoke_tool(tool_name, tool_args)
        
        # 构建审批请求
        from core.hitl_models import ApprovalRequest
        from core.hitl_policy import get_danger_info
        
        danger_level, risk_description = get_danger_info(tool_name)
        request = ApprovalRequest(
            tool_name=tool_name,
            arguments=tool_args,
            danger_level=danger_level,
            risk_description=risk_description
        )
        
        # 请求审批
        result = self.hitl_handler.request_approval(request)
        
        # 处理审批结果
        if result["decision"] == "rejected":
            reason = result.get("reason", "用户拒绝了此操作")
            return f"[HITL] 操作已被拒绝：{reason}"
        
        if result["decision"] == "skipped":
            return "[HITL] 操作已被跳过"
        
        # 批准（含修改参数）
        effective_args = result.get("modified_args", tool_args)
        return super().invoke_tool(tool_name, effective_args)
```

---

### Phase 3：多种启用方式（0.5 天）

#### 3.1 命令行参数

**文件**：`main.py`

```python
import argparse

def main():
    parser = argparse.ArgumentParser(description="WeaveMind Agent")
    parser.add_argument("--hitl", action="store_true", help="启用人工审批模式")
    parser.add_argument("--hitl-config", type=str, help="HITL 配置文件路径")
    args = parser.parse_args()
    
    cli = WeaveMindCLI(hitl_enabled=args.hitl)
    cli.run()
```

**使用方式**：
```bash
# 启动时启用 HITL
python main.py --hitl

# 或使用配置文件
python main.py --hitl-config .weavemind/hitl.json
```

#### 3.2 配置文件

**文件**：`config.yaml`

```yaml
hitl:
  enabled: false  # 默认关闭
  auto_enable_for_high_risk: true  # 高危操作自动启用
  tools_requiring_approval:
    - Bash
    - Write
    - Edit
  danger_levels:
    Bash:
      level: "🔴 高危"
      description: "将在系统上执行 Shell 命令"
    Write:
      level: "🟡 中危"
      description: "将写入或覆盖文件内容"
    Edit:
      level: "🟡 中危"
      description: "将编辑文件内容"
```

#### 3.3 环境变量

```bash
# 临时启用
HITL_ENABLED=true python main.py

# 持久化（添加到 .bashrc 或 .zshrc）
export HITL_ENABLED=true
```

#### 3.4 首次危险操作自动提示

```python
# cli/app.py

class WeaveMindCLI:
    def __init__(self, hitl_enabled=False):
        # ... 现有代码 ...
        
        # HITL 初始化
        self.hitl_handler = TerminalHitlHandler()
        self._has_shown_hitl_hint = False
        
        # 根据参数启用
        if hitl_enabled:
            self.hitl_handler.set_enabled(True)
        
        # 根据配置文件启用
        if settings.get("hitl.enabled", False):
            self.hitl_handler.set_enabled(True)
        
        # 根据环境变量启用
        if os.environ.get("HITL_ENABLED", "").lower() == "true":
            self.hitl_handler.set_enabled(True)
    
    def _check_and_prompt_hitl(self, tool_name: str):
        """首次危险操作时提示用户启用 HITL"""
        if self.hitl_handler.is_enabled():
            return
        
        if self._has_shown_hitl_hint:
            return
        
        from core.hitl_policy import requires_approval
        if not requires_approval(tool_name):
            return
        
        console.print()
        console.print("💡 [yellow]提示：检测到高危操作，你可以：[/yellow]")
        console.print("  - 输入 [bold]/hitl on[/bold] 启用人工审批")
        console.print("  - 输入 [green]y[/green] 继续执行（仅本次）")
        console.print("  - 输入 [red]n[/red] 取消执行")
        console.print()
        
        self._has_shown_hitl_hint = True
```

---

### Phase 4：集成到 CLI（0.5 天）

#### 4.1 添加 /hitl 命令

**文件**：`cli/commands.py`

```python
def handle_command(command: str, cli: 'WeaveMindCLI'):
    # ... 现有命令 ...
    
    if command.startswith("/hitl"):
        args = command.split()[1] if len(command.split()) > 1 else ""
        
        if args == "on":
            cli.hitl_handler.set_enabled(True)
            console.print("[green]✅ HITL 已启用，危险操作将在执行前请求确认[/green]")
        elif args == "off":
            cli.hitl_handler.set_enabled(False)
            console.print("[red]❌ HITL 已禁用[/red]")
        elif args == "status":
            status = "启用" if cli.hitl_handler.is_enabled() else "禁用"
            approved_count = len(cli.hitl_handler._approved_all_tools)
            console.print(f"[cyan]HITL 当前状态：{status}[/cyan]")
            console.print(f"[dim]已全部放行的工具：{approved_count} 个[/dim]")
        else:
            console.print("[yellow]用法：/hitl [on|off|status][/yellow]")
        return
```

#### 4.2 修改 /clear 命令

```python
if command == "/clear":
    cli.conversation = []
    cli.hitl_handler.clear_approved_all()  # 清除全部放行记录
    console.print("[green]已清除对话历史和审批状态[/green]")
    return
```

#### 4.3 修改 _create_agent_loop

```python
def _create_agent_loop(self, force_plan: bool = False):
    """重建 ToolRegistry 和 AgentLoop，同时重建意图直达处理器。"""
    
    # ✅ 使用带 HITL 的工具注册表
    self.tool_registry = HitlToolRegistry(
        hitl_handler=self.hitl_handler,
        memory_manager=self.memory,
        rag_pipeline=self.rag_pipeline
    )
    
    self._direct_intent = DirectIntentHandler(self.tool_registry)
    self.agent_loop = AgentLoop(
        tool_registry=self.tool_registry,
        permission_policy=self.permission_policy,
        hook_manager=self.hook_manager,
        memory=self.memory,
        force_plan_mode=force_plan,
    )
```

---

### Phase 5：流式渲染冲突处理（0.5 天）

#### 5.1 问题描述

Agent 流式输出时，渲染器有缓冲区。审批框紧贴着上游文字输出，视觉上很难分辨。

#### 5.2 解决方案

在进入 tool-call 迭代前，先调用 `renderer.resetBetweenIterations()`

**文件**：`cli/renderer.py`

```python
class InteractionStreamRenderer:
    def __init__(self):
        self._buffer = []
    
    def reset_between_iterations(self):
        """重置缓冲区，在工具调用前调用"""
        self._flush_buffer()
        console.print()  # 添加空行分隔
```

**文件**：`core/agent_loop.py`

```python
def _act(self, state: AgentState) -> dict:
    # ✅ 新增：flush 渲染缓冲区
    if self.hook_manager:
        self.hook_manager.emit("BeforeToolExecution", {})
    
    # 执行工具
    # ...
```

---

### Phase 6：测试（1 天）

#### 6.1 单元测试

**文件**：`tests/test_hitl.py`

```python
import pytest
from unittest.mock import MagicMock, patch
from core.hitl_models import ApprovalRequest, ApprovalResult, ApprovalDecision
from core.hitl_policy import requires_approval, get_danger_info
from tools.hitl_registry import HitlToolRegistry

class TestApprovalPolicy:
    """测试危险操作策略"""
    
    def test_requires_approval_for_dangerous_tools(self):
        """测试危险工具需要审批"""
        assert requires_approval("Bash") == True
        assert requires_approval("Write") == True
        assert requires_approval("Edit") == True
    
    def test_no_approval_for_safe_tools(self):
        """测试安全工具不需要审批"""
        assert requires_approval("Read") == False
        assert requires_approval("Glob") == False
        assert requires_approval("Grep") == False
    
    def test_get_danger_info(self):
        """测试获取危险信息"""
        level, desc = get_danger_info("Bash")
        assert "高危" in level
        assert "Shell" in desc

class TestHitlToolRegistry:
    """测试 HITL 工具注册表"""
    
    def test_safe_tools_bypass_hitl(self):
        """测试安全工具绕过 HITL"""
        mock_handler = MagicMock()
        mock_handler.is_enabled.return_value = True
        
        registry = HitlToolRegistry(hitl_handler=mock_handler)
        
        # 调用安全工具
        registry.invoke_tool("Read", {"path": "test.txt"})
        
        # 验证审批未被调用
        mock_handler.request_approval.assert_not_called()
    
    def test_dangerous_tools_trigger_approval(self):
        """测试危险工具触发审批"""
        mock_handler = MagicMock()
        mock_handler.is_enabled.return_value = True
        mock_handler.request_approval.return_value = {"decision": "approved"}
        
        registry = HitlToolRegistry(hitl_handler=mock_handler)
        
        # 调用危险工具
        registry.invoke_tool("Write", {"path": "test.txt", "content": "hello"})
        
        # 验证审批被调用
        mock_handler.request_approval.assert_called_once()
    
    def test_rejected_tool_returns_error(self):
        """测试拒绝的工具返回错误"""
        mock_handler = MagicMock()
        mock_handler.is_enabled.return_value = True
        mock_handler.request_approval.return_value = {
            "decision": "rejected",
            "reason": "用户拒绝了此操作"
        }
        
        registry = HitlToolRegistry(hitl_handler=mock_handler)
        
        result = registry.invoke_tool("Write", {"path": "test.txt"})
        assert "已被拒绝" in result
```

#### 6.2 集成测试

```python
class TestHitlIntegration:
    """测试 HITL 集成"""
    
    def test_hitl_workflow(self):
        """测试完整 HITL 工作流"""
        from cli.hitl_handler import TerminalHitlHandler
        from core.hitl_models import ApprovalRequest
        
        handler = TerminalHitlHandler()
        handler.set_enabled(True)
        
        request = ApprovalRequest(
            tool_name="Write",
            arguments={"path": "test.txt", "content": "hello"},
            danger_level="🟡 中危",
            risk_description="将写入或覆盖文件内容"
        )
        
        # 模拟用户输入
        with patch('builtins.input', return_value='y'):
            result = handler.request_approval(request)
            assert result["decision"] == "approved"
```

---

### Phase 7：文档与示例（0.5 天）

#### 7.1 更新 README

```markdown
## HITL 人工审批

WeaveMind 支持 Human-in-the-Loop (HITL) 人工审批模式，在执行危险操作前请求用户确认。

### 启用方式

1. **命令行参数**：
   ```bash
   python main.py --hitl
   ```

2. **配置文件**（`config.yaml`）：
   ```yaml
   hitl:
     enabled: true
   ```

3. **环境变量**：
   ```bash
   HITL_ENABLED=true python main.py
   ```

4. **运行时命令**：
   ```
   /hitl on   # 启用
   /hitl off  # 禁用
   ```

### 用户决策

- **y/Enter**：批准本次操作
- **a**：全部放行同类操作（本次会话）
- **n**：拒绝（可选原因）
- **s**：跳过本步骤
- **m**：修改参数后执行
```

#### 7.2 示例演示

```bash
# 启动带 HITL 的 Agent
python main.py --hitl

# 测试危险操作
> 写一个文件 test.txt，内容是 "hello world"

# 会看到审批面板
╭──────────────────────────────────────────────────────────────╮
│                       ⚠️ 需要审批                            │
├──────────────────────────────────────────────────────────────┤
│  工具: Write                                                 │
│  等级: 🟡 中危                                              │
│  风险: 将写入或覆盖文件内容，原有内容将丢失                  │
│                                                              │
│  参数:                                                       │
│    path      test.txt                                        │
│    content   hello world                                     │
╰──────────────────────────────────────────────────────────────╯

请选择：
  y/Enter 批准  a 全部放行  n 拒绝  s 跳过  m 修改参数

> y

✅ 操作已批准
```

---

## 六、总结

### PaiCLI 的 HITL 设计精髓

1. **静态规则判断**：`Set.contains()` 比 LLM 调用更可靠
2. **拦截层设计**：继承 `ToolRegistry`，只覆写一个方法
3. **五种用户决策**：覆盖大多数场景
4. **拒绝原因回传**：让 Agent 知道为什么被拒绝
5. **默认关闭**：不打断开发调试节奏

### 主流 Agent 的共识

1. **权限系统是必需的**：Claude Code、Codex、LangGraph 都有
2. **危险操作需要确认**：这是 Agent 安全的基础
3. **用户控制粒度**：支持一次批准 or 自动允许
4. **透明性**：让用户知道 Agent 在做什么

### WeaveMind 的升级路径

WeaveMind 已经有基础的权限系统，只需要：

1. 添加用户交互层（TerminalHitlHandler）
2. 在 `_act()` 中集成审批逻辑
3. 添加 `/hitl` 命令控制开关
4. 处理流式渲染冲突

**核心原则**：简单的东西往往比聪明的东西更可靠。一行 `Set.contains()` 比一次 LLM 调用更可信。

---

## 六、总结

### PaiCLI 的 HITL 设计精髓

1. **静态规则判断**：`Set.contains()` 比 LLM 调用更可靠
2. **拦截层设计**：继承 `ToolRegistry`，只覆写一个方法
3. **五种用户决策**：覆盖大多数场景
4. **拒绝原因回传**：让 Agent 知道为什么被拒绝
5. **默认关闭**：不打断开发调试节奏

### 主流 Agent 的共识

1. **权限系统是必需的**：Claude Code、Codex、LangGraph 都有
2. **危险操作需要确认**：这是 Agent 安全的基础
3. **用户控制粒度**：支持一次批准 or 自动允许
4. **透明性**：让用户知道 Agent 在做什么

### WeaveMind 的升级路径

WeaveMind 已经有基础的权限系统，只需要：

1. 添加用户交互层（TerminalHitlHandler）
2. 在 `_act()` 中集成审批逻辑
3. 添加 `/hitl` 命令控制开关
4. 处理流式渲染冲突

**核心原则**：简单的东西往往比聪明的东西更可靠。一行 `Set.contains()` 比一次 LLM 调用更可信。
