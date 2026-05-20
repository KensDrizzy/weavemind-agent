"""RAG 工具 — Agent 通过这些工具检索代码库。

SearchCode:     语义+关键词混合检索代码
IndexWorkspace: 批量索引工作区文件
"""

import logging
import time
from typing import Optional, Type

from pydantic import BaseModel, Field

from tools.base import WeaveMindTool

logger = logging.getLogger(__name__)


# ── 代码检索工具 ──────────────────────────────────────────


class SearchCodeInput(BaseModel):
    query: str = Field(
        description="检索需求，自然语言描述，如'用户认证逻辑'、'MemoryManager 类的实现'"
    )
    top_k: int = Field(
        default=5,
        description="返回结果数量，默认 5"
    )
    file_filter: Optional[str] = Field(
        default=None,
        description="文件路径过滤，如 '*.py' 只搜索 Python 文件"
    )
    source: Optional[str] = Field(
        default=None,
        description="索引源过滤，如 'weavemind' 只搜索该项目的代码"
    )
    chat_history: Optional[str] = Field(
        default=None,
        description="最近对话摘要，用于改写'它/这个/刚才'等指代不明的检索问题"
    )


class SearchCodeTool(WeaveMindTool):
    """检索代码库中与需求相关的代码片段。

    支持语义检索和关键词检索的混合模式：
    - 语义检索：根据自然语言描述找到语义相关的代码
    - 关键词检索：精确匹配类名、方法名、标识符
    - 混合评分：两种检索结果融合排序

    使用场景：
    - 理解代码结构："查找用户认证相关的代码"
    - 定位实现："MemoryManager 的 search 方法在哪"
    - 代码参考："看看其他地方怎么处理异常的"
    """

    name: str = "SearchCode"
    description: str = (
        "语义检索本地代码库，根据自然语言描述查找相关代码块。"
        "支持自然语言描述（如'用户认证逻辑'）和代码标识符精确匹配（如'MemoryManager'）。"
        "仅用于查找本地项目代码，不要用于访问 URL 或搜索互联网内容。"
    )
    args_schema: Type[BaseModel] = SearchCodeInput

    def __init__(self, rag_pipeline=None):
        super().__init__()
        self._rag_pipeline = rag_pipeline

    def _run(
        self,
        query: str,
        top_k: int = 5,
        file_filter: Optional[str] = None,
        source: Optional[str] = None,
        chat_history: Optional[str] = None,
    ) -> str:
        if not self._rag_pipeline:
            return """错误：代码检索服务未初始化。

💡 解决方案：
1. 使用 /index 命令索引代码库
2. 或调用 IndexWorkspace 工具索引

索引后，Agent 可以自动检索相关代码。"""

        # 检索前增量同步：快速检测变更文件并静默更新
        sync_summary = ""
        try:
            sync_result = self._rag_pipeline.sync_before_search(source_filter=source)
            if sync_result.get("updated", 0) > 0 or sync_result.get("deleted", 0) > 0 or sync_result.get("new_indexed", 0) > 0:
                parts = []
                if sync_result["updated"] > 0:
                    parts.append(f"更新{sync_result['updated']}个变更文件")
                if sync_result["deleted"] > 0:
                    parts.append(f"清理{sync_result['deleted']}个已删除文件")
                if sync_result["new_indexed"] > 0:
                    parts.append(f"索引{sync_result['new_indexed']}个新增文件")
                sync_summary = f"（增量同步: {', '.join(parts)}）"
        except Exception as e:
            logger.debug(f"sync_before_search 失败（不影响检索）: {e}")

        try:
            results = self._rag_pipeline.search(
                query=query, top_k=top_k, file_filter=file_filter,
                source_filter=source, strategy="hybrid",
                chat_history=[chat_history] if chat_history else None,
            )
        except Exception as e:
            logger.error(f"代码检索失败: {e}")
            return f"""检索失败: {e}

💡 可能的原因：
1. 代码库未索引 → 使用 /index 命令索引
2. 嵌入服务不可用 → 检查 config.yaml 中的 embedding 配置
3. 查询词太短或太模糊 → 尝试更具体的关键词"""

        if not results:
            return f"""未找到与 '{query}' 相关的代码。

💡 建议：
1. 使用更具体的关键词（如 'MemoryManager.search' 而不是 'search'）
2. 使用 /index 命令重新索引代码库
3. 尝试使用 file_filter 参数过滤文件类型（如 '*.py'）"""

        lines = [f"找到 {len(results)} 个相关代码片段{sync_summary}：\n"]
        for i, r in enumerate(results, 1):
            # 新鲜度标记
            freshness_mark = ""
            if r.chunk.indexed_at:
                age_seconds = time.time() - r.chunk.indexed_at
                if age_seconds < 300:  # 5 分钟内
                    freshness_mark = " 🟢"
                elif age_seconds < 3600:  # 1 小时内
                    freshness_mark = " 🟡"
                else:
                    freshness_mark = " 🔴"
            lines.append(f"--- [{i}] {r.chunk.display_name()} "
                         f"(score={r.score:.2f}, {r.chunk.chunk_type}, "
                         f"L{r.chunk.start_line}-{r.chunk.end_line}){freshness_mark} ---")
            lines.append(f"```{r.chunk.language}")
            # 截断过长的代码块
            content = r.chunk.content
            if len(content) > 800:
                content = content[:800] + "\n... (已截断)"
            lines.append(content)
            lines.append("```\n")

        return "\n".join(lines)


# ── 工作区索引工具 ──────────────────────────────────────────


class IndexWorkspaceInput(BaseModel):
    directory: str = Field(
        default=".",
        description="要索引的目录路径，默认为当前工作目录"
    )
    max_files: int = Field(
        default=500,
        description="最大索引文件数，防止意外索引超大目录"
    )
    source: Optional[str] = Field(
        default=None,
        description="索引源标签，如 'weavemind'、'omniagent'。不指定时自动取目录名"
    )


class IndexWorkspaceTool(WeaveMindTool):
    """索引工作区代码文件，建立检索数据库。

    索引后即可使用 SearchCode 工具检索代码。
    支持增量索引：已索引且未变更的文件会自动跳过。

    使用场景：
    - 开始工作前索引整个项目
    - 添加新文件后重新索引
    """

    name: str = "IndexWorkspace"
    description: str = (
        "索引工作区代码文件，建立检索数据库。"
        "索引后可使用 SearchCode 检索代码。"
        "支持增量索引，未变更文件自动跳过。"
    )
    args_schema: Type[BaseModel] = IndexWorkspaceInput

    def __init__(self, rag_pipeline=None):
        super().__init__()
        self._rag_pipeline = rag_pipeline

    def _run(self, directory: str = ".", max_files: int = 500, source: Optional[str] = None) -> str:
        if not self._rag_pipeline:
            return "错误：代码检索服务未初始化"

        try:
            stats = self._rag_pipeline.index_directory(directory, max_files=max_files, source=source)
        except Exception as e:
            logger.error(f"索引失败: {e}")
            return f"索引失败: {e}"

        lines = [
            "索引完成！",
            f"  文件数: {stats.total_files}",
            f"  代码块: {stats.total_chunks}",
            f"  耗时: {stats.index_time:.1f}s",
        ]
        if stats.chunks_by_language:
            lang_str = ", ".join(f"{k}={v}" for k, v in stats.chunks_by_language.items())
            lines.append(f"  语言分布: {lang_str}")

        return "\n".join(lines)
