from tools.base import WeaveMindTool
import os


class WebSearchTool(WeaveMindTool):
    name: str = "WebSearch"
    description: str = "Search the web via Tavily. Args: query"

    def _run(self, query: str) -> str:
        try:
            from tavily import TavilyClient
        except Exception as e:
            raise RuntimeError("WebSearch 不可用：缺少 tavily-python 依赖") from e

        api_key = os.environ.get("TAVILY_API_KEY")
        if not api_key:
            raise RuntimeError("WebSearch 不可用：未设置环境变量 TAVILY_API_KEY")

        client = TavilyClient(api_key=api_key)
        try:
            results = client.search(query, max_results=5)
        except Exception as e:
            raise RuntimeError(f"WebSearch 请求失败: {e}") from e
        return "\n\n".join(
            f"[{r['title']}]({r['url']})\n{r['content']}"
            for r in results.get("results", [])
        )
