"""Chrome DevTools MCP 集成测试。"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from mcp_client.chrome_launcher import ChromeLauncher
from mcp_client.chrome_formatter import is_chrome_tool, format_chrome_result
from mcp_client.client import MCPConnection
from mcp_client.manager import MCPManager


# ── ChromeLauncher 测试 ──────────────────────────────────────


class TestChromeLauncher:
    """ChromeLauncher 单元测试。"""

    def test_find_chrome_macos(self):
        """macOS 上能找到 Chrome 路径。"""
        import platform
        if platform.system() != "Darwin":
            pytest.skip("仅在 macOS 上运行")
        launcher = ChromeLauncher(port=9222)
        assert launcher.executable
        assert "Chrome" in launcher.executable or "Chromium" in launcher.executable

    def test_check_port_not_listening(self):
        """未监听的端口应返回 False。"""
        launcher = ChromeLauncher(port=19999)  # 不太可能被占用的端口
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
                pass  # 静默日志

        server = HTTPServer(("localhost", 0), DevToolsHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            launcher = ChromeLauncher(port=port)
            assert launcher.is_running() is True
        finally:
            server.shutdown()

    def test_launched_by_us_default_false(self):
        """初始状态 launched_by_us 应为 False。"""
        launcher = ChromeLauncher(port=9222)
        assert launcher.launched_by_us is False


# ── ChromeFormatter 测试 ─────────────────────────────────────


class TestChromeFormatter:
    """Chrome DevTools 结果格式化器测试。"""

    def test_is_chrome_tool_known(self):
        """已知 Chrome 工具应返回 True。"""
        assert is_chrome_tool("take_screenshot") is True
        assert is_chrome_tool("navigate_page") is True
        assert is_chrome_tool("click") is True
        assert is_chrome_tool("evaluate_script") is True
        assert is_chrome_tool("list_pages") is True

    def test_is_chrome_tool_unknown(self):
        """非 Chrome 工具应返回 False。"""
        assert is_chrome_tool("bash") is False
        assert is_chrome_tool("read_file") is False
        assert is_chrome_tool("") is False

    def test_format_error_result(self):
        """错误结果应格式化为 [Chrome错误] 前缀。"""
        mock_result = MagicMock()
        mock_result.isError = True
        mock_content = MagicMock()
        mock_content.type = "text"
        mock_content.text = "页面不存在"
        mock_result.content = [mock_content]

        result = format_chrome_result("navigate_page", mock_result)
        assert result.startswith("[Chrome错误]")
        assert "页面不存在" in result

    def test_format_generic_result(self):
        """通用结果应正常格式化。"""
        mock_result = MagicMock()
        mock_result.isError = False
        mock_content = MagicMock()
        mock_content.type = "text"
        mock_content.text = "已导航到 https://example.com"
        mock_result.content = [mock_content]

        result = format_chrome_result("navigate_page", mock_result)
        assert "https://example.com" in result

    def test_format_screenshot_saves_file(self):
        """截图结果应保存为文件。"""
        import base64
        from pathlib import Path

        # 创建假的 PNG 数据（1x1 红色像素）
        png_data = base64.b64encode(
            b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01'
            b'\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00'
            b'\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00\x00\x03'
            b'\x00\x01\x00\x18\xdd\x8d\xb4\x00\x00\x00\x00IEND\xaeB`\x82'
        ).decode()

        mock_result = MagicMock()
        mock_result.isError = False
        mock_content = MagicMock()
        mock_content.type = "image"
        mock_content.data = png_data
        mock_content.mimeType = "image/png"
        mock_result.content = [mock_content]

        result = format_chrome_result("take_screenshot", mock_result)
        assert "截图已保存" in result
        assert ".weavemind/chrome_screenshots" in result

    def test_format_long_text_truncated(self):
        """超长文本应被截断。"""
        mock_result = MagicMock()
        mock_result.isError = False
        mock_content = MagicMock()
        mock_content.type = "text"
        mock_content.text = "x" * 20000  # 超长文本
        mock_result.content = [mock_content]

        result = format_chrome_result("take_snapshot", mock_result)
        assert len(result) < 20000
        assert "已截断" in result


# ── MCPConnection 测试 ──────────────────────────────────────


class TestMCPConnection:
    """MCPConnection 单元测试。"""

    def test_detect_chrome_server_type(self):
        """含 chrome 子配置时应检测为 chrome 类型。"""
        config = {
            "name": "chrome",
            "transport": "stdio",
            "command": "npx",
            "chrome": {"auto_start": False, "port": 9222},
        }
        conn = MCPConnection(config)
        assert conn.server_type == "chrome"

    def test_detect_generic_server_type(self):
        """无 chrome 子配置时应检测为 generic 类型。"""
        config = {
            "name": "filesystem",
            "transport": "stdio",
            "command": "npx",
        }
        conn = MCPConnection(config)
        assert conn.server_type == "generic"

    def test_has_loop_attribute(self):
        """MCPConnection 应有 _loop 属性。"""
        config = {"name": "test", "transport": "stdio", "command": "echo"}
        conn = MCPConnection(config)
        assert hasattr(conn, "_loop")
        assert conn._loop is None  # 连接前应为 None


# ── MCPManager 测试 ──────────────────────────────────────────


class TestMCPManager:
    """MCPManager 单元测试。"""

    def test_no_servers_config(self):
        """无服务器配置时应正常初始化。"""
        manager = MCPManager(servers_config={})
        # 同步测试，不调用 async initialize
        assert manager._connections == {}
        assert manager._tools == []

    def test_chrome_launcher_not_created_by_default(self):
        """无 chrome 配置时不应创建 ChromeLauncher。"""
        manager = MCPManager(servers_config={
            "filesystem": {"enabled": True, "transport": "stdio", "command": "echo"},
        })
        assert manager._chrome_launcher is None

    def test_ensure_chrome_running_skips_if_running(self):
        """Chrome 已运行时 _ensure_chrome_running 不应启动新进程。"""
        manager = MCPManager(servers_config={})

        with patch.object(ChromeLauncher, "is_running", return_value=True):
            manager._ensure_chrome_running({"port": 9222})
            assert manager._chrome_launcher is not None
            # 不应调用 start()
            assert manager._chrome_launcher.launched_by_us is False


# ── 权限分类测试 ─────────────────────────────────────────────


class TestChromePermissions:
    """Chrome 工具权限分类测试。"""

    def test_safe_tools_exist(self):
        from permissions.modes import CHROME_SAFE_TOOLS
        assert "list_pages" in CHROME_SAFE_TOOLS
        assert "take_screenshot" in CHROME_SAFE_TOOLS

    def test_modify_tools_exist(self):
        from permissions.modes import CHROME_MODIFY_TOOLS
        assert "navigate_page" in CHROME_MODIFY_TOOLS
        assert "click" in CHROME_MODIFY_TOOLS
        assert "fill" in CHROME_MODIFY_TOOLS

    def test_dangerous_tools_exist(self):
        from permissions.modes import CHROME_DANGEROUS_TOOLS
        assert "evaluate_script" in CHROME_DANGEROUS_TOOLS
        assert "new_page" in CHROME_DANGEROUS_TOOLS

    def test_no_overlap_between_categories(self):
        from permissions.modes import CHROME_SAFE_TOOLS, CHROME_MODIFY_TOOLS, CHROME_DANGEROUS_TOOLS
        assert CHROME_SAFE_TOOLS & CHROME_MODIFY_TOOLS == set()
        assert CHROME_SAFE_TOOLS & CHROME_DANGEROUS_TOOLS == set()
        assert CHROME_MODIFY_TOOLS & CHROME_DANGEROUS_TOOLS == set()
