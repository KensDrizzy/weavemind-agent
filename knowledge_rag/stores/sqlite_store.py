"""SQLite fallback store for knowledge RAG metadata, vectors and FTS."""

from __future__ import annotations

import json
import math
import os
import sqlite3
from typing import Iterable

from knowledge_rag.models import KnowledgeChunk, KnowledgeDocument, KnowledgeSearchResult
from knowledge_rag.stores.keyword_base import KeywordStoreAdapter
from knowledge_rag.stores.vector_base import VectorStoreAdapter


class SQLiteKnowledgeStore(VectorStoreAdapter, KeywordStoreAdapter):
    """Local fallback store.

    Production deployments should swap this for Milvus dense+sparse and a
    metadata DB, but keeping the interface identical lets the rest of the app
    use the same pipeline.
    """

    def __init__(self, db_path: str = ".weavemind/knowledge/knowledge.db"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()

    def _create_tables(self) -> None:
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS documents(
                doc_id TEXT PRIMARY KEY,
                tenant_id TEXT,
                workspace_id TEXT,
                collection_id TEXT,
                source_file TEXT,
                file_name TEXT,
                file_hash TEXT,
                mime_type TEXT,
                created_at REAL,
                updated_at REAL,
                parser_version TEXT,
                chunker_version TEXT,
                embedding_provider TEXT,
                embedding_model TEXT,
                embedding_dimension INTEGER,
                embedding_revision TEXT,
                acl_hash TEXT,
                chunk_count INTEGER,
                status TEXT,
                metadata TEXT
            )
        """)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS chunks(
                chunk_id TEXT PRIMARY KEY,
                doc_id TEXT,
                tenant_id TEXT,
                workspace_id TEXT,
                collection_id TEXT,
                source_file TEXT,
                file_name TEXT,
                content TEXT,
                page_number INTEGER,
                section_path TEXT,
                element_type TEXT,
                bbox TEXT,
                created_at REAL,
                acl_hash TEXT,
                parser_version TEXT,
                chunker_version TEXT,
                embedding_provider TEXT,
                embedding_model TEXT,
                embedding_dimension INTEGER,
                embedding_revision TEXT,
                vector TEXT,
                metadata TEXT
            )
        """)
        self._conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                chunk_id UNINDEXED,
                doc_id UNINDEXED,
                collection_id,
                file_name,
                section_path,
                element_type,
                content,
                tokenize='unicode61'
            )
        """)
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_doc ON chunks(doc_id)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_scope ON chunks(tenant_id, workspace_id, collection_id)")
        self._conn.commit()

    def upsert_document(self, document: KnowledgeDocument) -> None:
        self._conn.execute(
            """INSERT OR REPLACE INTO documents
               (doc_id, tenant_id, workspace_id, collection_id, source_file,
                file_name, file_hash, mime_type, created_at, updated_at,
                parser_version, chunker_version, embedding_provider,
                embedding_model, embedding_dimension, embedding_revision,
                acl_hash, chunk_count, status, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                document.doc_id, document.tenant_id, document.workspace_id,
                document.collection_id, document.source_file, document.file_name,
                document.file_hash, document.mime_type, document.created_at,
                document.updated_at, document.parser_version, document.chunker_version,
                document.embedding_provider, document.embedding_model,
                document.embedding_dimension, document.embedding_revision,
                document.acl_hash, document.chunk_count, document.status,
                document.model_dump_json(),
            ),
        )
        self._conn.commit()

    def upsert_chunks(self, chunks: list[KnowledgeChunk], vectors: list[list[float]] | None = None) -> None:
        if vectors is None:
            vectors = [[] for _ in chunks]
        for chunk in chunks:
            self.delete_chunk(chunk.chunk_id)
        for chunk, vector in zip(chunks, vectors):
            metadata = chunk.to_metadata()
            self._conn.execute(
                """INSERT OR REPLACE INTO chunks
                   (chunk_id, doc_id, tenant_id, workspace_id, collection_id,
                    source_file, file_name, content, page_number, section_path,
                    element_type, bbox, created_at, acl_hash, parser_version,
                    chunker_version, embedding_provider, embedding_model,
                    embedding_dimension, embedding_revision, vector, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    chunk.chunk_id, chunk.doc_id, chunk.tenant_id, chunk.workspace_id,
                    chunk.collection_id, chunk.source_file, chunk.file_name,
                    chunk.content, chunk.page_number, json.dumps(chunk.section_path, ensure_ascii=False),
                    chunk.element_type, json.dumps(chunk.bbox, ensure_ascii=False),
                    chunk.created_at, chunk.acl_hash, chunk.parser_version,
                    chunk.chunker_version, chunk.embedding_provider, chunk.embedding_model,
                    chunk.embedding_dimension, chunk.embedding_revision,
                    json.dumps(vector), json.dumps(metadata, ensure_ascii=False),
                ),
            )
            self._conn.execute(
                """INSERT INTO chunks_fts
                   (chunk_id, doc_id, collection_id, file_name, section_path, element_type, content)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    chunk.chunk_id, chunk.doc_id, chunk.collection_id, chunk.file_name,
                    " > ".join(chunk.section_path), chunk.element_type, chunk.content,
                ),
            )
        self._conn.commit()

    def search(self, query_or_vector, top_k: int, filters: dict | None = None) -> list[KnowledgeSearchResult]:
        if isinstance(query_or_vector, str):
            return self.keyword_search(query_or_vector, top_k=top_k, filters=filters)
        return self.vector_search(query_or_vector, top_k=top_k, filters=filters)

    def vector_search(self, query_vector: list[float], top_k: int, filters: dict | None = None) -> list[KnowledgeSearchResult]:
        rows = self._fetch_chunk_rows(filters)
        scored: list[KnowledgeSearchResult] = []
        for row in rows:
            vector = json.loads(row["vector"] or "[]")
            if not vector:
                continue
            score = _cosine(query_vector, vector)
            chunk = self._row_to_chunk(row)
            scored.append(KnowledgeSearchResult(
                chunk=chunk,
                score=max(score, 0.0),
                semantic_score=max(score, 0.0),
                source="semantic",
            ))
        return sorted(scored, key=lambda r: -r.score)[:top_k]

    def keyword_search(self, query: str, top_k: int, filters: dict | None = None) -> list[KnowledgeSearchResult]:
        tokens = [t for t in query.strip().split() if len(t) > 1]
        if not tokens:
            return []
        fts_query = " OR ".join(f'"{t}"*' for t in tokens)
        sql = """
            SELECT c.*, bm25(chunks_fts) AS raw_score
            FROM chunks_fts
            JOIN chunks c ON c.chunk_id = chunks_fts.chunk_id
            WHERE chunks_fts MATCH ?
        """
        params: list = [fts_query]
        sql, params = self._append_filters(sql, params, filters, table_alias="c")
        sql += " ORDER BY raw_score LIMIT ?"
        params.append(top_k)
        try:
            rows = self._conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            return []
        results = []
        for row in rows:
            raw_score = row["raw_score"] or 0.0
            score = min(abs(raw_score) / 10.0, 1.0)
            results.append(KnowledgeSearchResult(
                chunk=self._row_to_chunk(row),
                score=score,
                keyword_score=score,
                source="keyword",
            ))
        return results

    def delete_chunk(self, chunk_id: str) -> None:
        self._conn.execute("DELETE FROM chunks_fts WHERE chunk_id = ?", (chunk_id,))
        self._conn.execute("DELETE FROM chunks WHERE chunk_id = ?", (chunk_id,))

    def delete_by_document(self, doc_id: str) -> None:
        self._conn.execute("DELETE FROM chunks_fts WHERE doc_id = ?", (doc_id,))
        self._conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
        self._conn.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))
        self._conn.commit()

    def rebuild_collection(self, collection: str) -> None:
        return None

    def get_document_by_hash(self, file_hash: str, collection_id: str, tenant_id: str, workspace_id: str) -> KnowledgeDocument | None:
        row = self._conn.execute(
            """SELECT * FROM documents
               WHERE file_hash = ? AND collection_id = ? AND tenant_id = ? AND workspace_id = ?
               LIMIT 1""",
            (file_hash, collection_id, tenant_id, workspace_id),
        ).fetchone()
        return self._row_to_document(row) if row else None

    def get_document(self, doc_id: str) -> KnowledgeDocument | None:
        row = self._conn.execute("SELECT * FROM documents WHERE doc_id = ?", (doc_id,)).fetchone()
        return self._row_to_document(row) if row else None

    def list_documents(self, filters: dict | None = None) -> list[KnowledgeDocument]:
        sql = "SELECT * FROM documents WHERE 1=1"
        params: list = []
        sql, params = self._append_filters(sql, params, filters, table_alias=None)
        sql += " ORDER BY updated_at DESC"
        return [self._row_to_document(row) for row in self._conn.execute(sql, params).fetchall()]

    def count_chunks(self, filters: dict | None = None) -> int:
        sql = "SELECT COUNT(*) AS n FROM chunks WHERE 1=1"
        params: list = []
        sql, params = self._append_filters(sql, params, filters, table_alias=None)
        row = self._conn.execute(sql, params).fetchone()
        return int(row["n"] if row else 0)

    def _fetch_chunk_rows(self, filters: dict | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM chunks WHERE 1=1"
        params: list = []
        sql, params = self._append_filters(sql, params, filters, table_alias=None)
        return self._conn.execute(sql, params).fetchall()

    @staticmethod
    def _append_filters(sql: str, params: list, filters: dict | None, table_alias: str | None) -> tuple[str, list]:
        if not filters:
            return sql, params
        prefix = f"{table_alias}." if table_alias else ""
        for key in ("tenant_id", "workspace_id", "collection_id", "doc_id", "acl_hash"):
            value = filters.get(key)
            if value:
                sql += f" AND {prefix}{key} = ?"
                params.append(value)
        return sql, params

    @staticmethod
    def _row_to_document(row: sqlite3.Row) -> KnowledgeDocument:
        return KnowledgeDocument(
            doc_id=row["doc_id"],
            tenant_id=row["tenant_id"],
            workspace_id=row["workspace_id"],
            collection_id=row["collection_id"],
            source_file=row["source_file"],
            file_name=row["file_name"],
            file_hash=row["file_hash"],
            mime_type=row["mime_type"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            parser_version=row["parser_version"],
            chunker_version=row["chunker_version"],
            embedding_provider=row["embedding_provider"],
            embedding_model=row["embedding_model"],
            embedding_dimension=row["embedding_dimension"],
            embedding_revision=row["embedding_revision"],
            acl_hash=row["acl_hash"],
            chunk_count=row["chunk_count"],
            status=row["status"],
        )

    @staticmethod
    def _row_to_chunk(row: sqlite3.Row) -> KnowledgeChunk:
        return KnowledgeChunk(
            chunk_id=row["chunk_id"],
            doc_id=row["doc_id"],
            tenant_id=row["tenant_id"],
            workspace_id=row["workspace_id"],
            collection_id=row["collection_id"],
            source_file=row["source_file"],
            file_name=row["file_name"],
            content=row["content"],
            page_number=row["page_number"],
            section_path=json.loads(row["section_path"] or "[]"),
            element_type=row["element_type"],
            bbox=json.loads(row["bbox"] or "null"),
            created_at=row["created_at"],
            acl_hash=row["acl_hash"],
            parser_version=row["parser_version"],
            chunker_version=row["chunker_version"],
            embedding_provider=row["embedding_provider"],
            embedding_model=row["embedding_model"],
            embedding_dimension=row["embedding_dimension"],
            embedding_revision=row["embedding_revision"],
        )

    def close(self) -> None:
        self._conn.close()


def _cosine(a: Iterable[float], b: Iterable[float]) -> float:
    aa = list(a)
    bb = list(b)
    if not aa or not bb or len(aa) != len(bb):
        return 0.0
    dot = sum(x * y for x, y in zip(aa, bb))
    na = math.sqrt(sum(x * x for x in aa))
    nb = math.sqrt(sum(y * y for y in bb))
    if not na or not nb:
        return 0.0
    return dot / (na * nb)
