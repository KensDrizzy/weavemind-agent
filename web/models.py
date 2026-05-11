"""搜索结果数据模型。"""

from dataclasses import dataclass


@dataclass
class SearchResult:
    """搜索引擎返回的单条结果。"""

    title: str
    url: str
    content: str
