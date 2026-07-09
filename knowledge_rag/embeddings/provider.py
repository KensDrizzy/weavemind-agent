"""Embedding provider abstraction for knowledge RAG."""

from __future__ import annotations

import hashlib
import logging
import math
import os
import re
from abc import ABC, abstractmethod

import settings

logger = logging.getLogger(__name__)


class EmbeddingProvider(ABC):
    provider = "base"
    model = "base"
    dimension = 384
    revision = "1"

    @abstractmethod
    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Embed texts into dense vectors."""

    def embed_query(self, text: str) -> list[float]:
        return self.embed_texts([text])[0]


class HashEmbeddingProvider(EmbeddingProvider):
    """Deterministic local embedding fallback.

    It is not meant to beat real embedding models; it keeps local tests,
    demos and fallback retrieval functional without network credentials.
    """

    provider = "hash"
    model = "hash-embedding"
    revision = "1"

    def __init__(self, dimension: int = 384):
        self.dimension = dimension

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        tokens = _tokens(text)
        if not tokens:
            return vector
        for token in tokens:
            digest = hashlib.md5(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimension
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        return [v / norm for v in vector]


class LangChainEmbeddingProvider(EmbeddingProvider):
    """Thin adapter around LangChain embeddings used by the existing code RAG."""

    def __init__(self):
        provider = settings.get("knowledge_rag.embedding.provider", settings.get("rag.embedding.provider", "openai"))
        model = settings.get("knowledge_rag.embedding.model", settings.get("rag.embedding.model", "text-embedding-3-small"))
        self.provider = provider
        self.model = model
        self.revision = str(settings.get("knowledge_rag.embedding.revision", "1"))

        if provider == "ollama":
            from langchain_ollama import OllamaEmbeddings
            base_url = settings.get("knowledge_rag.embedding.base_url", settings.get("rag.embedding.base_url", "http://localhost:11434"))
            self._impl = OllamaEmbeddings(model=model, base_url=base_url)
        else:
            from langchain_openai import OpenAIEmbeddings
            kwargs = {"model": model, "check_embedding_ctx_length": False}
            base_url = settings.get("knowledge_rag.embedding.base_url", settings.get("rag.embedding.base_url", None))
            api_key = settings.get("knowledge_rag.embedding.api_key", settings.get("rag.embedding.api_key", None))
            api_key_env = settings.get("knowledge_rag.embedding.api_key_env", settings.get("rag.embedding.api_key_env", None))
            if base_url:
                kwargs["base_url"] = base_url
            if api_key:
                kwargs["api_key"] = api_key
            elif api_key_env:
                env_value = os.environ.get(api_key_env)
                if env_value:
                    kwargs["api_key"] = env_value
            self._impl = OpenAIEmbeddings(**kwargs)
        self.dimension = int(settings.get("knowledge_rag.embedding.dimension", 1536))

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return self._impl.embed_documents(texts)

    def embed_query(self, text: str) -> list[float]:
        return self._impl.embed_query(text)


def create_embedding_provider() -> EmbeddingProvider:
    """Create embedding provider for knowledge RAG.

    Defaults to a real model (LangChain adapter) so production deployments
    get meaningful semantic search. "hash" remains available as an explicit
    offline fallback and for tests.
    """
    provider = settings.get("knowledge_rag.embedding.provider", "openai")
    if provider in {"hash", "local_hash"}:
        return HashEmbeddingProvider(
            dimension=int(settings.get("knowledge_rag.embedding.dimension", 384))
        )
    try:
        return LangChainEmbeddingProvider()
    except Exception:
        logger.warning(
            "真实 embedding provider %s 初始化失败，回退到 hash embedding", provider
        )
        return HashEmbeddingProvider(
            dimension=int(settings.get("knowledge_rag.embedding.dimension", 384))
        )


def _tokens(text: str) -> list[str]:
    latin = re.findall(r"[A-Za-z0-9_]{2,}", text.lower())
    cjk = re.findall(r"[\u4e00-\u9fff]{2,}", text)
    cjk_bigrams: list[str] = []
    for block in cjk:
        cjk_bigrams.extend(block[i:i + 2] for i in range(max(len(block) - 1, 0)))
    return latin + cjk + cjk_bigrams
