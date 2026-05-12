"""搜索引擎 Provider 抽象层。"""

from web.providers.base import SearchProvider
from web.providers.factory import SearchProviderFactory

__all__ = ["SearchProvider", "SearchProviderFactory"]

# 懒加载导入，避免缺少 SDK 时启动报错
_PROVIDER_REGISTRY = {
    "tavily": "web.providers.tavily.TavilyProvider",
    "zhipu": "web.providers.zhipu.ZhipuProvider",
    "serpapi": "web.providers.serpapi.SerpApiProvider",
    "searxng": "web.providers.searxng.SearXNGProvider",
    "duckduckgo": "web.providers.duckduckgo.DuckDuckGoProvider",
}
