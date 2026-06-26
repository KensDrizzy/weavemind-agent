"""联网搜索功能综合测试 — 覆盖边界情况和集成场景。"""

import time

import pytest
from unittest.mock import MagicMock, patch

from web.models import SearchResult
from web.providers.factory import SearchProviderFactory
from web.providers.tavily import TavilyProvider
from web.providers.searxng import SearXNGProvider
from web.providers.duckduckgo import DuckDuckGoProvider
from web.fetcher.policy import NetworkPolicy
from web.fetcher.extractor import HtmlExtractor
from web.fetcher.fetcher import WebFetcher


# ── NetworkPolicy 边界测试 ──


class TestNetworkPolicyEdgeCases:
    def test_block_ipv6_loopback(self):
        policy = NetworkPolicy()
        safe, _ = policy.validate_url("http://[::1]:8080")
        assert safe is False

    def test_block_private_172_31(self):
        policy = NetworkPolicy()
        safe, _ = policy.validate_url("http://172.31.255.255")
        assert safe is False

    def test_allow_172_32(self):
        """172.32.x.x 不是内网。"""
        policy = NetworkPolicy()
        safe, _ = policy.validate_url("http://172.32.0.1")
        assert safe is True

    def test_block_link_local(self):
        policy = NetworkPolicy()
        safe, _ = policy.validate_url("http://169.254.1.1")
        assert safe is False

    def test_block_data_protocol(self):
        policy = NetworkPolicy()
        safe, reason = policy.validate_url("data:text/html,<h1>hi</h1>")
        assert safe is False

    def test_block_javascript_protocol(self):
        policy = NetworkPolicy()
        safe, _ = policy.validate_url("javascript:alert(1)")
        assert safe is False

    def test_empty_url(self):
        policy = NetworkPolicy()
        safe, _ = policy.validate_url("")
        assert safe is False

    def test_no_scheme(self):
        policy = NetworkPolicy()
        safe, _ = policy.validate_url("example.com")
        assert safe is False

    def test_rate_limit_expires(self):
        """限流窗口过后应允许请求。"""
        policy = NetworkPolicy()
        policy._min_interval = 0.05
        policy.check_rate_limit("https://example.com")
        time.sleep(0.06)
        allowed, _ = policy.check_rate_limit("https://example.com")
        assert allowed is True


# ── HtmlExtractor 边界测试 ──


class TestHtmlExtractorEdgeCases:
    def test_whitespace_only_html(self):
        extractor = HtmlExtractor()
        assert extractor.extract("   \n\t  ") == ""

    def test_main_tag_extraction(self):
        html = """
        <html><body>
        <main>
            <h1>主内容标题</h1>
            <p>这是 main 标签内的正文内容，确保文本量足够长来通过最低要求。</p>
        </main>
        </body></html>
        """
        extractor = HtmlExtractor()
        result = extractor.extract(html)
        assert "主内容标题" in result

    def test_role_main_extraction(self):
        html = """
        <html><body>
        <div role="main">
            <p>这是 role=main 的正文内容，确保文本量足够长来通过最低要求。</p>
        </div>
        </body></html>
        """
        extractor = HtmlExtractor()
        result = extractor.extract(html)
        assert "正文内容" in result

    def test_high_link_density_penalized(self):
        """链接密度高的区域应被惩罚。"""
        html = """
        <html><body>
        <div id="links-area">
            <a href="/1">链接一链接一链接一</a>
            <a href="/2">链接二链接二链接二</a>
            <a href="/3">链接三链接三链接三</a>
            <a href="/4">链接四链接四链接四</a>
            <a href="/5">链接五链接五链接五</a>
        </div>
        <div id="content">
            <p>这是真正的正文内容区域，文字很多但链接很少。我们需要确保这段文字足够长来通过评分检查，并且链接密度低于导航区域。这样评分算法就能正确识别这里是正文。</p>
        </div>
        </body></html>
        """
        extractor = HtmlExtractor()
        result = extractor.extract(html)
        assert "真正的正文" in result

    def test_table_extraction(self):
        html = """
        <html><body><article>
        <p>表格示例，确保文本量足够长来通过最低要求。</p>
        <table>
            <tr><th>名称</th><th>值</th></tr>
            <tr><td>Python</td><td>3.12</td></tr>
        </table>
        </article></body></html>
        """
        extractor = HtmlExtractor()
        result = extractor.extract(html)
        assert "Python" in result
        assert "3.12" in result

    def test_nested_content(self):
        html = """
        <html><body><article>
        <div>
            <h2>嵌套标题</h2>
            <div><p>嵌套段落内容，确保文本量足够长来通过最低要求。</p></div>
        </div>
        </article></body></html>
        """
        extractor = HtmlExtractor()
        result = extractor.extract(html)
        assert "## 嵌套标题" in result
        assert "嵌套段落" in result

    def test_cookie_banner_removed(self):
        html = """
        <html><body>
        <div class="cookie-banner">接受 Cookie</div>
        <article><p>正文内容，确保文本量足够长来通过最低要求。</p></article>
        </body></html>
        """
        extractor = HtmlExtractor()
        result = extractor.extract(html)
        assert "Cookie" not in result
        assert "正文内容" in result

    def test_bold_text(self):
        html = """
        <html><body><article>
        <p>这是<strong>加粗</strong>文本，确保文本量足够长来通过最低要求。</p>
        </article></body></html>
        """
        extractor = HtmlExtractor()
        result = extractor.extract(html)
        assert "**加粗**" in result

    def test_list_items(self):
        html = """
        <html><body><article>
        <p>列表示例，确保文本量足够长来通过最低要求。</p>
        <ul>
            <li>第一项</li>
            <li>第二项</li>
        </ul>
        </article></body></html>
        """
        extractor = HtmlExtractor()
        result = extractor.extract(html)
        assert "- 第一项" in result
        assert "- 第二项" in result


# ── WebFetcher 边界测试 ──


class TestWebFetcherEdgeCases:
    def test_block_localhost_variants(self):
        fetcher = WebFetcher()
        for url in [
            "http://localhost:3000",
            "http://localhost.localdomain",
            "http://[::1]/path",
        ]:
            with pytest.raises(RuntimeError):
                fetcher.fetch(url)

    @patch("web.fetcher.fetcher.httpx.get")
    def test_redirect_url_returned(self, mock_get):
        html = "<html><head><title>T</title></head><body><article><p>内容足够长来通过最低要求。</p></article></body></html>"
        mock_resp = MagicMock()
        mock_resp.text = html
        mock_resp.url = "https://example.com/final"
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        fetcher = WebFetcher()
        result = fetcher.fetch("https://example.com/redirect")
        assert result["url"] == "https://example.com/final"

    @patch("web.fetcher.fetcher.httpx.get")
    def test_empty_html_returns_hint(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.text = "<html><body></body></html>"
        mock_resp.url = "https://spa.example.com"
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        fetcher = WebFetcher()
        result = fetcher.fetch("https://spa.example.com")
        # 实现已升级文案，统一对外提示用户走 WebSearch（避免 Agent 死循环重试 WebFetch）
        assert "无法提取" in result["content"] or "未提取到正文" in result["content"]


# ── SearchProviderFactory 边界测试 ──


class TestSearchProviderFactoryEdgeCases:
    def setup_method(self):
        SearchProviderFactory.reset()

    def teardown_method(self):
        SearchProviderFactory.reset()

    def test_auto_fallback_to_duckduckgo(self):
        """无 Tavily/SearXNG 时应回退到 DuckDuckGo。

        必须同时清空 env 和 settings：config.yaml 可能存在 tavily/serpapi key，
        否则自动检测会优先命中已配置的 Provider，无法走到 ddgs 回退分支。
        """
        with patch.dict("os.environ", {}, clear=True):
            with patch("settings.get", side_effect=lambda key, default=None: default):
                provider = SearchProviderFactory.create("auto")
                # DuckDuckGo 已安装（ddgs 包），应被选中
                assert provider.name() == "duckduckgo"

    def test_explicit_overrides_env(self):
        """显式指定应覆盖环境变量。"""
        SearchProviderFactory.reset()
        with patch.dict("os.environ", {"TAVILY_API_KEY": "key"}):
            provider = SearchProviderFactory.create("searxng")
            assert provider.name() == "searxng"


# ── WebSearch 工具端到端测试 ──


class TestWebSearchToolE2E:
    def setup_method(self):
        SearchProviderFactory.reset()

    def teardown_method(self):
        SearchProviderFactory.reset()

    def test_empty_results(self):
        mock_client = MagicMock()
        mock_client.search.return_value = {"results": []}

        with patch.dict("os.environ", {"TAVILY_API_KEY": "key"}):
            with patch("tavily.TavilyClient", return_value=mock_client):
                from tools.builtin.web_search import WebSearch
                result = WebSearch.invoke({"query": "nonexistent_xyz_123"})
        assert "未找到" in result

    def test_search_error_handled(self):
        mock_client = MagicMock()
        mock_client.search.side_effect = Exception("timeout")

        SearchProviderFactory.reset()
        with patch.dict("os.environ", {"TAVILY_API_KEY": "key"}):
            with patch("tavily.TavilyClient", return_value=mock_client):
                from tools.builtin.web_search import WebSearch
                result = WebSearch.invoke({"query": "test"})
        assert "失败" in result

    def test_top_k_parameter(self):
        mock_client = MagicMock()
        mock_client.search.return_value = {
            "results": [
                {"title": f"Result {i}", "url": f"https://r{i}.com", "content": f"Content {i}"}
                for i in range(3)
            ]
        }

        SearchProviderFactory.reset()
        with patch.dict("os.environ", {"TAVILY_API_KEY": "key"}):
            with patch("tavily.TavilyClient", return_value=mock_client):
                from tools.builtin.web_search import WebSearch
                result = WebSearch.invoke({"query": "test", "top_k": 3})
        assert "Result 0" in result
        assert "Result 2" in result


# ── WebFetch 工具端到端测试 ──


class TestWebFetchToolE2E:
    @patch("web.fetcher.fetcher.httpx.get")
    def test_fetch_with_redirect_note(self, mock_get):
        html = "<html><head><title>Final</title></head><body><article><p>内容足够长来通过最低要求。</p></article></body></html>"
        mock_resp = MagicMock()
        mock_resp.text = html
        mock_resp.url = "https://redirect-target.example.com/final"
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        from web.fetcher.fetcher import WebFetcher
        fetcher = WebFetcher()
        result = fetcher.fetch("https://redirect-target.example.com/old")
        assert result["url"] == "https://redirect-target.example.com/final"
        assert "Final" in result["title"]

    @patch("web.fetcher.fetcher.httpx.get")
    def test_fetch_raw_mode(self, mock_get):
        html = "<html><body><p>raw content</p></body></html>"
        mock_resp = MagicMock()
        mock_resp.text = html
        mock_resp.url = "https://raw.example.com"
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        from web.fetcher.fetcher import WebFetcher
        fetcher = WebFetcher()
        result = fetcher.fetch("https://raw.example.com", extract_content=False)
        assert "<p>raw content</p>" in result["content"]

    def test_fetch_ssrf_returns_error_string(self):
        """SSRF 攻击应返回错误字符串而非抛异常。"""
        from tools.builtin.web_fetch import WebFetch
        result = WebFetch.invoke({"url": "http://10.0.0.1/admin"})
        assert "内网" in result
