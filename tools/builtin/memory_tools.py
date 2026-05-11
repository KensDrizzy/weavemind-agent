"""记忆管理工具 — Agent 通过这些工具与记忆系统交互。

MemoryAdd:       保存事实到长期记忆（按需检索）
MemorySearch:    搜索长期记忆中的相关事实
CoreMemoryEdit:  编辑核心记忆块（始终在 system prompt 中，无需检索）
"""

from typing import Literal, Type

from pydantic import BaseModel, Field

from tools.base import WeaveMindTool


# ── 长期记忆工具 ──────────────────────────────────────────


class MemoryAddInput(BaseModel):
    content: str = Field(
        description="要保存的事实内容，如'用户偏好 JDK 17'、'项目使用 Maven 构建'"
    )


class MemoryAddTool(WeaveMindTool):
    """保存重要事实到长期记忆，跨会话保留。"""

    name: str = "MemoryAdd"
    description: str = (
        "将重要事实保存到长期记忆中，下次对话仍可使用。"
        "适用于：用户偏好、项目信息、重要决策。"
        "注意：长期记忆需要检索匹配才能被找到，不是始终可见的。"
        "如果信息需要始终可见（如用户偏好、项目关键配置），请使用 CoreMemoryEdit 工具。"
    )
    args_schema: Type[BaseModel] = MemoryAddInput

    def __init__(self, memory_manager=None):
        super().__init__()
        self._memory_manager = memory_manager

    def _run(self, content: str) -> str:
        if not self._memory_manager:
            return "错误：记忆管理器未初始化"
        saved = self._memory_manager.store_fact(content)
        if saved:
            return f"已保存到长期记忆: {content}"
        return f"该事实已存在，跳过重复保存"


class MemorySearchInput(BaseModel):
    query: str = Field(description="搜索关键词，如'JDK版本'、'构建工具'")


class MemorySearchTool(WeaveMindTool):
    """搜索长期记忆中的相关事实。"""

    name: str = "MemorySearch"
    description: str = (
        "搜索长期记忆中存储的事实。"
        "用于查找用户偏好、项目配置、历史决策等跨会话信息。"
    )
    args_schema: Type[BaseModel] = MemorySearchInput

    def __init__(self, memory_manager=None):
        super().__init__()
        self._memory_manager = memory_manager

    def _run(self, query: str) -> str:
        if not self._memory_manager:
            return "错误：记忆管理器未初始化"
        results = self._memory_manager.search_memory(query, limit=5)
        if not results:
            return f"未找到与 '{query}' 相关的记忆"
        lines = []
        for i, entry in enumerate(results, 1):
            lines.append(f"{i}. {entry.content}")
        return "\n".join(lines)


# ── 核心记忆工具 ──────────────────────────────────────────


class CoreMemoryEditInput(BaseModel):
    action: Literal["set", "append", "edit"] = Field(
        description=(
            "操作类型："
            "set — 整体替换某个块的内容；"
            "append — 向某个块追加内容；"
            "edit — 替换某个块中的指定文本"
        )
    )
    block: Literal["user", "project", "persona"] = Field(
        description=(
            "要编辑的记忆块："
            "user — 用户偏好、习惯（如'偏好 JDK 17、使用 macOS'）；"
            "project — 当前项目信息（如'Spring Boot 3.x + Maven'）；"
            "persona — Agent 行为规范（如'回复使用中文'）"
        )
    )
    content: str = Field(
        description=(
            "set/append 模式：要设置或追加的内容；"
            "edit 模式：要替换成的新文本"
        )
    )
    old_text: str = Field(
        default="",
        description="仅 edit 模式需要：要被替换的旧文本"
    )


class CoreMemoryEditTool(WeaveMindTool):
    """编辑核心记忆块 — 核心记忆始终在 system prompt 中，Agent 每轮都能看到。

    与长期记忆的区别：
    - 长期记忆（MemoryAdd）：存入后需要检索匹配才能被找到，适合项目细节、历史决策
    - 核心记忆（CoreMemoryEdit）：始终在 system prompt 中，无需检索，适合用户偏好、项目关键配置

    使用场景：
    - 用户明确表达了偏好（如"我偏好 JDK 17"）→ 写入 user 块
    - 识别到项目关键信息（如"项目用 Spring Boot 3.x"）→ 写入 project 块
    - 需要调整 Agent 行为（如"回复用中文"）→ 写入 persona 块
    """

    name: str = "CoreMemoryEdit"
    description: str = (
        "编辑核心记忆块。核心记忆始终在系统提示中，每轮对话都可见，无需检索。"
        "适用于需要始终记住的信息：用户偏好写入 user 块，项目关键配置写入 project 块，"
        "Agent 行为规范写入 persona 块。"
        "支持三种操作：set（整体替换）、append（追加）、edit（精确替换指定文本）。"
    )
    args_schema: Type[BaseModel] = CoreMemoryEditInput

    def __init__(self, memory_manager=None):
        super().__init__()
        self._memory_manager = memory_manager

    def _run(
        self,
        action: Literal["set", "append", "edit"],
        block: Literal["user", "project", "persona"],
        content: str,
        old_text: str = "",
    ) -> str:
        if not self._memory_manager:
            return "错误：记忆管理器未初始化"

        try:
            if action == "set":
                self._memory_manager.core_set(block, content)
                return f"核心记忆[{block}] 已更新: {content[:100]}"
            elif action == "append":
                self._memory_manager.core_append(block, content)
                return f"核心记忆[{block}] 已追加: {content[:100]}"
            elif action == "edit":
                if not old_text:
                    return "错误：edit 模式需要提供 old_text 参数"
                success = self._memory_manager.core_edit(block, old_text, content)
                if success:
                    return f"核心记忆[{block}] 已编辑: '{old_text[:50]}' → '{content[:50]}'"
                return f"编辑失败：核心记忆[{block}] 中未找到 '{old_text[:50]}'"
            else:
                return f"错误：未知操作 '{action}'，可用: set, append, edit"
        except ValueError as e:
            return f"错误：{e}"