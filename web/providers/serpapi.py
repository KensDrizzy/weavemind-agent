"""SerpAPI Provider — 通过 SerpAPI 搜索 Google 结果。

SerpAPI 是 Google 搜索的 API 封装，返回结构化的搜索结果。
需要单独注册获取 API Key: https://serpapi.com

Python SDK: google-search-results (pip install google-search-results)
"""

import logging
import os
from typing import List

import settings
from web.models import SearchResult
from web.providers.base import SearchProvider

logger = logging.getLogger(__name__)


class SerpApiProvider(SearchProvider):
    """SerpAPI 搜索 Provider — Google 搜索结果。"""

    def name(self) -> str:
        return "serpapi"

    def _get_api_key(self) -> str:
        """获取 API Key，优先从配置文件读取，回退到环境变量。"""
        return settings.get("web.search.serpapi.api_key") or os.environ.get("SERPAPI_KEY", "")

    def is_ready(self) -> bool:
        return bool(self._get_api_key())

    def unavailable_hint(self) -> str:
        return (
            "WebSearch 不可用：请配置 SerpAPI Key\n"
            "  方式1: 在 config.yaml 中设置 web.search.serpapi.api_key\n"
            "  方式2: 设置环境变量 export SERPAPI_KEY=your_key\n"
            "  获取地址: https://serpapi.com"
        )

    def search(self, query: str, top_k: int = 5) -> List[SearchResult]:
        from serpapi import GoogleSearch

        api_key = self._get_api_key()

        try:
            search = GoogleSearch({
                "q": query,
                "api_key": api_key,
                "num": top_k,
            })
            data = search.get_dict()
        except Exception as e:
            logger.error("SerpAPI 搜索失败: %s", e)
            raise RuntimeError(f"SerpAPI 搜索失败: {e}") from e

        results = []
        # organic_results 是 Google 搜索的主要结果
        for item in data.get("organic_results", [])[:top_k]:
            results.append(SearchResult(
                title=item.get("title", ""),
                url=item.get("link", ""),
                content=item.get("snippet", ""),
            ))

        # knowledge_graph 作为补充
        kg = data.get("knowledge_graph")
        if kg and not results:
            results.append(SearchResult(
                title=kg.get("title", ""),
                url=kg.get("website", ""),
                content=kg.get("description", ""),
            ))

        return results
