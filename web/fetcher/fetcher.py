"""网页抓取器 — HTTP 请求 + 安全检查 + 正文提取。

流程：
1. NetworkPolicy 校验 URL 安全性和限流
2. httpx 发起 HTTP GET（支持 SSL 回退）
3. 响应体截断（防 OOM）
4. HtmlExtractor 提取正文转 Markdown
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
