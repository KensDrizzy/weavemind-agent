"""Keyword store adapter interface."""

from abc import ABC, abstractmethod

from knowledge_rag.models import KnowledgeChunk, KnowledgeSearchResult


class KeywordStoreAdapter(ABC):
    @abstractmethod
    def upsert_chunks(self, chunks: list[KnowledgeChunk]) -> None:
        """Insert or update chunks into a keyword index."""

    @abstractmethod
    def search(self, query: str, top_k: int, filters: dict | None = None) -> list[KnowledgeSearchResult]:
        """Search keyword index with metadata filters."""

    @abstractmethod
    def delete_by_document(self, doc_id: str) -> None:
        """Delete all chunks for a document."""
