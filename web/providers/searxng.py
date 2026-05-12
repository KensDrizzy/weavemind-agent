"""SearXNG 搜索引擎 Provider（自部署，免费）。"""

import logging
import os
from typing import List

import httpx
import settings

from web.models import SearchResult
from web.providers.base import SearchProvider

logger = logging.getLogger(__name__)


class SearXNGProvider(SearchProvider):
    """SearXNG 搜索引擎 Provider。

    自部署的元搜索引擎，聚合 Google/Bing/DuckDuckGo 等多引擎结果。
    """

    def name(self) -> str:
        return "searxng"

    def _get_url(self) -> str:
        """获取 SearXNG 地址，优先从配置文件读取，回退到环境变量。"""
        return settings.get("web.search.searxng.url") or os.environ.get("SEARXNG_URL", "")

    def is_ready(self) -> bool:
        return bool(self._get_url())

    def unavailable_hint(self) -> str:
        return (
            "WebSearch 不可用：请配置 SearXNG 地址\n"
            "  方式1: 在 config.yaml 中设置 web.search.searxng.url\n"
            "  方式2: 设置环境变量 export SEARXNG_URL=http://localhost:8888\n"
            "  部署命令: docker run --rm -p 8888:8888 searxng/searxng"
        )

    def search(self, query: str, top_k: int = 5) -> List[SearchResult]:
        base_url = self._get_url().rstrip("/")

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
