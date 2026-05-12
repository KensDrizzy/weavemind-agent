# WeaveMindAgent MCP 集成分析与实现计划

> 本文档分析 PaiCLI 的 MCP 实现、主流 MCP 实现方式，并结合 WeaveMindAgent 现有架构，给出详细的 MCP 集成方案。
> 文档生成时间：2025-05-11

---

## 一、MCP 背景与核心概念

### 1.1 什么是 MCP（Model Context Protocol）

MCP 是 Anthropic 于 2024 年底推出的开放协议，旨在标准化 AI 模型与外部工具/数据源的连接方式。

**核心价值：**
- **标准化接口**：统一的工具发现、调用、权限管理机制
- **跨生态兼容**：一个 MCP Server 可被 Claude Desktop、Cursor、Windsurf 等多个客户端使用
- **本地优先**：支持本地进程（stdio）和远程 HTTP 两种传输方式
- **类型安全**：基于 JSON-RPC 2.0 的强类型协议

### 1.2 MCP 架构概览

```
┌─────────────────────────────────────────────────────────────┐
│                    MCP Client (如 WeaveMind)                  │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐       │
│  │  Session     │  │   Tools      │  │  Resources   │       │
│  │  Manager     │  │   Registry   │  │   Manager    │       │
│  └──────────────┘  └──────────────┘  └──────────────┘       │
└─────────────────────────────────────────────────────────────┘
                           │
         ┌─────────────────┼─────────────────┐
         │ stdio           │ HTTP           │
         │ (本地进程)       │ (远程服务)      │
         ▼                 ▼                 ▼
┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│  Filesystem  │    │   GitHub     │    │   Custom     │
│   Server     │    │    Server    │    │    Server    │
└──────────────┘    └──────────────┘    └──────────────┘
```

### 1.3 MCP 核心协议操作

| 操作 | 说明 |
|------|------|
| `initialize` | 客户端/服务端能力协商 |
| `tools/list` | 获取可用工具列表 |
| `tools/call` | 调用指定工具 |
| `resources/list` | 获取可用资源列表 |
| `resources/read` | 读取资源内容 |
| `prompts/list` | 获取可用提示词模板 |
| `prompts/get` | 获取提示词内容 |

---

## 二、PaiCLI 的 MCP 实现分析

### 2.1 PaiCLI 的 MCP 实现特点

从 `mcp.pdf` 文档分析，PaiCLI 是一个 **Java 实现的 MCP Client**，主要特点：

```java
// PaiCLI MCP 核心类设计（基于 PDF 提取的类结构）
public class MCPClient {
    private final String serverUrl;           // 服务端地址
    private final HttpClient httpClient;      // HTTP 客户端
    private final JsonRpcHandler rpcHandler;  // JSON-RPC 处理器
    
    // 核心方法
    public List<Tool> listTools();           // 获取工具列表
    public ToolResult callTool(String name, Map<String, Object> args);  // 调用工具
    public void connect();                   // 建立连接
    public void disconnect();                // 断开连接
}
```

**设计要点：**
1. **基于 HTTP 传输**：PaiCLI 主要使用 HTTP 方式连接 MCP Server
2. **工具动态发现**：运行时从 Server 获取工具列表，动态生成工具描述
3. **JSON-RPC 2.0**：完全遵循 MCP 协议规范
4. **资源管理**：支持 Resources 和 Prompts 的读取

### 2.2 PaiCLI 与原生工具的对比

| 特性 | PaiCLI MCP 集成 | 原生工具实现 |
|------|----------------|-------------|
| 扩展性 | 强（外部 Server 独立部署） | 弱（需修改代码） |
| 跨语言 | 支持（任意语言实现 Server） | 仅限 Java |
| 动态发现 | 支持 | 静态注册 |
| 配置复杂度 | 中等（需维护 Server 地址） | 低 |
| 安全性 | 可控（通过权限策略） | 完全控制 |

### 2.3 PaiCLI MCP 配置方式

```yaml
# PaiCLI 的 MCP 配置示例
mcp:
  servers:
    filesystem:
      type: stdio
      command: npx
      args: ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/files"]
    github:
      type: http
      url: https://api.githubcopilot.com/mcp
      headers:
        Authorization: Bearer ${GITHUB_TOKEN}
```

---

## 三、主流 MCP 实现方式对比

### 3.1 官方 Python SDK（modelcontextprotocol/python-sdk）

**GitHub**: https://github.com/modelcontextprotocol/python-sdk

**核心组件：**
```python
# MCP Python SDK 核心 API
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.sse import sse_client  # HTTP SSE 传输

# stdio 传输方式（本地进程）
async with stdio_client(server_params) as (read, write):
    async with ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        result = await session.call_tool("tool_name", {"arg": "value"})
```

**优点：**
- 官方维护，协议兼容性好
- 支持 stdio 和 HTTP(SSE) 两种传输
- 完整的类型定义和错误处理
- 异步 API 设计，性能优秀

**缺点：**
- 仅支持 Python 3.10+
- 相对较新，API 可能有变动
- 文档尚不完善

### 3.2 社区实现方式

| 实现 | 语言 | 传输方式 | 特点 |
|------|------|----------|------|
| mcp-sdk (官方) | Python | stdio/SSE | 官方标准实现 |
| FastMCP | Python | HTTP/WebSocket | 简化的 Server 框架 |
| mcp-go | Go | stdio/SSE | Go 语言实现 |
| mcp-node | Node.js | stdio/SSE | Node.js 实现 |

### 3.3 stdio vs HTTP 传输对比

| 特性 | stdio | HTTP/SSE |
|------|-------|----------|
| 适用场景 | 本地工具、命令行程序 | 远程服务、云端 API |
| 启动开销 | 低（共享进程） | 高（独立网络连接） |
| 并发能力 | 单进程内串行 | 支持大规模并发 |
| 安全性 | 高（无网络暴露） | 需额外认证加密 |
| 配置复杂度 | 低（仅需命令行） | 高（需 URL、Token） |

---

## 四、WeaveMindAgent 现有 MCP 实现分析

### 4.1 现有代码结构

```
/Users/lqf/projects/agentcode/WeaveMindAgent/mcp/
├── __init__.py      # 导出 MCPManager, MCPClient
├── client.py        # MCPClient 实现（基于官方 SDK）
└── manager.py       # MCPManager 实现（多 Server 管理）
```

### 4.2 现有实现代码

**client.py：**
```python
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from langchain_core.tools import StructuredTool
import asyncio


class MCPClient:
    def __init__(self, name: str, command: str, args: list[str]):
        self.name = name
        self.params = StdioServerParameters(command=command, args=args)
        self._tools: list = []

    async def connect(self):
        async with stdio_client(self.params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                self._tools = [
                    StructuredTool.from_function(
                        func=lambda **kwargs, t=t, s=session: asyncio.run(s.call_tool(t.name, kwargs)),
                        name=t.name,
                        description=t.description or "",
                    )
                    for t in tools.tools
                ]

    def get_tools(self) -> list:
        return self._tools
```

**manager.py：**
```python
from mcp.client import MCPClient
import settings


class MCPManager:
    def __init__(self, servers: dict = None):
        cfg = servers or settings.get("mcp.servers", {})
        self._clients = {
            name: MCPClient(name, s["command"], s.get("args", []))
            for name, s in cfg.items()
        }

    async def connect_all(self):
        for client in self._clients.values():
            await client.connect()

    def get_tools(self) -> list:
        tools = []
        for client in self._clients.values():
            tools.extend(client.get_tools())
        return tools
```

### 4.3 现有实现的问题

| 问题 | 说明 | 影响 |
|------|------|------|
| **Session 生命周期问题** | `connect()` 方法中 session 在 `async with` 块结束后即关闭 | 工具无法实际调用，session 已失效 |
| **工具调用逻辑错误** | `asyncio.run()` 在 lambda 中嵌套调用可能触发 "asyncio.run() cannot be called from a running event loop" | 调用工具时会抛出异常 |
| **缺乏错误处理** | 没有 try-catch 块处理连接失败、工具调用失败 | 单点故障导致整个系统崩溃 |
| **未集成到 ToolRegistry** | MCP 工具未注册到现有的 ToolRegistry 中 | CLI 无法使用 MCP 工具 |
| **缺少 HTTP 支持** | 仅实现了 stdio 传输 | 无法连接远程 MCP Server |
| **配置读取不完整** | 未读取环境变量、headers 等配置 | 认证类 Server 无法使用 |
| **缺少工具元数据** | 未处理 tool.inputSchema，无法正确生成参数结构 | LLM 无法正确调用工具 |

### 4.4 现有架构与 MCP 的关系

```
当前 WeaveMindAgent 架构：
┌──────────────────────────────────────────────┐
│  cli/app.py (WeaveMindCLI)                    │
│  ├── AgentLoop (core/agent_loop.py)          │
│  │     └── LangGraph StateMachine            │
│  ├── ToolRegistry (tools/registry.py)        │
│  │     ├── 内置工具 (Read/Write/Bash/...)   │
│  │     └── ❌ MCP 工具未接入                │
│  └── WeaveMindMemory (core/memory.py)        │
└──────────────────────────────────────────────┘
                        │
                        │  应接入
                        ▼
┌──────────────────────────────────────────────┐
│  mcp/manager.py (MCPManager)                 │
│  ├── MCPClient (stdio 传输)                  │
│  └── 尚未与 ToolRegistry 集成                │
└──────────────────────────────────────────────┘
```

---

## 五、WeaveMindAgent MCP 集成 Plan

### 5.1 设计目标

1. **无缝集成**：MCP 工具应像原生工具一样工作
2. **配置驱动**：通过 config.yaml 管理 MCP Server
3. **传输灵活**：同时支持 stdio 和 HTTP 传输
4. **错误隔离**：单个 MCP Server 故障不影响整体
5. **热加载**：支持配置变更后动态重新加载

### 5.2 技术方案选型

| 决策点 | 选择 | 理由 |
|--------|------|------|
| SDK | 官方 mcp-sdk | 协议兼容性最好 |
| 传输 | stdio + HTTP(SSE) | 覆盖所有场景 |
| 工具封装 | WeaveMindTool 子类 | 与现有架构统一 |
| 生命周期 | 应用级 Session 保持 | 避免 session 失效问题 |
| 配置方式 | config.yaml 扩展 | 与现有配置体系一致 |

### 5.3 实现步骤

#### Phase 1: 重构 MCP Client 层（1-2 天）

**5.3.1 修复 Session 生命周期问题**

```python
# mcp/client.py - 重构后
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.sse import sse_client
import asyncio
from typing import Optional, Any
from langchain_core.tools import StructuredTool
import logging

logger = logging.getLogger(__name__)


class MCPConnection:
    """保持长连接的 MCP 会话包装器"""
    
    def __init__(self, server_config: dict):
        self.name = server_config["name"]
        self.transport = server_config.get("transport", "stdio")
        self.config = server_config
        
        # Runtime
        self._session: Optional[ClientSession] = None
        self._exit_stack = None
        self._tools_info: list = []
        self._connected = False
    
    async def connect(self):
        """建立连接并保持 session"""
        from contextlib import AsyncExitStack
        
        self._exit_stack = AsyncExitStack()
        
        try:
            if self.transport == "stdio":
                params = StdioServerParameters(
                    command=self.config["command"],
                    args=self.config.get("args", []),
                    env=self.config.get("env")
                )
                read, write = await self._exit_stack.enter_async_context(
                    stdio_client(params)
                )
            else:  # http/sse
                url = self.config["url"]
                headers = self.config.get("headers", {})
                read, write = await self._exit_stack.enter_async_context(
                    sse_client(url, headers=headers)
                )
            
            self._session = await self._exit_stack.enter_async_context(
                ClientSession(read, write)
            )
            await self._session.initialize()
            
            # 缓存工具信息
            tools_response = await self._session.list_tools()
            self._tools_info = tools_response.tools
            self._connected = True
            
            logger.info(f"MCP Server '{self.name}' 连接成功，发现 {len(self._tools_info)} 个工具")
            
        except Exception as e:
            await self.disconnect()
            raise ConnectionError(f"无法连接 MCP Server '{self.name}': {e}")
    
    async def disconnect(self):
        """断开连接"""
        if self._exit_stack:
            await self._exit_stack.aclose()
        self._connected = False
        self._session = None
    
    async def call_tool(self, tool_name: str, arguments: dict) -> Any:
        """调用工具"""
        if not self._connected or not self._session:
            raise RuntimeError(f"MCP Server '{self.name}' 未连接")
        
        try:
            result = await self._session.call_tool(tool_name, arguments)
            return result
        except Exception as e:
            logger.error(f"调用工具 '{tool_name}' 失败: {e}")
            raise
    
    def get_tools_info(self) -> list:
        """获取工具元数据"""
        return self._tools_info
```

**5.3.2 创建 MCP Tool 包装类**

```python
# mcp/tools.py
from tools.base import WeaveMindTool
from pydantic import BaseModel, create_model
from typing import Type, Any
import json


def create_mcp_tool_class(tool_info, connection) -> Type[WeaveMindTool]:
    """根据 MCP 工具元数据动态创建 WeaveMindTool 子类"""
    
    # 从 inputSchema 生成 Pydantic 模型
    schema = tool_info.inputSchema
    if schema and schema.get("type") == "object":
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        
        fields = {}
        for name, prop in properties.items():
            field_type = _json_schema_to_python_type(prop)
            if name in required:
                fields[name] = (field_type, ...)
            else:
                default = prop.get("default")
                fields[name] = (field_type, default)
        
        args_schema = create_model(f"{tool_info.name}Args", **fields)
    else:
        args_schema = BaseModel
    
    # 动态创建工具类
    class MCPToolWrapper(WeaveMindTool):
        name: str = tool_info.name
        description: str = tool_info.description or f"MCP tool: {tool_info.name}"
        args_schema: Type[BaseModel] = args_schema
        
        def _run(self, **kwargs) -> str:
            """同步调用 wrapper - 实际由异步方法执行"""
            import asyncio
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # 在已有事件循环中（如 Jupyter）
                    import nest_asyncio
                    nest_asyncio.apply()
                    return loop.run_until_complete(self._arun(**kwargs))
                else:
                    return asyncio.run(self._arun(**kwargs))
            except RuntimeError:
                return asyncio.run(self._arun(**kwargs))
        
        async def _arun(self, **kwargs) -> str:
            """异步调用 MCP 工具"""
            result = await connection.call_tool(self.name, kwargs)
            
            # 解析工具返回结果
            if result.isError:
                return f"[MCP 错误] {result.content}"
            
            # 提取文本内容
            texts = []
            for content in result.content:
                if content.type == "text":
                    texts.append(content.text)
                elif content.type == "image":
                    texts.append(f"[图片: {content.mimeType}]")
            
            return "\n".join(texts) if texts else "(无返回内容)"
    
    return MCPToolWrapper


def _json_schema_to_python_type(prop: dict) -> type:
    """JSON Schema 类型映射到 Python 类型"""
    type_map = {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": bool,
        "array": list,
        "object": dict,
    }
    json_type = prop.get("type", "string")
    return type_map.get(json_type, str)
```

**5.3.3 重构 MCPManager**

```python
# mcp/manager.py - 重构后
import asyncio
from typing import Dict, List
import logging

from mcp.client import MCPConnection
from mcp.tools import create_mcp_tool_class
from tools.base import WeaveMindTool

logger = logging.getLogger(__name__)


class MCPManager:
    """MCP Server 管理器 - 替代原有实现"""
    
    def __init__(self, servers_config: dict = None):
        # 从 settings 读取配置
        import settings
        self._servers_config = servers_config or settings.get("mcp.servers", {})
        
        # 运行时状态
        self._connections: Dict[str, MCPConnection] = {}
        self._tools: List[WeaveMindTool] = []
        self._initialized = False
    
    async def initialize(self):
        """初始化所有 MCP Server 连接"""
        if self._initialized:
            return
        
        logger.info(f"开始初始化 {len(self._servers_config)} 个 MCP Server")
        
        for name, config in self._servers_config.items():
            config["name"] = name
            conn = MCPConnection(config)
            
            try:
                await conn.connect()
                self._connections[name] = conn
                
                # 注册工具
                for tool_info in conn.get_tools_info():
                    tool_class = create_mcp_tool_class(tool_info, conn)
                    tool_instance = tool_class()
                    self._tools.append(tool_instance)
                    
            except Exception as e:
                logger.error(f"初始化 MCP Server '{name}' 失败: {e}")
                # 继续初始化其他 Server，实现错误隔离
        
        self._initialized = True
        logger.info(f"MCP 初始化完成，共 {len(self._tools)} 个工具可用")
    
    async def shutdown(self):
        """关闭所有连接"""
        for name, conn in self._connections.items():
            try:
                await conn.disconnect()
                logger.info(f"已断开 MCP Server '{name}'")
            except Exception as e:
                logger.error(f"断开 '{name}' 时出错: {e}")
        
        self._connections.clear()
        self._tools.clear()
        self._initialized = False
    
    def get_tools(self) -> List[WeaveMindTool]:
        """获取所有 MCP 工具实例"""
        return self._tools.copy()
    
    def get_tools_info(self) -> dict:
        """获取工具信息摘要"""
        info = {}
        for conn in self._connections.values():
            info[conn.name] = [t.name for t in conn.get_tools_info()]
        return info
```

#### Phase 2: 集成到 ToolRegistry（0.5 天）

```python
# tools/registry.py - 修改
import logging

import settings
from tools.builtin.read import ReadTool
# ... 其他内置工具导入

from mcp.manager import MCPManager  # 新增

logger = logging.getLogger(__name__)


class ToolRegistry:
    def __init__(self, memory_manager=None, rag_pipeline=None, mcp_manager: MCPManager = None):
        self._tools: dict = {}
        self._memory_manager = memory_manager
        self._rag_pipeline = rag_pipeline
        self._mcp_manager = mcp_manager
        self._register_builtins()
        self._register_mcp_tools()  # 新增

    def _register_builtins(self):
        # 将读取/检索类工具放在前面，降低简单查询时对 Bash 的误用概率。
        for tool in [
            ReadTool(), GlobTool(), GrepTool(), WebFetchTool(), WebSearchTool(),
            AskUserTool(), EditTool(), WriteTool(), BashTool(),
            MemoryAddTool(memory_manager=self._memory_manager),
            MemorySearchTool(memory_manager=self._memory_manager),
            CoreMemoryEditTool(memory_manager=self._memory_manager),
        ]:
            self._tools[tool.name] = tool

        # RAG 工具（仅在 RAG 启用时注册）
        if settings.get("rag.enabled", False) and self._rag_pipeline:
            self._tools["SearchCode"] = SearchCodeTool(rag_pipeline=self._rag_pipeline)
            self._tools["IndexWorkspace"] = IndexWorkspaceTool(rag_pipeline=self._rag_pipeline)
            logger.info("RAG 工具已注册: SearchCode, IndexWorkspace")
    
    def _register_mcp_tools(self):
        """注册 MCP 工具"""
        if not self._mcp_manager:
            return
        
        mcp_tools = self._mcp_manager.get_tools()
        for tool in mcp_tools:
            # MCP 工具以 mcp_{server_name}_{tool_name} 格式注册
            # 或者保持原名称（需要检查冲突）
            if tool.name in self._tools:
                logger.warning(f"工具 '{tool.name}' 与内置工具重名，跳过")
                continue
            
            self._tools[tool.name] = tool
            logger.debug(f"注册 MCP 工具: {tool.name}")
        
        logger.info(f"已注册 {len(mcp_tools)} 个 MCP 工具")

    def register(self, tool):
        self._tools[tool.name] = tool

    def get(self, name: str):
        return self._tools.get(name)

    def get_all(self) -> list:
        return list(self._tools.values())

    def get_langchain_tools(self) -> list:
        """返回 LangChain 工具格式的列表（用于 LLM.bind_tools）。"""
        return self.get_all()
```

#### Phase 3: CLI 层集成（0.5 天）

```python
# cli/app.py - 修改初始化逻辑
import asyncio
from mcp.manager import MCPManager  # 新增

class WeaveMindCLI:
    def __init__(self):
        # ... 现有初始化代码
        
        # 初始化 MCP Manager
        self._mcp_manager = MCPManager()
        self._mcp_initialized = False
    
    async def _init_mcp(self):
        """异步初始化 MCP"""
        if not self._mcp_initialized:
            await self._mcp_manager.initialize()
            self._mcp_initialized = True
            
            # MCP 初始化后重建 ToolRegistry
            self._tool_registry = ToolRegistry(
                memory_manager=self._memory_manager,
                rag_pipeline=self._rag_pipeline,
                mcp_manager=self._mcp_manager
            )
            
            # 重新绑定工具到 AgentLoop
            tools = self._tool_registry.get_langchain_tools()
            self._agent_loop = AgentLoop(tools=tools)
    
    def run(self):
        """主循环 - 修改以支持异步初始化"""
        self._console.print(WELCOME_MESSAGE)
        
        # 异步初始化 MCP
        asyncio.run(self._init_mcp())
        
        # ... 后续现有的 REPL 循环
    
    # 添加关闭清理
    def _cleanup(self):
        """退出时清理资源"""
        if self._mcp_initialized:
            asyncio.run(self._mcp_manager.shutdown())
```

#### Phase 4: config.yaml 配置扩展（0.5 天）

```yaml
# config.yaml - 新增 MCP 配置

# ... 现有配置 ...

mcp:
  enabled: true  # 总开关
  
  servers:
    # stdio 传输示例：本地文件系统服务器
    filesystem:
      transport: stdio
      command: npx
      args: ["-y", "@modelcontextprotocol/server-filesystem", "/allowed/path"]
      env:
        NODE_ENV: production
    
    # HTTP 传输示例：GitHub Copilot MCP
    github:
      transport: http
      url: https://api.githubcopilot.com/mcp
      headers:
        Authorization: "Bearer ${GITHUB_TOKEN}"
        Accept: "application/json"
      timeout: 30
    
    # 另一个 stdio 示例：SQLite 数据库
    sqlite:
      transport: stdio
      command: uvx
      args: ["mcp-server-sqlite", "--db-path", "/path/to/db.sqlite"]

# 权限控制（可选扩展）
mcp_permissions:
  allowed_servers: ["filesystem", "sqlite"]  # 白名单
  disallowed_tools: ["filesystem_delete"]     # 禁用特定工具
```

#### Phase 5: 增强功能（可选，2-3 天）

**5.3.4 工具使用统计与监控**

```python
# mcp/metrics.py
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List


@dataclass
class ToolUsage:
    tool_name: str
    server_name: str
    call_count: int = 0
    total_duration: float = 0.0
    errors: int = 0
    last_called: datetime = None


class MCPMetricsCollector:
    """收集 MCP 工具使用指标"""
    
    def __init__(self):
        self._stats: Dict[str, ToolUsage] = {}
    
    def record_call(self, tool_name: str, server_name: str, duration: float, success: bool):
        key = f"{server_name}/{tool_name}"
        if key not in self._stats:
            self._stats[key] = ToolUsage(tool_name, server_name)
        
        stat = self._stats[key]
        stat.call_count += 1
        stat.total_duration += duration
        stat.last_called = datetime.now()
        if not success:
            stat.errors += 1
    
    def get_report(self) -> dict:
        return {
            "total_calls": sum(s.call_count for s in self._stats.values()),
            "tools": [
                {
                    "name": s.tool_name,
                    "server": s.server_name,
                    "calls": s.call_count,
                    "avg_duration": s.total_duration / s.call_count if s.call_count > 0 else 0,
                    "errors": s.errors
                }
                for s in self._stats.values()
            ]
        }
```

**5.3.5 健康检查与自动重连**

```python
# mcp/health.py
import asyncio
from typing import Callable


class MCPHealthMonitor:
    """MCP Server 健康监控"""
    
    def __init__(self, check_interval: int = 30):
        self._check_interval = check_interval
        self._handlers: List[Callable] = []
        self._running = False
    
    async def start(self, connections: Dict[str, MCPConnection]):
        """启动监控循环"""
        self._running = True
        while self._running:
            for name, conn in connections.items():
                healthy = await self._check(conn)
                if not healthy:
                    for handler in self._handlers:
                        await handler(name, conn)
            await asyncio.sleep(self._check_interval)
    
    async def _check(self, conn: MCPConnection) -> bool:
        """发送 ping 检查连通性"""
        try:
            # MCP 协议中的 ping 方法
            await conn._session.send_ping()
            return True
        except Exception:
            return False
```

---

## 六、迁移策略与测试计划

### 6.1 向后兼容性

- MCP 功能默认关闭（`mcp.enabled: false`）
- 未配置时不影响现有功能
- 工具名称冲突时优先使用内置工具

### 6.2 测试用例

```python
# tests/test_mcp.py
import pytest
from mcp.client import MCPConnection
from mcp.manager import MCPManager


@pytest.mark.asyncio
async def test_mcp_stdio_connection():
    """测试 stdio 传输连接"""
    config = {
        "name": "test",
        "transport": "stdio",
        "command": "echo",
        "args": ["test"]
    }
    conn = MCPConnection(config)
    # 注意：需要实际的 MCP Server 才能测试


@pytest.mark.asyncio
async def test_mcp_tool_registration():
    """测试工具注册"""
    # Mock 测试
    pass


def test_mcp_config_parsing():
    """测试配置解析"""
    from settings import get
    servers = get("mcp.servers", {})
    assert isinstance(servers, dict)
```

### 6.3 推荐 MCP Server 测试列表

| Server | 用途 | 传输 | 测试重点 |
|--------|------|------|----------|
| @modelcontextprotocol/server-filesystem | 文件操作 | stdio | 工具发现、文件读写 |
| @modelcontextprotocol/server-sqlite | 数据库 | stdio | 参数传递、SQL 执行 |
| @modelcontextprotocol/fetch | 网络请求 | stdio | HTTP 请求、错误处理 |

---

## 七、总结与展望

### 7.1 当前实现 vs 计划实现对比

| 维度 | 当前实现 | 计划实现 |
|------|----------|----------|
| Session 管理 | ❌ 短生命周期 | ✅ 长连接保持 |
| 工具封装 | ❌ 使用 LangChain StructuredTool | ✅ 继承 WeaveMindTool |
| HTTP 支持 | ❌ 未实现 | ✅ 支持 SSE 传输 |
| 错误处理 | ❌ 基本无 | ✅ 隔离+日志+重连 |
| 配置体系 | ⚠️ 简单读取 | ✅ 完整配置支持 |
| 集成度 | ❌ 未接入 ToolRegistry | ✅ 完全集成 |
| 监控 | ❌ 无 | ✅ 指标收集 |

### 7.2 预期收益

1. **工具生态扩展**：可接入官方和社区的 50+ MCP Server
2. **功能即插即用**：无需修改代码即可增加 capabilities
3. **标准化**：遵循业界标准，与 Claude Desktop 等工具兼容
4. **未来-proof**：Anthropic 主导，生态持续成长

### 7.3 风险提示

1. **协议变更风险**：MCP 仍在快速发展，需关注版本兼容性
2. **性能开销**：每个 MCP Server 是独立进程，资源占用需监控
3. **安全风险**：接入外部 Server 需谨慎审核权限

---

**下一步行动：**

1. 评审本 Plan 文档
2. 从 Phase 1 开始渐进式实现
3. 使用真实 MCP Server 进行端到端测试
4. 更新文档和用户指南

---

*文档编写完成，等待评审*
