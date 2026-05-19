"""Retrieval enhancement utilities for code RAG.

This module keeps query rewriting, result re-ranking, and search caching
separate from the core indexing pipeline so the default RAG path remains small
and easy to reason about.
"""

from __future__ import annotations

import copy
import json
import logging
import math
import re
import time
from collections import OrderedDict
from typing import Iterable, List, Optional

import settings
from rag.models import RetrievalResult

logger = logging.getLogger(__name__)


class QueryRewriter:
    """Builds code-search query variants.

    The rules path is intentionally dependency-free and deterministic. LLM
    rewrite is optional because it is slower and can fail when the provider is
    unavailable.
    """

    _CODE_SYNONYMS = {
        "认证": ["auth", "authenticate", "login"],
        "登录": ["login", "signin", "auth"],
        "授权": ["authorize", "permission", "access"],
        "权限": ["permission", "authorize", "policy"],
        "缓存": ["cache", "cached", "ttl"],
        "检索": ["search", "retrieve", "retrieval"],
        "搜索": ["search", "lookup", "find"],
        "索引": ["index", "indexed", "indexing"],
        "向量": ["vector", "embedding", "semantic"],
        "关键词": ["keyword", "bm25", "fts"],
        "分块": ["chunk", "chunker", "split"],
        "重排": ["rerank", "reranking", "rank"],
        "融合": ["hybrid", "fusion", "merge"],
        "同步": ["sync", "synchronize", "freshness"],
        "增量": ["incremental", "mtime", "md5"],
        "工具": ["tool", "tools"],
        "记忆": ["memory", "remember"],
        "会话": ["session", "conversation"],
        "配置": ["config", "settings"],
        "错误": ["error", "exception", "failure"],
        "失败": ["error", "exception", "failure"],
    }

    def __init__(self):
        self.enabled = settings.get("rag.query_rewrite.enabled", True)
        self.method = settings.get("rag.query_rewrite.method", "rules")
        self.max_queries = int(settings.get("rag.query_rewrite.max_queries", 3))
        self._llm = None

    def rewrite(self, query: str) -> List[str]:
        """Return query variants with the original query first."""
        query = (query or "").strip()
        if not query:
            return []
        if not self.enabled:
            return [query]

        variants = [query]
        variants.extend(self._rule_variants(query))

        if self.method == "llm":
            variants.extend(self._llm_variants(query))

        return self._dedupe(variants)[: max(1, self.max_queries)]

    def _rule_variants(self, query: str) -> List[str]:
        terms = []

        for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", query):
            split = self._split_identifier(token)
            if split and split.lower() != token.lower():
                terms.append(split)

        for needle, expansions in self._CODE_SYNONYMS.items():
            if needle in query:
                terms.extend(expansions)

        variants = []
        if terms:
            expanded = " ".join(self._dedupe(terms))
            variants.append(f"{query} {expanded}")

        compact = re.sub(r"[^\w\u4e00-\u9fff]+", " ", query).strip()
        if compact and compact != query:
            variants.append(compact)

        return variants

    def _llm_variants(self, query: str) -> List[str]:
        try:
            llm = self._get_llm()
            if not llm:
                return []
            from langchain_core.messages import HumanMessage, SystemMessage

            messages = [
                SystemMessage(
                    content=(
                        "You rewrite code-search queries. Return only a JSON "
                        "array of up to 3 concise search queries. Preserve code "
                        "identifiers and add likely English identifiers for "
                        "Chinese terms."
                    )
                ),
                HumanMessage(content=query),
            ]
            response = llm.invoke(messages)
            text = getattr(response, "content", str(response))
            parsed = _extract_json_array(text)
            return [str(x).strip() for x in parsed if str(x).strip()]
        except Exception as e:
            logger.debug(f"LLM query rewrite failed: {e}")
            return []

    def _get_llm(self):
        if self._llm is not None:
            return self._llm
        try:
            from core.llm_factory import create_llm

            provider = settings.get("rag.query_rewrite.provider", None)
            model = settings.get("rag.query_rewrite.model", None)
            self._llm = create_llm(provider=provider, model=model, max_tokens=512)
        except Exception as e:
            logger.debug(f"Create query rewrite LLM failed: {e}")
            self._llm = False
        return self._llm or None

    @staticmethod
    def _split_identifier(token: str) -> str:
        token = token.replace("_", " ")
        token = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", token)
        token = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", token)
        return token.strip()

    @staticmethod
    def _dedupe(values: Iterable[str]) -> List[str]:
        seen = set()
        out = []
        for value in values:
            key = value.strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(value.strip())
        return out


class ResultReranker:
    """Re-ranks retrieved candidates.

    Supported methods:
    - heuristic: deterministic local scoring fallback.
    - cross_encoder: uses sentence_transformers.CrossEncoder if installed.
    - llm: asks the configured chat model to score candidates.
    """

    def __init__(self):
        self.enabled = settings.get("rag.rerank.enabled", True)
        self.method = settings.get("rag.rerank.method", "heuristic")
        self.top_n = int(settings.get("rag.rerank.top_n", 20))
        self.model = settings.get(
            "rag.rerank.model", "cross-encoder/ms-marco-MiniLM-L-6-v2"
        )
        self._cross_encoder = None
        self._llm = None

    def rerank(
        self,
        query: str,
        results: List[RetrievalResult],
        top_k: int,
        query_variants: Optional[List[str]] = None,
    ) -> List[RetrievalResult]:
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
        self, query: str, results: List[RetrievalResult]
    ) -> List[RetrievalResult]:
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
            logger.debug(f"Cross-Encoder rerank failed: {e}")
            return self._heuristic_rerank(query, results)

    def _llm_rerank(
        self, query: str, results: List[RetrievalResult]
    ) -> List[RetrievalResult]:
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
                        "file": chunk.file_path,
                        "symbol": chunk.display_name(),
                        "type": chunk.chunk_type,
                        "lines": f"{chunk.start_line}-{chunk.end_line}",
                        "signature": chunk.signature or "",
                        "content": chunk.content[:900],
                    }
                )

            messages = [
                SystemMessage(
                    content=(
                        "You are re-ranking code search results. Score each "
                        "candidate for relevance to the query. Return only JSON "
                        "as an array of objects: [{\"id\": 0, \"score\": 0.95}]."
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
            logger.debug(f"LLM rerank failed: {e}")
            return self._heuristic_rerank(query, results)

    def _heuristic_rerank(
        self,
        query: str,
        results: List[RetrievalResult],
        query_variants: Optional[List[str]] = None,
    ) -> List[RetrievalResult]:
        terms = _query_terms(" ".join(query_variants or [query]))
        for result in results:
            heuristic = self._heuristic_score(result, terms)
            result.score = 0.7 * result.score + 0.3 * heuristic
        return sorted(results, key=lambda x: -x.score)

    @staticmethod
    def _heuristic_score(result: RetrievalResult, terms: set[str]) -> float:
        chunk = result.chunk
        name_text = f"{chunk.name} {chunk.parent_name or ''} {chunk.signature or ''}"
        path_text = chunk.file_path
        content_text = chunk.content[:4000]

        name_terms = _query_terms(name_text)
        path_terms = _query_terms(path_text)
        content_terms = _query_terms(content_text)

        if not terms:
            return 0.0

        name_overlap = len(terms & name_terms) / len(terms)
        path_overlap = len(terms & path_terms) / len(terms)
        content_overlap = len(terms & content_terms) / len(terms)
        type_boost = {
            "method": 0.10,
            "function": 0.10,
            "class": 0.07,
            "import": 0.02,
        }.get(chunk.chunk_type, 0.0)

        score = 0.45 * name_overlap + 0.15 * path_overlap + 0.40 * content_overlap
        return max(0.0, min(1.0, score + type_boost))

    @staticmethod
    def _candidate_text(result: RetrievalResult) -> str:
        chunk = result.chunk
        parts = [
            chunk.file_path,
            chunk.display_name(),
            chunk.chunk_type,
            chunk.signature or "",
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
            logger.debug(f"Create Cross-Encoder failed: {e}")
            self._cross_encoder = False
        return self._cross_encoder or None

    def _get_llm(self):
        if self._llm is not None:
            return self._llm
        try:
            from core.llm_factory import create_llm

            provider = settings.get("rag.rerank.provider", None)
            model = settings.get("rag.rerank.llm_model", None)
            self._llm = create_llm(provider=provider, model=model, max_tokens=1024)
        except Exception as e:
            logger.debug(f"Create rerank LLM failed: {e}")
            self._llm = False
        return self._llm or None


class SearchCache:
    """Small in-memory TTL + LRU cache for search results."""

    def __init__(self):
        self.enabled = settings.get("rag.cache.enabled", True)
        self.ttl_seconds = int(settings.get("rag.cache.ttl_seconds", 300))
        self.max_entries = int(settings.get("rag.cache.max_entries", 128))
        self._items: OrderedDict[str, tuple[float, str, List[RetrievalResult]]] = (
            OrderedDict()
        )

    def get(self, key: str, index_fingerprint: str) -> Optional[List[RetrievalResult]]:
        if not self.enabled:
            return None
        item = self._items.get(key)
        if not item:
            return None
        expires_at, fingerprint, results = item
        if expires_at < time.time() or fingerprint != index_fingerprint:
            self._items.pop(key, None)
            return None
        self._items.move_to_end(key)
        return copy.deepcopy(results)

    def set(self, key: str, index_fingerprint: str, results: List[RetrievalResult]):
        if not self.enabled or self.max_entries <= 0:
            return
        expires_at = time.time() + max(1, self.ttl_seconds)
        self._items[key] = (expires_at, index_fingerprint, copy.deepcopy(results))
        self._items.move_to_end(key)
        while len(self._items) > self.max_entries:
            self._items.popitem(last=False)

    def clear(self):
        self._items.clear()

    @staticmethod
    def make_key(**parts) -> str:
        return json.dumps(parts, ensure_ascii=False, sort_keys=True)


def _extract_json_array(text: str) -> list:
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
    for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*|[\u4e00-\u9fff]+", text):
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
