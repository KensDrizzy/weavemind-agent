"""KnowledgeRAGPipeline for user-uploaded enterprise documents."""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import time
from pathlib import Path
from typing import Iterable, Optional

import settings
from knowledge_rag.chunkers import StructuralChunker
from knowledge_rag.embeddings import create_embedding_provider
from knowledge_rag.models import KnowledgeDocument, KnowledgeIndexStats, KnowledgeSearchResult
from knowledge_rag.parsers import create_parser
from knowledge_rag.retrieval import KnowledgeReranker, pack_context, rrf_fuse
from knowledge_rag.stores import MilvusKnowledgeStore, SQLiteKnowledgeStore

logger = logging.getLogger(__name__)


class KnowledgeRAGPipeline:
    """Indexes and searches user documents separately from code RAG."""

    def __init__(self):
        root_dir = settings.get("knowledge_rag.root_dir", ".weavemind/knowledge")
        os.makedirs(root_dir, exist_ok=True)
        self.root_dir = root_dir
        self.embedding = create_embedding_provider()
        self.parser = create_parser()
        self.chunker = StructuralChunker(
            max_chars=int(settings.get("knowledge_rag.chunking.max_chars", 3600)),
            overlap_chars=int(settings.get("knowledge_rag.chunking.overlap_chars", 400)),
        )
        self.reranker = KnowledgeReranker()

        self.vector_store = self._create_vector_store()
        # Keyword search remains SQLite FTS5 for now; it is cheap and good enough.
        db_path = settings.get("knowledge_rag.sqlite_db", os.path.join(root_dir, "knowledge.db"))
        self.keyword_store = SQLiteKnowledgeStore(db_path=db_path)

    def _create_vector_store(self):
        provider = settings.get("knowledge_rag.vector_store.provider", "sqlite")
        if provider == "milvus":
            uri = settings.get("knowledge_rag.vector_store.uri", "http://localhost:19530")
            token = settings.get("knowledge_rag.vector_store.token", "")
            return MilvusKnowledgeStore(
                uri=uri,
                token=token,
                collection_name="knowledge",
                embedding_dimension=self.embedding.dimension,
            )
        db_path = settings.get("knowledge_rag.sqlite_db", os.path.join(self.root_dir, "knowledge.db"))
        return SQLiteKnowledgeStore(db_path=db_path)

    def index_path(
        self,
        path: str,
        collection_id: str = "default",
        tenant_id: str = "default",
        workspace_id: str = "default",
        acl_hash: str = "public",
        max_files: int = 200,
    ) -> KnowledgeIndexStats:
        start = time.time()
        path_obj = Path(path).expanduser()
        files = self._collect_files(path_obj, max_files=max_files)
        stats = KnowledgeIndexStats(total_documents=len(files))
        for file_path in files:
            try:
                document, chunks = self.index_file(
                    str(file_path),
                    collection_id=collection_id,
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                    acl_hash=acl_hash,
                )
                if chunks == 0:
                    stats.skipped_documents += 1
                else:
                    stats.indexed_chunks += chunks
            except Exception:
                stats.failed_documents += 1
        stats.total_chunks = stats.indexed_chunks
        stats.index_time = time.time() - start
        return stats

    def index_file(
        self,
        file_path: str,
        collection_id: str = "default",
        tenant_id: str = "default",
        workspace_id: str = "default",
        acl_hash: str = "public",
    ) -> tuple[KnowledgeDocument, int]:
        abs_path = str(Path(file_path).expanduser().resolve())
        file_hash = self._file_hash(abs_path)
        existing = self.keyword_store.get_document_by_hash(file_hash, collection_id, tenant_id, workspace_id)
        if existing:
            return existing, 0

        now = time.time()
        doc_id = hashlib.sha1(
            f"{tenant_id}:{workspace_id}:{collection_id}:{abs_path}:{file_hash}".encode("utf-8")
        ).hexdigest()
        mime_type = mimetypes.guess_type(abs_path)[0] or "application/octet-stream"
        document = KnowledgeDocument(
            doc_id=doc_id,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            collection_id=collection_id,
            source_file=abs_path,
            file_name=os.path.basename(abs_path),
            file_hash=file_hash,
            mime_type=mime_type,
            created_at=now,
            updated_at=now,
            parser_version=self.parser.provider,
            chunker_version=self.chunker.version,
            embedding_provider=self.embedding.provider,
            embedding_model=self.embedding.model,
            embedding_dimension=self.embedding.dimension,
            embedding_revision=self.embedding.revision,
            acl_hash=acl_hash,
        )

        elements = self.parser.parse(abs_path)
        if not elements:
            logger.warning("Parser %s 未从 %s 提取到内容", self.parser.provider, abs_path)
        chunks = self.chunker.chunk(document, elements)
        document.chunk_count = len(chunks)
        vectors = self.embedding.embed_texts([chunk.content for chunk in chunks]) if chunks else []

        self.keyword_store.delete_by_document(doc_id)
        self.vector_store.delete_by_document(doc_id)
        self.keyword_store.upsert_document(document)
        self.keyword_store.upsert_chunks(chunks, vectors)
        self.vector_store.upsert_chunks(chunks, vectors)
        return document, len(chunks)

    def search(
        self,
        query: str,
        top_k: int = 8,
        collection_id: Optional[str] = None,
        tenant_id: str = "default",
        workspace_id: str = "default",
        acl_hash: Optional[str] = None,
        chat_history: Optional[Iterable] = None,
    ) -> list[KnowledgeSearchResult]:
        if settings.get("knowledge_rag.incremental_sync_before_search", True):
            self.sync_scope(collection_id=collection_id, tenant_id=tenant_id, workspace_id=workspace_id)

        query = self._rewrite_query(query, chat_history)
        filters = self._filters(collection_id, tenant_id, workspace_id, acl_hash)
        candidate_k = max(top_k * 4, top_k)
        query_vector = self.embedding.embed_query(query)
        semantic = self.vector_store.search(query_vector, candidate_k, filters=filters)
        keyword = self.keyword_store.keyword_search(query, candidate_k, filters=filters)
        fused = rrf_fuse(
            semantic,
            keyword,
            top_k=max(top_k, self.reranker.top_n),
            k=int(settings.get("knowledge_rag.retrieval.rrf_k", 60)),
        )
        return self.reranker.rerank(query, fused, top_k=top_k)

    def ask(
        self,
        query: str,
        top_k: int = 6,
        collection_id: Optional[str] = None,
        tenant_id: str = "default",
        workspace_id: str = "default",
        acl_hash: Optional[str] = None,
        chat_history: Optional[Iterable] = None,
    ) -> str:
        results = self.search(
            query=query,
            top_k=top_k,
            collection_id=collection_id,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            acl_hash=acl_hash,
            chat_history=chat_history,
        )
        if not results:
            return "没有在知识库中找到可支持回答的证据，因此拒绝编造答案。"

        context = pack_context(
            results,
            max_chars=int(settings.get("knowledge_rag.retrieval.max_context_chars", 8000)),
        )
        citations = []
        seen = set()
        for result in results[:5]:
            citation = result.chunk.citation()
            if citation not in seen:
                citations.append(citation)
                seen.add(citation)
        return self._generate_answer(query, context, citations)

    def list_documents(
        self,
        collection_id: Optional[str] = None,
        tenant_id: str = "default",
        workspace_id: str = "default",
    ) -> list[KnowledgeDocument]:
        return self.keyword_store.list_documents(self._filters(collection_id, tenant_id, workspace_id, None))

    def delete_document(self, doc_id: str) -> bool:
        if not self.keyword_store.get_document(doc_id):
            return False
        self.keyword_store.delete_by_document(doc_id)
        self.vector_store.delete_by_document(doc_id)
        return True

    def reindex(
        self,
        collection_id: Optional[str] = None,
        tenant_id: str = "default",
        workspace_id: str = "default",
    ) -> KnowledgeIndexStats:
        documents = self.list_documents(collection_id=collection_id, tenant_id=tenant_id, workspace_id=workspace_id)
        stats = KnowledgeIndexStats(total_documents=len(documents))
        start = time.time()
        for document in documents:
            source_file = document.source_file
            self.keyword_store.delete_by_document(document.doc_id)
            self.vector_store.delete_by_document(document.doc_id)
            if not os.path.exists(source_file):
                stats.failed_documents += 1
                continue
            try:
                _, chunks = self.index_file(
                    source_file,
                    collection_id=document.collection_id,
                    tenant_id=document.tenant_id,
                    workspace_id=document.workspace_id,
                    acl_hash=document.acl_hash,
                )
                stats.indexed_chunks += chunks
            except Exception:
                stats.failed_documents += 1
        stats.total_chunks = stats.indexed_chunks
        stats.index_time = time.time() - start
        return stats

    def sync_scope(
        self,
        collection_id: Optional[str] = None,
        tenant_id: str = "default",
        workspace_id: str = "default",
    ) -> KnowledgeIndexStats:
        """Incremental sync: reindex changed/missing files and remove stale docs."""
        filters = self._filters(collection_id, tenant_id, workspace_id, None)
        documents = self.keyword_store.list_documents(filters)

        stats = KnowledgeIndexStats(total_documents=len(documents))
        indexed_paths: set[str] = set()

        for document in documents:
            source_file = document.source_file
            if not os.path.exists(source_file):
                self.delete_document(document.doc_id)
                stats.failed_documents += 1
                continue

            current_hash = self._file_hash(source_file)
            if current_hash == document.file_hash:
                indexed_paths.add(source_file)
                continue

            try:
                self.delete_document(document.doc_id)
                _, chunks = self.index_file(
                    source_file,
                    collection_id=document.collection_id,
                    tenant_id=document.tenant_id,
                    workspace_id=document.workspace_id,
                    acl_hash=document.acl_hash,
                )
                stats.indexed_chunks += chunks
                indexed_paths.add(source_file)
            except Exception:
                stats.failed_documents += 1

        # Index new files under root_dir that belong to this scope.
        root_path = Path(self.root_dir)
        if root_path.is_dir():
            for file_path in self._collect_files(root_path, max_files=2000):
                abs_path = str(file_path.resolve())
                if abs_path in indexed_paths:
                    continue
                try:
                    _, chunks = self.index_file(
                        abs_path,
                        collection_id=collection_id or "default",
                        tenant_id=tenant_id,
                        workspace_id=workspace_id,
                        acl_hash="public",
                    )
                    stats.indexed_chunks += chunks
                    stats.total_documents += 1
                except Exception:
                    stats.failed_documents += 1

        stats.total_chunks = stats.indexed_chunks
        return stats

    @staticmethod
    def _rewrite_query(query: str, chat_history: Optional[Iterable]) -> str:
        if not chat_history:
            return query
        try:
            from rag.retrieval_enhancements import QueryRewriter

            rewriter = QueryRewriter()
            rewriter.enabled = True
            rewriter.method = "auto"
            rewriter.max_queries = 1
            variants = rewriter.rewrite(query, chat_history=chat_history)
            return variants[0] if variants else query
        except Exception as e:
            logger.debug("知识库查询改写失败: %s", e)
            return query

    @staticmethod
    def _generate_answer(query: str, context: str, citations: list[str]) -> str:
        try:
            from core.llm_factory import create_llm
            from langchain_core.messages import HumanMessage, SystemMessage

            llm = create_llm(max_tokens=2048)
            citation_list = "\n".join(f"{i + 1}. {c}" for i, c in enumerate(citations))
            messages = [
                SystemMessage(
                    content=(
                        "你是一位严谨的企业知识库问答助手。请严格根据下面提供的资料片段回答问题，"
                        "每个关键事实后必须标注引用，格式为 [文件名 p.页码 · 章节路径]。"
                        "如果资料中没有相关信息，请明确说明无法回答，不要编造。"
                    )
                ),
                HumanMessage(
                    content=(
                        f"用户问题：{query}\n\n"
                        f"资料片段：\n{context}\n\n"
                        f"可用引用：\n{citation_list}\n\n"
                        "请用中文给出带引用的完整回答。"
                    )
                ),
            ]
            response = llm.invoke(messages)
            return str(getattr(response, "content", response)).strip()
        except Exception as e:
            logger.error("知识库问答生成失败: %s", e)
            return (
                "已找到可用于回答的知识库证据，但生成回答时发生错误。\n\n"
                f"证据片段：\n{context}"
            )

    @staticmethod
    def _filters(collection_id: Optional[str], tenant_id: str, workspace_id: str, acl_hash: Optional[str]) -> dict:
        filters = {"tenant_id": tenant_id, "workspace_id": workspace_id}
        if collection_id:
            filters["collection_id"] = collection_id
        if acl_hash:
            filters["acl_hash"] = acl_hash
        return filters

    @staticmethod
    def _collect_files(path: Path, max_files: int) -> list[Path]:
        if path.is_file():
            return [path]
        if not path.is_dir():
            raise FileNotFoundError(str(path))
        files: list[Path] = []
        ignored_dirs = {".git", ".weavemind", "node_modules", "__pycache__", ".venv", "venv"}
        # 避免把向量/关键词库的 SQLite 自身文件当资料索引（root_dir 与元数据目录可能重合）
        ignored_exts = {".db", ".db-shm", ".db-wal", ".db-journal", ".lock", ".tmp"}
        for root, dirs, names in os.walk(path):
            dirs[:] = [d for d in dirs if d not in ignored_dirs]
            for name in names:
                candidate = Path(root) / name
                if not candidate.is_file() or name.startswith("."):
                    continue
                lower = name.lower()
                if any(lower.endswith(ext) for ext in ignored_exts):
                    continue
                if lower.startswith("knowledge.db"):
                    continue
                files.append(candidate)
                if len(files) >= max_files:
                    return files
        return files

    @staticmethod
    def _file_hash(file_path: str) -> str:
        h = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
