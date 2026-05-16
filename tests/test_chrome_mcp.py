"""Chrome DevTools MCP 集成测试。"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from mcp_client.chrome_launcher import ChromeLauncher
from mcp_client.chrome_formatter import is_chrome_tool, format_chrome_result
from mcp_client.client import MCPConnection
from mcp_client.manager import MCPManager
from mcp_client.browser_guard import BrowserGuard


# ── ChromeLauncher 测试 ──────────────────────────────────────


class TestChromeLauncher:
    """ChromeLauncher 单元测试。"""

    def test_check_port_not_listening(self):
        """未监听的端口应返回 False。"""
        launcher = ChromeLauncher(port=19999)
        assert launcher.is_running() is False

    def test_check_port_listening(self):
        """已监听且响应 /json/version 的端口应返回 True。"""
        from http.server import HTTPServer, BaseHTTPRequestHandler
        import threading
        import json

        class DevToolsHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/json/version":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"Browser": "Chrome/130.0"}).encode())
                else:
                    self.send_response(404)
                    self.end_headers()
            def log_message(self, format, *args):
                pass

        server = HTTPServer(("localhost", 0), DevToolsHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            launcher = ChromeLauncher(port=port)
            assert launcher.is_running() is True
        finally:
            server.shutdown()


# ── ChromeFormatter 测试 ─────────────────────────────────────


class TestChromeFormatter:
    """Chrome DevTools 结果格式化器测试。"""

    def test_is_chrome_tool_known(self):
        assert is_chrome_tool("take_screenshot") is True
        assert is_chrome_tool("navigate_page") is True
        assert is_chrome_tool("click") is True

    def test_is_chrome_tool_unknown(self):
        assert is_chrome_tool("bash") is False
        assert is_chrome_tool("read_file") is False

    def test_format_error_result(self):
        mock_result = MagicMock()
        mock_result.isError = True
        mock_content = MagicMock()
        mock_content.type = "text"
        mock_content.text = "页面不存在"
        mock_result.content = [mock_content]
        result = format_chrome_result("navigate_page", mock_result)
        assert result.startswith("[Chrome错误]")

    def test_format_generic_result(self):
        mock_result = MagicMock()
        mock_result.isError = False
        mock_content = MagicMock()
        mock_content.type = "text"
        mock_content.text = "已导航到 https://example.com"
        mock_result.content = [mock_content]
        result = format_chrome_result("navigate_page", mock_result)
        assert "https://example.com" in result


# ── MCPManager 测试 ──────────────────────────────────────────


class TestMCPManager:
    """MCPManager 单元测试。"""

    def test_no_servers_config(self):
        manager = MCPManager(servers_config={})
        assert manager._connections == {}
        assert manager._tools == []

    def test_default_mode_is_isolated(self):
        manager = MCPManager(servers_config={})
        assert manager.get_chrome_mode() == "isolated"
        assert manager.is_isolated_mode() is True
        assert manager.is_shared_mode() is False

    def test_browser_guard_initialized(self):
        manager = MCPManager(servers_config={})
        assert manager.get_browser_guard() is not None

    def test_isolated_args_constant(self):
        assert "--isolated" in MCPManager.ISOLATED_ARGS
        assert "--autoConnect" in MCPManager.SHARED_AUTOCONNECT_ARGS
        assert "--browserUrl" in MCPManager.SHARED_BROWSER_URL_ARGS


# ── BrowserGuard 测试 ──────────────────────────────────────────


class TestBrowserGuard:
    """BrowserGuard 单元测试。"""

    def test_detect_login_url(self):
        guard = BrowserGuard()
        assert guard.detect_login_page("", "https://example.com/login") is True
        assert guard.detect_login_page("", "https://example.com/signin") is True
        assert guard.detect_login_page("", "https://example.com/home") is False

    def test_detect_login_content(self):
        guard = BrowserGuard()
        assert guard.detect_login_page('<input type="password">') is True
        assert guard.detect_login_page("请登录您的账号") is True
        assert guard.detect_login_page("欢迎回来") is False

    def test_apply_after_execution_navigate(self):
        guard = BrowserGuard()
        guard.apply_after_execution("navigate_page", {"url": "https://example.com"}, "")
        assert guard.last_navigated_url == "https://example.com"

    def test_apply_after_execution_new_page(self):
        guard = BrowserGuard()
        guard.apply_after_execution("new_page", {"url": "https://example.com"}, "page-abc123 opened")
        assert "page-abc123" in guard.agent_opened_tabs

    def test_needs_confirmation_sensitive_write(self):
        guard = BrowserGuard()
        needs, msg = guard.needs_confirmation("click", "https://www.alipay.com/pay")
        assert needs is True

    def test_needs_confirmation_safe_write(self):
        guard = BrowserGuard()
        needs, msg = guard.needs_confirmation("click", "https://example.com/page")
        assert needs is False

    def test_check_tool_use_blocks_sensitive_write(self):
        guard = BrowserGuard()
        allowed, reason = guard.check_tool_use("click", "https://www.alipay.com/pay")
        assert allowed is False
