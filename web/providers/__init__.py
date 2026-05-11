"""搜索引擎 Provider 抽象层。"""

from web.providers.base import SearchProvider
from web.providers.factory import SearchProviderFactory

__all__ = ["SearchProvider", "SearchProviderFactory"]
