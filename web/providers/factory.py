"""搜索引擎工厂：根据配置和环境变量自动选择 Provider。

选择逻辑：
1. config.yaml 中 web.search.provider 显式指定 → 优先
2. TAVILY_API_KEY 已配 → tavily
3. GLM_API_KEY / ZHIPU_API_KEY 已配 → zhipu
4. SERPAPI_KEY 已配 → serpapi
5. SEARXNG_URL 已配 → searxng
6. duckduckgo-search 已安装 → duckduckgo
7. 默认 → tavily（占位，调用时会友好提示）
"""

import logging
from typing import Optional

import settings
from web.providers.base import SearchProvider

logger = logging.getLogger(__name__)

# Provider 名称 → 模块路径映射
_PROVIDER_MAP = {
    "tavily": "web.providers.tavily.TavilyProvider",
    "zhipu": "web.providers.zhipu.ZhipuProvider",
    "serpapi": "web.providers.serpapi.SerpApiProvider",
    "searxng": "web.providers.searxng.SearXNGProvider",
    "duckduckgo": "web.providers.duckduckgo.DuckDuckGoProvider",
}


class SearchProviderFactory:
    """搜索引擎工厂：懒加载 + 自动检测。"""

    _instance: Optional[SearchProvider] = None

    @classmethod
    def create(cls, provider_name: Optional[str] = None) -> SearchProvider:
        """创建或返回缓存的 Provider 实例。"""
        if cls._instance is not None:
            return cls._instance

        name = provider_name or settings.get("web.search.provider", "auto")

        if name != "auto":
            cls._instance = cls._create_by_name(name)
            return cls._instance

        # 自动检测：按优先级尝试各 Provider
        for candidate in ["tavily", "zhipu", "serpapi", "searxng", "duckduckgo"]:
            provider = cls._create_by_name(candidate)
            if provider.is_ready():
                logger.info("自动选择搜索引擎: %s", candidate)
                cls._instance = provider
                return provider

        # 无可用 Provider，返回 Tavily 占位（调用时会友好提示）
        logger.warning("无可用的搜索引擎 Provider，使用 Tavily 占位")
        from web.providers.tavily import TavilyProvider
        cls._instance = TavilyProvider()
        return cls._instance

    @classmethod
    def reset(cls):
        """重置缓存（测试用）。"""
        cls._instance = None

    @staticmethod
    def _create_by_name(name: str) -> SearchProvider:
        """按名称创建 Provider 实例（懒加载导入）。"""
        name = name.lower().strip()
        module_path = _PROVIDER_MAP.get(name)
        if not module_path:
            available = ", ".join(_PROVIDER_MAP.keys())
            raise ValueError(f"未知的搜索引擎: {name}（支持: {available}）")

        module_name, class_name = module_path.rsplit(".", 1)
        import importlib
        module = importlib.import_module(module_name)
        return getattr(module, class_name)()
