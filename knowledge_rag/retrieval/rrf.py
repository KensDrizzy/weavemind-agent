"""Reciprocal Rank Fusion for hybrid knowledge retrieval."""

from knowledge_rag.models import KnowledgeSearchResult


def rrf_fuse(
    semantic_results: list[KnowledgeSearchResult],
    keyword_results: list[KnowledgeSearchResult],
    top_k: int,
    k: int = 60,
) -> list[KnowledgeSearchResult]:
    merged: dict[str, KnowledgeSearchResult] = {}

    for rank, result in enumerate(semantic_results, 1):
        entry = merged.get(result.chunk.chunk_id)
        if entry is None:
            entry = result.model_copy(deep=True)
            entry.score = 0.0
            merged[result.chunk.chunk_id] = entry
        entry.semantic_score = max(entry.semantic_score, result.semantic_score)
        entry.score += 1.0 / (k + rank)

    for rank, result in enumerate(keyword_results, 1):
        entry = merged.get(result.chunk.chunk_id)
        if entry is None:
            entry = result.model_copy(deep=True)
            entry.score = 0.0
            merged[result.chunk.chunk_id] = entry
        else:
            entry.source = "hybrid"
        entry.keyword_score = max(entry.keyword_score, result.keyword_score)
        entry.score += 1.0 / (k + rank)

    for result in merged.values():
        if result.semantic_score > 0 and result.keyword_score > 0:
            result.score += 0.005
    return sorted(merged.values(), key=lambda r: -r.score)[:top_k]
