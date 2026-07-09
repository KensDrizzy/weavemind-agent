"""Context packing for AskKnowledge."""

from knowledge_rag.models import KnowledgeSearchResult


def pack_context(results: list[KnowledgeSearchResult], max_chars: int = 8000) -> str:
    parts: list[str] = []
    used = 0
    for index, result in enumerate(results, 1):
        block = f"[{index}] {result.format_for_llm(max_chars=1600)}"
        if used + len(block) > max_chars and parts:
            break
        parts.append(block)
        used += len(block)
    return "\n\n".join(parts)
