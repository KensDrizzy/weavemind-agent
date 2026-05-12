# MCP 实现代码示例

本文档提供 WeaveMindAgent MCP 集成的关键代码实现示例，供开发参考。

---

## 1. 完整版 mcp/client.py

```python
"""MCP Client - 支持 stdio 和 HTTP(SSE) 传输的长连接客户端。"""

import asyncio
import logging
from contextlib import AsyncExitStack
from typing import Optional, Any, List

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import Tool as MCPToolInfo

logger = logging.getLogger(__name__)


class MCPConnection:
    """
    MCP Server 连接管理器。
    
    保持长连接，支持工具调用和资源读取。
    """
    
    def __init__(self, server_config: dict):
        """
        Args:
            server_config: 服务器配置字典
                - name: 服务器名称
                - transport: "stdio" 或 "http"
                - command/args/env: stdio 参数
                - url/headers: http 参数
        """
        self.name = server_config["name"]
        self.transport = server_config.get("transport", "stdio")
        self.config = server_config
        
        # Runtime state
        self._session: Optional[ClientSession] = None
        self._exit_stack: Optional[AsyncExitStack] = None
        self._tools_info: List[MCPToolInfo] = []
        self._connected = False
    
    async def connect(self) -> bool:
        """
        建立与 MCP Server 的连接。
        
        Returns:
            bool: 连接是否成功
        """
        self._exit_stack = AsyncExitStack()
        
        try:
            if self.transport == "stdio":
                success = await self._connect_stdio()
            elif self.transport in ("http", "sse"):
                success = await self._connect_http()
            else:
                raise ValueError(f"不支持的传输方式: {self.transport}")
            
            if success:
                # 获取工具列表
                tools_response = await self._session.list_tools()
                self._tools_info = tools_response.tools
                self._connected = True
                
                logger.info(
                    f"MCP Server '{self.name}' 连接成功，"
                    f"发现 {len(self._tools_info)} 个工具: "
                    f"{[t.name for t in self._tools_info]}"
                )
                return True
            
        except Exception as e:
            logger.error(f"连接 MCP Server '{self.name}' 失败: {e}")
            await self.disconnect()
            return False
    
    async def _connect_stdio(self) -> bool:
        """建立 stdio 传输连接。"""
        params = StdioServerParameters(
            command=self.config["command"],
            args=self.config.get("args", []),
            env=self._merge_env()
        )
        
        read, write = await self._exit_stack.enter_async_context(
            stdio_client(params)
        )
        self._session = await self._exit_stack.enter_async_context(
            ClientSession(read, write)
        )
        await self._session.initialize()
        return True
    
    async def _connect_http(self) -> bool:
        """建立 HTTP/SSE 传输连接。"""
        from mcp.client.sse import sse_client
        
        url = self.config["url"]
        headers = self.config.get("headers", {})
        timeout = self.config.get("timeout", 30)
        
        read, write = await self._exit_stack.enter_async_context(
            sse_client(url, headers=headers, timeout=timeout)
        )
        self._session = await self._exit_stack.enter_async_context(
            ClientSession(read, write)
        )
        await self._session.initialize()
        return True
    
    def _merge_env(self) -> Optional[dict]:
        """合并环境变量。"""
        import os
        env = self.config.get("env", {})
        if env:
            merged = os.environ.copy()
            merged.update(env)
            return merged
        return None
    
    async def disconnect(self):
        """断开连接并清理资源。"""
        if self._exit_stack:
            try:
                await self._exit_stack.aclose()
            except Exception as e:
                logger.warning(f"断开连接时出错: {e}")
        
        self._session = None
        self._exit_stack = None
        self._connected = False
        self._tools_info = []
    
    async def call_tool(self, tool_name: str, arguments: dict) -> Any:
        """
        调用 MCP 工具。
        
        Args:
            tool_name: 工具名称
            arguments: 工具参数
        
        Returns:
            Tool 调用结果
        
        Raises:
            RuntimeError: 未连接时抛出
            Exception: 工具调用失败时抛出
        """
        if not self._connected or not self._session:
            raise RuntimeError(f"MCP Server '{self.name}' 未连接")
        
        logger.debug(f"调用工具 '{tool_name}' 参数: {arguments}")
        
        try:
            result = await self._session.call_tool(tool_name, arguments)
            return result
        except Exception as e:
            logger.error(f"调用工具 '{tool_name}' 失败: {e}")
            raise
    
    async def list_resources(self) -> List[dict]:
        """列出可用资源（如果 Server 支持）。"""
        if not self._connected or not self._session:
            return []
        
        try:
            resources = await self._session.list_resources()
            return resources
        except Exception as e:
            logger.debug(f"Server '{self.name}' 不支持资源列表: {e}")
            return []
    
    def get_tools_info(self) -> List[MCPToolInfo]:
        """获取工具元数据列表。"""
        return self._tools_info.copy()
    
    def is_connected(self) -> bool:
        """检查连接状态。"""
        return self._connected
```

---

## 2. 完整版 mcp/tools.py

```python
"""MCP 工具包装器 - 将 MCP 工具转换为 WeaveMindTool。"""

import logging
from typing import Type, Any, Optional
from pydantic import BaseModel, Field, create_model

from mcp.types import Tool as MCPToolInfo
from tools.base import WeaveMindTool

logger = logging.getLogger(__name__)


def create_mcp_tool_class(
    tool_info: MCPToolInfo, 
    connection
) -> Type[WeaveMindTool]:
    """
    根据 MCP 工具元数据动态创建 WeaveMindTool 子类。
    
    Args:
        tool_info: MCP 工具元数据
        connection: MCPConnection 实例
    
    Returns:
        WeaveMindTool 子类
    """
    
    # 从 inputSchema 生成 Pydantic Args Schema
    args_schema = _create_args_schema(tool_info)
    
    # 工具类定义
    class MCPToolWrapper(WeaveMindTool):
        """MCP 工具包装器"""
        
        name: str = tool_info.name
        description: str = _format_description(tool_info)
        args_schema: Type[BaseModel] = args_schema
        
        # 实例属性
        _connection = connection
        _tool_info = tool_info
        
        def _run(self, **kwargs) -> str:
            """同步执行入口。"""
            return asyncio.run(self._arun(**kwargs))
        
        async def _arun(self, **kwargs) -> str:
            """异步执行 MCP 工具调用。"""
            result = await self._connection.call_tool(self.name, kwargs)
            return _format_result(result)
    
    # 重命名类名以便调试
    MCPToolWrapper.__name__ = f"MCP_{tool_info.name}"
    MCPToolWrapper.__qualname__ = f"MCP_{tool_info.name}"
    
    return MCPToolWrapper


def _create_args_schema(tool_info: MCPToolInfo) -> Type[BaseModel]:
    """根据 JSON Schema 创建 Pydantic 模型。"""
    
    schema = tool_info.inputSchema
    
    if not schema or schema.get("type") != "object":
        # 无参数或不符合对象格式，返回空模型
        class EmptyArgs(BaseModel):
            pass
        return EmptyArgs
    
    properties = schema.get("properties", {})
    required = set(schema.get("required", []))
    
    fields = {}
    for name, prop in properties.items():
        field_type = _json_schema_to_python_type(prop)
        description = prop.get("description", "")
        default = prop.get("default")
        
        if name in required:
            # 必需字段
            fields[name] = (field_type, Field(..., description=description))
        elif default is not None:
            # 有默认值
            fields[name] = (field_type, Field(default=default, description=description))
        else:
            # 可选字段，无默认值
            fields[name] = (Optional[field_type], Field(None, description=description))
    
    if not fields:
        class EmptyArgs(BaseModel):
            pass
        return EmptyArgs
    
    # 动态创建模型
    return create_model(
        f"{tool_info.name}Args",
        __doc__=f"Arguments for {tool_info.name}",
        **fields
    )


def _json_schema_to_python_type(prop: dict) -> type:
    """将 JSON Schema 类型转换为 Python 类型。"""
    
    json_type = prop.get("type", "string")
    
    type_map = {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": bool,
    }
    
    if json_type == "array":
        items = prop.get("items", {})
        item_type = _json_schema_to_python_type(items)
        return List[item_type]
    
    if json_type == "object":
        return dict
    
    return type_map.get(json_type, str)


def _format_description(tool_info: MCPToolInfo) -> str:
    """格式化工具描述。"""
    desc = tool_info.description or f"MCP tool: {tool_info.name}"
    
    # 添加参数信息
    schema = tool_info.inputSchema
    if schema and schema.get("type") == "object":
        properties = schema.get("properties", {})
        if properties:
            desc += "\n\n参数:"
            for name, prop in properties.items():
                prop_desc = prop.get("description", "无描述")
                prop_type = prop.get("type", "any")
                desc += f"\n- {name} ({prop_type}): {prop_desc}"
    
    return desc


def _format_result(result) -> str:
    """格式化工具返回结果。"""
    
    if result.isError:
        # 提取错误信息
        error_texts = []
        for content in result.content:
            if content.type == "text":
                error_texts.append(content.text)
        error_msg = "\n".join(error_texts) if error_texts else "未知错误"
        return f"[MCP错误] {error_msg}"
    
    # 提取正常返回内容
    texts = []
    images = []
    
    for content in result.content:
        if content.type == "text":
            texts.append(content.text)
        elif content.type == "image":
            # 图片数据通常需要特殊处理
            images.append(f"[图片数据: {content.mimeType}]")
        elif content.type == "resource":
            # 资源引用
            resource = content.resource
            texts.append(f"[资源: {resource.uri}]")
    
    output = []
    if texts:
        output.append("\n".join(texts))
    if images:
        output.append("\n".join(images))
    
    return "\n".join(output) if output else "(工具执行完成，无返回内容)"


# 处理 imports
try:
    import asyncio
    from typing import List
except ImportError:
    pass
```

---

## 3. 完整版 mcp/manager.py

```python
"""MCP Manager - 多服务器管理和工具聚合。"""

import asyncio
import logging
from typing import Dict, List, Optional

from mcp.client import MCPConnection
from mcp.tools import create_mcp_tool_class
from tools.base import WeaveMindTool

logger = logging.getLogger(__name__)


class MCPManager:
    """
    MCP Server 管理器。
    
    管理多个 MCP Server 连接，聚合所有可用工具。
    """
    
    def __init__(self, servers_config: Optional[dict] = None):
        """
        Args:
            servers_config: 服务器配置字典，默认从 settings 读取
        """
        if servers_config is None:
            import settings
            servers_config = settings.get("mcp.servers", {})
        
        self._servers_config = servers_config
        
        # Runtime state
        self._connections: Dict[str, MCPConnection] = {}
        self._tools: List[WeaveMindTool] = []
        self._initialized = False
        self._init_lock = asyncio.Lock()
    
    async def initialize(self) -> bool:
        """
        初始化所有 MCP Server 连接。
        
        Returns:
            bool: 是否至少成功连接一个 Server
        """
        async with self._init_lock:
            if self._initialized:
                return True
            
            if not self._servers_config:
                logger.info("未配置 MCP Server，跳过初始化")
                self._initialized = True
                return True
            
            logger.info(f"开始初始化 {len(self._servers_config)} 个 MCP Server")
            
            success_count = 0
            for name, config in self._servers_config.items():
                config["name"] = name
                
                if not config.get("enabled", True):
                    logger.info(f"MCP Server '{name}' 已禁用，跳过")
                    continue
                
                try:
                    conn = MCPConnection(config)
                    success = await conn.connect()
                    
                    if success:
                        self._connections[name] = conn
                        success_count += 1
                        
                        # 注册该 Server 的工具
                        for tool_info in conn.get_tools_info():
                            try:
                                tool_class = create_mcp_tool_class(tool_info, conn)
                                tool_instance = tool_class()
                                self._tools.append(tool_instance)
                            except Exception as e:
                                logger.error(f"创建工具 '{tool_info.name}' 失败: {e}")
                    else:
                        logger.warning(f"MCP Server '{name}' 连接失败")
                
                except Exception as e:
                    logger.error(f"初始化 MCP Server '{name}' 时出错: {e}")
                    # 继续初始化其他 Server，实现错误隔离
            
            self._initialized = True
            
            if success_count > 0:
                logger.info(
                    f"MCP 初始化完成: {success_count}/{len(self._servers_config)} "
                    f"Server 已连接，共 {len(self._tools)} 个工具可用"
                )
            else:
                logger.warning("没有可用的 MCP Server")
            
            return success_count > 0
    
    async def shutdown(self):
        """关闭所有连接。"""
        if not self._initialized:
            return
        
        logger.info("正在关闭 MCP 连接...")
        
        for name, conn in list(self._connections.items()):
            try:
                await conn.disconnect()
                logger.debug(f"已断开 MCP Server '{name}'")
            except Exception as e:
                logger.error(f"断开 '{name}' 时出错: {e}")
        
        self._connections.clear()
        self._tools.clear()
        self._initialized = False
        
        logger.info("MCP 连接已清理")
    
    def get_tools(self) -> List[WeaveMindTool]:
        """获取所有可用的 MCP 工具。"""
        return self._tools.copy()
    
    def get_tools_info(self) -> dict:
        """
        获取工具信息摘要。
        
        Returns:
            dict: {server_name: [tool_names]}
        """
        info = {}
        for name, conn in self._connections.items():
            info[name] = [t.name for t in conn.get_tools_info()]
        return info
    
    def get_connection(self, name: str) -> Optional[MCPConnection]:
        """获取指定名称的连接。"""
        return self._connections.get(name)
    
    def is_initialized(self) -> bool:
        """检查是否已初始化。"""
        return self._initialized
    
    async def health_check(self) -> dict:
        """
        检查所有连接的健康状态。
        
        Returns:
            dict: {server_name: bool}
        """
        results = {}
        for name, conn in self._connections.items():
            try:
                # 发送 ping 检查
                if conn._session:
                    await conn._session.send_ping()
                    results[name] = True
                else:
                    results[name] = False
            except Exception:
                results[name] = False
        
        return results
```

---

## 4. ToolRegistry 集成修改

```python
# tools/registry.py - 关键修改部分

class ToolRegistry:
    def __init__(
        self, 
        memory_manager=None, 
        rag_pipeline=None,
        mcp_manager=None  # 新增参数
    ):
        self._tools: dict = {}
        self._memory_manager = memory_manager
        self._rag_pipeline = rag_pipeline
        self._mcp_manager = mcp_manager
        self._mcp_tools_registered = False
        
        self._register_builtins()
        self._register_mcp_tools()
    
    def _register_mcp_tools(self):
        """注册 MCP 工具。"""
        if not self._mcp_manager:
            return
        
        if not self._mcp_manager.is_initialized():
            logger.debug("MCP Manager 尚未初始化，跳过工具注册")
            return
        
        mcp_tools = self._mcp_manager.get_tools()
        registered = 0
        skipped = 0
        
        for tool in mcp_tools:
            # 检查名称冲突
            if tool.name in self._tools:
                # MCP 工具优先，覆盖内置工具
                logger.warning(
                    f"工具 '{tool.name}' 与内置工具重名，MCP 版本覆盖内置版本"
                )
            
            self._tools[tool.name] = tool
            registered += 1
            logger.debug(f"注册 MCP 工具: {tool.name}")
        
        self._mcp_tools_registered = True
        logger.info(f"MCP 工具注册完成: {registered} 个成功, {skipped} 个跳过")
```

---

## 5. CLI 集成修改

```python
# cli/app.py - 关键修改部分

class WeaveMindCLI:
    def __init__(self):
        # ... 现有初始化代码
        
        # MCP Manager - 延迟初始化
        self._mcp_manager = MCPManager()
    
    async def _async_init(self):
        """异步初始化组件。"""
        # 1. 初始化 MCP
        try:
            await self._mcp_manager.initialize()
            
            # MCP 工具信息展示
            mcp_info = self._mcp_manager.get_tools_info()
            if mcp_info:
                self._console.print("\n[dim]📡 MCP Servers:[/dim]")
                for server, tools in mcp_info.items():
                    self._console.print(f"[dim]   • {server}: {', '.join(tools[:3])}{'...' if len(tools) > 3 else ''}[/dim]")
        except Exception as e:
            self._console.print(f"[yellow]⚠️ MCP 初始化失败: {e}[/yellow]")
        
        # 2. 创建 ToolRegistry（现在传入 mcp_manager）
        self._tool_registry = ToolRegistry(
            memory_manager=self._memory_manager,
            rag_pipeline=self._rag_pipeline,
            mcp_manager=self._mcp_manager
        )
        
        # 3. 初始化 AgentLoop
        tools = self._tool_registry.get_langchain_tools()
        self._agent_loop = AgentLoop(
            tools=tools,
            memory=self._memory_manager,
        )
    
    def run(self):
        """主入口。"""
        self._console.print(WELCOME_MESSAGE)
        
        # 异步初始化
        try:
            asyncio.run(self._async_init())
        except KeyboardInterrupt:
            self._console.print("\n[dim]初始化被中断[/dim]")
            return
        
        # 注册退出清理
        import atexit
        atexit.register(self._cleanup)
        
        # REPL 循环
        self._repl()
    
    def _cleanup(self):
        """退出时清理。"""
        try:
            if hasattr(self, '_mcp_manager'):
                asyncio.run(self._mcp_manager.shutdown())
        except Exception as e:
            logger.error(f"清理时出错: {e}")
```

---

## 6. 配置示例

```yaml
# config.yaml

# MCP 配置
mcp:
  enabled: true  # 总开关
  
  servers:
    # 文件系统服务器 - stdio 传输
    filesystem:
      enabled: true
      transport: stdio
      command: npx
      args: 
        - "-y"
        - "@modelcontextprotocol/server-filesystem"
        - "/Users/lqf/projects"  # 允许的根目录
      env:
        NODE_ENV: production
    
    # SQLite 数据库 - stdio 传输
    sqlite:
      enabled: true
      transport: stdio
      command: uvx
      args:
        - mcp-server-sqlite
        - --db-path
        - "/Users/lqf/data/app.db"
    
    # GitHub API - HTTP 传输
    github:
      enabled: false  # 默认关闭，需要配置 Token
      transport: http
      url: https://api.github.com/mcp
      headers:
        Authorization: "Bearer ${GITHUB_TOKEN}"
        Accept: "application/vnd.github+json"
      timeout: 30
    
    # Brave 搜索
    brave-search:
      enabled: true
      transport: stdio
      command: npx
      args:
        - "-y"
        - "@modelcontextprotocol/server-brave-search"
      env:
        BRAVE_API_KEY: "${BRAVE_API_KEY}"
```

---

## 7. 使用示例

```python
# 示例 1: 直接使用 MCPManager
async def demo():
    from mcp.manager import MCPManager
    
    manager = MCPManager()
    await manager.initialize()
    
    # 获取所有 MCP 工具
    tools = manager.get_tools()
    print(f"可用工具: {[t.name for t in tools]}")
    
    # 获取工具信息
    info = manager.get_tools_info()
    print(f"工具信息: {info}")
    
    await manager.shutdown()


# 示例 2: 在 Agent 中使用 MCP 工具
async def use_mcp_in_agent():
    from mcp.manager import MCPManager
    from tools.registry import ToolRegistry
    from core.agent_loop import AgentLoop
    
    # 初始化 MCP
    mcp_manager = MCPManager()
    await mcp_manager.initialize()
    
    # 创建 ToolRegistry（包含 MCP 工具）
    registry = ToolRegistry(mcp_manager=mcp_manager)
    tools = registry.get_langchain_tools()
    
    # 创建 Agent
    agent = AgentLoop(tools=tools)
    
    # MCP 工具现在已经可以像普通工具一样被使用了
    # 例如: 使用 filesystem/read_file 工具


# 运行示例
if __name__ == "__main__":
    asyncio.run(demo())
```

---

*代码示例完成，供开发参考使用*
