"""智谱搜索 Provider — 通过 GLM-4 的 web_search 工具实现联网搜索。

智谱的搜索不是独立 API，而是 GLM 模型的内置工具。
调用方式：向 GLM-4 发送消息 + tools=[web_search]，模型会自动搜索并返回结果。

参考: https://open.bigmodel.cn/dev/api/normal-model/glm-4
"""

import json
import logging
import os
from typing import List

import settings
from web.models import SearchResult
from web.providers.base import SearchProvider

logger = logging.getLogger(__name__)


class ZhipuProvider(SearchProvider):
    """智谱搜索 Provider — 利用 GLM-4 的 web_search 内置工具。"""

    def name(self) -> str:
        return "zhipu"

    def _get_api_key(self) -> str:
        """获取 API Key，优先从配置文件读取，回退到环境变量。"""
        return (
            settings.get("web.search.zhipu.api_key")
            or os.environ.get("GLM_API_KEY")
            or os.environ.get("ZHIPU_API_KEY")
            or ""
        )

    def is_ready(self) -> bool:
        return bool(self._get_api_key())

    def unavailable_hint(self) -> str:
        return (
            "WebSearch 不可用：请配置智谱 API Key\n"
            "  方式1: 在 config.yaml 中设置 web.search.zhipu.api_key\n"
            "  方式2: 设置环境变量 export GLM_API_KEY=your_key\n"
            "  获取地址: https://open.bigmodel.cn"
        )

    def search(self, query: str, top_k: int = 5) -> List[SearchResult]:
        from zhipuai import ZhipuAI

        api_key = self._get_api_key()
        client = ZhipuAI(api_key=api_key)

        try:
            response = client.chat.completions.create(
                model="glm-4",
                messages=[{"role": "user", "content": query}],
                tools=[{"type": "web_search", "web_search": {"enable": True}}],
            )
        except Exception as e:
            logger.error("智谱搜索失败: %s", e)
            raise RuntimeError(f"智谱搜索失败: {e}") from e

        results = []
        # 智谱搜索结果在 message 的 content 或 tool_calls 中
        message = response.choices[0].message

        # 方式1：直接从 content 提取（模型搜索后直接回答）
        content = message.content or ""

        # 方式2：从 tool_calls 的搜索结果中提取 URL
        if hasattr(message, "tool_calls") and message.tool_calls:
            for tc in message.tool_calls:
                if tc.type == "web_search":
                    try:
                        search_data = json.loads(tc.function.arguments)
                        items = search_data.get("search_results", [])
                        for item in items[:top_k]:
                            results.append(SearchResult(
                                title=item.get("title", ""),
                                url=item.get("link", item.get("url", "")),
                                content=item.get("content", item.get("snippet", "")),
                            ))
                    except (json.JSONDecodeError, AttributeError):
                        pass

        # 如果没有从 tool_calls 提取到结果，把模型回答作为单条结果
        if not results and content:
            results.append(SearchResult(
                title=f"智谱搜索: {query}",
                url="",
                content=content,
            ))

        return results[:top_k]
