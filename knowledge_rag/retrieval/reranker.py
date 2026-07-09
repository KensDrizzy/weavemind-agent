"""Re-ranker for knowledge RAG results.

Heavily inspired by rag.retrieval_enhancements.ResultReranker but adapted to
KnowledgeSearchResult so we don't leak code-RAG specific attributes into the
knowledge pipeline.
"""

from __future__ import annotations

import logging
import math
import re
from typing import Iterable, List, Optional

import settings
from knowledge_rag.models import KnowledgeSearchResult

logger = logging.getLogger(__name__)


class KnowledgeReranker:
    """Re-rank knowledge retrieval candidates.

    Methods:
    - heuristic: deterministic local scoring fallback.
    - cross_encoder: uses sentence_transformers.CrossEncoder if installed.
    - llm: asks the configured chat model to score candidates.
    """

    def __init__(self):
        self.enabled = settings.get("knowledge_rag.retrieval.rerank.enabled", True)
        self.method = settings.get("knowledge_rag.retrieval.rerank.method", "heuristic")
        self.top_n = int(settings.get("knowledge_rag.retrieval.rerank.top_n", 20))
        self.model = settings.get(
            "knowledge_rag.retrieval.rerank.model",
            "cross-encoder/ms-marco-MiniLM-L-6-v2",
        )
        self._cross_encoder = None
        self._llm = None

    def rerank(
        self,
        query: str,
        results: List[KnowledgeSearchResult],
        top_k: int,
        query_variants: Optional[List[str]] = None,
    ) -> List[KnowledgeSearchResult]:
        if not results:
            return []
        if not self.enabled:
            return results[:top_k]

        candidates = results[: max(top_k, self.top_n)]
        if self.method == "cross_encoder":
            ranked = self._cross_encoder_rerank(query, candidates)
        elif self.method == "llm":
            ranked = self._llm_rerank(query, candidates)
        else:
            ranked = self._heuristic_rerank(query, candidates, query_variants)

        return ranked[:top_k]

    def _cross_encoder_rerank(
        self, query: str, results: List[KnowledgeSearchResult]
    ) -> List[KnowledgeSearchResult]:
        try:
            model = self._get_cross_encoder()
            if not model:
                return self._heuristic_rerank(query, results)
            pairs = [(query, self._candidate_text(r)) for r in results]
            scores = list(model.predict(pairs))
            normalized = _minmax(scores)
            for result, score in zip(results, normalized):
                result.score = 0.3 * result.score + 0.7 * score
            return sorted(results, key=lambda x: -x.score)
        except Exception as e:
            logger.debug("Cross-Encoder rerank failed: %s", e)
            return self._heuristic_rerank(query, results)

    def _llm_rerank(
        self, query: str, results: List[KnowledgeSearchResult]
    ) -> List[KnowledgeSearchResult]:
        try:
            llm = self._get_llm()
            if not llm:
                return self._heuristic_rerank(query, results)
            from langchain_core.messages import HumanMessage, SystemMessage

            payload = []
            for idx, result in enumerate(results):
                chunk = result.chunk
                payload.append(
                    {
                        "id": idx,
                        "file": chunk.file_name,
                        "section": " > ".join(chunk.section_path),
                        "page": chunk.page_number,
                        "type": chunk.element_type,
                        "content": chunk.content[:900],
                    }
                )

            messages = [
                SystemMessage(
                    content=(
                        "You are re-ranking knowledge base search results. "
                        "Score each candidate for relevance to the query. "
                        'Return only JSON as an array of objects: [{"id": 0, "score": 0.95}].'
                    )
                ),
                HumanMessage(
                    content=json.dumps(
                        {"query": query, "candidates": payload},
                        ensure_ascii=False,
                    )
                ),
            ]
            response = llm.invoke(messages)
            parsed = _extract_json_array(getattr(response, "content", str(response)))
            score_by_id = {}
            for item in parsed:
                if not isinstance(item, dict):
                    continue
                idx = item.get("id")
                score = item.get("score")
                if isinstance(idx, int) and isinstance(score, (int, float)):
                    score_by_id[idx] = max(0.0, min(1.0, float(score)))

            if not score_by_id:
                return self._heuristic_rerank(query, results)

            for idx, result in enumerate(results):
                llm_score = score_by_id.get(idx, 0.0)
                result.score = 0.25 * result.score + 0.75 * llm_score
            return sorted(results, key=lambda x: -x.score)
        except Exception as e:
            logger.debug("LLM rerank failed: %s", e)
            return self._heuristic_rerank(query, results)

    def _heuristic_rerank(
        self,
        query: str,
        results: List[KnowledgeSearchResult],
        query_variants: Optional[List[str]] = None,
    ) -> List[KnowledgeSearchResult]:
        terms = _query_terms(" ".join(query_variants or [query]))
        for result in results:
            heuristic = self._heuristic_score(result, terms)
            result.score = 0.7 * result.score + 0.3 * heuristic
        return sorted(results, key=lambda x: -x.score)

    @staticmethod
    def _heuristic_score(result: KnowledgeSearchResult, terms: set[str]) -> float:
        chunk = result.chunk
        name_text = f"{chunk.file_name} {' > '.join(chunk.section_path)}"
        content_text = chunk.content[:4000]

        name_terms = _query_terms(name_text)
        content_terms = _query_terms(content_text)

        if not terms:
            return 0.0

        name_overlap = len(terms & name_terms) / len(terms)
        content_overlap = len(terms & content_terms) / len(terms)
        type_boost = {
            "title": 0.10,
            "table": 0.07,
            "list": 0.05,
        }.get(chunk.element_type, 0.0)

        score = 0.35 * name_overlap + 0.65 * content_overlap
        return max(0.0, min(1.0, score + type_boost))

    @staticmethod
    def _candidate_text(result: KnowledgeSearchResult) -> str:
        chunk = result.chunk
        parts = [
            chunk.file_name,
            " > ".join(chunk.section_path),
            chunk.element_type,
            chunk.content,
        ]
        return "\n".join(p for p in parts if p)

    def _get_cross_encoder(self):
        if self._cross_encoder is not None:
            return self._cross_encoder
        try:
            from sentence_transformers import CrossEncoder

            self._cross_encoder = CrossEncoder(self.model)
        except Exception as e:
            logger.debug("Create Cross-Encoder failed: %s", e)
            self._cross_encoder = False
        return self._cross_encoder or None

    def _get_llm(self):
        if self._llm is not None:
            return self._llm
        try:
            from core.llm_factory import create_llm

            provider = settings.get("knowledge_rag.retrieval.rerank.provider", None)
            model = settings.get("knowledge_rag.retrieval.rerank.llm_model", None)
            self._llm = create_llm(provider=provider, model=model, max_tokens=1024)
        except Exception as e:
            logger.debug("Create rerank LLM failed: %s", e)
            self._llm = False
        return self._llm or None


def _extract_json_array(text: str) -> list:
    import json

    text = (text or "").strip()
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        pass

    match = re.search(r"\[[\s\S]*\]", text)
    if not match:
        return []
    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        return []


def _query_terms(text: str) -> set[str]:
    terms = set()
    for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*|[一-鿿]+", text):
        if len(token) <= 1:
            continue
        parts = [token.lower()]
        if "_" in token:
            parts.extend(p.lower() for p in token.split("_") if len(p) > 1)
        parts.extend(_split_camel_for_terms(token))
        terms.update(parts)
    return terms


def _split_camel_for_terms(token: str) -> List[str]:
    pieces = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", token).split()
    return [p.lower() for p in pieces if len(p) > 1]


def _minmax(scores: Iterable[float]) -> List[float]:
    values = [float(x) for x in scores]
    if not values:
        return []
    low = min(values)
    high = max(values)
    if math.isclose(low, high):
        return [0.5 for _ in values]
    return [(x - low) / (high - low) for x in values]
