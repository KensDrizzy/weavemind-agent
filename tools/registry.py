"""工具注册表 — 管理所有可用工具。"""

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
    def __init__(self, memory_manager=None, rag_pipeline=None):
        self._tools: dict = {}
        self._memory_manager = memory_manager
        self._rag_pipeline = rag_pipeline
        self._register_builtins()

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

    def register(self, tool):
        self._tools[tool.name] = tool

    def get(self, name: str):
        return self._tools.get(name)

    def get_all(self) -> list:
        return list(self._tools.values())

    def get_langchain_tools(self) -> list:
        """返回 LangChain 工具格式的列表（用于 LLM.bind_tools）。"""
        return self.get_all()
