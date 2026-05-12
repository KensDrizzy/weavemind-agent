"""Tavily 搜索引擎 Provider。"""

import logging
import os
from typing import List

import settings
from web.models import SearchResult
from web.providers.base import SearchProvider

logger = logging.getLogger(__name__)


class TavilyProvider(SearchProvider):
    """Tavily 搜索引擎 Provider（AI 优化的搜索 API）。"""

    def name(self) -> str:
        return "tavily"

    def _get_api_key(self) -> str:
        """获取 API Key，优先从配置文件读取，回退到环境变量。"""
        return settings.get("web.search.tavily.api_key") or os.environ.get("TAVILY_API_KEY", "")

    def is_ready(self) -> bool:
        return bool(self._get_api_key())

    def unavailable_hint(self) -> str:
        return (
            "WebSearch 不可用：请配置 Tavily API Key\n"
            "  方式1: 在 config.yaml 中设置 web.search.tavily.api_key\n"
            "  方式2: 设置环境变量 export TAVILY_API_KEY=your_key\n"
            "  获取地址: https://tavily.com"
        )

    def search(self, query: str, top_k: int = 5) -> List[SearchResult]:
        from tavily import TavilyClient

        api_key = self._get_api_key()
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
