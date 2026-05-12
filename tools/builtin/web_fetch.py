"""WebFetch 工具 — 抓取指定 URL，提取正文转 Markdown。

设计决策：
- 和 WebSearch 是两个独立工具
- WebSearch 是"我不知道去哪找"，WebFetch 是"我知道 URL，帮我把内容拿回来"
- Agent 可以先搜再抓：WebSearch 拿到 URL → WebFetch 抓取详情
- 内置 SSRF 防护和限流，防止 Agent 访问内网或疯狂重试
"""

from tools.base import WeaveMindTool


class WebFetchTool(WeaveMindTool):
    name: str = "WebFetch"
    description: str = (
        "抓取指定 URL 的网页内容，提取正文并转为 Markdown 格式。"
        "适用场景：用户要求查看某个网页内容、读取在线文档、获取 URL 对应的详细信息。"
        "常与 WebSearch 配合使用：先搜索获取 URL，再抓取详情。"
        "注意：仅适用于静态/SSR 页面，JS 渲染的 SPA 页面或反爬站点可能返回空正文。"
        "参数：url（完整 URL），max_chars（最大字符数，默认 8000）"
    )

    def _run(self, url: str, max_chars: int = 8000) -> str:
        # 尝试使用增强版 WebFetcher（有 SSRF 防护 + 正文提取）
        try:
            from web.fetcher.fetcher import WebFetcher
            return self._fetch_enhanced(url, max_chars)
        except ImportError:
            # web 模块不存在，回退到基础版
            return self._fetch_basic(url, max_chars)

    def _fetch_enhanced(self, url: str, max_chars: int) -> str:
        """增强版抓取：SSRF 防护 + 正文提取 + Markdown 转换。"""
        from web.fetcher.fetcher import WebFetcher

        fetcher = WebFetcher()
        try:
            result = fetcher.fetch(url, extract_content=True)
        except RuntimeError as e:
            return f"WebFetch 失败: {e}"

        content = result.get("content", "")
        title = result.get("title", "")

        if not content or content.startswith("未提取到正文"):
            return (
                f"未提取到正文。可能是 JS 渲染页面或防爬站点，当前无法处理。\n"
                f"URL: {url}\n"
                "建议：尝试用 WebSearch 搜索相关信息作为替代。"
            )

        truncated = content[:max_chars]
        if len(content) > max_chars:
            truncated += f"\n\n...（内容已截断，共 {len(content)} 字符）"

        return f"# {title}\n\n{truncated}" if title else truncated

    def _fetch_basic(self, url: str, max_chars: int) -> str:
        """基础版抓取：简单 HTTP 请求 + 正则去标签（回退方案）。"""
        import re
        import httpx

        # 基础 SSRF 防护（即使没有 web 模块也要防）
        from urllib.parse import urlparse
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return f"URL 安全检查失败：禁止访问 {parsed.scheme}:// 协议（仅支持 http/https）"
        host = (parsed.hostname or "").lower()
        if host in ("localhost", "127.0.0.1", "::1") or host.startswith(("192.168.", "10.", "172.16.")):
            return "URL 安全检查失败：禁止访问内网地址"

        headers = {"User-Agent": "Mozilla/5.0 (compatible; WeaveMind/1.0)"}
        try:
            resp = httpx.get(url, follow_redirects=True, timeout=30, headers=headers)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            if "ssl" not in str(e).lower():
                return f"WebFetch 请求失败: {e}"
            try:
                resp = httpx.get(
                    url, follow_redirects=True, timeout=30,
                    headers=headers, verify=False,
                )
                resp.raise_for_status()
            except httpx.HTTPError as e2:
                return f"WebFetch 请求失败(SSL 回退后仍失败): {e2}"

        # 正则去标签（基础版，没有 BeautifulSoup 时的回退）
        text = re.sub(r"<[^>]+>", " ", resp.text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:max_chars]
