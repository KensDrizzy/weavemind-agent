"""WebSearch 工具 — 通过搜索引擎查询互联网信息。

支持三种搜索引擎（自动检测优先级）：
1. Tavily（需 API Key，AI 优化结果）
2. SearXNG（自部署，聚合多引擎）
3. DuckDuckGo（免费，无需配置）
"""

import logging
from typing import Optional

from langchain_core.tools import tool

from web.models import SearchResult
from web.providers.factory import SearchProviderFactory

logger = logging.getLogger(__name__)


@tool
def WebSearch(query: str, top_k: int = 5) -> str:
    """搜索互联网获取最新信息。

    当需要查找最新资讯、技术文档、API 用法、错误解决方案等时使用。
    返回搜索结果列表，每条包含标题、链接和摘要。

    Args:
        query: 搜索关键词，尽量具体以获得更好结果
        top_k: 返回结果数量，默认 5
    """
    provider = SearchProviderFactory.create()

    if not provider.is_ready():
        return provider.unavailable_hint()

    try:
        results: list[SearchResult] = provider.search(query, top_k)
    except Exception as e:
        logger.error("WebSearch 执行失败: %s", e)
        return f"搜索失败: {e}"

    if not results:
        return f"未找到与 \"{query}\" 相关的结果"

    # 格式化输出
    lines = [f"搜索结果（{provider.name()}）:\n"]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r.title}")
        lines.append(f"   {r.url}")
        lines.append(f"   {r.content}\n")

    return "\n".join(lines)
