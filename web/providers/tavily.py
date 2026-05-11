"""Tavily 搜索引擎 Provider。"""

import logging
import os
from typing import List

from web.models import SearchResult
from web.providers.base import SearchProvider

logger = logging.getLogger(__name__)


class TavilyProvider(SearchProvider):
    """Tavily 搜索引擎 Provider（AI 优化的搜索 API）。"""

    def name(self) -> str:
        return "tavily"

    def is_ready(self) -> bool:
        return bool(os.environ.get("TAVILY_API_KEY"))

    def unavailable_hint(self) -> str:
        return "WebSearch 不可用：请设置环境变量 TAVILY_API_KEY（https://tavily.com 获取）"

    def search(self, query: str, top_k: int = 5) -> List[SearchResult]:
        from tavily import TavilyClient

        api_key = os.environ["TAVILY_API_KEY"]
        client = TavilyClient(api_key=api_key)

        try:
            response = client.search(query, max_results=top_k)
        except Exception as e:
            logger.error("Tavily 搜索失败: %s", e)
            raise RuntimeError(f"Tavily 搜索失败: {e}") from e

        results = []
        for item in response.get("results", []):
            results.append(SearchResult(
                title=item.get("title", ""),
                url=item.get("url", ""),
                content=item.get("content", ""),
            ))
        return results
