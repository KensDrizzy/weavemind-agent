"""Milvus vector store adapter for knowledge RAG.

 production deployments should point to a real Milvus/Zilliz instance via
 knowledge_rag.vector_store.uri. This adapter keeps the same interface as
 SQLiteKnowledgeStore so the pipeline can switch between them transparently.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Optional

from knowledge_rag.models import KnowledgeChunk, KnowledgeSearchResult
from knowledge_rag.stores.vector_base import VectorStoreAdapter

logger = logging.getLogger(__name__)


class MilvusKnowledgeStore(VectorStoreAdapter):
    """Dense vector store backed by Milvus."""

    provider = "milvus"

    def __init__(
        self,
        uri: str = "http://localhost:19530",
        token: str = "",
        collection_name: str = "knowledge",
        embedding_dimension: int = 1536,
    ):
        from pymilvus import Collection, CollectionSchema, DataType, FieldSchema, connections

        self.uri = uri
        self.token = token
        self.collection_name = collection_name
        self.embedding_dimension = embedding_dimension
        self._conn_alias = f"knowledge_rag_{id(self)}"

        try:
            connections.connect(alias=self._conn_alias, uri=uri, token=token or "")
        except Exception as e:
            logger.error("连接 Milvus 失败: %s", e)
            raise

        self._collection = self._get_or_create_collection()

    def _get_or_create_collection(self):
        from pymilvus import Collection, DataType, FieldSchema, CollectionSchema, utility

        if utility.has_collection(self.collection_name, using=self._conn_alias):
            collection = Collection(self.collection_name, using=self._conn_alias)
            self._ensure_index(collection)
            collection.load()
            return collection

        fields = [
            FieldSchema(name="chunk_id", dtype=DataType.VARCHAR, max_length=64, is_primary=True),
            FieldSchema(name="doc_id", dtype=DataType.VARCHAR, max_length=64),
            FieldSchema(name="tenant_id", dtype=DataType.VARCHAR, max_length=64),
            FieldSchema(name="workspace_id", dtype=DataType.VARCHAR, max_length=64),
            FieldSchema(name="collection_id", dtype=DataType.VARCHAR, max_length=128),
            FieldSchema(name="source_file", dtype=DataType.VARCHAR, max_length=2048),
            FieldSchema(name="file_name", dtype=DataType.VARCHAR, max_length=512),
            FieldSchema(name="content", dtype=DataType.VARCHAR, max_length=65535),
            FieldSchema(name="page_number", dtype=DataType.INT64),
            FieldSchema(name="section_path", dtype=DataType.VARCHAR, max_length=2048),
            FieldSchema(name="element_type", dtype=DataType.VARCHAR, max_length=64),
            FieldSchema(name="created_at", dtype=DataType.FLOAT),
            FieldSchema(name="acl_hash", dtype=DataType.VARCHAR, max_length=128),
            FieldSchema(name="embedding_provider", dtype=DataType.VARCHAR, max_length=64),
            FieldSchema(name="embedding_model", dtype=DataType.VARCHAR, max_length=256),
            FieldSchema(name="embedding_dimension", dtype=DataType.INT64),
            FieldSchema(name="embedding_revision", dtype=DataType.VARCHAR, max_length=64),
            FieldSchema(name="vector", dtype=DataType.FLOAT_VECTOR, dim=self.embedding_dimension),
        ]
        schema = CollectionSchema(fields, description="WeaveMind knowledge RAG chunks")
        collection = Collection(self.collection_name, schema, using=self._conn_alias)
        self._ensure_index(collection)
        collection.load()
        return collection

    def _ensure_index(self, collection) -> None:
        from pymilvus import Index

        try:
            index_params = {
                "metric_type": "COSINE",
                "index_type": "HNSW",
                "params": {"M": 16, "efConstruction": 200},
            }
            # Avoid recreating existing index
            if not collection.indexes:
                Index(collection, "vector", index_params, using=self._conn_alias)
        except Exception as e:
            logger.warning("创建 Milvus 索引失败（可能已存在）: %s", e)

    def upsert_chunks(self, chunks: list[KnowledgeChunk], vectors: list[list[float]] | None = None) -> None:
        if not chunks:
            return
        if vectors is None:
            vectors = [[] for _ in chunks]

        data = {key: [] for key in self._field_names()}
        for chunk, vector in zip(chunks, vectors):
            data["chunk_id"].append(chunk.chunk_id)
            data["doc_id"].append(chunk.doc_id)
            data["tenant_id"].append(chunk.tenant_id)
            data["workspace_id"].append(chunk.workspace_id)
            data["collection_id"].append(chunk.collection_id)
            data["source_file"].append(chunk.source_file)
            data["file_name"].append(chunk.file_name)
            data["content"].append(chunk.content[:65535])
            data["page_number"].append(chunk.page_number or 0)
            data["section_path"].append(" > ".join(chunk.section_path)[:2048])
            data["element_type"].append(chunk.element_type or "paragraph")
            data["created_at"].append(float(chunk.created_at))
            data["acl_hash"].append(chunk.acl_hash)
            data["embedding_provider"].append(chunk.embedding_provider)
            data["embedding_model"].append(chunk.embedding_model)
            data["embedding_dimension"].append(chunk.embedding_dimension)
            data["embedding_revision"].append(chunk.embedding_revision)
            data["vector"].append(vector)

        try:
            self._collection.upsert(data)
        except Exception as e:
            logger.error("Milvus upsert 失败: %s", e)
            raise

    def search(self, query_vector: list[float], top_k: int, filters: dict | None = None) -> list[KnowledgeSearchResult]:
        from pymilvus import Collection

        if not isinstance(self._collection, Collection):
            return []

        expr = self._filters_to_expr(filters)
        search_params = {"metric_type": "COSINE", "params": {"ef": max(64, top_k * 2)}}
        try:
            results = self._collection.search(
                data=[query_vector],
                anns_field="vector",
                param=search_params,
                limit=top_k,
                expr=expr or None,
                output_fields=self._field_names(),
            )
        except Exception as e:
            logger.error("Milvus 向量搜索失败: %s", e)
            return []

        out: list[KnowledgeSearchResult] = []
        if not results:
            return out
        for hit in results[0]:
            entity = hit.entity
            score = float(hit.distance)
            chunk = self._entity_to_chunk(entity)
            out.append(KnowledgeSearchResult(
                chunk=chunk,
                score=max(score, 0.0),
                semantic_score=max(score, 0.0),
                source="semantic",
            ))
        return out

    def delete_by_document(self, doc_id: str) -> None:
        expr = f'doc_id == "{self._escape(doc_id)}"'
        try:
            self._collection.delete(expr)
        except Exception as e:
            logger.error("Milvus 删除文档失败: %s", e)
            raise

    def delete_by_chunk_id(self, chunk_id: str) -> None:
        expr = f'chunk_id == "{self._escape(chunk_id)}"'
        try:
            self._collection.delete(expr)
        except Exception as e:
            logger.error("Milvus 删除 chunk 失败: %s", e)
            raise

    def rebuild_collection(self, collection: str) -> None:
        expr = f'collection_id == "{self._escape(collection)}"'
        try:
            self._collection.delete(expr)
        except Exception as e:
            logger.error("Milvus 重建集合失败: %s", e)
            raise

    def count_chunks(self, filters: dict | None = None) -> int:
        expr = self._filters_to_expr(filters)
        try:
            return self._collection.query(
                expr=expr or "",
                output_fields=["count(*)"],
            )[0].get("count(*)", 0)
        except Exception as e:
            logger.error("Milvus 统计 chunk 失败: %s", e)
            return 0

    def close(self) -> None:
        from pymilvus import connections

        try:
            connections.disconnect(self._conn_alias)
        except Exception:
            pass

    @staticmethod
    def _field_names() -> list[str]:
        return [
            "chunk_id", "doc_id", "tenant_id", "workspace_id", "collection_id",
            "source_file", "file_name", "content", "page_number", "section_path",
            "element_type", "created_at", "acl_hash", "embedding_provider",
            "embedding_model", "embedding_dimension", "embedding_revision", "vector",
        ]

    @staticmethod
    def _entity_to_chunk(entity) -> KnowledgeChunk:
        def _get(name: str, default=""):
            return getattr(entity, name, default) or default

        return KnowledgeChunk(
            chunk_id=_get("chunk_id"),
            doc_id=_get("doc_id"),
            tenant_id=_get("tenant_id"),
            workspace_id=_get("workspace_id"),
            collection_id=_get("collection_id"),
            source_file=_get("source_file"),
            file_name=_get("file_name"),
            content=_get("content"),
            page_number=_get("page_number") or None,
            section_path=_get("section_path").split(" > ") if _get("section_path") else [],
            element_type=_get("element_type"),  # type: ignore[arg-type]
            bbox=None,
            created_at=float(_get("created_at") or time.time()),
            acl_hash=_get("acl_hash"),
            parser_version="milvus@1",
            chunker_version="structural@1",
            embedding_provider=_get("embedding_provider"),
            embedding_model=_get("embedding_model"),
            embedding_dimension=int(_get("embedding_dimension") or 0),
            embedding_revision=_get("embedding_revision"),
        )

    @staticmethod
    def _filters_to_expr(filters: dict | None) -> str:
        if not filters:
            return ""
        parts = []
        for key, value in filters.items():
            if value is None:
                continue
            if key in ("tenant_id", "workspace_id", "collection_id", "doc_id", "acl_hash", "file_name"):
                parts.append(f'{key} == "{MilvusKnowledgeStore._escape(str(value))}"')
        return " and ".join(parts)

    @staticmethod
    def _escape(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')
