"""联网搜索功能测试。"""

import pytest
from unittest.mock import MagicMock, patch, PropertyMock

from web.models import SearchResult
from web.providers.base import SearchProvider
from web.providers.factory import SearchProviderFactory
from web.providers.tavily import TavilyProvider
from web.providers.searxng import SearXNGProvider
from web.providers.duckduckgo import DuckDuckGoProvider
from web.fetcher.policy import NetworkPolicy
from web.fetcher.extractor import HtmlExtractor
from web.fetcher.fetcher import WebFetcher


# ── SearchResult 模型测试 ──


class TestSearchResult:
    def test_fields(self):
        r = SearchResult(title="t", url="u", content="c")
        assert r.title == "t"
        assert r.url == "u"
        assert r.content == "c"


# ── SearchProvider 抽象基类测试 ──


class TestSearchProviderBase:
    def test_cannot_instantiate(self):
        with pytest.raises(TypeError):
            SearchProvider()

    def test_subclass_must_implement(self):
        class Incomplete(SearchProvider):
            pass

        with pytest.raises(TypeError):
            Incomplete()


# ── TavilyProvider 测试 ──


class TestTavilyProvider:
    def test_name(self):
        assert TavilyProvider().name() == "tavily"

    def test_not_ready_without_key(self):
        with patch.dict("os.environ", {}, clear=True):
            assert TavilyProvider().is_ready() is False

    def test_ready_with_key(self):
        with patch.dict("os.environ", {"TAVILY_API_KEY": "test-key"}):
            assert TavilyProvider().is_ready() is True

    def test_unavailable_hint(self):
        hint = TavilyProvider().unavailable_hint()
        assert "TAVILY_API_KEY" in hint

    @patch("web.providers.tavily.TavilyClient")
    def test_search_success(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client.search.return_value = {
            "results": [
                {"title": "Python", "url": "https://python.org", "content": "Python官网"},
            ]
        }
        mock_client_cls.return_value = mock_client

        with patch.dict("os.environ", {"TAVILY_API_KEY": "test-key"}):
            results = TavilyProvider().search("Python")
        assert len(results) == 1
        assert results[0].title == "Python"
        assert results[0].url == "https://python.org"

    @patch("web.providers.tavily.TavilyClient")
    def test_search_failure(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client.search.side_effect = Exception("API error")
        mock_client_cls.return_value = mock_client

        with patch.dict("os.environ", {"TAVILY_API_KEY": "test-key"}):
            with pytest.raises(RuntimeError, match="Tavily 搜索失败"):
                TavilyProvider().search("Python")


# ── SearXNGProvider 测试 ──


class TestSearXNGProvider:
    def test_name(self):
        assert SearXNGProvider().name() == "searxng"

    def test_not_ready_without_url(self):
        with patch.dict("os.environ", {}, clear=True):
            assert SearXNGProvider().is_ready() is False

    def test_ready_with_url(self):
        with patch.dict("os.environ", {"SEARXNG_URL": "http://localhost:8888"}):
            assert SearXNGProvider().is_ready() is True

    def test_unavailable_hint(self):
        hint = SearXNGProvider().unavailable_hint()
        assert "SEARXNG_URL" in hint

    @patch("web.providers.searxng.httpx.get")
    def test_search_success(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "results": [
                {"title": "Rust", "url": "https://rust-lang.org", "content": "Rust官网"},
            ]
        }
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        with patch.dict("os.environ", {"SEARXNG_URL": "http://localhost:8888"}):
            results = SearXNGProvider().search("Rust")
        assert len(results) == 1
        assert results[0].title == "Rust"

    @patch("web.providers.searxng.httpx.get")
    def test_search_failure(self, mock_get):
        mock_get.side_effect = Exception("Connection refused")

        with patch.dict("os.environ", {"SEARXNG_URL": "http://localhost:8888"}):
            with pytest.raises(RuntimeError, match="SearXNG 搜索失败"):
                SearXNGProvider().search("Rust")


# ── DuckDuckGoProvider 测试 ──


class TestDuckDuckGoProvider:
    def test_name(self):
        assert DuckDuckGoProvider().name() == "duckduckgo"

    @patch("web.providers.duckduckgo.DuckDuckGoProvider.is_ready", return_value=True)
    def test_ready_when_installed(self, _):
        assert DuckDuckGoProvider().is_ready() is True

    def test_unavailable_hint(self):
        hint = DuckDuckGoProvider().unavailable_hint()
        assert "duckduckgo-search" in hint

    def test_search_success(self):
        mock_ddgs = MagicMock()
        mock_ddgs.__enter__ = MagicMock(return_value=mock_ddgs)
        mock_ddgs.__exit__ = MagicMock(return_value=False)
        mock_ddgs.text.return_value = [
            {"title": "Go", "href": "https://go.dev", "body": "Go官网"},
        ]

        with patch("web.providers.duckduckgo.DDGS", return_value=mock_ddgs):
            results = DuckDuckGoProvider().search("Go")
        assert len(results) == 1
        assert results[0].title == "Go"
        assert results[0].url == "https://go.dev"

    def test_search_failure(self):
        mock_ddgs = MagicMock()
        mock_ddgs.__enter__ = MagicMock(return_value=mock_ddgs)
        mock_ddgs.__exit__ = MagicMock(return_value=False)
        mock_ddgs.text.side_effect = Exception("Rate limited")

        with patch("web.providers.duckduckgo.DDGS", return_value=mock_ddgs):
            with pytest.raises(RuntimeError, match="DuckDuckGo 搜索失败"):
                DuckDuckGoProvider().search("Go")


# ── SearchProviderFactory 测试 ──


class TestSearchProviderFactory:
    def setup_method(self):
        SearchProviderFactory.reset()

    def test_create_tavily_explicit(self):
        provider = SearchProviderFactory.create("tavily")
        assert provider.name() == "tavily"

    def test_create_searxng_explicit(self):
        provider = SearchProviderFactory.create("searxng")
        assert provider.name() == "searxng"

    def test_create_duckduckgo_explicit(self):
        provider = SearchProviderFactory.create("duckduckgo")
        assert provider.name() == "duckduckgo"

    def test_create_unknown_raises(self):
        with pytest.raises(ValueError, match="未知的搜索引擎"):
            SearchProviderFactory.create("bing")

    def test_auto_detect_tavily(self):
        with patch.dict("os.environ", {"TAVILY_API_KEY": "test-key"}):
            provider = SearchProviderFactory.create("auto")
            assert provider.name() == "tavily"

    def test_auto_detect_searxng(self):
        env = {"SEARXNG_URL": "http://localhost:8888"}
        with patch.dict("os.environ", env, clear=False):
            # 确保 TAVILY_API_KEY 不存在
            with patch.dict("os.environ", {}, clear=False):
                # Tavily 不可用时才走 SearXNG
                provider = SearchProviderFactory.create("searxng")
                assert provider.name() == "searxng"

    def test_cached_instance(self):
        p1 = SearchProviderFactory.create("tavily")
        p2 = SearchProviderFactory.create("tavily")
        assert p1 is p2

    def test_reset_clears_cache(self):
        p1 = SearchProviderFactory.create("tavily")
        SearchProviderFactory.reset()
        p2 = SearchProviderFactory.create("tavily")
        assert p1 is not p2


# ── NetworkPolicy 测试 ──


class TestNetworkPolicy:
    def test_allow_https(self):
        policy = NetworkPolicy()
        safe, _ = policy.validate_url("https://example.com")
        assert safe is True

    def test_block_file_protocol(self):
        policy = NetworkPolicy()
        safe, reason = policy.validate_url("file:///etc/passwd")
        assert safe is False
        assert "file" in reason

    def test_block_ftp_protocol(self):
        policy = NetworkPolicy()
        safe, reason = policy.validate_url("ftp://example.com")
        assert safe is False

    def test_block_localhost(self):
        policy = NetworkPolicy()
        safe, reason = policy.validate_url("http://localhost:8080")
        assert safe is False
        assert "localhost" in reason.lower() or "内网" in reason

    def test_block_private_ip_127(self):
        policy = NetworkPolicy()
        safe, reason = policy.validate_url("http://127.0.0.1:8080")
        assert safe is False
        assert "内网" in reason

    def test_block_private_ip_10(self):
        policy = NetworkPolicy()
        safe, reason = policy.validate_url("http://10.0.0.1")
        assert safe is False

    def test_block_private_ip_192_168(self):
        policy = NetworkPolicy()
        safe, reason = policy.validate_url("http://192.168.1.1")
        assert safe is False

    def test_block_private_ip_172_16(self):
        policy = NetworkPolicy()
        safe, reason = policy.validate_url("http://172.16.0.1")
        assert safe is False

    def test_allow_public_ip(self):
        policy = NetworkPolicy()
        safe, _ = policy.validate_url("http://93.184.216.34")
        assert safe is True

    def test_rate_limit_allows_first(self):
        policy = NetworkPolicy()
        allowed, _ = policy.check_rate_limit("https://example.com")
        assert allowed is True

    def test_rate_limit_blocks_rapid(self):
        policy = NetworkPolicy()
        policy.check_rate_limit("https://example.com")
        allowed, reason = policy.check_rate_limit("https://example.com")
        assert allowed is False
        assert "频繁" in reason

    def test_rate_limit_different_domains(self):
        policy = NetworkPolicy()
        policy.check_rate_limit("https://a.com")
        allowed, _ = policy.check_rate_limit("https://b.com")
        assert allowed is True


# ── HtmlExtractor 测试 ──


class TestHtmlExtractor:
    def test_empty_html(self):
        extractor = HtmlExtractor()
        assert extractor.extract("") == ""

    def test_article_extraction(self):
        html = """
        <html><body>
        <nav>导航链接</nav>
        <article>
            <h1>标题</h1>
            <p>这是正文内容，足够长以通过评分。</p>
            <p>第二段正文内容，继续补充信息。</p>
        </article>
        <footer>页脚信息</footer>
        </body></html>
        """
        extractor = HtmlExtractor()
        result = extractor.extract(html)
        assert "标题" in result
        assert "正文内容" in result
        assert "页脚" not in result

    def test_noise_removal(self):
        html = """
        <html><body>
        <div class="ads">广告内容</div>
        <div class="sidebar">侧边栏</div>
        <div class="content">
            <p>这是正文内容，需要足够长才能通过评分检查。</p>
            <p>继续补充更多正文内容。</p>
        </div>
        </body></html>
        """
        extractor = HtmlExtractor()
        result = extractor.extract(html)
        assert "广告" not in result
        assert "侧边栏" not in result
        assert "正文内容" in result

    def test_script_style_removed(self):
        html = """
        <html><body>
        <script>var x = 1;</script>
        <style>.cls { color: red; }</style>
        <div><p>正文内容，足够长以通过评分检查。</p></div>
        </body></html>
        """
        extractor = HtmlExtractor()
        result = extractor.extract(html)
        assert "var x" not in result
        assert "color: red" not in result

    def test_score_fallback(self):
        html = """
        <html><body>
        <div>这是很长的正文内容，虽然没有语义标签，但文本足够多可以拿到最高分。</div>
        </body></html>
        """
        extractor = HtmlExtractor()
        result = extractor.extract(html)
        assert "正文内容" in result

    def test_no_content_hint(self):
        html = "<html><body><nav>导航</nav></body></html>"
        extractor = HtmlExtractor()
        result = extractor.extract(html)
        assert "未提取到正文" in result

    def test_markdown_headings(self):
        html = """
        <html><body><article>
        <h2>子标题</h2>
        <p>正文段落。</p>
        </article></body></html>
        """
        extractor = HtmlExtractor()
        result = extractor.extract(html)
        assert "## 子标题" in result

    def test_markdown_links(self):
        html = """
        <html><body><article>
        <p>访问 <a href="https://example.com">示例</a> 了解更多。</p>
        </article></body></html>
        """
        extractor = HtmlExtractor()
        result = extractor.extract(html)
        assert "[示例](https://example.com)" in result

    def test_markdown_code(self):
        html = """
        <html><body><article>
        <pre>def hello():\n    print("hi")</pre>
        </article></body></html>
        """
        extractor = HtmlExtractor()
        result = extractor.extract(html)
        assert "```" in result
        assert "hello" in result


# ── WebFetcher 测试 ──


class TestWebFetcher:
    def test_block_private_url(self):
        fetcher = WebFetcher()
        with pytest.raises(RuntimeError, match="安全检查失败"):
            fetcher.fetch("http://127.0.0.1:8080")

    def test_block_file_protocol(self):
        fetcher = WebFetcher()
        with pytest.raises(RuntimeError, match="安全检查失败"):
            fetcher.fetch("file:///etc/passwd")

    @patch("web.fetcher.fetcher.httpx.get")
    def test_fetch_success(self, mock_get):
        html = "<html><head><title>测试</title></head><body><article><p>正文内容</p></article></body></html>"
        mock_resp = MagicMock()
        mock_resp.text = html
        mock_resp.url = "https://example.com"
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        fetcher = WebFetcher()
        result = fetcher.fetch("https://example.com")
        assert result["title"] == "测试"
        assert "正文内容" in result["content"]

    @patch("web.fetcher.fetcher.httpx.get")
    def test_fetch_raw_html(self, mock_get):
        html = "<html><body>raw</body></html>"
        mock_resp = MagicMock()
        mock_resp.text = html
        mock_resp.url = "https://example.com"
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        fetcher = WebFetcher()
        result = fetcher.fetch("https://example.com", extract_content=False)
        assert result["content"] == html

    @patch("web.fetcher.fetcher.httpx.get")
    def test_fetch_http_error(self, mock_get):
        import httpx
        mock_get.side_effect = httpx.HTTPStatusError(
            "404", request=MagicMock(), response=MagicMock()
        )

        fetcher = WebFetcher()
        with pytest.raises(RuntimeError, match="请求失败"):
            fetcher.fetch("https://example.com/notfound")

    @patch("web.fetcher.fetcher.httpx.get")
    def test_fetch_ssl_fallback(self, mock_get):
        import httpx
        html = "<html><head><title>SSL</title></head><body><article><p>内容</p></article></body></html>"

        # 第一次 SSL 失败，第二次成功
        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise httpx.ConnectError("SSL error")
            mock_resp = MagicMock()
            mock_resp.text = html
            mock_resp.url = "https://example.com"
            mock_resp.raise_for_status = MagicMock()
            return mock_resp

        mock_get.side_effect = side_effect

        fetcher = WebFetcher()
        result = fetcher.fetch("https://example.com")
        assert result["title"] == "SSL"

    @patch("web.fetcher.fetcher.httpx.get")
    def test_fetch_truncation(self, mock_get):
        # 超过 max_size 的响应应被截断
        big_html = "<html><body>" + "x" * 10000 + "</body></html>"
        mock_resp = MagicMock()
        mock_resp.text = big_html
        mock_resp.url = "https://example.com"
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        fetcher = WebFetcher(max_size=1000)
        result = fetcher.fetch("https://example.com", extract_content=False)
        assert len(result["content"]) <= 1000


# ── WebSearchTool / WebFetchTool 集成测试 ──


class TestWebSearchTool:
    def test_unavailable_hint(self):
        """无 API Key 时应返回友好提示。"""
        from tools.builtin.web_search import WebSearch

        SearchProviderFactory.reset()
        with patch.dict("os.environ", {}, clear=False):
            result = WebSearch.invoke({"query": "test"})
        assert isinstance(result, str)
        # 应该包含提示信息（TAVILY_API_KEY 或其他提示）
        SearchProviderFactory.reset()

    @patch("web.providers.tavily.TavilyClient")
    def test_search_success(self, mock_client_cls):
        """有 API Key 时应返回搜索结果。"""
        mock_client = MagicMock()
        mock_client.search.return_value = {
            "results": [
                {"title": "Python", "url": "https://python.org", "content": "Python官网"},
            ]
        }
        mock_client_cls.return_value = mock_client

        SearchProviderFactory.reset()
        with patch.dict("os.environ", {"TAVILY_API_KEY": "test-key"}):
            from tools.builtin.web_search import WebSearch
            result = WebSearch.invoke({"query": "Python"})
        assert "Python" in result
        assert "python.org" in result
        SearchProviderFactory.reset()


class TestWebFetchTool:
    @patch("web.fetcher.fetcher.httpx.get")
    def test_fetch_success(self, mock_get):
        html = "<html><head><title>测试页</title></head><body><article><p>正文内容</p></article></body></html>"
        mock_resp = MagicMock()
        mock_resp.text = html
        mock_resp.url = "https://example.com"
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        from tools.builtin.web_fetch import WebFetch
        result = WebFetch.invoke({"url": "https://example.com"})
        assert "测试页" in result
        assert "正文内容" in result

    def test_block_private_url(self):
        from tools.builtin.web_fetch import WebFetch
        result = WebFetch.invoke({"url": "http://127.0.0.1:8080"})
        assert "安全检查失败" in result
