"""网页抓取器 — HTTP 请求 + 安全检查 + 正文提取。

流程：
1. NetworkPolicy 校验 URL 安全性和限流
2. httpx 发起 HTTP GET（支持 SSL 回退）
3. 响应体截断（防 OOM）
4. HtmlExtractor 提取正文转 Markdown
5. 正文为空时降级提取 meta 信息（SPA 页面兜底）
"""

import logging
from typing import Optional

import httpx

from web.fetcher.extractor import HtmlExtractor
from web.fetcher.policy import NetworkPolicy

logger = logging.getLogger(__name__)


class WebFetcher:
    """网页抓取器。"""

    def __init__(
        self,
        max_size: int = 5 * 1024 * 1024,
        timeout: int = 30,
        policy: Optional[NetworkPolicy] = None,
    ):
        self._policy = policy or NetworkPolicy()
        self._extractor = HtmlExtractor()
        self._max_size = max_size
        self._timeout = timeout

    def fetch(self, url: str, extract_content: bool = True) -> dict:
        """抓取网页内容。

        Args:
            url: 目标 URL
            extract_content: 是否提取正文（False 返回原始 HTML）

        Returns:
            {"title": str, "content": str, "url": str}

        Raises:
            RuntimeError: URL 不安全或请求失败
        """
        # 安全检查
        safe, reason = self._policy.validate_url(url)
        if not safe:
            raise RuntimeError(f"URL 安全检查失败: {reason}")

        allowed, reason = self._policy.check_rate_limit(url)
        if not allowed:
            raise RuntimeError(reason)

        # HTTP 请求
        headers = {"User-Agent": "Mozilla/5.0 (compatible; WeaveMind/1.0)"}
        resp = self._do_request(url, headers)

        # 截断大响应
        content = resp.text[: self._max_size]

        # 提取标题
        title = self._extract_title(content)

        # 提取正文
        if extract_content:
            extracted = self._extractor.extract(content)

            # 正文为空时降级提取 meta 信息（SPA 页面兜底）
            if not extracted or extracted.startswith("未提取到正文"):
                meta_info = self._extract_meta_fallback(content)
                if meta_info:
                    extracted = (
                        f"⚠ 该页面为 JS 渲染的 SPA 应用，无法提取正文内容。\n"
                        f"建议使用 WebSearch 搜索该网站信息，不要重试 WebFetch。\n\n"
                        f"从页面 HTML 源码中提取的元信息：\n{meta_info}"
                    )
                else:
                    extracted = (
                        "⚠ 该页面为 JS 渲染或防爬页面，无法提取任何内容。\n"
                        "请改用 WebSearch 搜索该网站信息，不要重试 WebFetch。"
                    )

            return {"title": title, "content": extracted, "url": str(resp.url)}
        else:
            return {"title": title, "content": content, "url": str(resp.url)}

    def _do_request(self, url: str, headers: dict) -> httpx.Response:
        """执行 HTTP 请求，SSL 失败时回退。"""
        try:
            resp = httpx.get(
                url,
                headers=headers,
                follow_redirects=True,
                timeout=self._timeout,
            )
            resp.raise_for_status()
            return resp
        except httpx.HTTPError as e:
            if "ssl" not in str(e).lower():
                raise RuntimeError(f"WebFetch 请求失败: {e}") from e
            logger.warning("SSL 错误，回退到 verify=False: %s", url)

        try:
            resp = httpx.get(
                url,
                headers=headers,
                follow_redirects=True,
                timeout=self._timeout,
                verify=False,
            )
            resp.raise_for_status()
            return resp
        except httpx.HTTPError as e:
            raise RuntimeError(f"WebFetch 请求失败(SSL 回退后仍失败): {e}") from e

    @staticmethod
    def _extract_title(html: str) -> str:
        """从 HTML 提取标题。"""
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        title_tag = soup.find("title")
        return title_tag.get_text(strip=True) if title_tag else ""

    @staticmethod
    def _extract_meta_fallback(html: str) -> str:
        """SPA 页面降级：从 HTML 源码提取 meta 信息。

        提取：<title>、<meta description>、<meta og:title/description/url>、
        <noscript> 内容、<link rel="alternate"> 等。
        """
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")

        parts = []

        # title
        title = soup.find("title")
        if title and title.get_text(strip=True):
            parts.append(f"标题: {title.get_text(strip=True)}")

        # meta description
        desc = soup.find("meta", attrs={"name": "description"})
        if desc and desc.get("content"):
            parts.append(f"描述: {desc['content']}")

        # Open Graph 标签
        for tag_name in ["og:title", "og:description", "og:site_name", "og:url"]:
            og = soup.find("meta", attrs={"property": tag_name})
            if og and og.get("content"):
                label = tag_name.replace("og:", "")
                parts.append(f"{label}: {og['content']}")

        # noscript 内容（SPA 页面为搜索引擎提供的备用内容）
        noscript = soup.find("noscript")
        if noscript:
            ns_text = noscript.get_text(strip=True)
            if ns_text and len(ns_text) > 10:
                preview = ns_text[:300]
                parts.append(f"noscript 内容: {preview}")

        # link rel="alternate"（RSS、sitemap 等）
        for link in soup.find_all("link", rel="alternate"):
            href = link.get("href", "")
            type_ = link.get("type", "")
            if href:
                parts.append(f"alternate({type_}): {href}")

        return "\n".join(parts) if parts else ""
