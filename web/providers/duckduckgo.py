"""DuckDuckGo 搜索引擎 Provider（免费，无需 API Key）。"""

import logging
from typing import List

from web.models import SearchResult
from web.providers.base import SearchProvider

logger = logging.getLogger(__name__)


class DuckDuckGoProvider(SearchProvider):
    """DuckDuckGo 搜索引擎 Provider。

    完全免费，无需 API Key。通过 duckduckgo-search 库调用。
    适合无 Tavily/SearXNG 时的开箱即用场景。
    """

    def name(self) -> str:
        return "duckduckgo"

    def is_ready(self) -> bool:
        try:
            import duckduckgo_search  # noqa: F401
            return True
        except ImportError:
            return False

    def unavailable_hint(self) -> str:
        return (
            "WebSearch 不可用：请安装 duckduckgo-search\n"
            "  pip install duckduckgo-search"
        )

    def search(self, query: str, top_k: int = 5) -> List[SearchResult]:
        from duckduckgo_search import DDGS

        results = []
        try:
            with DDGS() as ddgs:
                for item in ddgs.text(query, max_results=top_k):
                    results.append(SearchResult(
                        title=item.get("title", ""),
                        url=item.get("href", ""),
                        content=item.get("body", ""),
                    ))
        except Exception as e:
            logger.error("DuckDuckGo 搜索失败: %s", e)
            raise RuntimeError(f"DuckDuckGo 搜索失败: {e}") from e

        return results
