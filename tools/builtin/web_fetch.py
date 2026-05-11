"""WebFetch 工具 — 抓取网页内容并提取正文。

特性：
- SSRF 防护（禁止内网地址、危险协议）
- 域名级限流
- HTML 正文提取（去除广告/导航等噪声）
- 自动转 Markdown 格式
"""

import logging

from langchain_core.tools import tool

from web.fetcher.fetcher import WebFetcher

logger = logging.getLogger(__name__)

_fetcher = WebFetcher()


@tool
def WebFetch(url: str, extract_content: bool = True) -> str:
    """抓取网页内容并提取正文。

    用于获取指定 URL 的网页内容。自动清理广告、导航等噪声，
    提取正文并转为 Markdown 格式。

    注意：
    - 不支持需要登录的页面
    - JS 渲染的 SPA 页面可能提取不到正文
    - 被反爬保护的页面可能无法访问

    Args:
        url: 要抓取的网页 URL
        extract_content: 是否提取正文（True=只返回正文，False=返回原始 HTML）
    """
    try:
        result = _fetcher.fetch(url, extract_content=extract_content)
    except RuntimeError as e:
        return str(e)

    title = result["title"]
    content = result["content"]
    final_url = result["url"]

    parts = []
    if title:
        parts.append(f"# {title}\n")
    if final_url != url:
        parts.append(f"（重定向至: {final_url}）\n")
    parts.append(content)

    return "\n".join(parts)
