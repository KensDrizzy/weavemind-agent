"""WebSearch 工具 — 搜索互联网，获取实时信息。

设计决策：
- 搜索和抓取是两个独立工具，不是一个"联网工具"
- 搜索是"我不知道去哪找"，抓取是"我知道 URL，帮我把内容拿回来"
- Agent 根据用户意图自主选择用哪个，也可先搜再抓
"""

import os
from typing import Optional

from tools.base import WeaveMindTool


class WebSearchTool(WeaveMindTool):
    name: str = "WebSearch"
    description: str = (
        "搜索互联网，获取实时信息。"
        "适用场景：用户询问最新版本、官方文档、技术资讯、时事新闻、"
        "人物信息、近期事件等需要最新知识的问题。"
        "当你的训练数据中没有相关信息，或信息可能已过时时，应主动使用此工具。"
        "参数：query（搜索关键词），top_k（返回结果数量，默认5）"
    )

    def _run(self, query: str, top_k: int = 5) -> str:
        # 尝试通过 Provider 工厂获取搜索引擎
        try:
            from web.providers.factory import SearchProviderFactory
            provider = SearchProviderFactory.create()

            if not provider.is_ready():
                return (
                    f"WebSearch 不可用：{provider.unavailable_hint()}\n"
                    "请配置搜索引擎后重试，或直接基于已有知识回答。"
                )

            results = provider.search(query, top_k)

        except ImportError:
            # web 模块不存在，回退到 Tavily 直接调用
            return self._search_tavily(query, top_k)
        except Exception as e:
            # Provider 工厂失败，回退到 Tavily
            return self._search_tavily(query, top_k, fallback_error=str(e))

        if not results:
            return f"未找到与「{query}」相关的结果。建议换用不同关键词重试。"

        # 格式化输出：带编号、标题、链接、摘要
        formatted = []
        for i, r in enumerate(results, 1):
            title = getattr(r, "title", "") if hasattr(r, "title") else r.get("title", "")
            url = getattr(r, "url", "") if hasattr(r, "url") else r.get("url", "")
            content = getattr(r, "content", "") if hasattr(r, "content") else r.get("content", "")
            formatted.append(f"[{i}. {title}]({url})\n{content}")

        return "\n\n---\n\n".join(formatted)

    def _search_tavily(self, query: str, top_k: int = 5, fallback_error: str = "") -> str:
        """Tavily 直接调用回退。"""
        try:
            from tavily import TavilyClient
        except ImportError:
            return (
                "WebSearch 不可用：缺少 tavily-python 依赖。"
                "请运行 pip install tavily-python 安装。"
            )

        # 优先从 config.yaml 读取，回退到环境变量
        import settings
        api_key = settings.get("web.search.tavily.api_key") or os.environ.get("TAVILY_API_KEY", "")
        if not api_key:
            hint = f"（Provider 工厂错误: {fallback_error}）" if fallback_error else ""
            return (
                f"WebSearch 不可用：未设置 Tavily API Key{hint}\n"
                "获取 API Key: https://tavily.com\n"
                "或部署 SearXNG: docker run --rm -p 8888:8888 searxng/searxng"
            )

        try:
            client = TavilyClient(api_key=api_key)
            results = client.search(query, max_results=top_k)
        except Exception as e:
            # 区分 SSL 错误和 API Key 错误
            error_msg = str(e)
            if "ssl" in error_msg.lower() or "SSL" in error_msg:
                return (
                    f"WebSearch 请求失败（SSL 连接问题）：{error_msg}\n"
                    "建议：检查网络连接，或尝试使用其他搜索引擎（SearXNG、DuckDuckGo）"
                )
            return f"WebSearch 请求失败: {e}"

        items = results.get("results", [])
        if not items:
            return f"未找到与「{query}」相关的结果。"

        formatted = []
        for i, r in enumerate(items, 1):
            formatted.append(f"[{i}. {r.get('title', '')}]({r.get('url', '')})\n{r.get('content', '')}")

        return "\n\n---\n\n".join(formatted)
