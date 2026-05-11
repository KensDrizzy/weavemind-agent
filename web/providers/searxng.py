"""SearXNG 搜索引擎 Provider（自部署，免费）。"""

import logging
import os
from typing import List

import httpx

from web.models import SearchResult
from web.providers.base import SearchProvider

logger = logging.getLogger(__name__)


class SearXNGProvider(SearchProvider):
    """SearXNG 搜索引擎 Provider。

    自部署的元搜索引擎，聚合 Google/Bing/DuckDuckGo 等多引擎结果。
    需要设置环境变量 SEARXNG_URL 指向实例地址。
    """

    def name(self) -> str:
        return "searxng"

    def is_ready(self) -> bool:
        return bool(os.environ.get("SEARXNG_URL"))

    def unavailable_hint(self) -> str:
        return (
            "WebSearch 不可用：请部署 SearXNG 并设置 SEARXNG_URL\n"
            "  docker run --rm -p 8888:8888 searxng/searxng\n"
            "  export SEARXNG_URL=http://localhost:8888"
        )

    def search(self, query: str, top_k: int = 5) -> List[SearchResult]:
        base_url = os.environ["SEARXNG_URL"].rstrip("/")

        try:
            resp = httpx.get(
                f"{base_url}/search",
                params={"q": query, "format": "json", "categories": "general"},
                timeout=30,
            )
            resp.raise_for_status()
        except httpx.HTTPError as e:
            logger.error("SearXNG 搜索失败: %s", e)
            raise RuntimeError(f"SearXNG 搜索失败: {e}") from e

        data = resp.json()
        results = []
        for item in data.get("results", [])[:top_k]:
            results.append(SearchResult(
                title=item.get("title", ""),
                url=item.get("url", ""),
                content=item.get("content", ""),
            ))
        return results
