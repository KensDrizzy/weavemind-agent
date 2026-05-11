from tools.base import WeaveMindTool
import httpx


class WebFetchTool(WeaveMindTool):
    name: str = "WebFetch"
    description: str = "Fetch and extract text from a URL. Args: url, prompt (optional)"

    def _run(self, url: str, prompt: str = "") -> str:
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; WeaveMind/1.0)",
        }
        try:
            resp = httpx.get(url, follow_redirects=True, timeout=30, headers=headers)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            # 部分站点存在 SSL 兼容问题，回退到 verify=False 尝试一次
            if "ssl" not in str(e).lower():
                raise RuntimeError(f"WebFetch 请求失败: {e}") from e
            try:
                resp = httpx.get(
                    url,
                    follow_redirects=True,
                    timeout=30,
                    headers=headers,
                    verify=False,
                )
                resp.raise_for_status()
            except httpx.HTTPError as e2:
                raise RuntimeError(f"WebFetch 请求失败(SSL 回退后仍失败): {e2}") from e2
        # Strip HTML tags minimally
        import re
        text = re.sub(r"<[^>]+>", " ", resp.text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:8000]
