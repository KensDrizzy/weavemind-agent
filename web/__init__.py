"""联网搜索模块 — 搜索引擎 Provider + 网页抓取。"""

from web.models import SearchResult
from web.providers.factory import SearchProviderFactory
from web.fetcher.fetcher import WebFetcher

__all__ = ["SearchResult", "SearchProviderFactory", "WebFetcher"]
