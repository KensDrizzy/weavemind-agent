"""搜索引擎 Provider 抽象基类。

关键设计：
- is_ready() + unavailable_hint() 是防御式设计
- 用户没配 API Key 时不会崩溃，而是友好提示
"""

from abc import ABC, abstractmethod
from typing import List

from web.models import SearchResult


class SearchProvider(ABC):
    """搜索引擎 Provider 抽象基类。"""

    @abstractmethod
    def name(self) -> str:
        """Provider 名称。"""
        ...

    @abstractmethod
    def is_ready(self) -> bool:
        """是否可用（API Key / 服务地址是否配好）。"""
        ...

    @abstractmethod
    def unavailable_hint(self) -> str:
        """不可用时的友好提示。"""
        ...

    @abstractmethod
    def search(self, query: str, top_k: int = 5) -> List[SearchResult]:
        """执行搜索，返回结果列表。"""
        ...
