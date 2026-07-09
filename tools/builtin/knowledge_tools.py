"""Knowledge RAG tools for uploaded/user documents."""

import logging
from typing import Optional, Type

from pydantic import BaseModel, Field

from tools.base import WeaveMindTool

logger = logging.getLogger(__name__)


class IndexKnowledgeInput(BaseModel):
    path: str = Field(description="要索引的文件或目录路径")
    collection: str = Field(default="default", description="知识库集合名称")
    tenant_id: str = Field(default="default", description="租户 ID")
    workspace_id: str = Field(default="default", description="工作区 ID")
    acl_hash: str = Field(default="public", description="ACL 摘要，用于检索阶段过滤")
    max_files: int = Field(default=200, description="目录索引时最多处理的文件数")


class IndexKnowledgeTool(WeaveMindTool):
    name: str = "IndexKnowledge"
    description: str = (
        "索引用户上传或本地资料文件/目录到知识库。"
        "适用于 PDF、Word、Markdown、HTML、TXT 等企业资料，不用于代码库检索。"
    )
    args_schema: Type[BaseModel] = IndexKnowledgeInput

    def __init__(self, knowledge_pipeline=None):
        super().__init__()
        self._knowledge_pipeline = knowledge_pipeline

    def _run(
        self,
        path: str,
        collection: str = "default",
        tenant_id: str = "default",
        workspace_id: str = "default",
        acl_hash: str = "public",
        max_files: int = 200,
    ) -> str:
        if not self._knowledge_pipeline:
            return "错误：Knowledge RAG 服务未初始化。请启用 knowledge_rag.enabled。"
        try:
            stats = self._knowledge_pipeline.index_path(
                path,
                collection_id=collection,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                acl_hash=acl_hash,
                max_files=max_files,
            )
        except Exception as e:
            logger.error("资料索引失败: %s", e)
            return f"资料索引失败: {e}"
        return (
            "资料索引完成：\n"
            f"  文档数: {stats.total_documents}\n"
            f"  新增 chunk: {stats.indexed_chunks}\n"
            f"  跳过重复: {stats.skipped_documents}\n"
            f"  失败: {stats.failed_documents}\n"
            f"  耗时: {stats.index_time:.1f}s"
        )


class SearchKnowledgeInput(BaseModel):
    query: str = Field(description="资料检索问题或关键词")
    top_k: int = Field(default=8, description="返回片段数量")
    collection: Optional[str] = Field(default=None, description="只检索指定知识库集合")
    tenant_id: str = Field(default="default", description="租户 ID")
    workspace_id: str = Field(default="default", description="工作区 ID")
    acl_hash: Optional[str] = Field(default=None, description="ACL 摘要过滤")
    chat_history: Optional[str] = Field(
        default=None,
        description="近期对话历史（多轮指代消解用），格式为 role: content 的文本",
    )


class SearchKnowledgeTool(WeaveMindTool):
    name: str = "SearchKnowledge"
    description: str = (
        "检索用户上传资料、企业知识库、合同、制度、PDF、Word、Markdown 或图片 OCR 文本。"
        "返回带文件名、页码和章节路径的可溯源片段。"
    )
    args_schema: Type[BaseModel] = SearchKnowledgeInput

    def __init__(self, knowledge_pipeline=None):
        super().__init__()
        self._knowledge_pipeline = knowledge_pipeline

    def _run(
        self,
        query: str,
        top_k: int = 8,
        collection: Optional[str] = None,
        tenant_id: str = "default",
        workspace_id: str = "default",
        acl_hash: Optional[str] = None,
        chat_history: Optional[str] = None,
    ) -> str:
        if not self._knowledge_pipeline:
            return "错误：Knowledge RAG 服务未初始化。请启用 knowledge_rag.enabled。"
        try:
            results = self._knowledge_pipeline.search(
                query,
                top_k=top_k,
                collection_id=collection,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                acl_hash=acl_hash,
                chat_history=chat_history.split("\n") if chat_history else None,
            )
        except Exception as e:
            logger.error("资料检索失败: %s", e)
            return f"资料检索失败: {e}"
        if not results:
            return f"未在知识库中找到与 '{query}' 相关的资料。"
        lines = [f"找到 {len(results)} 个相关资料片段：\n"]
        for i, result in enumerate(results, 1):
            lines.append(
                f"--- [{i}] {result.chunk.citation()} "
                f"(score={result.score:.3f}, {result.chunk.element_type}, source={result.source}) ---"
            )
            content = result.chunk.content
            if len(content) > 900:
                content = content[:900] + "\n... (已截断)"
            lines.append(content)
            lines.append("")
        return "\n".join(lines)


class AskKnowledgeInput(SearchKnowledgeInput):
    top_k: int = Field(default=6, description="用于回答的证据片段数量")


class AskKnowledgeTool(WeaveMindTool):
    name: str = "AskKnowledge"
    description: str = (
        "基于知识库资料回答问题，并强制使用引用。"
        "适用于'这份资料里说了什么'、合同/制度/上传文档问答；无证据时应拒答。"
    )
    args_schema: Type[BaseModel] = AskKnowledgeInput

    def __init__(self, knowledge_pipeline=None):
        super().__init__()
        self._knowledge_pipeline = knowledge_pipeline

    def _run(
        self,
        query: str,
        top_k: int = 6,
        collection: Optional[str] = None,
        tenant_id: str = "default",
        workspace_id: str = "default",
        acl_hash: Optional[str] = None,
        chat_history: Optional[str] = None,
    ) -> str:
        if not self._knowledge_pipeline:
            return "错误：Knowledge RAG 服务未初始化。请启用 knowledge_rag.enabled。"
        try:
            return self._knowledge_pipeline.ask(
                query,
                top_k=top_k,
                collection_id=collection,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                acl_hash=acl_hash,
                chat_history=chat_history.split("\n") if chat_history else None,
            )
        except Exception as e:
            logger.error("资料问答失败: %s", e)
            return f"资料问答失败: {e}"


class ListKnowledgeInput(BaseModel):
    collection: Optional[str] = Field(default=None, description="只列出指定集合")
    tenant_id: str = Field(default="default", description="租户 ID")
    workspace_id: str = Field(default="default", description="工作区 ID")


class ListKnowledgeTool(WeaveMindTool):
    name: str = "ListKnowledge"
    description: str = "列出知识库中已索引的资料文档。"
    args_schema: Type[BaseModel] = ListKnowledgeInput

    def __init__(self, knowledge_pipeline=None):
        super().__init__()
        self._knowledge_pipeline = knowledge_pipeline

    def _run(
        self,
        collection: Optional[str] = None,
        tenant_id: str = "default",
        workspace_id: str = "default",
    ) -> str:
        if not self._knowledge_pipeline:
            return "错误：Knowledge RAG 服务未初始化。请启用 knowledge_rag.enabled。"
        docs = self._knowledge_pipeline.list_documents(
            collection_id=collection,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
        )
        if not docs:
            return "知识库暂无已索引文档。"
        lines = [f"知识库文档（{len(docs)}）："]
        for doc in docs:
            lines.append(
                f"- {doc.doc_id[:10]}  {doc.file_name}  "
                f"collection={doc.collection_id} chunks={doc.chunk_count}"
            )
        return "\n".join(lines)


class DeleteKnowledgeInput(BaseModel):
    doc_id: str = Field(description="要删除的文档 ID")


class DeleteKnowledgeTool(WeaveMindTool):
    name: str = "DeleteKnowledge"
    description: str = "删除知识库文档及其索引。"
    args_schema: Type[BaseModel] = DeleteKnowledgeInput

    def __init__(self, knowledge_pipeline=None):
        super().__init__()
        self._knowledge_pipeline = knowledge_pipeline

    def _run(self, doc_id: str) -> str:
        if not self._knowledge_pipeline:
            return "错误：Knowledge RAG 服务未初始化。请启用 knowledge_rag.enabled。"
        if self._knowledge_pipeline.delete_document(doc_id):
            return f"已删除知识库文档: {doc_id}"
        return f"未找到知识库文档: {doc_id}"


class ReindexKnowledgeInput(ListKnowledgeInput):
    pass


class ReindexKnowledgeTool(WeaveMindTool):
    name: str = "ReindexKnowledge"
    description: str = "重建知识库索引，适用于 parser/chunker/embedding 版本变更后。"
    args_schema: Type[BaseModel] = ReindexKnowledgeInput

    def __init__(self, knowledge_pipeline=None):
        super().__init__()
        self._knowledge_pipeline = knowledge_pipeline

    def _run(
        self,
        collection: Optional[str] = None,
        tenant_id: str = "default",
        workspace_id: str = "default",
    ) -> str:
        if not self._knowledge_pipeline:
            return "错误：Knowledge RAG 服务未初始化。请启用 knowledge_rag.enabled。"
        stats = self._knowledge_pipeline.reindex(
            collection_id=collection,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
        )
        return (
            "知识库重建完成：\n"
            f"  文档数: {stats.total_documents}\n"
            f"  重建 chunk: {stats.indexed_chunks}\n"
            f"  失败: {stats.failed_documents}\n"
            f"  耗时: {stats.index_time:.1f}s"
        )
