"""Vector store adapter interface."""

from abc import ABC, abstractmethod

from knowledge_rag.models import KnowledgeChunk, KnowledgeSearchResult


class VectorStoreAdapter(ABC):
    @abstractmethod
    def upsert_chunks(self, chunks: list[KnowledgeChunk], vectors: list[list[float]]) -> None:
        """Insert or update chunks and their dense vectors."""

    @abstractmethod
    def search(self, query_vector: list[float], top_k: int, filters: dict | None = None) -> list[KnowledgeSearchResult]:
        """Search vectors with metadata filters."""

    @abstractmethod
    def delete_by_document(self, doc_id: str) -> None:
        """Delete all chunks for a document."""

    @abstractmethod
    def rebuild_collection(self, collection: str) -> None:
        """Rebuild a collection if the backing store supports it."""
