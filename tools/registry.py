"""工具注册表 — 管理所有可用工具，包括内置工具和 MCP 工具。"""

import logging

import settings
from tools.builtin.read import ReadTool
from tools.builtin.write import WriteTool
from tools.builtin.edit import EditTool
from tools.builtin.bash import BashTool
from tools.builtin.glob import GlobTool
from tools.builtin.grep import GrepTool
from tools.builtin.web_search import WebSearchTool
from tools.builtin.web_fetch import WebFetchTool
from tools.builtin.ask_user import AskUserTool
from tools.builtin.memory_tools import MemoryAddTool, MemorySearchTool, CoreMemoryEditTool
from tools.builtin.rag_tools import SearchCodeTool, IndexWorkspaceTool

logger = logging.getLogger(__name__)


class ToolRegistry:
    def __init__(self, memory_manager=None, rag_pipeline=None, mcp_manager=None):
        self._tools: dict = {}
        self._memory_manager = memory_manager
        self._rag_pipeline = rag_pipeline
        self._mcp_manager = mcp_manager
        self._mcp_tools_registered = False
        self._register_builtins()
        self._register_mcp_tools()

    def _register_builtins(self):
        # 工具注册顺序影响 LLM 选择倾向：排在前面更容易被选中。
        # SearchCode 优先级最高（语义检索一次搞定），Read/Glob/Grep 作为回退。
        tools_list = []

        # RAG 工具优先注册（仅在 RAG 启用时）
        if settings.get("rag.enabled", False) and self._rag_pipeline:
            tools_list.append(SearchCodeTool(rag_pipeline=self._rag_pipeline))
            tools_list.append(IndexWorkspaceTool(rag_pipeline=self._rag_pipeline))
            logger.info("RAG 工具已注册: SearchCode, IndexWorkspace")

        # 读取/检索类工具
        tools_list.extend([
            ReadTool(), GlobTool(), GrepTool(),
            WebFetchTool(), WebSearchTool(),
            AskUserTool(),
        ])
        # 修改类工具
        tools_list.extend([
            EditTool(), WriteTool(), BashTool(),
        ])
        # 记忆工具
        tools_list.extend([
            MemoryAddTool(memory_manager=self._memory_manager),
            MemorySearchTool(memory_manager=self._memory_manager),
            CoreMemoryEditTool(memory_manager=self._memory_manager),
        ])

        for tool in tools_list:
            self._tools[tool.name] = tool

    def _register_mcp_tools(self):
        """注册 MCP 工具（从 MCPManager 获取已连接 Server 的工具）。"""
        if not self._mcp_manager:
            return

        if not self._mcp_manager.is_initialized():
            logger.debug("MCP Manager 尚未初始化，跳过工具注册")
            return

        mcp_tools = self._mcp_manager.get_tools()
        registered = 0
        skipped = 0

        for tool in mcp_tools:
            if tool.name in self._tools:
                logger.warning(
                    "MCP 工具 '%s' 与内置工具重名，MCP 版本覆盖内置版本",
                    tool.name,
                )

            self._tools[tool.name] = tool
            registered += 1
            logger.debug("注册 MCP 工具: %s", tool.name)

        self._mcp_tools_registered = True
        if registered > 0:
            logger.info("MCP 工具注册完成: %d 个成功, %d 个跳过", registered, skipped)

    def register(self, tool):
        self._tools[tool.name] = tool

    def get(self, name: str):
        return self._tools.get(name)

    def get_all(self) -> list:
        return list(self._tools.values())

    def get_langchain_tools(self) -> list:
        """返回 LangChain 工具格式的列表（用于 LLM.bind_tools）。"""
        return self.get_all()
