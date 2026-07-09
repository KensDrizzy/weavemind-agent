"""Data models for user document knowledge RAG."""

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


ElementType = Literal["title", "paragraph", "list", "table", "image", "code", "page"]


class ParsedElement(BaseModel):
    """Structured element emitted by a document parser."""

    text: str
    element_type: ElementType = "paragraph"
    page_number: Optional[int] = None
    section_path: list[str] = Field(default_factory=list)
    bbox: Optional[list[float]] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class KnowledgeDocument(BaseModel):
    """Metadata for an indexed knowledge document."""

    doc_id: str
    tenant_id: str = "default"
    workspace_id: str = "default"
    collection_id: str = "default"
    source_file: str
    file_name: str
    file_hash: str
    mime_type: str = "text/plain"
    created_at: float
    updated_at: float
    parser_version: str = "plain-text@1"
    chunker_version: str = "structural@1"
    embedding_provider: str = "hash"
    embedding_model: str = "hash-embedding"
    embedding_dimension: int = 384
    embedding_revision: str = "1"
    acl_hash: str = "public"
    chunk_count: int = 0
    status: str = "succeeded"

    def display_name(self) -> str:
        return f"{self.file_name} ({self.collection_id})"


class KnowledgeChunk(BaseModel):
    """A source-grounded document chunk."""

    chunk_id: str
    doc_id: str
    tenant_id: str = "default"
    workspace_id: str = "default"
    collection_id: str = "default"
    source_file: str
    file_name: str
    content: str
    page_number: Optional[int] = None
    section_path: list[str] = Field(default_factory=list)
    element_type: ElementType = "paragraph"
    bbox: Optional[list[float]] = None
    created_at: float = 0.0
    acl_hash: str = "public"
    parser_version: str = "plain-text@1"
    chunker_version: str = "structural@1"
    embedding_provider: str = "hash"
    embedding_model: str = "hash-embedding"
    embedding_dimension: int = 384
    embedding_revision: str = "1"

    def to_metadata(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "workspace_id": self.workspace_id,
            "collection_id": self.collection_id,
            "doc_id": self.doc_id,
            "source_file": self.source_file,
            "file_name": self.file_name,
            "page_number": self.page_number,
            "section_path": " > ".join(self.section_path),
            "element_type": self.element_type,
            "bbox": self.bbox,
            "created_at": self.created_at,
            "acl_hash": self.acl_hash,
            "parser_version": self.parser_version,
            "chunker_version": self.chunker_version,
            "embedding_provider": self.embedding_provider,
            "embedding_model": self.embedding_model,
            "embedding_dimension": self.embedding_dimension,
            "embedding_revision": self.embedding_revision,
        }

    def citation(self) -> str:
        page = f" p.{self.page_number}" if self.page_number else ""
        section = f" · {' > '.join(self.section_path)}" if self.section_path else ""
        return f"[{self.file_name}{page}{section}]"


class KnowledgeSearchResult(BaseModel):
    """Search result with hybrid scores and citation metadata."""

    chunk: KnowledgeChunk
    score: float
    semantic_score: float = 0.0
    keyword_score: float = 0.0
    source: str = "hybrid"

    def format_for_llm(self, max_chars: int = 1200) -> str:
        content = self.chunk.content
        if len(content) > max_chars:
            content = content[:max_chars] + "\n... (已截断)"
        return (
            f"{self.chunk.citation()} score={self.score:.3f} source={self.source}\n"
            f"{content}"
        )


class KnowledgeIndexStats(BaseModel):
    total_documents: int = 0
    total_chunks: int = 0
    indexed_chunks: int = 0
    skipped_documents: int = 0
    failed_documents: int = 0
    index_time: float = 0.0
